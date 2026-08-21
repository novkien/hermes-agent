-- Keep aligned with store.py _SCHEMA_V4.
ALTER TABLE event_replay ADD COLUMN profile_id TEXT NOT NULL DEFAULT '';
ALTER TABLE event_replay ADD COLUMN resource_key TEXT NOT NULL DEFAULT '';
ALTER TABLE event_replay ADD COLUMN operation TEXT NOT NULL DEFAULT 'invalidate';
ALTER TABLE event_replay ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_event_replay_profile_id ON event_replay(profile_id, id);
