-- store.db — v2 schema: session execution mode.
--
-- Adds execution_mode to session_persona: 'gateway' (default, legacy shared
-- multiplexed gateway — unchanged behavior) or 'runner' (its own
-- profile-scoped `hermes serve --isolated` process spawned by
-- runner_manager.py). The BFF is the sole source of this fact — it is the
-- one deciding which path a given session runs on — so it belongs in the
-- same storage-minimalism pointer table as profile_name, not a new table.
--
-- Mirrors agent_mission_control/session_persona_store.py _SCHEMA_V2. Applied
-- by SessionPersonaStore.migrate() via the user_version pragma; this file is
-- the standalone migration artifact for deployment/review.

ALTER TABLE session_persona ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'gateway';
