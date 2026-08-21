#!/usr/bin/env python3
"""Executable contract checks for the complete live-resource manifest."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_mission_control.event_bus import EventBus, sse_frame  # noqa: E402
from agent_mission_control.live_resources import (  # noqa: E402
    EVENT_RESOURCES,
    RESOURCE_SPECS,
    ROUTE_RESOURCES,
    canonical_event,
    validate_resource_contract,
)
from agent_mission_control.store import Store  # noqa: E402


EXPECTED_ROUTES = {
    "overview", "chat", "sessions", "fleet", "kanban", "cron", "activity",
    "alerts", "analytics", "issues", "permits", "room-binding", "threads",
    "action-audit", "skills", "memory", "profiles", "models", "tools", "mcp",
    "plugins", "repositories", "webhooks", "channels", "artifacts", "files",
    "system-manager", "logs", "command-center", "settings", "llama-proxy", "9router",
}

PRODUCED_EVENTS = {
    "task.changed", "run.changed", "session.changed", "session.running",
    "permit.changed", "issue.changed", "cron.changed", "log.appended",
    "alert.changed", "source.health", "cache.invalidated", "repository.changed",
    "system-manager.changed", "plugins.changed", "profiles.changed", "mcp.changed",
    "toolsets.changed", "webhooks.changed", "channels.changed", "memory.changed",
    "gateway.changed",
}


async def test_event_transport() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "control.db")
        bus = EventBus(store)
        event = await bus.publish(
            "task.changed", "kanban", "task", "task-1", {"status": "running"}
        )
        assert event is not None
        assert event["resource_key"] == "kanban.tasks"
        assert event["operation"] == "upsert"
        frame = sse_frame(event)
        assert "event: state.change\n" in frame
        assert "event: task.changed\n" not in frame
        data_line = next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
        payload = json.loads(data_line)
        assert payload["event_type"] == "task.changed"
        assert payload["resource_key"] == "kanban.tasks"
        store.close()


def main() -> None:
    validate_resource_contract()
    assert set(ROUTE_RESOURCES) == EXPECTED_ROUTES
    assert len(ROUTE_RESOURCES) == 32
    assert PRODUCED_EVENTS <= set(EVENT_RESOURCES)
    assert all(resources for resources in ROUTE_RESOURCES.values())
    assert all(key in RESOURCE_SPECS for values in ROUTE_RESOURCES.values() for key in values)
    assert canonical_event("session.changed", "").operation == "invalidate"
    assert canonical_event("session.changed", "session-1").operation == "upsert"

    registry = (ROOT / "frontend" / "dist" / "pure" / "route-registry.js").read_text()
    registry_keys = set(re.findall(r"^  ['\"]?([a-z0-9-]+)['\"]?: route\(", registry, re.MULTILINE))
    assert registry_keys == EXPECTED_ROUTES, (registry_keys ^ EXPECTED_ROUTES)

    events_js = (ROOT / "frontend" / "dist" / "events.js").read_text()
    assert "addEventListener('state.change'" in events_js
    assert "for (const type of SseClient.EVENT_TYPES)" not in events_js
    assert "event.event_type" in events_js
    asyncio.run(test_event_transport())
    print("LIVE_RESOURCE_CONTRACT_TESTS=PASS")


if __name__ == "__main__":
    main()
