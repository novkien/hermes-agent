from types import SimpleNamespace

from agent import session_event_relay as relay_module


def test_attach_session_event_relay_wraps_and_restores_agent_callbacks(monkeypatch):
    original_events = []
    agent = SimpleNamespace(
        stream_delta_callback=lambda text, *a, **k: original_events.append(("delta", text)),
        reasoning_callback=lambda text, *a, **k: original_events.append(("reasoning", text)),
        tool_progress_callback=lambda event, **kwargs: original_events.append((event, kwargs)),
    )

    monkeypatch.setattr(
        relay_module, "gateway_ingest_target", lambda: ("http://127.0.0.1:8642", "key")
    )
    monkeypatch.setattr(relay_module.SessionEventRelay, "start", lambda self: self)

    relay = relay_module.attach_session_event_relay(agent, "session-1", "cli")
    assert relay is not None
    agent.stream_delta_callback("answer")
    agent.reasoning_callback("thought")
    agent.tool_progress_callback(
        "tool.completed", tool_name="read_file", result="ok", tool_call_id="call-1"
    )

    queued = [relay._queue.get_nowait() for _ in range(3)]
    assert [item["event"] for item in queued] == [
        "assistant.delta",
        "reasoning.delta",
        "tool.completed",
    ]
    assert original_events[0] == ("delta", "answer")
    assert original_events[1] == ("reasoning", "thought")

    relay.restore()
    assert agent.stream_delta_callback("next") is None
    assert original_events[-1] == ("delta", "next")


def test_attach_session_event_relay_skips_api_server_platform(monkeypatch):
    monkeypatch.setattr(
        relay_module, "gateway_ingest_target", lambda: ("http://127.0.0.1:8642", "key")
    )
    assert (
        relay_module.attach_session_event_relay(SimpleNamespace(), "session-1", "api_server")
        is None
    )


def test_gateway_ingest_target_prefers_pinned_observation_env(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_EVENT_RELAY_URL", "http://127.0.0.1:8642/")
    monkeypatch.setenv("HERMES_SESSION_EVENT_RELAY_KEY", "dispatcher-key")
    # Simulate the assignee profile replacing its own API listener settings.
    monkeypatch.setenv("API_SERVER_PORT", "49152")
    monkeypatch.setenv("API_SERVER_KEY", "profile-key")

    assert relay_module.gateway_ingest_target() == (
        "http://127.0.0.1:8642",
        "dispatcher-key",
    )


def test_gateway_ingest_target_keeps_legacy_api_server_fallback(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_EVENT_RELAY_URL", raising=False)
    monkeypatch.delenv("HERMES_SESSION_EVENT_RELAY_KEY", raising=False)
    monkeypatch.setenv("API_SERVER_PORT", "8765")
    monkeypatch.setenv("API_SERVER_KEY", "legacy-key")

    assert relay_module.gateway_ingest_target() == (
        "http://127.0.0.1:8765",
        "legacy-key",
    )


class _RecordingTurnRelay:
    message_id = "message-1"

    def __init__(self):
        self.events = []
        self.restored = False
        self.closed = False

    def emit(self, name, payload):
        self.events.append((name, payload))

    def restore(self):
        self.restored = True

    def close(self):
        self.closed = True


def _finish_event_names(relay):
    return [name for name, _payload in relay.events]


def test_finish_turn_relay_preserves_success_sequence():
    relay = _RecordingTurnRelay()

    relay_module.finish_turn_relay(
        relay,
        {
            "final_response": "done",
            "usage": {"total_tokens": 7},
            "completed": True,
            "failed": False,
            "interrupted": False,
        },
    )

    assert _finish_event_names(relay) == [
        "assistant.completed",
        "run.completed",
        "done",
    ]
    assert relay.events[0][1] == {
        "message_id": "message-1",
        "content": "done",
        "completed": True,
        "partial": False,
        "interrupted": False,
    }
    assert relay.events[1][1] == {
        "message_id": "message-1",
        "completed": True,
        "messages": [],
        "usage": {"total_tokens": 7},
    }
    assert relay.restored is True
    assert relay.closed is True


def test_finish_turn_relay_emits_error_for_explicit_failure():
    relay = _RecordingTurnRelay()

    relay_module.finish_turn_relay(
        relay,
        {
            "final_response": "partial answer",
            "error": "provider failed",
            "completed": False,
            "failed": True,
            "interrupted": False,
        },
    )

    assert _finish_event_names(relay) == ["error", "done"]
    assert relay.events[0][1] == {
        "message_id": "message-1",
        "error": "provider failed",
    }


def test_finish_turn_relay_emits_error_when_result_is_missing():
    relay = _RecordingTurnRelay()

    relay_module.finish_turn_relay(relay, None)

    assert _finish_event_names(relay) == ["error", "done"]
    assert relay.events[0][1]["error"] == (
        "The relayed turn failed before producing a final response."
    )


def test_finish_turn_relay_preserves_interrupted_stop_semantics():
    relay = _RecordingTurnRelay()

    relay_module.finish_turn_relay(
        relay,
        {
            "final_response": "partial answer",
            "completed": False,
            "failed": False,
            "partial": False,
            "interrupted": True,
        },
    )

    assert _finish_event_names(relay) == [
        "assistant.completed",
        "run.completed",
        "done",
    ]
    assert relay.events[0][1]["completed"] is False
    assert relay.events[0][1]["partial"] is True
    assert relay.events[0][1]["interrupted"] is True
    assert relay.events[1][1]["completed"] is False
    assert relay.events[1][1]["partial"] is True
    assert relay.events[1][1]["interrupted"] is True
