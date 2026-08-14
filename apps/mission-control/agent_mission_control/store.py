"""Local control store (agentos-dashboard.db) — SQLite WAL.

Owns EXACTLY the 8 permitted dashboard-control tables (architecture-freeze
§9). NO Hermes record bodies: no session/message/task/permit/issue content is
ever written here. Writes happen through this module only.

Schema versioning: user_version pragma + sequential migration scripts.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

_SCHEMA_VERSION = 3

# v1 CREATE statements. Table names/columns are part of the accepted contract
# (architecture-freeze §9) — do not rename or extend without a new migration.
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    csrf_token  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    profile_id TEXT,
    PRIMARY KEY (key, profile_id)
);
CREATE TABLE IF NOT EXISTS saved_views (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    route      TEXT NOT NULL,
    filters    TEXT NOT NULL DEFAULT '{}',
    profile_id TEXT
);
CREATE TABLE IF NOT EXISTS alert_rules (
    id      TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    config  TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS alert_acknowledgements (
    id         TEXT PRIMARY KEY,
    alert_id   TEXT NOT NULL,
    action     TEXT NOT NULL,
    expires_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    target          TEXT,
    profile_id      TEXT,
    timestamp       TEXT NOT NULL,
    request_summary TEXT NOT NULL,
    upstream_status INTEGER,
    result          TEXT
);
CREATE TABLE IF NOT EXISTS cache_metadata (
    key         TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL,
    fingerprint TEXT,
    fetched_at  TEXT NOT NULL,
    stale_after TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_fingerprints (
    source_id    TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_audit_request_id ON action_audit(request_id);
CREATE INDEX IF NOT EXISTS idx_action_audit_timestamp  ON action_audit(timestamp);
"""

# v2 (Stage 5): DB-backed event replay buffer (bounded at read time).
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS event_replay (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL UNIQUE,
    event_type  TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    source_id   TEXT NOT NULL DEFAULT '',
    entity_type TEXT NOT NULL DEFAULT '',
    entity_id   TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    coverage    TEXT NOT NULL DEFAULT 'polled'
);
CREATE INDEX IF NOT EXISTS idx_event_replay_occurred ON event_replay(occurred_at DESC);
"""

# v3 (S8-FIX t_7fcdab02): reconcile action_audit.profile_id to nullable.
#
# Some runtime DBs were created by an earlier store DDL with
# `profile_id TEXT NOT NULL DEFAULT ''` (plus stricter target/request_summary/
# result and INTEGER timestamp), diverging from the canonical 001_initial.sql
# (`profile_id TEXT` nullable). chat session-create passed profile_id=None and
# 500'd with NOT NULL constraint failed. SQLite cannot drop NOT NULL via ALTER
# TABLE, so this uses the standard table-rebuild pattern; type affinity
# converts the legacy INTEGER timestamp to TEXT. Mirrors
# migrations/003_audit_profile_null.sql.
_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS action_audit_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    target          TEXT,
    profile_id      TEXT,
    timestamp       TEXT NOT NULL,
    request_summary TEXT NOT NULL,
    upstream_status INTEGER,
    result          TEXT
);
INSERT INTO action_audit_new
    (id, request_id, actor, action, target, profile_id, timestamp,
     request_summary, upstream_status, result)
    SELECT id, request_id, actor, action, target, profile_id, timestamp,
           request_summary, upstream_status, result
    FROM action_audit;
DROP TABLE action_audit;
ALTER TABLE action_audit_new RENAME TO action_audit;
CREATE INDEX IF NOT EXISTS idx_action_audit_request_id ON action_audit(request_id);
CREATE INDEX IF NOT EXISTS idx_action_audit_timestamp  ON action_audit(timestamp);
"""

# Ordered migrations: (version, sql). v1 is idempotent (IF NOT EXISTS) so a
# fresh DB and an existing v1 DB converge.
_MIGRATIONS: list[tuple[int, str]] = [
    (1, _SCHEMA_V1),
    (2, _SCHEMA_V2),
    (3, _SCHEMA_V3),
]


class StoreError(RuntimeError):
    pass


def migrate(db_path: str | Path, *, max_retries: int = 3) -> None:
    """Apply pending migrations; raises StoreError on failure (no partial apply).

    Uses an EXCLUSIVE transaction + busy_timeout so concurrent workers cannot
    observe a half-migrated schema.
    """
    path = str(db_path)
    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            con = sqlite3.connect(path, timeout=10.0)
            try:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("PRAGMA busy_timeout=5000")
                con.execute("PRAGMA foreign_keys=ON")
                con.execute("BEGIN EXCLUSIVE")
                cur = con.execute("PRAGMA user_version")
                version = int(cur.fetchone()[0])
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
    raise StoreError(f"migration failed after {max_retries} attempts: {last_err}")


class Store:
    """Thread-safe handle over the local control DB (check_same_thread=False)."""

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
            self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- internal helpers -------------------------------------------------
    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _commit(self) -> None:
        with self._lock:
            self._conn.commit()

    # -- sessions ---------------------------------------------------------
    def create_session(self, sid: str, csrf_token: str, ttl_seconds: int) -> None:
        now = time.time()
        self._execute(
            "INSERT INTO sessions (id, created_at, expires_at, csrf_token) "
            "VALUES (?, ?, ?, ?)",
            (sid, _iso(now), _iso(now + ttl_seconds), csrf_token),
        )
        self._commit()

    def get_session(self, sid: str) -> Optional[dict[str, Any]]:
        row = self._execute(
            "SELECT id, created_at, expires_at, csrf_token FROM sessions WHERE id = ?",
            (sid,),
        ).fetchone()
        return dict(row) if row else None

    def delete_session(self, sid: str) -> None:
        self._execute("DELETE FROM sessions WHERE id = ?", (sid,))
        self._commit()

    def delete_expired_sessions(self, now: float | None = None) -> int:
        now = now or time.time()
        cur = self._execute(
            "DELETE FROM sessions WHERE expires_at < ?", (_iso(now),)
        )
        self._commit()
        return cur.rowcount

    def list_sessions(self) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT id, created_at, expires_at, csrf_token FROM sessions ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- action audit (append-only) ---------------------------------------
    def append_audit(
        self,
        request_id: str,
        actor: str,
        action: str,
        target: str | None,
        profile_id: str | None,
        request_summary: str,
        upstream_status: int | None,
        result: str | None,
    ) -> None:
        """Append one audit row. Raises sqlite3.Error on failure so callers
        can abort the mutation BEFORE any upstream call."""
        self._execute(
            "INSERT INTO action_audit (request_id, actor, action, target, "
            "profile_id, timestamp, request_summary, upstream_status, result) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                actor,
                action,
                target,
                profile_id,
                _iso(time.time()),
                request_summary,
                upstream_status,
                result,
            ),
        )
        self._commit()

    def list_audit(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT id, request_id, actor, action, target, profile_id, timestamp, "
            "request_summary, upstream_status, result FROM action_audit "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (max(1, min(limit, 500)), max(0, offset)),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_audit(self) -> int:
        return int(self._execute("SELECT COUNT(*) FROM action_audit").fetchone()[0])

    def complete_audit(self, request_id: str, upstream_status: int, result: str) -> None:
        """Mark the pending audit row(s) for a request_id with the upstream
        outcome (in-place UPDATE — append-only refers to row creation; the
        pending row is updated, never duplicated)."""
        self._execute(
            "UPDATE action_audit SET upstream_status = ?, result = ? "
            "WHERE request_id = ? AND result = 'pending'",
            (upstream_status, result, request_id),
        )
        self._commit()

    def audit_failures_since(self, window_seconds: int, min_count: int) -> bool:
        """R11: >=min_count mutation failures with upstream_status 4xx/5xx in window."""
        cutoff = _iso(time.time() - window_seconds)
        rows = self._execute(
            "SELECT COUNT(*) AS c FROM action_audit WHERE timestamp>=? "
            "AND upstream_status IS NOT NULL AND upstream_status>=400 AND upstream_status<600",
            (cutoff,),
        ).fetchone()
        return int(rows["c"] if rows else 0) >= min_count

    # -- preferences / saved views / alerts (thin, JSON-value) ------------
    def set_preference(self, key: str, value: Any, profile_id: str | None = None) -> None:
        self._execute(
            "INSERT INTO preferences (key, value, profile_id) VALUES (?, ?, ?) "
            "ON CONFLICT(key, profile_id) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value), profile_id),
        )
        self._commit()

    def get_preference(self, key: str, profile_id: str | None = None) -> Optional[Any]:
        row = self._execute(
            "SELECT value FROM preferences WHERE key = ? AND profile_id IS ?",
            (key, profile_id),
        ).fetchone()
        if not row:
            # SQLite `IS ?` matches NULL; fetch again for non-null profile
            row = self._execute(
                "SELECT value FROM preferences WHERE key = ? AND profile_id = ?",
                (key, profile_id),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return row["value"]

    def list_preferences(self, profile_id: str | None = None) -> dict[str, Any]:
        """Global preferences overlaid with the profile's own, profile winning."""
        out: dict[str, Any] = {}
        for scope in (None, profile_id) if profile_id else (None,):
            rows = self._execute(
                "SELECT key, value FROM preferences WHERE profile_id IS ?",
                (scope,),
            ).fetchall()
            for row in rows:
                try:
                    out[row["key"]] = json.loads(row["value"])
                except (TypeError, json.JSONDecodeError):
                    out[row["key"]] = row["value"]
        return out

    def save_view(self, view_id: str, name: str, route: str, filters: dict,
                  profile_id: str | None = None) -> None:
        self._execute(
            "INSERT INTO saved_views (id, name, route, filters, profile_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (view_id, name, route, json.dumps(filters), profile_id),
        )
        self._commit()

    # -- cache metadata ---------------------------------------------------
    def upsert_cache_metadata(
        self, key: str, source_id: str, fingerprint: str | None,
        fetched_at: float, stale_after: float,
    ) -> None:
        self._execute(
            "INSERT INTO cache_metadata (key, source_id, fingerprint, fetched_at, stale_after) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET source_id=excluded.source_id, "
            "fingerprint=excluded.fingerprint, fetched_at=excluded.fetched_at, "
            "stale_after=excluded.stale_after",
            (key, source_id, fingerprint, _iso(fetched_at), _iso(stale_after)),
        )
        self._commit()

    def get_cache_metadata(self, key: str) -> Optional[dict[str, Any]]:
        row = self._execute(
            "SELECT key, source_id, fingerprint, fetched_at, stale_after "
            "FROM cache_metadata WHERE key = ?",
            (key,),
        ).fetchone()
        return dict(row) if row else None

    # -- schema fingerprints ---------------------------------------------
    def record_fingerprint(self, source_id: str, fingerprint: str) -> None:
        self._execute(
            "INSERT INTO schema_fingerprints (source_id, fingerprint, recorded_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(source_id) DO UPDATE SET fingerprint=excluded.fingerprint, "
            "recorded_at=excluded.recorded_at",
            (source_id, fingerprint, _iso(time.time())),
        )
        self._commit()

    def get_fingerprint(self, source_id: str) -> Optional[str]:
        row = self._execute(
            "SELECT fingerprint FROM schema_fingerprints WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        return row["fingerprint"] if row else None

    def list_fingerprints(self) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT source_id, fingerprint, recorded_at FROM schema_fingerprints "
            "ORDER BY recorded_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- alert rules / acknowledgements -----------------------------------
    def upsert_alert_rule(self, rule_id: str, config: dict, enabled: bool = True) -> None:
        self._execute(
            "INSERT INTO alert_rules (id, rule_id, config, enabled) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET config=excluded.config, enabled=excluded.enabled",
            (rule_id, rule_id, json.dumps(config), 1 if enabled else 0),
        )
        self._commit()

    def list_alert_rules(self) -> list[dict[str, Any]]:
        rows = self._execute("SELECT * FROM alert_rules ORDER BY rule_id").fetchall()
        return [dict(r) for r in rows]

    def is_alert_rule_enabled(self, rule_id: str) -> bool:
        row = self._execute(
            "SELECT enabled FROM alert_rules WHERE rule_id = ?", (rule_id,)
        ).fetchone()
        return bool(row and row["enabled"])

    def acknowledge_alert(self, alert_id: str, action: str, expires_at: float | None = None) -> None:
        self._execute(
            "INSERT INTO alert_acknowledgements (id, alert_id, action, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET action=excluded.action, "
            "expires_at=excluded.expires_at, created_at=excluded.created_at",
            (f"{alert_id}:{action}", alert_id, action,
             int(expires_at) if expires_at is not None else None, _iso(time.time())),
        )
        self._commit()

    def clear_acknowledgements(self, alert_id: str) -> None:
        """Drop every ack/snooze row for an alert that has resolved.

        Without this, the next occurrence of the same condition would come back
        already acknowledged and stay invisible to the operator.
        """
        self._execute(
            "DELETE FROM alert_acknowledgements WHERE alert_id = ?", (alert_id,)
        )
        self._commit()

    def get_acknowledgement(self, alert_id: str) -> Optional[dict[str, Any]]:
        row = self._execute(
            "SELECT * FROM alert_acknowledgements WHERE alert_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (alert_id,),
        ).fetchone()
        return dict(row) if row else None

    # -- event replay (migration v2, Stage 5) -----------------------------
    def insert_event_replay(
        self,
        event_id: str,
        event_type: str,
        occurred_at: int,
        source_id: str,
        entity_type: str,
        entity_id: str,
        payload: dict,
        coverage: str,
    ) -> bool:
        """Insert if event_id not already present. Returns True if inserted."""
        cur = self._execute(
            "INSERT OR IGNORE INTO event_replay (event_id, event_type, occurred_at, "
            "source_id, entity_type, entity_id, payload_json, coverage) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, event_type, occurred_at, source_id, entity_type, entity_id,
             json.dumps(payload), coverage),
        )
        self._commit()
        return cur.rowcount > 0

    def replay_events_after(self, last_event_id: str, limit: int = 2000) -> list[dict[str, Any]]:
        """Events strictly newer than last_event_id (Last-Event-ID replay).

        Resolves the event_id to its numeric row id first, then returns rows
        with id > that id (bounded, indexed).
        """
        row = self._execute(
            "SELECT id FROM event_replay WHERE event_id = ?", (last_event_id,)
        ).fetchone()
        if row is None:
            return []
        rows = self._execute(
            "SELECT event_id, event_type, occurred_at, source_id, entity_type, entity_id, "
            "payload_json, coverage FROM event_replay WHERE id > ? "
            "ORDER BY id ASC LIMIT ?",
            (row["id"], limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json"))
            out.append(d)
        return out

    def replay_latest(self, limit: int = 2000) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT event_id, event_type, occurred_at, source_id, entity_type, entity_id, "
            "payload_json, coverage FROM event_replay ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json"))
            out.append(d)
        return out

    def replay_last_event_id(self) -> str:
        row = self._execute(
            "SELECT event_id FROM event_replay ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["event_id"] if row else ""

    def event_replay_count(self) -> int:
        return int(self._execute("SELECT COUNT(*) FROM event_replay").fetchone()[0])

    # -- introspection (evidence) ----------------------------------------
    def table_names(self) -> list[str]:
        rows = self._execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]

    def schema_version(self) -> int:
        return int(self._execute("PRAGMA user_version").fetchone()[0])

    # -- test/legacy-compat accessors (S5 suite used conn()/user_version()) --
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def user_version(self) -> int:
        return self.schema_version()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
