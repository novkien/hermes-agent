"""Event fabric (Stage 5 item 1) — merged into agent_mission_control (S8).

One authenticated SSE endpoint backed by:
- in-memory bounded ring buffer (last N events, default 500)
- DB-backed replay (local store table event_replay, last 2000)
- dedup by (source_id, event_id) — repeats dropped
- envelope per architecture-freeze 8.5:
  {event_id, event_type, occurred_at, profile_id, entity_type, entity_id,
   source_id, payload, coverage: native|polled|derived}
- heartbeat comments `: ping` (liveness only; NEVER fake business events)
- `Last-Event-ID` replay support
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import OrderedDict, deque
from typing import Any, Awaitable, Callable, Optional

from .live_resources import canonical_event
from .read_model import ReadModel
from .store import Store

Subscriber = Callable[[dict], Awaitable[None]]


class CoalescingQueue:
    """Bounded async queue that keeps only the newest state per entity."""

    def __init__(self, maxsize: int = 256) -> None:
        self.maxsize = max(8, int(maxsize))
        self._items: OrderedDict[str, dict] = OrderedDict()
        self._condition = asyncio.Condition()
        self._sequence = 0

    @staticmethod
    def _state_key(event: dict) -> str | None:
        if "_frame" in event:
            return None
        return "\x1f".join(
            str(event.get(name) or "")
            for name in ("profile_id", "resource_key", "entity_id")
        )

    async def put(self, event: dict) -> None:
        async with self._condition:
            key = self._state_key(event)
            if key is None:
                self._sequence += 1
                key = f"frame:{self._sequence}"
            if key in self._items:
                self._items[key] = event
                self._items.move_to_end(key)
            elif len(self._items) < self.maxsize:
                self._items[key] = event
            else:
                profile = str(event.get("profile_id") or "")
                resource = str(event.get("resource_key") or "*")
                # Overflow is state loss, so discard queued state deltas and
                # request one bounded resync. Keep no unbounded backlog.
                self._items.clear()
                self._items[f"resync:{profile}:{resource}"] = {
                    "event_id": f"resync-{int(time.time() * 1000)}",
                    "event_type": "state.resync",
                    "resource_key": resource,
                    "operation": "resync-required",
                    "profile_id": profile,
                    "entity_type": "",
                    "entity_id": "",
                    "source_id": "event-bus",
                    "revision": int(event.get("revision") or 0),
                    "occurred_at": int(time.time()),
                    "payload": {"reason": "subscriber-overflow"},
                    "coverage": "derived",
                }
            self._condition.notify()

    async def get(self) -> dict:
        async with self._condition:
            while not self._items:
                await self._condition.wait()
            _key, event = self._items.popitem(last=False)
            return event

    def qsize(self) -> int:
        return len(self._items)


class EventBus:
    def __init__(
        self,
        store: Store,
        ring_buffer_size: int = 500,
        db_replay_limit: int = 2000,
        heartbeat_seconds: float = 15,
        retry_ms: int = 5000,
        cache: Optional[Any] = None,
        read_model: ReadModel | None = None,
        subscriber_queue_size: int = 256,
    ) -> None:
        self.store = store
        self.ring_buffer_size = ring_buffer_size
        self.db_replay_limit = db_replay_limit
        self.heartbeat_seconds = heartbeat_seconds
        self.retry_ms = retry_ms
        self.cache = cache
        self.read_model = read_model
        self.subscriber_queue_size = subscriber_queue_size
        self._ring: deque[dict] = deque(maxlen=ring_buffer_size)
        self._subscribers: dict[str, set[Subscriber]] = {}
        self._lock = asyncio.Lock()
        self._seen: set[tuple[str, str]] = set()
        self._seen_max = 20000
        self._state_fingerprints: dict[tuple[str, str, str, str], str] = {}

    # ---- publish ----------------------------------------------------------
    async def publish(
        self,
        event_type: str,
        source_id: str,
        entity_type: str = "",
        entity_id: str = "",
        payload: Optional[dict] = None,
        coverage: str = "polled",
        profile_id: str = "",
        event_id: Optional[str] = None,
        _publish: Any = None,
        resource_key: str | None = None,
        operation: str | None = None,
        revision: int | None = None,
    ) -> Optional[dict]:
        """Publish one event. Dedup by (source_id, event_id); returns None if dropped."""
        profile_id = str(profile_id or "")
        if event_id is None:
            event_id = f"{int(time.time()*1000)}-{uuid.uuid4().hex[:12]}"
        if event_type == "cache.invalidated" and self.cache is not None and _publish is None:
            # Derived event: invalidate the backend cache for the source and
            # only emit when something was actually dropped. The client uses
            # this to clear its own prefetch cache (app.js).
            removed = self.cache.invalidate(source_id=source_id)
            if removed <= 0:
                return None
            payload = {**(payload or {}), "removed": removed}
            return await self.publish(
                event_type, source_id, entity_type, entity_id,
                payload, "derived", profile_id, event_id, _publish=object(),
            )
        dedup_key = (source_id, event_id)
        if dedup_key in self._seen:
            return None
        occurred_at = int(time.time())
        mapping = canonical_event(event_type, entity_id)
        resource_key = resource_key or mapping.resource_key
        operation = operation or mapping.operation
        if revision is None and self.read_model is not None:
            revision = self.read_model.revision(
                resource_key, profile_id=profile_id or "default"
            )
        revision = int(revision or 0)
        state_key = (profile_id, resource_key, entity_id, operation)
        state_fingerprint = json.dumps(payload or {}, sort_keys=True, default=str)
        if operation != "resync-required" and self._state_fingerprints.get(state_key) == state_fingerprint:
            return None
        event = {
            "event_id": event_id,
            "event_type": event_type,
            "resource_key": resource_key,
            "operation": operation,
            "revision": revision,
            "occurred_at": occurred_at,
            "profile_id": profile_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "source_id": source_id,
            "payload": payload or {},
            "coverage": coverage,
        }
        async with self._lock:
            # re-check under lock (publish is the single writer)
            if dedup_key in self._seen:
                return None
            self._seen.add(dedup_key)
            self._state_fingerprints[state_key] = state_fingerprint
            if len(self._state_fingerprints) > self._seen_max:
                oldest = next(iter(self._state_fingerprints))
                self._state_fingerprints.pop(oldest, None)
            if len(self._seen) > self._seen_max:
                self._seen = set(list(self._seen)[-self._seen_max:])
            self._ring.append(event)
            try:
                self.store.insert_event_replay(
                    event["event_id"], event["event_type"], event["occurred_at"],
                    event["source_id"], event["entity_type"], event["entity_id"],
                    event["payload"], event["coverage"], event["profile_id"],
                    event["resource_key"], event["operation"], event["revision"],
                )
            except TypeError:
                # Frozen test doubles and rollback stores can still expose the
                # v2 signature during a mixed-version canary.
                self.store.insert_event_replay(
                    event["event_id"], event["event_type"], event["occurred_at"],
                    event["source_id"], event["entity_type"], event["entity_id"],
                    event["payload"], event["coverage"],
                )
            subs = list(self._subscribers.get(event_type, ())) + list(
                self._subscribers.get("*", ())
            )
        for fn in subs:
            try:
                await fn(dict(event))
            except Exception:
                # never let a subscriber failure break the bus
                pass
        return event

    async def safe_publish(
        self,
        event_type: str,
        source_id: str,
        entity_type: str = "",
        entity_id: str = "",
        payload: Optional[dict] = None,
        coverage: str = "polled",
        profile_id: str = "",
        resource_key: str | None = None,
        operation: str | None = None,
        revision: int | None = None,
    ) -> None:
        """Fire-and-forget publish: never raises, never delays the caller's path.

        Route handlers must not fail because of event publishing (the audit/
        upstream chain is the source of truth); subscribers are already
        isolated inside publish, but the publish call itself (store write,
        subscriber awaits) stays guarded here.
        """
        try:
            await self.publish(
                event_type, source_id, entity_type, entity_id,
                payload, coverage=coverage, profile_id=profile_id,
                resource_key=resource_key, operation=operation, revision=revision,
            )
        except Exception:
            pass

    # ---- subscribe --------------------------------------------------------
    def subscribe(self, event_type: str, fn: Subscriber) -> None:
        self._subscribers.setdefault(event_type, set()).add(fn)

    def unsubscribe(self, event_type: str, fn: Subscriber) -> None:
        self._subscribers.get(event_type, set()).discard(fn)

    # ---- replay -----------------------------------------------------------
    def ring_events(self) -> list[dict]:
        return [dict(e) for e in self._ring]

    async def replay_after(
        self, last_event_id: Optional[str], profile_id: str | None = None
    ) -> list[dict]:
        """Events after cursor: ring first (fast path), then DB replay."""
        if not last_event_id:
            ring = list(self._ring)
            if len(ring) >= self.ring_buffer_size:
                return self._profile_events(ring, profile_id)
            db = self.store.replay_latest(self.db_replay_limit)
            # merge: DB is older than ring; return DB + ring deduped by event_id
            seen = {e["event_id"] for e in ring}
            return self._profile_events(
                [e for e in db if e["event_id"] not in seen] + ring, profile_id
            )

        # cursor replay: find position in ring
        ring = list(self._ring)
        for i, e in enumerate(ring):
            if e["event_id"] == last_event_id:
                return self._profile_events(ring[i + 1:], profile_id)
        # The cursor fell out of the ring, so the DB is the complete ordered
        # source of truth after that cursor. Do not remove rows merely because
        # they are also still present in the ring: replay_after() returns one
        # list and the caller does not append the ring separately.
        replay = self.store.replay_events_after(last_event_id, self.db_replay_limit)
        if replay:
            return self._profile_events(replay, profile_id)
        has_cursor = getattr(self.store, "event_replay_has", lambda _event_id: False)(last_event_id)
        if has_cursor:
            return []
        return [self._resync_event(profile_id or "", "cursor-not-found")]

    @staticmethod
    def _profile_events(events: list[dict], profile_id: str | None) -> list[dict]:
        if profile_id is None:
            return events
        return [
            event for event in events
            if not event.get("profile_id") or event.get("profile_id") == profile_id
        ]

    @staticmethod
    def _resync_event(profile_id: str, reason: str) -> dict:
        return {
            "event_id": f"resync-{int(time.time() * 1000)}",
            "event_type": "state.resync", "resource_key": "*",
            "operation": "resync-required", "profile_id": profile_id,
            "entity_type": "", "entity_id": "", "source_id": "event-bus",
            "revision": 0, "occurred_at": int(time.time()),
            "payload": {"reason": reason}, "coverage": "derived",
        }

    def last_event_id(self) -> str:
        if self._ring:
            return self._ring[-1]["event_id"]
        return self.store.replay_last_event_id()

    # ---- subscriber queue helpers ------------------------------------------
    def make_queue(self) -> CoalescingQueue:
        return CoalescingQueue(self.subscriber_queue_size)


def sse_frame(event: dict, retry_ms: Optional[int] = None) -> str:
    """Render one replayable state event under the unified transport name."""
    payload = dict(event)
    if not payload.get("resource_key"):
        mapping = canonical_event(
            str(payload.get("event_type") or ""), str(payload.get("entity_id") or "")
        )
        payload.update(
            resource_key=mapping.resource_key,
            operation=mapping.operation,
            revision=0,
            profile_id=payload.get("profile_id") or "",
        )
    lines = [f"id: {payload['event_id']}", "event: state.change"]
    data = json.dumps(payload)
    # SSE data lines: split newlines
    for part in data.split("\n"):
        lines.append(f"data: {part}")
    lines.append("")
    if retry_ms is not None:
        return f"retry: {retry_ms}\n" + "\n".join(lines) + "\n"
    return "\n".join(lines) + "\n"


def sse_frame_named(event_type: str, payload: dict) -> str:
    """Render an SSE frame for something that is not a bus event.

    The bus envelope (`event_id`, `entity_type`, `coverage`, …) exists so a
    client can replay and de-duplicate a fleet event. Live turn frames have
    neither property — they are a stream, not a log — so they ride this channel
    under their own event name without pretending to be bus events.
    """
    data = json.dumps(payload)
    lines = [f"event: {event_type}"]
    for part in data.split("\n"):
        lines.append(f"data: {part}")
    lines.append("")
    return "\n".join(lines) + "\n"


def sse_heartbeat() -> str:
    return ": ping\n\n"
