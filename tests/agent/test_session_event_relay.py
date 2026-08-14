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
