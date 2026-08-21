-- Separate live read-model schema. Keep aligned with read_model.py SCHEMA_V1.
CREATE TABLE resource_snapshots (
    profile_id TEXT NOT NULL, resource_key TEXT NOT NULL, revision INTEGER NOT NULL,
    fingerprint TEXT NOT NULL DEFAULT '', fetched_at REAL NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY (profile_id, resource_key)
);
CREATE TABLE resource_entities (
    profile_id TEXT NOT NULL, resource_key TEXT NOT NULL, entity_id TEXT NOT NULL,
    revision INTEGER NOT NULL, occurred_at REAL NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY (profile_id, resource_key, entity_id)
);
CREATE INDEX idx_resource_entities_revision
    ON resource_entities (profile_id, resource_key, revision, entity_id);
CREATE TABLE source_state (
    profile_id TEXT NOT NULL, resource_key TEXT NOT NULL, cursor TEXT,
    revision INTEGER NOT NULL DEFAULT 0, last_success_at REAL, last_error_at REAL,
    last_error TEXT, schema_fingerprint TEXT, health TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY (profile_id, resource_key)
);
PRAGMA user_version=1;
