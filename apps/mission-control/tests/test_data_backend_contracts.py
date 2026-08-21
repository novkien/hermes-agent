#!/usr/bin/env python3
"""Deterministic behavioral/security contracts for LocalDataBackend."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mission_control.data_backend import (
    BackendHealth,
    BackendResult,
    DataBackend,
    DataBackendError,
    LocalDataBackend,
    Settings,
    read_call,
)
from agent_mission_control.data_backend import db as backend_db
from agent_mission_control.data_backend.queries import (
    KANBAN_BOARD_TABLES,
    kanban_boards_capabilities,
)


def normalized(value):
    if isinstance(value, BackendResult):
        value = value.to_envelope()
    elif isinstance(value, BackendHealth):
        value = value.to_payload()
    if isinstance(value, dict):
        return {
            key: normalized(child)
            for key, child in sorted(value.items())
            if key not in {"query_ms", "fetched_at", "uptime", "request_id"}
        }
    if isinstance(value, (list, tuple)):
        return [normalized(child) for child in value]
    return value


TASK_SCHEMA = """
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
 status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_by TEXT,
 created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER,
 workspace_kind TEXT DEFAULT 'scratch', workspace_path TEXT, branch_name TEXT,
 claim_lock TEXT, claim_expires INTEGER, tenant TEXT, result TEXT,
 idempotency_key TEXT, consecutive_failures INTEGER DEFAULT 0,
 worker_pid INTEGER, last_failure_error TEXT, max_runtime_seconds INTEGER,
 last_heartbeat_at INTEGER, current_run_id INTEGER, workflow_template_id TEXT,
 current_step_key TEXT, skills TEXT, model_override TEXT, max_retries INTEGER,
 goal_mode INTEGER DEFAULT 0, goal_max_turns INTEGER, session_id TEXT,
 project_id TEXT, block_kind TEXT, block_recurrences INTEGER DEFAULT 0,
 provider_override TEXT, reasoning_effort TEXT
);
CREATE TABLE task_links (parent_id TEXT, child_id TEXT, PRIMARY KEY(parent_id, child_id));
CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INTEGER);
CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
CREATE TABLE task_runs (
 id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, step_key TEXT, status TEXT,
 claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER,
 max_runtime_seconds INTEGER, last_heartbeat_at INTEGER, started_at INTEGER,
 ended_at INTEGER, outcome TEXT, summary TEXT, metadata TEXT, error TEXT
);
CREATE TABLE task_attachments (
 id INTEGER PRIMARY KEY, task_id TEXT, filename TEXT, stored_path TEXT,
 content_type TEXT, size INTEGER, uploaded_by TEXT, created_at INTEGER
);
CREATE TABLE kanban_notify_subs (
 task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT,
 PRIMARY KEY(task_id, platform, chat_id, thread_id)
);
"""

PERMITS_SCHEMA = """
CREATE TABLE permits (
 permit_id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, status TEXT,
 approved TEXT DEFAULT '', executed TEXT DEFAULT 'no', severity TEXT DEFAULT '',
 fingerprint TEXT UNIQUE, approval_note TEXT DEFAULT '', action_plan TEXT DEFAULT '',
 execution_result TEXT DEFAULT ''
);
"""

ISSUES_SCHEMA = """
CREATE TABLE issues (
 id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE, issue TEXT, context TEXT,
 reproduction TEXT, initial_assessment TEXT, severity TEXT, impact TEXT,
 expected_behavior TEXT, status TEXT, resolution TEXT, verification TEXT,
 merged_into_id INTEGER, first_seen_at TEXT, last_seen_at TEXT, resolved_at TEXT,
 created_at TEXT, updated_at TEXT, occurrence_count INTEGER, created_by TEXT,
 last_updated_by TEXT, deleted_at TEXT, deleted_reason TEXT
);
CREATE TABLE issue_occurrences (
 id INTEGER PRIMARY KEY, issue_id INTEGER, event_type TEXT, context TEXT,
 reproduction TEXT, initial_assessment TEXT, resolution TEXT, verification TEXT,
 evidence_ref TEXT, occurred_at TEXT, reporter TEXT, session_ref TEXT,
 task_ref TEXT, tool_ref TEXT
);
"""

SESSION_COLUMNS = (
    "id TEXT PRIMARY KEY", "source TEXT", "user_id TEXT", "session_key TEXT",
    "chat_id TEXT", "chat_type TEXT", "thread_id TEXT", "display_name TEXT",
    "expiry_finalized INTEGER", "model TEXT", "model_config TEXT",
    "system_prompt TEXT", "system_prompt_hash TEXT", "parent_session_id TEXT",
    "started_at REAL", "ended_at REAL", "end_reason TEXT", "message_count INTEGER",
    "tool_call_count INTEGER", "input_tokens INTEGER", "output_tokens INTEGER",
    "cache_read_tokens INTEGER", "cache_write_tokens INTEGER",
    "reasoning_tokens INTEGER", "cwd TEXT", "git_branch TEXT", "git_repo_root TEXT",
    "billing_provider TEXT", "billing_base_url TEXT", "billing_mode TEXT",
    "estimated_cost_usd REAL", "actual_cost_usd REAL", "cost_status TEXT",
    "cost_source TEXT", "pricing_version TEXT", "title TEXT", "title_source TEXT",
    "last_activity_at REAL", "last_activity_description TEXT",
    "last_activity_provenance TEXT", "api_call_count INTEGER", "handoff_state TEXT",
    "handoff_platform TEXT", "handoff_error TEXT", "profile_name TEXT",
    "archived INTEGER", "pinned INTEGER", "last_read_at REAL"
)


def create_database(path: Path, schema: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(schema)
    return connection


def create_kanban(path: Path, task_id: str, session_id: str, created_at: int) -> None:
    connection = create_database(path, TASK_SCHEMA)
    connection.execute(
        "INSERT INTO tasks (id,title,body,assignee,status,priority,created_by,created_at,"
        "workspace_path,max_runtime_seconds,skills,session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id, "Fixture task", "private task body", "worker", "running", 3,
            "tester", created_at, "/bounded/workspace", 60, "[]", session_id,
        ),
    )
    connection.execute(
        "INSERT INTO task_comments VALUES (1,?,?,?,?)",
        (task_id, "tester", "comment", created_at),
    )
    connection.execute(
        "INSERT INTO task_events VALUES (1,?,NULL,'created',?,?)",
        (task_id, json.dumps({"status": "running"}), created_at),
    )
    connection.execute(
        "INSERT INTO task_runs (id,task_id,profile,status,started_at,metadata) "
        "VALUES (1,?,?,?,?,'private metadata')",
        (task_id, "worker", "running", created_at),
    )
    connection.execute(
        "INSERT INTO task_attachments VALUES (1,?,'report.txt','/secret/storage/report.txt',"
        "'text/plain',12,'tester',?)",
        (task_id, created_at),
    )
    connection.commit()
    connection.close()


def state_schema(include_fts: bool = True) -> str:
    schema = [f"CREATE TABLE sessions ({', '.join(SESSION_COLUMNS)});"]
    schema.append(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
        "content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, "
        "timestamp REAL, token_count INTEGER, finish_reason TEXT, api_content TEXT);"
    )
    if include_fts:
        schema.append(
            "CREATE VIRTUAL TABLE messages_fts USING fts5(content, tool_name, tool_calls, "
            "content='messages', content_rowid='id');"
        )
    schema.append(
        "CREATE TABLE session_model_usage (session_id TEXT, model TEXT, "
        "billing_provider TEXT, billing_base_url TEXT, billing_mode TEXT, task TEXT, "
        "api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
        "cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER, "
        "estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT, "
        "cost_source TEXT, first_seen REAL, last_seen REAL);"
    )
    schema.append(
        "CREATE TABLE llm_provider_requests (id INTEGER, session_id TEXT, "
        "api_request_id TEXT, turn_id TEXT, api_call_count INTEGER, attempt INTEGER, "
        "captured_at REAL, provider TEXT, model TEXT, api_mode TEXT, transport TEXT, "
        "base_request_id TEXT, payload_json TEXT);"
    )
    return "\n".join(schema)


def insert_session(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    parent: str | None = None,
    ended_at: float | None = None,
    end_reason: str | None = None,
    started_at: float = 100.0,
    profile: str = "default",
) -> None:
    connection.execute(
        "INSERT INTO sessions (id,source,user_id,session_key,chat_id,chat_type,thread_id,"
        "display_name,expiry_finalized,model,model_config,system_prompt,system_prompt_hash,"
        "parent_session_id,started_at,ended_at,end_reason,message_count,tool_call_count,"
        "input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,"
        "billing_provider,billing_base_url,billing_mode,estimated_cost_usd,actual_cost_usd,"
        "title,last_activity_at,last_activity_description,last_activity_provenance,"
        "api_call_count,profile_name,archived,pinned) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            session_id, "telegram", "user-1", f"key-{session_id}", "chat-1", "group",
            "10", "Fixture", 0, "model", "{}", "never expose prompt", "hash", parent,
            started_at, ended_at, end_reason, 1, 0, 1, 1, 0, 0, 0, "provider", "http://local",
            "test", 0.1, 0.0, "Fixture session", started_at, "active", "fixture", 1,
            profile, 0, 0,
        ),
    )


def create_state(path: Path, *, worker_anchor: bool = False) -> None:
    connection = create_database(path, state_schema(include_fts=not worker_anchor))
    if worker_anchor:
        insert_session(connection, "worker-session", started_at=300.0, profile="worker")
        connection.execute(
            "INSERT INTO messages VALUES (1,'worker-session','user',?,NULL,NULL,NULL,300,4,NULL,NULL)",
            ("work kanban task task-1",),
        )
    else:
        insert_session(
            connection,
            "root-session",
            ended_at=150.0,
            end_reason="session_reset",
            started_at=100.0,
        )
        insert_session(connection, "tip-session", parent="root-session", started_at=200.0)
        connection.execute(
            "INSERT INTO messages VALUES (1,'root-session','user','searchable phrase',"
            "NULL,NULL,NULL,110,3,NULL,'secret api content')"
        )
        connection.execute(
            "INSERT INTO messages VALUES (2,'tip-session','assistant','private transcript',"
            "NULL,NULL,NULL,210,4,'stop',NULL)"
        )
        connection.execute(
            "INSERT INTO session_model_usage VALUES "
            "('tip-session','model','provider','http://local','test','chat',1,10,2,0,0,0,0.1,0,NULL,NULL,200,210)"
        )
        connection.execute(
            "INSERT INTO llm_provider_requests VALUES "
            "(1,'tip-session','req','turn',1,1,205,'provider','model','chat','http',NULL,'secret payload')"
        )
        connection.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    connection.commit()
    connection.close()


def create_permits(path: Path) -> None:
    connection = create_database(path, PERMITS_SCHEMA)
    connection.execute(
        "INSERT INTO permits VALUES "
        "('permit-1','2026-01-01','2026-01-01','pending_approval','','no','high','fp','','','')"
    )
    connection.commit()
    connection.close()


def create_issues(path: Path) -> None:
    connection = create_database(path, ISSUES_SCHEMA)
    connection.execute(
        "INSERT INTO issues VALUES (1,'fp','issue','context','steps',NULL,'high','impact',"
        "'expected','open',NULL,NULL,NULL,'2026','2026',NULL,'2026','2026',1,'tester','tester',NULL,NULL)"
    )
    connection.execute(
        "INSERT INTO issue_occurrences VALUES "
        "(1,1,'observed','context',NULL,NULL,NULL,NULL,NULL,'2026','tester',NULL,NULL,NULL)"
    )
    connection.commit()
    connection.close()


def create_occupancy(path: Path) -> None:
    connection = create_database(
        path,
        """
        CREATE TABLE room_task_bindings (
          chat_id TEXT, task_id TEXT, room_slot INTEGER, origin_session_key TEXT,
          origin_chat_id TEXT, origin_thread_id TEXT, status TEXT,
          terminal_request_id TEXT, bound_at REAL, updated_at REAL
        );
        CREATE TABLE room_reservations (
          chat_id TEXT, task_id TEXT, requester_session_key TEXT, room_slot INTEGER,
          ceo_thread_id TEXT, created_at REAL, expires_at REAL
        );
        CREATE TABLE a2a_outbox (
          request_id TEXT, task_id TEXT, room_slot INTEGER, chat_id TEXT, created_at REAL
        );
        CREATE TABLE completed_task_ids (chat_id TEXT, task_id TEXT, completed_at REAL);
        """,
    )
    connection.execute(
        "INSERT INTO room_task_bindings VALUES "
        "('chat-1','task-1',1,'origin','chat-1','10','ACTIVE',NULL,1,2)"
    )
    connection.execute(
        "INSERT INTO room_reservations VALUES ('chat-1','task-2','requester',2,'10',1,2)"
    )
    connection.execute("INSERT INTO a2a_outbox VALUES ('r','task-1',1,'chat-1',1)")
    connection.commit()
    connection.close()


def create_fake_scripts(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "permits_db.py").write_text(
        "import json,sys\n"
        "pid=sys.argv[sys.argv.index('--permit-id')+1]\n"
        "print(json.dumps({'success': pid == 'permit-1'}))\n",
        encoding="utf-8",
    )
    (path / "agent_notes_db.py").write_text(
        "import json\nprint(json.dumps({'success': True}))\n",
        encoding="utf-8",
    )


def build_backend(root: Path) -> LocalDataBackend:
    home = root / "hermes"
    create_kanban(home / "kanban/boards/task/kanban.db", "task-1", "root-session", 100)
    create_kanban(home / "kanban/boards/ops/kanban.db", "task-2", "tip-session", 200)
    create_permits(home / "workspace/state/permits.db")
    create_issues(home / "workspace/state/agent-notes/issues.db")
    create_state(home / "state.db")
    create_state(home / "profiles/worker/state.db", worker_anchor=True)
    create_occupancy(home / "workspace/state/session-injector/room_bindings.sqlite3")
    (home / "memories").mkdir(parents=True)
    (home / "memories/MEMORY.md").write_text("fixture memory", encoding="utf-8")
    (home / "config.yaml").write_text(
        "telegram:\n"
        "  bot_token: never-return-this\n"
        "  extra:\n"
        "    room_chat_id: chat-1\n"
        "    room_slots:\n"
        "      - slot: 1\n"
        "        ceo_thread_id: '10'\n"
        "        api_key: never-return-this\n"
        "    group_topics:\n"
        "      - chat_id: chat-1\n"
        "        topics:\n"
        "          - name: CEO\n"
        "            thread_id: '10'\n"
        "            token: never-return-this\n",
        encoding="utf-8",
    )
    create_fake_scripts(home / "scripts")
    return LocalDataBackend(Settings(home))


async def assert_error(status: int, awaitable) -> DataBackendError:
    try:
        await awaitable
    except DataBackendError as exc:
        assert exc.status == status, (exc.status, exc.detail)
        return exc
    raise AssertionError(f"expected DataBackendError({status})")


async def exercise_backend(root: Path) -> None:
    initial_deadlines = len(backend_db._ADAPTER_STATE)
    backend = build_backend(root)
    assert isinstance(backend, DataBackend)
    database_paths = [Path(spec.path) for spec in backend.settings.sources.values()]
    sizes_before = {path: path.stat().st_size for path in database_paths}

    health = await backend.health()
    assert health.status == "ok"
    assert {row["source_id"] for row in health.sources} == {
        "kanban", "permits", "issues", "state"
    }
    capabilities = await backend.capabilities()
    assert set(capabilities.data) == {"kanban", "permits", "issues", "state"}
    assert {row["board"] for row in capabilities.data["kanban"]["boards"]} == {
        "task", "ops"
    }

    boards = await backend.kanban_boards()
    assert {row["board"] for row in boards.data} == {"task", "ops"}
    tasks = await backend.kanban_tasks(board="all", limit=10)
    assert {row["id"] for row in tasks.data} == {"task-1", "task-2"}
    assert all("body" not in row for row in tasks.data)
    await assert_error(422, backend.kanban_tasks(sort="body"))
    await assert_error(404, backend.kanban_tasks(board="../../etc"))
    detail = await backend.kanban_task("task-1")
    assert detail.data["body"] == "private task body"
    assert "stored_path" not in detail.data["attachments"][0]
    assert (await backend.kanban_task_events("task-1")).data[0]["kind"] == "created"
    assert (await backend.kanban_task_runs("task-1")).data[0]["status"] == "running"
    assert (await backend.kanban_task_attachments("task-1")).data[0]["filename"] == "report.txt"
    worker = await backend.kanban_worker_session("task-1")
    assert worker.data["session_id"] == "worker-session"
    assert worker.data["profile"] == "worker"
    summary = await backend.kanban_summary(board="all")
    assert summary.data["totals"]["total_tasks"] == 2

    permits = await backend.permits(status="pending_approval")
    assert [row["permit_id"] for row in permits.data] == ["permit-1"]
    assert (await backend.permit("permit-1")).data["severity"] == "high"
    decision = await backend.decide_permit("permit-1", {"approved": True})
    assert decision.meta["mutations_supported"] == ["decide"]
    await assert_error(400, backend.decide_permit("permit-1", {"arbitrary": "x"}))
    await assert_error(404, backend.decide_permit("../../outside", {"approved": True}))

    issues = await backend.issues(status="open")
    assert [row["id"] for row in issues.data] == [1]
    assert len((await backend.issue(1)).data["occurrences"]) == 1
    update = await backend.update_issue("1", {"status": "resolved"})
    assert update.data["issue_id"] == 1
    await assert_error(400, backend.update_issue("1", {"arbitrary": "x"}))
    await assert_error(400, backend.update_issue("../../outside", {"status": "resolved"}))

    search = await backend.search_sessions("searchable", limit=5)
    assert search.data[0]["session_id"] == "root-session"
    assert "content" not in search.data[0]
    rooms = await backend.room_sessions("chat-1", history=True)
    assert rooms.data["sessions"][0]["id"] == "tip-session"
    assert len(rooms.data["thread_sessions"]) == 2
    tips = await backend.session_tips(["root-session"])
    assert tips.data["tips"]["root-session"]["tip_id"] == "tip-session"
    cards = await backend.room_cards("chat-1", per_thread=5)
    assert cards.data["counts"]["10"] == 2
    threads = await backend.thread_sessions("chat-1", ["10"])
    assert threads.data["sessions_by_thread"]["10"][0]["id"] == "tip-session"
    timeline = await backend.session_timeline("tip-session")
    assert "system_prompt" not in timeline.data["session"]
    assert "content" not in timeline.data["messages"][0]
    assert "api_content" not in timeline.data["messages"][0]
    assert "payload_json" not in (timeline.data["provider_requests"][0] or {})

    for source_id in ("kanban", "permits", "issues", "state"):
        fingerprint = await backend.source_fingerprint(source_id)
        if source_id == "state":
            assert fingerprint.data["schema_fingerprint"] is None
        else:
            assert len(fingerprint.data["schema_fingerprint"]) == 64
    await assert_error(404, backend.source_fingerprint("arbitrary"))

    binding = await backend.room_binding()
    serialized_binding = json.dumps(binding.data).lower()
    assert binding.data["room_chat_id"] == "chat-1"
    assert binding.data["occupancy_available"] is True
    assert "api_key" not in serialized_binding
    assert "token" not in serialized_binding
    assert "never-return-this" not in serialized_binding

    shadow = LocalDataBackend(Settings(root / "hermes"))
    parity_cases = (
        ("health", (), {}),
        ("capabilities", (), {}),
        ("memory_file", ("memory",), {}),
        ("kanban_boards", (), {}),
        ("kanban_tasks", (), {"board": "all", "limit": 10}),
        ("kanban_task", ("task-1",), {}),
        ("kanban_task_events", ("task-1",), {"limit": 1}),
        ("kanban_task_runs", ("task-1",), {"limit": 1}),
        ("kanban_task_attachments", ("task-1",), {"limit": 1}),
        ("kanban_worker_session", ("task-1",), {}),
        ("kanban_summary", (), {"board": "all"}),
        ("permits", (), {"limit": 10}),
        ("permit", ("permit-1",), {}),
        ("issues", (), {"limit": 10}),
        ("issue", (1,), {"occurrence_limit": 1}),
        ("search_sessions", ("searchable",), {"limit": 5}),
        ("room_sessions", ("chat-1",), {"limit": 5, "history": True}),
        ("session_tips", (["root-session"],), {}),
        ("room_cards", ("chat-1",), {"per_thread": 5}),
        ("thread_sessions", ("chat-1", ["10"]), {}),
        ("session_timeline", ("tip-session",), {"limit": 5}),
        ("source_fingerprint", ("permits",), {}),
        ("room_binding", (), {}),
    )
    for method, args, kwargs in parity_cases:
        primary = await getattr(backend, method)(*args, **kwargs)
        secondary = await getattr(shadow, method)(*args, **kwargs)
        assert normalized(primary) == normalized(secondary), method

    # The browser compatibility path is an explicit typed dispatch table,
    # never an arbitrary HTTP proxy.
    result = await read_call(
        backend, "/kanban/tasks", {"board": "all", "page": "1", "limit": "2"}
    )
    assert isinstance(result, BackendResult) and len(result.data) == 2
    result = await read_call(backend, "/permits", {"page": "1", "limit": "2"})
    assert isinstance(result, BackendResult) and len(result.data) <= 2
    result = await read_call(backend, "/issues", {"page": "1", "limit": "2"})
    assert isinstance(result, BackendResult) and len(result.data) <= 2
    try:
        await read_call(backend, "/kanban/tasks", {"limit": "not-an-integer"})
    except DataBackendError as exc:
        assert exc.status == 422
    else:
        raise AssertionError("typed dispatch accepted an invalid integer")
    try:
        await read_call(backend, "/not-allowlisted")
    except DataBackendError as exc:
        assert exc.status == 404
    else:
        raise AssertionError("typed dispatch accepted an unknown path")
    await shadow.aclose()

    memory = await backend.memory_file("memory")
    assert memory.data["content"] == "fixture memory"
    saved = await backend.save_memory_file("memory.md", "updated fixture")
    assert saved.data["size"] == len("updated fixture")
    await assert_error(404, backend.memory_file("../../outside"))
    await assert_error(
        413,
        backend.save_memory_file("memory", "x" * 1_000_001),
    )
    assert {path.name for path in backend.settings.memory_dir.iterdir()} == {"MEMORY.md"}
    assert backend.settings.scripts_dir == root / "hermes" / "scripts"
    outside_script = root / "outside_issue_script.py"
    outside_script.write_text("raise SystemExit('must not run')\n", encoding="utf-8")
    issue_script = backend.settings.scripts_dir / "agent_notes_db.py"
    issue_script.unlink()
    issue_script.symlink_to(outside_script)
    await assert_error(503, backend.update_issue("1", {"status": "resolved"}))

    assert {path: path.stat().st_size for path in database_paths} == sizes_before
    deadline_count = len(backend_db._ADAPTER_STATE)
    for _ in range(100):
        await backend.source_fingerprint("permits")
    assert len(backend_db._ADAPTER_STATE) == deadline_count
    await backend.aclose()
    assert len(backend_db._ADAPTER_STATE) == initial_deadlines


def test_read_only_statement_guard() -> None:
    for statement in (
        "INSERT INTO x VALUES (1)",
        "UPDATE x SET y=1",
        "DELETE FROM x",
        "DROP TABLE x",
        "VACUUM",
        "PRAGMA journal_mode=wal",
        "WITH x AS (SELECT 1) DELETE FROM y",
    ):
        try:
            backend_db.assert_read_only(statement)
        except sqlite3.OperationalError:
            continue
        raise AssertionError(f"write statement was accepted: {statement}")


def test_in_process_package_has_no_http_auth_or_home_coupling() -> None:
    package = ROOT / "agent_mission_control" / "data_backend"
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in package.glob("*.py")
    )
    for forbidden in (
        "Path.home(",
        "ADAPTER_TOKEN",
        "ADAPTER_URL",
        "Authorization: Bearer",
        "FastAPI(",
    ):
        assert forbidden not in combined, forbidden


def test_capabilities_degrade_one_unavailable_board() -> None:
    class UnavailableStore:
        def fingerprint(self, *, recompute: bool = False):
            raise sqlite3.OperationalError("unable to open database file")

        def row_count(self, table: str):
            raise AssertionError(f"row_count must not run for unreachable board: {table}")

    class Registry:
        def board_names(self):
            return ["transient"]

        def _store(self, name: str):
            assert name == "transient"
            return UnavailableStore()

    result = kanban_boards_capabilities(Registry())
    assert result == {
        "boards": [
            {
                "board": "transient",
                "schema_fingerprint": None,
                "schema_drift": False,
                "reachable": False,
                "row_counts": {table: None for table in KANBAN_BOARD_TABLES},
            }
        ]
    }


def main() -> None:
    test_read_only_statement_guard()
    test_in_process_package_has_no_http_auth_or_home_coupling()
    test_capabilities_degrade_one_unavailable_board()
    with tempfile.TemporaryDirectory(prefix="mission-control-data-backend-") as temp:
        asyncio.run(exercise_backend(Path(temp)))
    print("DATA_BACKEND_CONTRACT_TESTS=PASS")


if __name__ == "__main__":
    main()
