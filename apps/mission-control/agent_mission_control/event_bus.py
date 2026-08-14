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
from collections import deque
from typing import Awaitable, Callable, Optional

from .store import Store

Subscriber = Callable[[dict], Awaitable[None]]


class EventBus:
    def __init__(
        self,
        store: Store,
        ring_buffer_size: int = 500,
        db_replay_limit: int = 2000,
        heartbeat_seconds: float = 15,
        retry_ms: int = 5000,
        cache: Optional[Any] = None,
    ) -> None:
        self.store = store
        self.ring_buffer_size = ring_buffer_size
        self.db_replay_limit = db_replay_limit
        self.heartbeat_seconds = heartbeat_seconds
        self.retry_ms = retry_ms
        self.cache = cache
        self._ring: deque[dict] = deque(maxlen=ring_buffer_size)
        self._subscribers: dict[str, set[Subscriber]] = {}
        self._lock = asyncio.Lock()
        self._seen: set[tuple[str, str]] = set()
        self._seen_max = 20000

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
    ) -> Optional[dict]:
        """Publish one event. Dedup by (source_id, event_id); returns None if dropped."""
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
        event = {
            "event_id": event_id,
            "event_type": event_type,
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
            if len(self._seen) > self._seen_max:
                self._seen = set(list(self._seen)[-self._seen_max:])
            self._ring.append(event)
            self.store.insert_event_replay(
                event["event_id"],
                event["event_type"],
                event["occurred_at"],
                event["source_id"],
                event["entity_type"],
                event["entity_id"],
                event["payload"],
                event["coverage"],
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

    async def replay_after(self, last_event_id: Optional[str]) -> list[dict]:
        """Events after cursor: ring first (fast path), then DB replay."""
        if not last_event_id:
            ring = list(self._ring)
            if len(ring) >= self.ring_buffer_size:
                return ring
            db = self.store.replay_latest(self.db_replay_limit)
            # merge: DB is older than ring; return DB + ring deduped by event_id
            seen = {e["event_id"] for e in ring}
            return [e for e in db if e["event_id"] not in seen] + ring

        # cursor replay: find position in ring
        ring = list(self._ring)
        for i, e in enumerate(ring):
            if e["event_id"] == last_event_id:
                return ring[i + 1:]
        # The cursor fell out of the ring, so the DB is the complete ordered
        # source of truth after that cursor. Do not remove rows merely because
        # they are also still present in the ring: replay_after() returns one
        # list and the caller does not append the ring separately.
        return self.store.replay_events_after(last_event_id, self.db_replay_limit)

    def last_event_id(self) -> str:
        if self._ring:
            return self._ring[-1]["event_id"]
        return self.store.replay_last_event_id()

    # ---- subscriber queue helpers ------------------------------------------
    def make_queue(self) -> "asyncio.Queue[dict]":
        return asyncio.Queue()


def sse_frame(event: dict, retry_ms: Optional[int] = None) -> str:
    """Render one event as an SSE frame (id/event/data lines)."""
    lines = [f"id: {event['event_id']}", f"event: {event['event_type']}"]
    data = json.dumps(event)
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
