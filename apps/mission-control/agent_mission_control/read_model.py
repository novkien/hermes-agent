"""Persistent, sanitized live read model for Mission Control.

This database is intentionally separate from the control store.  It contains
only projector-approved operational state and is disposable: corruption or a
write failure degrades bootstrap data without blocking ordinary reads or any
mutation path.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterable

from .live_resources import RESOURCE_SPECS, ROUTE_RESOURCES


READ_MODEL_SCHEMA_VERSION = 1
MAX_ENTITIES_PER_RESOURCE = 5000
MAX_PROJECTED_JSON_BYTES = 1_000_000

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS resource_snapshots (
    profile_id TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    fingerprint TEXT NOT NULL DEFAULT '',
    fetched_at REAL NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (profile_id, resource_key)
);
CREATE TABLE IF NOT EXISTS resource_entities (
    profile_id TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    occurred_at REAL NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (profile_id, resource_key, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_resource_entities_revision
    ON resource_entities (profile_id, resource_key, revision, entity_id);
CREATE TABLE IF NOT EXISTS source_state (
    profile_id TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    cursor TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    last_success_at REAL,
    last_error_at REAL,
    last_error TEXT,
    schema_fingerprint TEXT,
    health TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY (profile_id, resource_key)
);
"""


# Fields are intentionally explicit. Unknown upstream keys are dropped.
PROJECTOR_FIELDS: dict[str, frozenset[str]] = {
    "source.health": frozenset({"source_id", "healthy", "status", "previous", "checked_at", "message"}),
    "sessions": frozenset({"id", "session_id", "title", "profile", "platform", "model", "message_count", "created_at", "updated_at", "last_activity_at", "last_active", "ended_at", "archived"}),
    "sessions.running": frozenset({"session_id", "run_id", "started_at", "platform", "profile"}),
    "kanban.tasks": frozenset({"id", "title", "status", "assignee", "priority", "board", "profile", "current_run_id", "created_at", "updated_at", "started_at", "completed_at", "last_heartbeat_at"}),
    "permits": frozenset({"permit_id", "id", "title", "status", "severity", "approved", "executed", "created_at", "updated_at", "expires_at"}),
    "issues": frozenset({"id", "issue", "title", "summary", "status", "severity", "occurrence_count", "first_seen_at", "last_seen_at", "updated_at"}),
    "cron.jobs": frozenset({"id", "name", "state", "enabled", "schedule", "last_run_at", "next_run_at", "last_status", "profile"}),
    "alerts": frozenset({"id", "rule_id", "title", "state", "severity", "created_at", "updated_at", "snoozed_until"}),
    "repositories": frozenset({"repo", "name", "branch", "head", "ahead", "behind", "dirty", "status", "updated_at"}),
    "system-manager.inventory": frozenset({"id", "name", "type", "host_id", "state", "status", "enabled", "updated_at"}),
    "rooms.binding": frozenset({"slot", "state", "task_id", "thread_ids", "held_since", "reserved"}),
    "rooms.sessions": frozenset({"session_id", "chat_id", "thread_id", "profile", "title", "last_activity_at"}),
}

SUMMARY_FIELDS: dict[str, frozenset[str]] = {
    "overview.summary": frozenset({"total", "running", "ready", "blocked", "done", "pending_permits", "open_issues"}),
    "analytics.usage": frozenset({"total_tokens", "tokens", "token_count", "input_tokens", "output_tokens", "cost", "currency", "period", "from", "to"}),
    "iframe.health": frozenset({"service", "healthy", "status", "checked_at"}),
}

FORBIDDEN_KEYS = frozenset({
    "authorization", "cookie", "cookies", "token", "access_token", "api_key",
    "password", "secret", "headers", "content", "messages", "transcript",
    "stored_path", "attachment_path", "env", ".env",
})


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:4096]
    if isinstance(value, list):
        return [_safe_scalar(item) for item in value[:200] if not isinstance(item, dict)]
    return str(value)[:4096]


def project_entity(resource_key: str, value: dict[str, Any]) -> dict[str, Any]:
    allowed = PROJECTOR_FIELDS.get(resource_key)
    if allowed is None:
        raise ValueError(f"resource has no entity projector: {resource_key}")
    projected = {
        key: _safe_scalar(child)
        for key, child in value.items()
        if key in allowed and key.lower() not in FORBIDDEN_KEYS
    }
    return projected


def project_summary(resource_key: str, value: dict[str, Any]) -> dict[str, Any]:
    allowed = SUMMARY_FIELDS.get(resource_key, frozenset())
    return {
        key: _safe_scalar(child)
        for key, child in value.items()
        if key in allowed and key.lower() not in FORBIDDEN_KEYS
    }


def _json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PROJECTED_JSON_BYTES:
        raise ValueError("projected resource exceeds persistence bound")
    return encoded


class ReadModel:
    """Thread-safe SQLite WAL read model with fail-open degradation."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.available = False
        self.error: str | None = None
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=3000")
            self._conn = conn
            self._migrate()
            self.available = True
        except (OSError, sqlite3.DatabaseError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"[:500]
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.DatabaseError:
                    pass
            self._conn = None

    def _migrate(self) -> None:
        assert self._conn is not None
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > READ_MODEL_SCHEMA_VERSION:
            raise sqlite3.DatabaseError("read-model schema is newer than this binary")
        if version < 1:
            self._conn.executescript(SCHEMA_V1)
            self._conn.execute(f"PRAGMA user_version={READ_MODEL_SCHEMA_VERSION}")
            self._conn.commit()

    def _connection(self) -> sqlite3.Connection:
        if not self.available or self._conn is None:
            raise sqlite3.DatabaseError(self.error or "read model unavailable")
        return self._conn

    @staticmethod
    def _profile(profile_id: str | None) -> str:
        value = str(profile_id or "default").strip()
        if not value or len(value) > 128:
            raise ValueError("invalid profile id")
        return value

    def _next_revision(self, conn: sqlite3.Connection, profile_id: str, resource_key: str) -> int:
        row = conn.execute(
            "SELECT revision FROM source_state WHERE profile_id=? AND resource_key=?",
            (profile_id, resource_key),
        ).fetchone()
        return int(row[0] if row else 0) + 1

    @classmethod
    def _scope(cls, resource_key: str, profile_id: str | None) -> str:
        return "__global__" if RESOURCE_SPECS[resource_key].profile_scope == "global" else cls._profile(profile_id)

    def replace_entities(
        self,
        resource_key: str,
        rows: Iterable[dict[str, Any]],
        *,
        profile_id: str = "default",
        fingerprint: str = "",
        schema_fingerprint: str | None = None,
    ) -> int:
        spec = RESOURCE_SPECS[resource_key]
        if spec.persistence not in {"entities", "metadata"} or not spec.entity_key:
            raise ValueError(f"resource is not entity-persistable: {resource_key}")
        profile = self._scope(resource_key, profile_id)
        projected: list[tuple[str, dict[str, Any]]] = []
        for raw in list(rows)[:MAX_ENTITIES_PER_RESOURCE]:
            if not isinstance(raw, dict):
                continue
            value = project_entity(resource_key, raw)
            entity_id = value.get(spec.entity_key)
            if entity_id in (None, ""):
                continue
            projected.append((str(entity_id), value))
        now = time.time()
        try:
            with self._lock:
                conn = self._connection()
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT revision,fingerprint FROM resource_snapshots WHERE profile_id=? AND resource_key=?",
                    (profile, resource_key),
                ).fetchone()
                if existing is not None and fingerprint and existing["fingerprint"] == fingerprint:
                    conn.execute(
                        "UPDATE resource_snapshots SET fetched_at=? WHERE profile_id=? AND resource_key=?",
                        (now, profile, resource_key),
                    )
                    conn.execute(
                        "UPDATE source_state SET last_success_at=?,last_error=NULL,health='healthy' "
                        "WHERE profile_id=? AND resource_key=?",
                        (now, profile, resource_key),
                    )
                    conn.commit()
                    return int(existing["revision"])
                revision = self._next_revision(conn, profile, resource_key)
                conn.execute(
                    "DELETE FROM resource_entities WHERE profile_id=? AND resource_key=?",
                    (profile, resource_key),
                )
                conn.executemany(
                    "INSERT INTO resource_entities "
                    "(profile_id,resource_key,entity_id,revision,occurred_at,payload_json) "
                    "VALUES (?,?,?,?,?,?)",
                    [
                        (profile, resource_key, entity_id, revision, now, _json(value))
                        for entity_id, value in projected
                    ],
                )
                summary = {"count": len(projected)}
                conn.execute(
                    "INSERT INTO resource_snapshots "
                    "(profile_id,resource_key,revision,fingerprint,fetched_at,payload_json) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(profile_id,resource_key) DO UPDATE SET "
                    "revision=excluded.revision,fingerprint=excluded.fingerprint,"
                    "fetched_at=excluded.fetched_at,payload_json=excluded.payload_json",
                    (profile, resource_key, revision, fingerprint, now, _json(summary)),
                )
                conn.execute(
                    "INSERT INTO source_state "
                    "(profile_id,resource_key,revision,last_success_at,last_error_at,last_error,schema_fingerprint,health) "
                    "VALUES (?,?,?,?,NULL,NULL,?,'healthy') "
                    "ON CONFLICT(profile_id,resource_key) DO UPDATE SET "
                    "revision=excluded.revision,last_success_at=excluded.last_success_at,"
                    "last_error=NULL,health='healthy',schema_fingerprint=COALESCE(excluded.schema_fingerprint,source_state.schema_fingerprint)",
                    (profile, resource_key, revision, now, schema_fingerprint),
                )
                conn.commit()
                return revision
        except (sqlite3.DatabaseError, OSError, ValueError) as exc:
            self._degrade(exc)
            return 0

    def replace_summary(
        self,
        resource_key: str,
        payload: dict[str, Any],
        *,
        profile_id: str = "default",
        fingerprint: str = "",
    ) -> int:
        spec = RESOURCE_SPECS[resource_key]
        if spec.persistence not in {"snapshot", "metadata"}:
            raise ValueError(f"resource is not snapshot-persistable: {resource_key}")
        profile = self._scope(resource_key, profile_id)
        projected = project_summary(resource_key, payload)
        now = time.time()
        try:
            with self._lock:
                conn = self._connection()
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT revision,fingerprint FROM resource_snapshots WHERE profile_id=? AND resource_key=?",
                    (profile, resource_key),
                ).fetchone()
                if existing is not None and fingerprint and existing["fingerprint"] == fingerprint:
                    conn.execute(
                        "UPDATE resource_snapshots SET fetched_at=? WHERE profile_id=? AND resource_key=?",
                        (now, profile, resource_key),
                    )
                    conn.execute(
                        "UPDATE source_state SET last_success_at=?,last_error=NULL,health='healthy' "
                        "WHERE profile_id=? AND resource_key=?",
                        (now, profile, resource_key),
                    )
                    conn.commit()
                    return int(existing["revision"])
                revision = self._next_revision(conn, profile, resource_key)
                conn.execute(
                    "INSERT INTO resource_snapshots "
                    "(profile_id,resource_key,revision,fingerprint,fetched_at,payload_json) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(profile_id,resource_key) DO UPDATE SET "
                    "revision=excluded.revision,fingerprint=excluded.fingerprint,"
                    "fetched_at=excluded.fetched_at,payload_json=excluded.payload_json",
                    (profile, resource_key, revision, fingerprint, now, _json(projected)),
                )
                conn.execute(
                    "INSERT INTO source_state (profile_id,resource_key,revision,last_success_at,last_error_at,last_error,health) "
                    "VALUES (?,?,?,?,NULL,NULL,'healthy') ON CONFLICT(profile_id,resource_key) DO UPDATE SET "
                    "revision=excluded.revision,last_success_at=excluded.last_success_at,last_error=NULL,health='healthy'",
                    (profile, resource_key, revision, now),
                )
                conn.commit()
                return revision
        except (sqlite3.DatabaseError, OSError, ValueError) as exc:
            self._degrade(exc)
            return 0

    def record_failure(
        self, resource_keys: Iterable[str], error: Exception | str, *, profile_id: str = "default"
    ) -> None:
        message = f"{type(error).__name__}: {error}" if isinstance(error, Exception) else str(error)
        message = message[:500]
        try:
            with self._lock:
                conn = self._connection()
                now = time.time()
                conn.execute("BEGIN IMMEDIATE")
                for resource_key in resource_keys:
                    profile = self._scope(resource_key, profile_id)
                    conn.execute(
                        "INSERT INTO source_state "
                        "(profile_id,resource_key,revision,last_error_at,last_error,health) "
                        "VALUES (?,?,0,?,?,'stale') ON CONFLICT(profile_id,resource_key) DO UPDATE SET "
                        "last_error_at=excluded.last_error_at,last_error=excluded.last_error,health='stale'",
                        (profile, resource_key, now, message),
                    )
                conn.commit()
        except (sqlite3.DatabaseError, OSError) as exc:
            self._degrade(exc)

    def upsert_entity(
        self, resource_key: str, raw: dict[str, Any], *, profile_id: str = "default"
    ) -> tuple[int, dict[str, Any]]:
        """Apply one mutation/native delta and return its resource revision."""
        spec = RESOURCE_SPECS[resource_key]
        if spec.persistence not in {"entities", "metadata"} or not spec.entity_key:
            raise ValueError(f"resource is not entity-persistable: {resource_key}")
        profile = self._scope(resource_key, profile_id)
        value = project_entity(resource_key, raw)
        entity_id = value.get(spec.entity_key)
        if entity_id in (None, ""):
            raise ValueError(f"entity lacks {spec.entity_key}: {resource_key}")
        entity_id = str(entity_id)
        encoded = _json(value)
        now = time.time()
        try:
            with self._lock:
                conn = self._connection()
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT revision,payload_json FROM resource_entities "
                    "WHERE profile_id=? AND resource_key=? AND entity_id=?",
                    (profile, resource_key, entity_id),
                ).fetchone()
                if current is not None and current["payload_json"] == encoded:
                    conn.commit()
                    return int(current["revision"]), value
                revision = self._next_revision(conn, profile, resource_key)
                conn.execute(
                    "INSERT INTO resource_entities "
                    "(profile_id,resource_key,entity_id,revision,occurred_at,payload_json) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(profile_id,resource_key,entity_id) DO UPDATE SET "
                    "revision=excluded.revision,occurred_at=excluded.occurred_at,payload_json=excluded.payload_json",
                    (profile, resource_key, entity_id, revision, now, encoded),
                )
                count = int(conn.execute(
                    "SELECT COUNT(*) FROM resource_entities WHERE profile_id=? AND resource_key=?",
                    (profile, resource_key),
                ).fetchone()[0])
                conn.execute(
                    "INSERT INTO resource_snapshots "
                    "(profile_id,resource_key,revision,fingerprint,fetched_at,payload_json) "
                    "VALUES (?,?,?,'',?,?) ON CONFLICT(profile_id,resource_key) DO UPDATE SET "
                    "revision=excluded.revision,fetched_at=excluded.fetched_at,payload_json=excluded.payload_json",
                    (profile, resource_key, revision, now, _json({"count": count})),
                )
                conn.execute(
                    "INSERT INTO source_state (profile_id,resource_key,revision,last_success_at,last_error,health) "
                    "VALUES (?,?,?,?,NULL,'healthy') ON CONFLICT(profile_id,resource_key) DO UPDATE SET "
                    "revision=excluded.revision,last_success_at=excluded.last_success_at,last_error=NULL,health='healthy'",
                    (profile, resource_key, revision, now),
                )
                conn.commit()
                return revision, value
        except (sqlite3.DatabaseError, OSError) as exc:
            self._degrade(exc)
            return 0, value

    def delete_entity(
        self, resource_key: str, entity_id: str, *, profile_id: str = "default"
    ) -> int:
        spec = RESOURCE_SPECS[resource_key]
        if not spec.entity_key:
            raise ValueError(f"resource has no entity key: {resource_key}")
        profile = self._scope(resource_key, profile_id)
        now = time.time()
        try:
            with self._lock:
                conn = self._connection()
                conn.execute("BEGIN IMMEDIATE")
                exists = conn.execute(
                    "SELECT 1 FROM resource_entities WHERE profile_id=? AND resource_key=? AND entity_id=?",
                    (profile, resource_key, str(entity_id)),
                ).fetchone()
                if exists is None:
                    conn.commit()
                    return self._next_revision(conn, profile, resource_key) - 1
                revision = self._next_revision(conn, profile, resource_key)
                conn.execute(
                    "DELETE FROM resource_entities WHERE profile_id=? AND resource_key=? AND entity_id=?",
                    (profile, resource_key, str(entity_id)),
                )
                count = int(conn.execute(
                    "SELECT COUNT(*) FROM resource_entities WHERE profile_id=? AND resource_key=?",
                    (profile, resource_key),
                ).fetchone()[0])
                conn.execute(
                    "UPDATE resource_snapshots SET revision=?,fetched_at=?,payload_json=? "
                    "WHERE profile_id=? AND resource_key=?",
                    (revision, now, _json({"count": count}), profile, resource_key),
                )
                conn.execute(
                    "UPDATE source_state SET revision=?,last_success_at=?,last_error=NULL,health='healthy' "
                    "WHERE profile_id=? AND resource_key=?",
                    (revision, now, profile, resource_key),
                )
                conn.commit()
                return revision
        except (sqlite3.DatabaseError, OSError) as exc:
            self._degrade(exc)
            return 0

    def revision(self, resource_key: str, *, profile_id: str = "default") -> int:
        profile = self._scope(resource_key, profile_id)
        if not self.available:
            return 0
        with self._lock:
            row = self._connection().execute(
                "SELECT revision FROM source_state WHERE profile_id=? AND resource_key=?",
                (profile, resource_key),
            ).fetchone()
        return int(row[0] if row else 0)

    def _degrade(self, exc: Exception) -> None:
        self.error = f"{type(exc).__name__}: {exc}"[:500]
        try:
            if self._conn is not None:
                self._conn.rollback()
        except sqlite3.DatabaseError:
            pass
        if isinstance(exc, sqlite3.DatabaseError):
            self.available = False

    def resource(
        self, resource_key: str, *, profile_id: str = "default", after_revision: int = 0
    ) -> dict[str, Any]:
        if resource_key not in RESOURCE_SPECS:
            raise KeyError(resource_key)
        profile = self._scope(resource_key, profile_id)
        if not self.available:
            return {"resource_key": resource_key, "revision": 0, "entities": [], "snapshot": None, "provenance": "unavailable", "error": self.error}
        with self._lock:
            conn = self._connection()
            state = conn.execute(
                "SELECT * FROM source_state WHERE profile_id=? AND resource_key=?",
                (profile, resource_key),
            ).fetchone()
            revision = int(state["revision"] if state else 0)
            if after_revision and revision <= after_revision:
                return {"resource_key": resource_key, "revision": revision, "entities": [], "snapshot": None, "provenance": "unchanged"}
            snapshot_row = conn.execute(
                "SELECT payload_json,fetched_at,fingerprint FROM resource_snapshots "
                "WHERE profile_id=? AND resource_key=?",
                (profile, resource_key),
            ).fetchone()
            entity_rows = conn.execute(
                "SELECT entity_id,payload_json,revision FROM resource_entities "
                "WHERE profile_id=? AND resource_key=? ORDER BY entity_id LIMIT ?",
                (profile, resource_key, MAX_ENTITIES_PER_RESOURCE),
            ).fetchall()
        spec = RESOURCE_SPECS[resource_key]
        last_success = float(state["last_success_at"] or 0) if state else 0
        stale = bool(state and state["last_error"]) or (
            bool(last_success and spec.freshness_ttl > 0)
            and time.time() - last_success > spec.freshness_ttl
        )
        return {
            "resource_key": resource_key,
            "revision": revision,
            "entities": [
                {"entity_id": row["entity_id"], "revision": int(row["revision"]), "payload": json.loads(row["payload_json"])}
                for row in entity_rows
            ],
            "snapshot": json.loads(snapshot_row["payload_json"]) if snapshot_row else None,
            "fetched_at": float(snapshot_row["fetched_at"]) if snapshot_row else None,
            "fingerprint": str(snapshot_row["fingerprint"]) if snapshot_row else "",
            "provenance": "stale" if stale else ("live" if last_success else "missing"),
            "last_error": str(state["last_error"]) if state and state["last_error"] else None,
        }

    def bootstrap(self, route: str, *, profile_id: str = "default") -> dict[str, Any]:
        resources = ROUTE_RESOURCES.get(route)
        if resources is None:
            raise KeyError(route)
        return {
            "profile_id": self._profile(profile_id),
            "route": route,
            "resources": {key: self.resource(key, profile_id=profile_id) for key in resources},
        }

    def health(self) -> dict[str, Any]:
        if not self.available:
            return {"status": "unavailable", "schema_version": 0, "error": self.error, "sources": []}
        with self._lock:
            conn = self._connection()
            rows = conn.execute(
                "SELECT profile_id,resource_key,revision,last_success_at,last_error_at,last_error,health "
                "FROM source_state ORDER BY profile_id,resource_key"
            ).fetchall()
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        return {
            "status": "healthy" if all(row["health"] != "stale" for row in rows) else "degraded",
            "schema_version": version,
            "sources": [dict(row) for row in rows],
        }

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self.available = False
