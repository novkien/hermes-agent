"""Session-scoped live event fan-out.

A turn's stream used to belong to whoever started it: ``_handle_session_chat_stream``
builds a private ``asyncio.Queue`` per request, so a second client watching the same
session saw nothing until the turn finished and its messages were persisted. That made
the dashboard's chat view blind to anything driven from the CLI, Telegram or cron — the
transcript sat still and then arrived all at once.

This hub is the missing broadcast layer. Every frame a turn emits is offered to each
subscriber's own buffer, so subscribers never steal each other's frames — the deliberate
difference from ``_run_streams``/``GET /v1/runs/{run_id}/events``, whose single queue is
drained destructively by whichever reader gets there first.

Nothing here is a transport. Producers inside the gateway call :meth:`publish` directly;
producers in another process (the CLI runs its own agent in its own process, and the only
IPC the two share is this HTTP API) reach the same method through the ingest endpoint.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set

# Losing a delta costs a few characters of text that the persisted transcript still
# has. Losing one of these strands the reader in a turn that never ends, so a full
# buffer drops text rather than the frames that close the turn out.
TERMINAL_EVENTS = frozenset({"run.completed", "error", "done"})
DROPPABLE_EVENTS = frozenset({"assistant.delta", "reasoning.delta", "tool.progress"})
# The subset that is pure appended text, so consecutive ones can be replayed as
# a single frame without changing what the reader ends up with.
DROPPABLE_TEXT = frozenset({"assistant.delta", "reasoning.delta"})

DEFAULT_RING_SIZE = 2000
DEFAULT_QUEUE_LIMIT = 2000
DEFAULT_RUNNING_TTL_SECONDS = 3600.0


class _Subscriber:
    """One reader's private buffer.

    A plain bounded ``asyncio.Queue`` would block or raise once full, and neither is
    acceptable here: a slow browser must never apply backpressure to the agent. So the
    buffer is a deque that sheds text under pressure and wakes its reader through an
    event instead.
    """

    __slots__ = ("_items", "_limit", "_wake", "dropped")

    def __init__(self, limit: int = DEFAULT_QUEUE_LIMIT) -> None:
        self._items: Deque[Dict[str, Any]] = deque()
        self._limit = max(1, int(limit))
        self._wake = asyncio.Event()
        self.dropped = 0

    def offer(self, frame: Dict[str, Any]) -> None:
        if len(self._items) >= self._limit:
            for index, existing in enumerate(self._items):
                if existing.get("event") in DROPPABLE_EVENTS:
                    del self._items[index]
                    self.dropped += 1
                    break
            else:
                # Nothing sheddable is buffered. A terminal frame still goes in —
                # overshooting the limit by one beats never closing the turn.
                if frame.get("event") not in TERMINAL_EVENTS:
                    self.dropped += 1
                    return
        self._items.append(frame)
        self._wake.set()

    async def get(self, timeout: float) -> Optional[Dict[str, Any]]:
        """Next frame, or ``None`` when ``timeout`` elapses with nothing buffered."""
        if not self._items:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return None
        self._wake.clear()
        return self._items.popleft() if self._items else None


class SessionEventHub:
    """Fan out a session's turn events to every client watching that session."""

    def __init__(
        self,
        *,
        ring_size: int = DEFAULT_RING_SIZE,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
        running_ttl_seconds: float = DEFAULT_RUNNING_TTL_SECONDS,
    ) -> None:
        self._subscribers: Dict[str, Set[_Subscriber]] = {}
        self._rings: Dict[str, Deque[Dict[str, Any]]] = {}
        self._running: Dict[str, Dict[str, Any]] = {}
        self._ring_size = max(1, int(ring_size))
        self._queue_limit = max(1, int(queue_limit))
        self._running_ttl = float(running_ttl_seconds)

    # -------------------------------------------------------------- producing

    def publish(self, session_id: str, name: str, payload: Dict[str, Any]) -> None:
        """Offer one frame to every subscriber. Must run on the event loop.

        Deliberately cheap when nobody is watching — a couple of dict lookups — so a
        turn pays essentially nothing for a feature that is idle most of the time.
        """
        if not session_id or not name:
            return
        frame = {"event": name, "data": payload}
        self._track_running(session_id, name, payload)

        # Only a live turn is worth replaying. Once it is over the transcript is
        # persisted, and a late subscriber reads it from history instead.
        if session_id in self._running:
            ring = self._rings.get(session_id)
            if ring is None:
                ring = deque(maxlen=self._ring_size)
                self._rings[session_id] = ring
            ring.append(frame)
        else:
            self._rings.pop(session_id, None)

        for subscriber in tuple(self._subscribers.get(session_id, ())):
            subscriber.offer(frame)

    def _track_running(self, session_id: str, name: str, payload: Dict[str, Any]) -> None:
        if name == "run.started":
            self._rings.pop(session_id, None)
            self._running[session_id] = {
                "session_id": session_id,
                "run_id": payload.get("run_id"),
                "started_at": payload.get("ts") or time.time(),
                "platform": payload.get("platform") or "gateway",
            }
        elif name in TERMINAL_EVENTS:
            # `done` follows `run.completed`/`error`, so this runs twice per turn.
            # Both are idempotent.
            self._running.pop(session_id, None)

    def sweep(self, now: Optional[float] = None) -> None:
        """Forget runs whose producer died without ever sending a terminal frame.

        A CLI process killed mid-turn cannot retract its own ``run.started``, so
        without this a session would claim to be running forever.
        """
        cutoff = (now or time.time()) - self._running_ttl
        for session_id, meta in tuple(self._running.items()):
            if float(meta.get("started_at") or 0.0) < cutoff:
                self._running.pop(session_id, None)
                self._rings.pop(session_id, None)

    # -------------------------------------------------------------- consuming

    def subscribe(self, session_id: str, after_seq: int = 0) -> _Subscriber:
        """Attach a reader, pre-loaded with the live turn's frames after ``after_seq``.

        The replay is what makes opening the tab mid-turn work: the reader gets the
        thinking and tool rows that already happened, then continues live, instead of
        joining a conversation halfway through a sentence.
        """
        subscriber = _Subscriber(self._queue_limit)
        if session_id in self._running:
            catch_up = [
                frame for frame in self._rings.get(session_id, ())
                if _frame_seq(frame) > after_seq
            ]
            for frame in _coalesce(catch_up):
                subscriber.offer(frame)
        self._subscribers.setdefault(session_id, set()).add(subscriber)
        return subscriber

    def unsubscribe(self, session_id: str, subscriber: _Subscriber) -> None:
        readers = self._subscribers.get(session_id)
        if not readers:
            return
        readers.discard(subscriber)
        if not readers:
            self._subscribers.pop(session_id, None)

    # ------------------------------------------------------------- inspecting

    def running(self) -> List[Dict[str, Any]]:
        """Sessions with a turn in flight, newest first."""
        self.sweep()
        return sorted(
            (dict(meta) for meta in self._running.values()),
            key=lambda meta: float(meta.get("started_at") or 0.0),
            reverse=True,
        )

    def is_running(self, session_id: str) -> bool:
        return session_id in self._running

    def subscriber_count(self, session_id: str) -> int:
        return len(self._subscribers.get(session_id, ()))


def _coalesce(frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge runs of consecutive text deltas into one frame each.

    Only for catch-up, never for storage: filtering by `after_seq` happens
    first, so each frame is still addressed by its own sequence number and a
    resume can never be handed text it already has.

    Catching up costs the same to a reader whether a paragraph arrives as four
    hundred frames or as one, but four hundred frames cost four hundred SSE
    writes, four hundred parses and four hundred reducer passes before the first
    live token is even reached. A dense turn — the audit pipeline in thread 331
    ran a hundred and thirty tool calls — is exactly where that adds up.
    """
    merged: List[Dict[str, Any]] = []
    for frame in frames:
        name = frame.get("event")
        data = frame.get("data")
        if merged and name in DROPPABLE_TEXT and isinstance(data, dict):
            previous = merged[-1]
            prior = previous.get("data")
            if (
                previous.get("event") == name
                and isinstance(prior, dict)
                and prior.get("message_id") == data.get("message_id")
                and isinstance(prior.get("delta"), str)
                and isinstance(data.get("delta"), str)
            ):
                # A copy, not a mutation: the ring keeps the originals so a
                # later subscriber still sees the true sequence.
                combined = dict(prior)
                combined["delta"] = prior["delta"] + data["delta"]
                combined["seq"] = data.get("seq", prior.get("seq"))
                merged[-1] = {"event": name, "data": combined}
                continue
        merged.append(frame)
    return merged


def _frame_seq(frame: Dict[str, Any]) -> int:
    data = frame.get("data")
    if isinstance(data, dict):
        try:
            return int(data.get("seq") or 0)
        except (TypeError, ValueError):
            return 0
    return 0
