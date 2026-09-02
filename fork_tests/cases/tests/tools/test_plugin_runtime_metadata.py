import model_tools


def test_plugin_dispatch_receives_tool_call_id(monkeypatch):
    seen = {}

    def fake_dispatch(name, args, **kwargs):
        seen.update(kwargs)
        return '{"success": true}'

    monkeypatch.setattr(model_tools.registry, "dispatch", fake_dispatch)

    result = model_tools.handle_function_call(
        "plugin_test_tool",
        {"value": 1},
        task_id="task-1",
        session_id="session-1",
        tool_call_id="call-1",
        skip_pre_tool_call_hook=True,
        skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True,
    )

    assert result == '{"success": true}'
    assert seen["task_id"] == "task-1"
    assert seen["session_id"] == "session-1"
    assert seen["tool_call_id"] == "call-1"
