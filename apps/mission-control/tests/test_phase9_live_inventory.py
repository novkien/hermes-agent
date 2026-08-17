#!/usr/bin/env python3
"""Live repository/System Manager worker and redaction contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_mission_control.app import _wire_audit_live_updates  # noqa: E402
from agent_mission_control.read_model import ReadModel  # noqa: E402
from agent_mission_control.store import Store  # noqa: E402
from agent_mission_control.workers import SourceWorkers  # noqa: E402


class CaptureBus:
    def __init__(self) -> None:
        self.events: list[tuple[tuple, dict]] = []

    async def publish(self, *args, **kwargs):
        self.events.append((args, kwargs))

    async def safe_publish(self, *args, **kwargs):
        self.events.append((args, kwargs))


class RepositoryService:
    def status_all(self, *, fetch: bool, include_github: bool):
        assert fetch is True and include_github is True
        return [{
            "name": "hermes", "repo_full_name": "owner/hermes", "state": "synced",
            "branch": "master", "local_sha": "abc", "remote_sha": "abc",
            "ahead": 0, "behind": 0, "path": "/secret/checkout",
            "origin_url": "ssh://private.example/repo", "working_tree": {"dirty": False},
        }]


class SystemManagerClient:
    async def request(self, method, path, *, request_id, json_body):
        assert (method, path) == ("POST", "/v1/db/read")
        table = json_body["table"]
        rows = {
            "services": [{
                "id": "svc-1", "name": "mission-control", "observed_state": "running",
                "recent_logs": "never persist this log body", "health": "healthy",
            }],
            "api": [{
                "id": "api-1", "service": "provider", "base_url": "https://example.invalid",
                "api_key": "secret-api-key", "notes": "private",
            }],
            "accounts": [{
                "id": "account-1", "provider": "example", "username": "operator",
                "password": "secret-password", "totp_secret": "secret-totp",
            }],
            "notes": [{
                "id": "note-1", "title": "Inventory note", "body": "private body",
                "category": "ops",
            }],
        }[table]
        return 200, {"rows": rows}


def workers(model: ReadModel) -> SourceWorkers:
    value = SourceWorkers.__new__(SourceWorkers)
    value.bus = CaptureBus()
    value.alert_engine = None
    value.read_model = model
    value.cfg = SimpleNamespace(live_default_profile="alpha")
    value.repository_service = RepositoryService()
    value.system_manager_client = SystemManagerClient()
    value._initialized_sources = set()
    value._repository_entities = {}
    value._system_manager_entities = {}
    return value


async def main_async() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "live.db"
        model = ReadModel(path)
        value = workers(model)

        # Audit notifications happen after each durable transition and are
        # isolated from unrelated session writes.  This is the immediate
        # native-delta path; the inventory worker remains convergence backup.
        audit_store = Store(Path(tmp) / "control.db")
        notifications: list[dict] = []
        audit_store.set_audit_listener(notifications.append)
        audit_store.create_session("session-1", "csrf", 60)
        audit_store.delete_session("session-1")
        assert notifications == []
        audit_store.append_audit(
            "request-1", "operator", "permit.decide", "permit-1", "alpha",
            '{"decision":"approve"}', None, "pending",
        )
        assert len(notifications) == 1
        assert notifications[-1]["request_id"] == "request-1"
        assert notifications[-1]["result"] == "pending"
        assert notifications[-1]["upstream_status"] is None
        audit_store.complete_audit("request-1", 200, "completed")
        assert len(notifications) == 2
        assert notifications[-1]["id"] == notifications[0]["id"]
        assert notifications[-1]["result"] == "completed"
        assert notifications[-1]["upstream_status"] == 200
        audit_store.close()

        live_audit_store = Store(Path(tmp) / "control-live.db")
        live_audit_bus = CaptureBus()
        _wire_audit_live_updates(SimpleNamespace(
            store=live_audit_store,
            event_bus=live_audit_bus,
            read_model=model,
        ))
        live_audit_store.append_audit(
            "request-live", "operator", "issue.update", "issue-1", "alpha",
            '{"status":"resolved"}', None, "pending",
        )
        live_audit_store.complete_audit("request-live", 200, "completed")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(live_audit_bus.events) == 2
        pending_args, pending_kwargs = live_audit_bus.events[0]
        completed_args, completed_kwargs = live_audit_bus.events[1]
        assert pending_args[:4] == (
            "audit.changed", "control-store", "audit", "1"
        )
        assert pending_args[4]["result"] == "pending"
        assert completed_args[4]["result"] == "completed"
        assert pending_kwargs["resource_key"] == "action.audit"
        assert completed_kwargs["revision"] > pending_kwargs["revision"]
        assert pending_kwargs["profile_id"] == completed_kwargs["profile_id"] == ""
        audit_resource = model.resource("action.audit", profile_id="alpha")
        assert audit_resource["entities"][0]["payload"]["result"] == "completed"
        live_audit_store.close()

        repositories = await value._fetch_repositories()
        assert repositories[0]["name"] == "hermes"
        await value._persist_success("repositories", repositories, value._fp_repositories(repositories))
        await value._on_repositories(repositories, "one")
        assert value.bus.events == [], "initial repository inventory must not flood SSE"

        changed = [{**repositories[0], "state": "ahead", "local_sha": "def", "ahead": 1}]
        await value._on_repositories(changed, "two")
        assert len(value.bus.events) == 1
        args, kwargs = value.bus.events[-1]
        assert args[:4] == ("repository.changed", "repository-worker", "repository", "hermes")
        assert args[4]["state"] == "ahead" and kwargs["operation"] == "upsert"
        await value._on_repositories([], "three")
        assert value.bus.events[-1][1]["operation"] == "delete"

        inventory = await value._fetch_system_manager()
        assert {row["entity_key"] for row in inventory} == {
            "services:svc-1", "api:api-1", "accounts:account-1", "notes:note-1",
        }
        await value._persist_success(
            "system-manager", inventory, value._fp_system_manager(inventory)
        )
        await value._on_system_manager(inventory, "one")
        updated = [
            {**row, "health": "degraded"} if row["entity_key"] == "services:svc-1" else row
            for row in inventory
        ]
        await value._on_system_manager(updated, "two")
        args, kwargs = value.bus.events[-1]
        assert args[:4] == (
            "system-manager.changed", "system-manager", "inventory", "services:svc-1"
        )
        assert args[4]["health"] == "degraded" and kwargs["operation"] == "upsert"

        repo_resource = model.resource("repositories", profile_id="alpha")
        assert [row["entity_id"] for row in repo_resource["entities"]] == ["hermes"]
        system_resource = model.resource("system-manager.inventory", profile_id="alpha")
        assert len(system_resource["entities"]) == 4
        assert all(":" in row["entity_id"] for row in system_resource["entities"])

        # Scan both the decoded contract and raw SQLite bytes: neither route
        # path, credentials, note/log bodies nor arbitrary source payload may survive projection.
        encoded = path.read_bytes()
        for forbidden in (
            b"secret-api-key", b"secret-password", b"secret-totp",
            b"never persist this log body", b"private body", b"/secret/checkout",
            b"ssh://private.example/repo",
        ):
            assert forbidden not in encoded, forbidden

        model.close()


def main() -> None:
    asyncio.run(main_async())
    print("PHASE9_LIVE_INVENTORY_TESTS=PASS")


if __name__ == "__main__":
    main()
