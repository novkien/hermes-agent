-- agentos-dashboard.db — v1 control-store schema (SQLite)
-- Mirrors agent_mission_control/store.py _SCHEMA_V1. Applied by store.migrate()
-- via the user_version pragma; this file is the standalone migration artifact
-- for deployment/review. EXACTLY the 8 permitted tables (architecture-freeze §9).

PRAGMA journal_mode=WAL;

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
