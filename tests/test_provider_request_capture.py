from __future__ import annotations

import json
import threading

from hermes_state import (
    SessionDB,
    _apply_provider_payload_delta,
    _provider_payload_delta,
    _provider_payload_json,
)
from run_agent import AIAgent


def test_provider_payload_delta_is_exact_and_order_preserving():
    previous = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [{"name": "read_file"}],
    }
    current = {
        "model": "test-model",
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "done"},
        ],
        "tools": [{"name": "read_file"}],
        "reasoning": {"effort": "high"},
    }

    delta = _provider_payload_delta(previous, current)
    rebuilt = _apply_provider_payload_delta(previous, delta)

    assert rebuilt == current
    assert _provider_payload_json(rebuilt) == _provider_payload_json(current)


def test_session_provider_requests_round_trip_and_delta_encode(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(
        session_id="provider-log",
        source="telegram",
        session_key="agent:main:telegram:group:-100:42",
    )
    first = {
        "model": "test-model",
        "messages": [{"role": "system", "content": "x" * 10_000}],
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
    }
    second = {
        **first,
        "messages": [
            *first["messages"],
            {"role": "user", "content": "read it"},
        ],
    }

    try:
        first_id = db.append_llm_provider_request(
            "provider-log",
            payload=first,
            transport="openai.chat.completions.create",
            api_request_id="turn-1:api:1",
            attempt=1,
        )
        second_id = db.append_llm_provider_request(
            "provider-log",
            payload=second,
            transport="openai.chat.completions.create",
            api_request_id="turn-1:api:2",
            attempt=1,
        )

        stored = db._conn.execute(
            "SELECT id, payload_encoding, base_request_id, payload_json "
            "FROM llm_provider_requests ORDER BY id"
        ).fetchall()
        records = db.get_llm_provider_requests("provider-log")

        assert [row["id"] for row in stored] == [first_id, second_id]
        assert stored[0]["payload_encoding"] == "full"
        assert stored[1]["payload_encoding"] == "delta-v1"
        assert stored[1]["base_request_id"] == first_id
        assert len(stored[1]["payload_json"]) < len(stored[0]["payload_json"])
        assert [record["payload"] for record in records] == [first, second]
        assert {record["payload_integrity"] for record in records} == {"verified"}

        assert db.delete_session("provider-log") is True
        remaining = db._conn.execute(
            "SELECT COUNT(*) FROM llm_provider_requests"
        ).fetchone()[0]
        assert remaining == 0
    finally:
        db.close()


def test_agent_capture_flattens_sdk_extra_body_and_drops_client_controls(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("capture", "cli")
    agent = object.__new__(AIAgent)
    agent._session_db = db
    agent._session_db_created = True
    agent._provider_request_log_lock = threading.Lock()
    agent._provider_request_log_context = None
    agent._persist_disabled = False
    agent.session_id = "capture"
    agent._gateway_session_key = None
    agent.provider = "custom"
    agent.model = "test-model"
    agent.api_mode = "chat_completions"

    context = agent._begin_provider_request_capture(
        task_id="task-1",
        turn_id="turn-1",
        api_request_id="turn-1:api:1",
        api_call_count=1,
    )
    try:
        row_id = agent._record_provider_request_payload(
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": "hello"}],
                "extra_body": {"reasoning": {"effort": "high"}},
                "extra_headers": {"Authorization": "Bearer secret"},
                "timeout": object(),
            },
            transport="openai.chat.completions.create",
        )
        record = db.get_llm_provider_requests("capture")[0]
    finally:
        agent._end_provider_request_capture(context)
        db.close()

    assert row_id == record["id"]
    assert record["attempt"] == 1
    assert record["payload"] == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning": {"effort": "high"},
    }
    assert json.loads(record["payload_json_raw"]) == record["payload"]
