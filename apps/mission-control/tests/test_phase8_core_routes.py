#!/usr/bin/env python3
"""Core live-route mutation convergence contracts for Phase 8."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_mission_control.read_model import ReadModel  # noqa: E402
from agent_mission_control.routes import Router  # noqa: E402


def mutation_request(method: str, path: str, body: dict, query: str = "profile=alpha") -> Request:
    raw = json.dumps(body).encode("utf-8")
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    request = Request({
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": query.encode(), "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80), "state": {},
    }, receive)
    request.state.request_id = "phase8-cron"
    return request


async def main_async() -> None:
    order: list[str] = []
    published: list[dict] = []

    class Dashboard:
        async def request(self, method, path, **kwargs):
            order.append("upstream")
            assert (method, path) == ("PUT", "/api/cron/jobs/j1")
            assert kwargs["json_body"] == {"updates": {"name": "nightly", "enabled": False}}
            return 200, {"job": {"id": "j1", "name": "nightly", "state": "scheduled"}}, {}

    class Store:
        def append_audit(self, **kwargs):
            assert kwargs["result"] == "pending"
            order.append("audit:pending")

        def complete_audit(self, request_id, upstream_status, result):
            assert request_id == "phase8-cron" and upstream_status == 200 and result == "ok"
            order.append("audit:complete")

    class Bus:
        async def safe_publish(self, event_type, source_id, entity_type, entity_id, payload, **kwargs):
            order.append("publish")
            published.append({
                "event_type": event_type, "source_id": source_id, "entity_type": entity_type,
                "entity_id": entity_id, "payload": payload, **kwargs,
            })

    with tempfile.TemporaryDirectory() as tmp:
        router = object.__new__(Router)
        router.dashboard = Dashboard()
        router.store = Store()
        router.event_bus = Bus()
        router.read_model = ReadModel(Path(tmp) / "live.db")
        router.s = SimpleNamespace(live_default_profile="default")
        router._guard_mutation = lambda _request: order.append("guard") or {"id": "owner"}

        response = await Router.upstream_mutation(
            router,
            mutation_request(
                "PATCH", "/api/upstream/api/cron/jobs/j1",
                {"updates": {"name": "nightly", "enabled": False, "secret": "drop"}},
            ),
            "api/cron/jobs/j1",
        )
        assert response.status_code == 200
        assert order == ["guard", "audit:pending", "upstream", "audit:complete", "publish"]

        resource = router.read_model.resource("cron.jobs", profile_id="alpha")
        assert resource["revision"] == 1
        assert resource["entities"] == [{
            "entity_id": "j1", "revision": 1,
            "payload": {"enabled": False, "id": "j1", "name": "nightly", "state": "scheduled"},
        }]
        assert published == [{
            "event_type": "cron.changed", "source_id": "dashboard", "entity_type": "cron_job",
            "entity_id": "j1",
            "payload": {"enabled": False, "id": "j1", "name": "nightly", "state": "scheduled"},
            "coverage": "native", "profile_id": "alpha", "resource_key": "cron.jobs",
            "operation": "upsert", "revision": 1,
        }], published
        router.read_model.close()


def main() -> None:
    asyncio.run(main_async())
    print("PHASE8_CORE_ROUTE_TESTS=PASS")


if __name__ == "__main__":
    main()
