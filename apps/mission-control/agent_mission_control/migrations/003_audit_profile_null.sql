-- agentos-dashboard.db — v3 migration: reconcile action_audit to nullable
-- profile_id (S8-FIX t_7fcdab02).
--
-- Background: migrations/001_initial.sql declares `profile_id TEXT` (nullable)
-- as the canonical contract, but some runtime agentos-dashboard.db files were
-- created by an earlier store DDL with `profile_id TEXT NOT NULL DEFAULT ''`
-- (plus stricter target/request_summary/result and an INTEGER timestamp). The
-- v1 migration's `CREATE TABLE IF NOT EXISTS` never reconciles an existing
-- table, so the runtime diverged and chat session-create 500'd:
--   sqlite3.IntegrityError: NOT NULL constraint failed: action_audit.profile_id
--
-- SQLite cannot drop NOT NULL via ALTER TABLE. Standard table-rebuild pattern:
-- create the canonical table under a temp name, copy rows (type affinity
-- converts INTEGER timestamp -> TEXT), drop the old table, rename.
--
-- Applied by store.migrate() via the user_version pragma after 002_stage5.sql.

PRAGMA journal_mode=WAL;

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
