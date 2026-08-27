"""Regression tests for operator block cancellation of live Kanban workers."""

from __future__ import annotations

import importlib.util
import json
import signal
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _live_task(conn):
    task_id = kb.create_task(conn, title="live worker", assignee="worker")
    host = kb._claimer_id().split(":", 1)[0]
    claim_lock = f"{host}:test-block"
    assert kb.claim_task(conn, task_id, claimer=claim_lock) is not None
    kb._set_worker_pid(conn, task_id, 12345)
    conn.commit()
    return task_id, claim_lock


def test_operator_block_terminates_live_worker(kanban_home):
    """Blocking a running task must signal its worker after the DB commit."""
    conn = kb.connect()
    signals = []
    alive = {"value": True}

    def signal_fn(pid, sig):
        signals.append((pid, sig))
        if sig == signal.SIGTERM:
            alive["value"] = False

    original_alive = kb._pid_alive
    kb._pid_alive = lambda _pid: alive["value"]
    try:
        task_id, claim_lock = _live_task(conn)
        assert kb.block_task_and_terminate(
            conn,
            task_id,
            reason="owner stopped the work",
            kind="needs_input",
            signal_fn=signal_fn,
        ) is True

        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.worker_pid is None
        assert task.current_run_id is None
        assert signals == [(12345, signal.SIGTERM)]

        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        kinds = [row[0] for row in events]
        assert "blocked" in kinds
        assert "worker_terminated" in kinds
        terminated = next(row for row in events if row[0] == "worker_terminated")
        payload = json.loads(terminated[1])
        assert payload["worker_pid"] == 12345
        assert payload["claim_lock"] == claim_lock
        assert payload["termination"]["terminated"] is True
    finally:
        kb._pid_alive = original_alive
        conn.close()


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_block_lifecycle_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.router


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def test_dashboard_block_uses_operator_termination_path(client, monkeypatch):
    """The dashboard's blocked transition must not use passive block_task."""
    task = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "dashboard live worker", "assignee": "worker"},
    ).json()["task"]
    conn = kb.connect()
    try:
        host = kb._claimer_id().split(":", 1)[0]
        claim_lock = f"{host}:dashboard-block"
        assert kb.claim_task(conn, task["id"], claimer=claim_lock) is not None
        kb._set_worker_pid(conn, task["id"], 54321)
        conn.commit()
    finally:
        conn.close()

    calls = []

    def fake_terminate(pid, lock, *, signal_fn=None):
        calls.append((pid, lock))
        return {
            "prev_pid": pid,
            "host_local": True,
            "termination_attempted": True,
            "terminated": True,
            "sigkill": False,
        }

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", fake_terminate)
    response = client.patch(
        f"/api/plugins/kanban/tasks/{task['id']}",
        json={"status": "blocked", "block_reason": "owner stopped"},
    )
    assert response.status_code == 200, response.text
    assert calls == [(54321, claim_lock)]
