from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from agent_mission_control.data_backend import room_occupancy


def _create_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE room_task_bindings (
              chat_id TEXT, task_id TEXT, room_slot INTEGER,
              origin_session_key TEXT, origin_chat_id TEXT,
              origin_thread_id TEXT, status TEXT,
              terminal_request_id TEXT, bound_at REAL, updated_at REAL
            );
            CREATE TABLE room_reservations (
              chat_id TEXT, task_id TEXT, requester_session_key TEXT,
              room_slot INTEGER, ceo_thread_id TEXT,
              created_at REAL, expires_at REAL
            );
            CREATE TABLE a2a_outbox (
              request_id TEXT, task_id TEXT, room_slot INTEGER,
              chat_id TEXT, created_at REAL
            );
            CREATE TABLE completed_task_ids (
              chat_id TEXT, task_id TEXT, completed_at REAL
            );
            INSERT INTO room_task_bindings VALUES
              ('chat-1', 'task-1', 1, 'session', 'chat-1', '10',
               'ACTIVE', NULL, 1, 2);
            """
        )


def test_read_live_occupancy_retries_transient_lock(tmp_path: Path) -> None:
    path = tmp_path / "room_bindings.sqlite3"
    _create_db(path)
    real_connect = room_occupancy._connect
    calls = 0

    def flaky_connect(db_path: Path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(db_path)

    with patch.object(room_occupancy, "_connect", side_effect=flaky_connect):
        result = room_occupancy.read_live_occupancy(path)

    assert result["occupancy_available"] is True
    assert result["live_occupancy"][0]["task_id"] == "task-1"
    assert calls == 2


def test_read_live_occupancy_reports_final_sqlite_detail(tmp_path: Path) -> None:
    path = tmp_path / "room_bindings.sqlite3"
    path.touch()
    with patch.object(
        room_occupancy,
        "_connect",
        side_effect=sqlite3.OperationalError("no such table: room_task_bindings"),
    ):
        result = room_occupancy.read_live_occupancy(path)

    assert result["occupancy_available"] is False
    assert result["occupancy_note"] == (
        "occupancy unavailable: no such table: room_task_bindings"
    )
