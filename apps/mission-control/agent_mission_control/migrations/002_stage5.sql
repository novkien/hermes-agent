-- agentos-dashboard.db — v2 control-store migration (Stage 5 event replay)
-- Mirrors agent_mission_control/store.py _SCHEMA_V2. Applied by store.migrate()
-- via the user_version pragma after 001_initial.sql; this file is the
-- standalone migration artifact for deployment/review.
--
-- v2 (Stage 5): DB-backed event replay buffer (bounded at read time).
-- Added by the S8 merge: S5 sql/migrations.sql v2 event_replay folded in.

PRAGMA journal_mode=WAL;

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
