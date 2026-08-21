-- store.db — v1 schema (dashboard-owned data Hermes cannot hold).
--
-- Separate database file from agentos-dashboard.db: that one is the control
-- store frozen at its 8 permitted tables (architecture-freeze §9). This file
-- exists for dashboard state upstream has nowhere to keep.
--
-- Storage minimalism is enforced here: only a pointer, never a copy. A chat
-- session created from another profile's persona gets that profile's SOUL.md
-- forwarded to the gateway as `system_prompt`, and the gateway session row
-- keeps the resolved text but not the profile name it came from. The name is
-- therefore the one fact unrecoverable from upstream, and the only fact stored
-- below — profile details, model/provider and soul text are always fetched
-- live from the Hermes dashboard instead.
--
-- Mirrors agent_mission_control/session_persona_store.py _SCHEMA_V1. Applied by
-- SessionPersonaStore.migrate() via the user_version pragma; this file is the
-- standalone migration artifact for deployment/review.

PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS session_persona (
    session_id   TEXT PRIMARY KEY,
    profile_name TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
