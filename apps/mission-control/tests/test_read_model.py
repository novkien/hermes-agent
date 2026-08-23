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
    project_summary,
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

    worker = PollWorker(
        "test",
        1,
        fetch,
        delta,
        lambda _data: "same",
        on_success=success,
        on_failure=failure,
    )
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
        assert (
            model.available
            and model.health()["schema_version"] == READ_MODEL_SCHEMA_VERSION
        )
        assert model._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"  # noqa: SLF001

        alpha_rev = model.replace_entities(
            "kanban.tasks",
            [
                task(
                    "a",
                    "Alpha",
                    token="must-not-persist",
                    content="body",
                    attachment_path="/secret",
                )
            ],
            profile_id="alpha",
            fingerprint="fp-a",
        )
        assert alpha_rev == 1
        # Identical success refreshes freshness but does not manufacture a new revision.
        assert (
            model.replace_entities(
                "kanban.tasks",
                [task("a", "Alpha")],
                profile_id="alpha",
                fingerprint="fp-a",
            )
            == alpha_rev
        )
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
        duplicate_revision = model.replace_entities(
            "catalog.profiles",
            [{"name": "default", "model": "old"}, {"name": "default", "model": "new"}],
            profile_id="alpha",
            fingerprint="duplicate-profile",
        )
        duplicate_rows = model.resource("catalog.profiles", profile_id="alpha")[
            "entities"
        ]
        assert duplicate_revision == 1 and len(duplicate_rows) == 1
        assert duplicate_rows[0]["payload"]["model"] == "new"
        next_revision, projected = model.upsert_entity(
            "kanban.tasks",
            task("a", "Alpha updated", password="drop"),
            profile_id="alpha",
        )
        assert next_revision == 2 and projected["title"] == "Alpha updated"
        assert model.revision("kanban.tasks", profile_id="alpha") == 2

        # Mutation/native deltas are intentionally partial. They must enrich,
        # not erase, the last projected entity stored by the polling worker.
        model.replace_entities(
            "issues",
            [
                {
                    "id": 9,
                    "title": "Keep this title",
                    "severity": "high",
                    "status": "open",
                }
            ],
            profile_id="alpha",
        )
        issue_revision, issue = model.upsert_entity(
            "issues", {"id": 9, "status": "resolved"}, profile_id="alpha"
        )
        assert issue_revision == 2
        assert issue == {
            "id": 9,
            "title": "Keep this title",
            "severity": "high",
            "status": "resolved",
        }
        assert (
            model.resource("issues", profile_id="alpha")["entities"][0]["payload"][
                "title"
            ]
            == "Keep this title"
        )

        analytics = project_summary(
            "analytics.usage",
            {
                "period_days": 30,
                "daily": [
                    {"day": "2026-08-17", "total_tokens": 12, "messages": ["drop"]}
                ],
                "by_model": [
                    {"model": "safe-model", "api_calls": 2, "authorization": "drop"}
                ],
                "totals": {"total_tokens": 12, "estimated_cost": 0.2, "secret": "drop"},
                "content": "drop",
                "token": "drop",
            },
        )
        assert analytics["daily"] == [{"day": "2026-08-17", "total_tokens": 12}]
        assert analytics["by_model"] == [{"model": "safe-model", "api_calls": 2}]
        assert analytics["totals"] == {"total_tokens": 12, "estimated_cost": 0.2}
        assert "content" not in analytics and "token" not in analytics
        assert model.delete_entity("kanban.tasks", "a", profile_id="alpha") == 3
        assert model.resource("kanban.tasks", profile_id="alpha")["entities"] == []
        # Restore a last-known-good row for stale/restart checks below.
        restored_revision, _ = model.upsert_entity(
            "kanban.tasks", task("a", "Alpha"), profile_id="alpha"
        )
        assert restored_revision == 4

        model.record_failure(
            ("kanban.tasks",), RuntimeError("source offline"), profile_id="alpha"
        )
        stale = model.resource("kanban.tasks", profile_id="alpha")
        assert stale["provenance"] == "stale"
        assert stale["entities"][0]["entity_id"] == "a", (
            "failure erased last-known-good"
        )
        unchanged = model.resource(
            "kanban.tasks", profile_id="alpha", after_revision=restored_revision
        )
        assert unchanged["provenance"] == "unchanged" and unchanged["entities"] == []

        bootstrap = model.bootstrap("kanban", profile_id="alpha")
        assert bootstrap["profile_id"] == "alpha"
        assert set(bootstrap["resources"]) == {
            "kanban.boards",
            "kanban.tasks",
            "kanban.runs",
        }
        try:
            model.replace_summary(
                "memory.detail", {"content": "forbidden"}, profile_id="alpha"
            )
        except ValueError:
            pass
        else:
            raise AssertionError("memory content was accepted for persistence")

        model.close()
        reopened = ReadModel(path)
        assert (
            reopened.resource("kanban.tasks", profile_id="alpha")["entities"][0][
                "entity_id"
            ]
            == "a"
        )
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
            row[0]
            for row in migrated._conn.execute(  # noqa: SLF001
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"resource_snapshots", "resource_entities", "source_state"} <= tables
        migrated.close()

        # v2 removes only legacy profile-scoped copies of resources whose
        # contract is global.  Profile-scoped operational state survives.
        scope_path = Path(tmp) / "scope-migration.db"
        scoped = ReadModel(scope_path)
        scoped.close()
        conn = sqlite3.connect(scope_path)
        now = 1.0
        conn.execute(
            "INSERT INTO resource_snapshots VALUES (?,?,?,?,?,?)",
            ("default", "action.audit", 1, "legacy", now, '{"count":1}'),
        )
        conn.execute(
            "INSERT INTO resource_entities VALUES (?,?,?,?,?,?)",
            ("default", "action.audit", "1", 1, now, '{"id":1}'),
        )
        conn.execute(
            "INSERT INTO source_state "
            "(profile_id,resource_key,revision,last_success_at,health) VALUES (?,?,?,?,?)",
            ("default", "action.audit", 1, now, "healthy"),
        )
        conn.execute(
            "INSERT INTO resource_snapshots VALUES (?,?,?,?,?,?)",
            ("default", "kanban.tasks", 1, "keep", now, '{"count":0}'),
        )
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        scoped = ReadModel(scope_path)
        assert scoped.available
        for table in ("resource_snapshots", "resource_entities", "source_state"):
            count = scoped._conn.execute(  # noqa: SLF001
                f"SELECT COUNT(*) FROM {table} WHERE profile_id='default' "
                "AND resource_key='action.audit'"
            ).fetchone()[0]
            assert count == 0
        assert (
            scoped._conn.execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM resource_snapshots WHERE profile_id='default' "
                "AND resource_key='kanban.tasks'"
            ).fetchone()[0]
            == 1
        )
        scoped.close()

    asyncio.run(test_poll_hooks())
    print("READ_MODEL_TESTS=PASS")


if __name__ == "__main__":
    main()
