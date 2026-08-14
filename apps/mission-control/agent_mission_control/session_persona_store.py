"""Dashboard-owned store (store.db) — SQLite WAL.

Separate file and connection from ``store.py``: that one is the control store
frozen at its 8 permitted tables, and this is where dashboard data that Hermes
has nowhere to keep goes instead.

Storage minimalism is the rule here, not a convention: only what upstream
cannot hold at all belongs in this file, and only as a pointer. Everything a
Hermes surface already serves — profile details, model/provider, soul text,
session metadata — stays upstream and is fetched live on every read. The
persona table below therefore holds a session id and a profile name, nothing
else.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA_VERSION = 1

# v1: which profile's persona a chat session was created with. The gateway
# session row stores the resolved system_prompt but not its origin, so the name
# is unrecoverable from upstream once the session exists.
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS session_persona (
    session_id   TEXT PRIMARY KEY,
    profile_name TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""

_MIGRATIONS: list[tuple[int, str]] = [
    (1, _SCHEMA_V1),
]


class SessionPersonaStoreError(RuntimeError):
    pass


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def migrate(db_path: str | Path, *, max_retries: int = 3) -> None:
    path = str(db_path)
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            con = sqlite3.connect(path, timeout=10.0)
            try:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("PRAGMA busy_timeout=5000")
                con.execute("BEGIN EXCLUSIVE")
                version = int(con.execute("PRAGMA user_version").fetchone()[0])
                for v, sql in _MIGRATIONS:
                    if v > version:
                        con.executescript(sql)
                        con.execute(f"PRAGMA user_version = {v}")
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()
            return
        except sqlite3.OperationalError as e:  # locked — retry
            last_err = e
            time.sleep(0.2 * (attempt + 1))
    raise SessionPersonaStoreError(
        f"migration failed after {max_retries} attempts: {last_err}"
    )


class SessionPersonaStore:
    """Thread-safe handle over store.db (check_same_thread=False)."""

    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        migrate(self._path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self._path, timeout=10.0, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def set_persona(self, session_id: str, profile_name: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO session_persona (session_id, profile_name, created_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "profile_name=excluded.profile_name, created_at=excluded.created_at",
                (session_id, profile_name, _iso(time.time())),
            )
            self._conn.commit()

    def get_persona(self, session_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT profile_name FROM session_persona WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row["profile_name"] if row else None
