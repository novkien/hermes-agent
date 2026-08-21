#!/usr/bin/env python3
"""Revision, replay, coalescing and entity-delta contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_mission_control.event_bus import CoalescingQueue, EventBus  # noqa: E402
from agent_mission_control.read_model import ReadModel  # noqa: E402
from agent_mission_control.store import Store  # noqa: E402
from agent_mission_control.workers import SourceWorkers  # noqa: E402


async def main_async() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        model = ReadModel(Path(tmp) / "live.db")
        store = Store(Path(tmp) / "control.db")
        model.replace_entities(
            "issues", [{"id": 1, "issue": "one", "status": "open"}],
            profile_id="alpha", fingerprint="fp-1",
        )
        bus = EventBus(store, read_model=model, ring_buffer_size=2, subscriber_queue_size=8)
        first = await bus.publish(
            "issue.changed", "issues", "issue", "1",
            {"id": 1, "issue": "one", "status": "open"}, profile_id="alpha",
        )
        assert first and first["revision"] == 1 and first["resource_key"] == "issues"
        # Same final state from a later poll is convergence, not a second event.
        duplicate = await bus.publish(
            "issue.changed", "issues", "issue", "1",
            {"id": 1, "issue": "one", "status": "open"}, profile_id="alpha",
        )
        assert duplicate is None
        beta = await bus.publish(
            "issue.changed", "issues", "issue", "2",
            {"id": 2, "issue": "two", "status": "open"}, profile_id="beta",
            revision=4,
        )
        assert beta
        alpha_replay = await bus.replay_after(None, profile_id="alpha")
        assert alpha_replay and all(event.get("profile_id") in {"", "alpha"} for event in alpha_replay)
        assert not any(event.get("profile_id") == "beta" for event in alpha_replay)
        gap = await bus.replay_after("cursor-does-not-exist", profile_id="alpha")
        assert len(gap) == 1 and gap[0]["operation"] == "resync-required"

        persisted = store.replay_latest(10)
        assert any(
            event["profile_id"] == "alpha" and event["resource_key"] == "issues"
            and event["operation"] == "upsert" and event["revision"] == 1
            for event in persisted
        )

        queue = CoalescingQueue(maxsize=8)
        for revision in range(100):
            await queue.put({
                "profile_id": "alpha", "resource_key": "issues", "entity_id": "1",
                "revision": revision, "payload": {"value": revision},
            })
        assert queue.qsize() == 1
        assert (await queue.get())["revision"] == 99
        for index in range(9):
            await queue.put({
                "profile_id": "alpha", "resource_key": "issues", "entity_id": str(index),
                "revision": index,
            })
        assert queue.qsize() == 1
        assert (await queue.get())["operation"] == "resync-required"

        class CaptureBus:
            def __init__(self): self.events = []
            async def publish(self, *args, **kwargs): self.events.append((args, kwargs))

        workers = SourceWorkers.__new__(SourceWorkers)
        workers.bus = CaptureBus()
        workers.alert_engine = None
        workers._initialized_sources = set()
        workers._permit_entities = {}
        workers._feed_alerts = lambda *_args: None
        first_rows = [{"permit_id": "p1", "status": "pending", "severity": "low"}]
        await SourceWorkers._on_permits(workers, first_rows, "one")
        assert workers.bus.events == [], "initial bootstrap emitted a list flood"
        changed_rows = [{"permit_id": "p1", "status": "approved", "severity": "low"}]
        await SourceWorkers._on_permits(workers, changed_rows, "two")
        assert len(workers.bus.events) == 1
        assert workers.bus.events[0][0][4]["status"] == "approved"
        await SourceWorkers._on_permits(workers, changed_rows, "two")
        assert len(workers.bus.events) == 1, "unchanged row emitted a duplicate"
        await SourceWorkers._on_permits(workers, [], "three")
        assert len(workers.bus.events) == 2
        assert workers.bus.events[-1][1]["operation"] == "delete"

        model.close()
        store.close()


def main() -> None:
    asyncio.run(main_async())
    print("EVENT_DELTA_TESTS=PASS")


if __name__ == "__main__":
    main()
