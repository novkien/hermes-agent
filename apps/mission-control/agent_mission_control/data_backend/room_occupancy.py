"""Live room-slot occupancy, read-only.

The static half of /room-binding comes from config.yaml (which slots exist).
This is the other half: which task actually holds each slot right now. The
session-injector plugin owns that state in its own SQLite file.

Read-only by construction: the connection is opened `mode=ro` with
`PRAGMA query_only=1`, and the plugin remains the only writer. A locked or
missing database degrades to an empty list — occupancy is a monitoring
overlay, so it must never be able to fail the endpoint that carries the slot
configuration.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

QUERY_TIMEOUT_SECONDS = 2.0
MAX_ROWS = 200
READ_ATTEMPTS = 3
READ_RETRY_DELAY_SECONDS = (0.05, 0.15)

_BINDINGS_SQL = """
SELECT chat_id, task_id, room_slot, origin_session_key, origin_chat_id,
       origin_thread_id, status, terminal_request_id, bound_at, updated_at
  FROM room_task_bindings
 ORDER BY room_slot
 LIMIT ?
"""

_RESERVATIONS_SQL = """
SELECT chat_id, task_id, requester_session_key, room_slot, ceo_thread_id,
       created_at, expires_at
  FROM room_reservations
 ORDER BY room_slot
 LIMIT ?
"""

# The last task each slot carried, reconstructed from the handoff outbox.
# room_task_bindings only holds tasks that are in flight right now, so between
# tasks every slot reads as empty even though the room clearly just ran work.
# This gives each slot its most recent task_id plus whether that task has since
# been recorded complete, so the UI can show "slot 1 last ran TASK-X (done)"
# instead of nothing at all.
_RECENT_BINDINGS_SQL = """
SELECT o.room_slot AS room_slot,
       o.task_id   AS task_id,
       o.chat_id   AS chat_id,
       MAX(o.created_at) AS last_seen_at,
       (SELECT COUNT(*) FROM completed_task_ids c
         WHERE c.chat_id = o.chat_id AND c.task_id = o.task_id) AS completed
  FROM a2a_outbox o
 WHERE o.room_slot IS NOT NULL
 GROUP BY o.room_slot, o.task_id, o.chat_id
 ORDER BY last_seen_at DESC
 LIMIT ?
"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{path}?mode=ro", uri=True, timeout=QUERY_TIMEOUT_SECONDS
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    # A writer may briefly hold the WAL/read lock while the session-injector
    # plugin commits a binding transition. Respect the same bounded wait as
    # sqlite3.connect() instead of failing the monitoring overlay immediately.
    conn.execute(f"PRAGMA busy_timeout={int(QUERY_TIMEOUT_SECONDS * 1000)}")
    return conn


def _retryable_error(exc: sqlite3.Error) -> bool:
    detail = str(exc).lower()
    return any(
        marker in detail
        for marker in ("locked", "busy", "snapshot", "interrupted")
    )


def _read_live_occupancy_once(path: Path) -> dict[str, Any]:
    conn = None
    try:
        conn = _connect(path)
        bindings = [dict(r) for r in conn.execute(_BINDINGS_SQL, (MAX_ROWS,))]
        try:
            reservations = [dict(r) for r in conn.execute(_RESERVATIONS_SQL, (MAX_ROWS,))]
        except sqlite3.Error:
            # Older plugin revisions predate this table.
            reservations = []
        try:
            seen: set[int] = set()
            recent = []
            for row in conn.execute(_RECENT_BINDINGS_SQL, (MAX_ROWS,)):
                item = dict(row)
                slot = item.get("room_slot")
                if slot in seen:
                    continue
                seen.add(slot)
                item["completed"] = bool(item.get("completed"))
                recent.append(item)
            recent.sort(key=lambda r: r.get("room_slot") or 0)
        except sqlite3.Error:
            recent = []
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    return {
        "live_occupancy": bindings,
        "reservations": reservations,
        "recent_bindings": recent,
        "occupancy_available": True,
    }


def read_live_occupancy(db_path: str | Path) -> dict[str, Any]:
    """Return {live_occupancy, reservations, occupancy_source}.

    Never raises: the caller merges this into a response that must still be
    useful when the plugin's database is busy or absent.
    """
    path = Path(db_path)
    empty: dict[str, Any] = {
        "live_occupancy": [],
        "reservations": [],
        "recent_bindings": [],
        "occupancy_available": False,
    }
    if not path.is_file():
        empty["occupancy_note"] = "session-injector state database not found"
        return empty

    last_error: sqlite3.Error | None = None
    for attempt in range(READ_ATTEMPTS):
        try:
            return _read_live_occupancy_once(path)
        except sqlite3.Error as exc:
            last_error = exc
            if not _retryable_error(exc) or attempt + 1 >= READ_ATTEMPTS:
                break
            time.sleep(READ_RETRY_DELAY_SECONDS[attempt])

    detail = str(last_error).strip() if last_error else "unknown SQLite error"
    empty["occupancy_note"] = f"occupancy unavailable: {detail}"
    return empty
