#!/usr/bin/env python3
"""Persistent read-model, redaction and worker integration contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_mission_control.read_model import (  # noqa: E402
    READ_MODEL_SCHEMA_VERSION,
    ReadModel,
)
from agent_mission_control.workers import PollWorker  # noqa: E402


def task(task_id: str, title: str, **extra):
    return {"id": task_id, "title": title, "status": "running", **extra}


async def test_poll_hooks() -> None:
    calls: list[tuple[str, object]] = []
    fail = False

    async def fetch():
        if fail:
            raise RuntimeError("offline")
        return [{"id": "one"}]

    async def delta(_data, fp):
        calls.append(("delta", fp))

    async def success(_data, fp):
        calls.append(("success", fp))

    async def failure(exc):
        calls.append(("failure", type(exc).__name__))

    worker = PollWorker("test", 1, fetch, delta, lambda _data: "same", on_success=success, on_failure=failure)
    await worker.tick_once()
    await worker.tick_once()
    fail = True
    await worker.tick_once()
    assert [kind for kind, _ in calls] == ["success", "delta", "success", "failure"]
    assert worker.last_error == "offline" and worker.backoff_seconds == 2


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "live.db"
        model = ReadModel(path)
        assert model.available and model.health()["schema_version"] == READ_MODEL_SCHEMA_VERSION
        assert model._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"  # noqa: SLF001

        alpha_rev = model.replace_entities(
            "kanban.tasks",
            [task("a", "Alpha", token="must-not-persist", content="body", attachment_path="/secret")],
            profile_id="alpha",
            fingerprint="fp-a",
        )
        assert alpha_rev == 1
        # Identical success refreshes freshness but does not manufacture a new revision.
        assert model.replace_entities(
            "kanban.tasks", [task("a", "Alpha")], profile_id="alpha", fingerprint="fp-a"
        ) == alpha_rev
        beta_rev = model.replace_entities(
            "kanban.tasks", [task("b", "Beta")], profile_id="beta", fingerprint="fp-b"
        )
        assert beta_rev == 1
        alpha = model.resource("kanban.tasks", profile_id="alpha")
        beta = model.resource("kanban.tasks", profile_id="beta")
        assert [row["entity_id"] for row in alpha["entities"]] == ["a"]
        assert [row["entity_id"] for row in beta["entities"]] == ["b"]
        assert "token" not in alpha["entities"][0]["payload"]
        assert "content" not in alpha["entities"][0]["payload"]
        next_revision, projected = model.upsert_entity(
            "kanban.tasks", task("a", "Alpha updated", password="drop"), profile_id="alpha"
        )
        assert next_revision == 2 and projected["title"] == "Alpha updated"
        assert model.revision("kanban.tasks", profile_id="alpha") == 2
        assert model.delete_entity("kanban.tasks", "a", profile_id="alpha") == 3
        assert model.resource("kanban.tasks", profile_id="alpha")["entities"] == []
        # Restore a last-known-good row for stale/restart checks below.
        restored_revision, _ = model.upsert_entity(
            "kanban.tasks", task("a", "Alpha"), profile_id="alpha"
        )
        assert restored_revision == 4

        model.record_failure(("kanban.tasks",), RuntimeError("source offline"), profile_id="alpha")
        stale = model.resource("kanban.tasks", profile_id="alpha")
        assert stale["provenance"] == "stale"
        assert stale["entities"][0]["entity_id"] == "a", "failure erased last-known-good"
        unchanged = model.resource("kanban.tasks", profile_id="alpha", after_revision=restored_revision)
        assert unchanged["provenance"] == "unchanged" and unchanged["entities"] == []

        bootstrap = model.bootstrap("kanban", profile_id="alpha")
        assert bootstrap["profile_id"] == "alpha"
        assert set(bootstrap["resources"]) == {"kanban.boards", "kanban.tasks", "kanban.runs"}
        try:
            model.replace_summary("memory.detail", {"content": "forbidden"}, profile_id="alpha")
        except ValueError:
            pass
        else:
            raise AssertionError("memory content was accepted for persistence")

        model.close()
        reopened = ReadModel(path)
        assert reopened.resource("kanban.tasks", profile_id="alpha")["entities"][0]["entity_id"] == "a"
        reopened.close()

        raw = path.read_bytes().lower()
        assert b"must-not-persist" not in raw
        assert b"/secret" not in raw

        corrupt_path = Path(tmp) / "corrupt.db"
        corrupt_path.write_bytes(b"not a sqlite database")
        corrupt = ReadModel(corrupt_path)
        assert not corrupt.available
        assert corrupt.health()["status"] == "unavailable"
        assert corrupt.resource("issues")["provenance"] == "unavailable"

        # Migration is idempotent on an already-current database.
        migrated = ReadModel(Path(tmp) / "idempotent.db")
        migrated.close()
        migrated = ReadModel(Path(tmp) / "idempotent.db")
        assert migrated.available
        tables = {
            row[0] for row in migrated._conn.execute(  # noqa: SLF001
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"resource_snapshots", "resource_entities", "source_state"} <= tables
        migrated.close()

    asyncio.run(test_poll_hooks())
    print("READ_MODEL_TESTS=PASS")


if __name__ == "__main__":
    main()
