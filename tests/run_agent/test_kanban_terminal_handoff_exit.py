"""Regression coverage for dispatcher workers that outlive a terminal run."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from run_agent import AIAgent


def _tool_definition(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _tool_response(name: str):
    call = SimpleNamespace(
        id="call_terminal",
        type="function",
        function=SimpleNamespace(name=name, arguments="{}"),
    )
    message = SimpleNamespace(
        content=None,
        reasoning_content=None,
        reasoning=None,
        tool_calls=[call],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        model="test/model",
        usage=None,
    )


def test_blocked_worker_exits_before_another_provider_iteration(monkeypatch):
    """A finished Kanban run must stop the worker's model/tool loop.

    The live regression was a worker that successfully called
    ``kanban_block``.  The board closed its run and cleared ``worker_pid``,
    but the process made more provider calls and kept running tools because
    the conversation loop never inspected the now-terminal run.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_blocked")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")

    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=[_tool_definition("kanban_block")],
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=3,
        )

    agent._cached_system_prompt = "You are a Kanban worker."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._session_db = None
    agent._session_json_enabled = False

    provider_calls = 0

    def _provider_call(_api_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls > 1:
            raise AssertionError(
                "terminal Kanban run made another provider iteration"
            )
        return _tool_response("kanban_block")

    def _execute_terminal_tool(assistant_message, messages, *_args):
        call = assistant_message.tool_calls[0]
        messages.append(
            {
                "role": "tool",
                "name": "kanban_block",
                "tool_call_id": call.id,
                "content": '{"ok": true, "status": "blocked"}',
            }
        )

    terminal_run = {
        "task_id": "t_blocked",
        "run_id": 42,
        "task_status": "blocked",
        "run_status": "blocked",
        "outcome": "blocked",
    }

    agent._interruptible_api_call = _provider_call
    with (
        patch.object(agent, "_execute_tool_calls", side_effect=_execute_terminal_tool),
        patch.object(agent, "_flush_messages_to_session_db", return_value=True),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch(
            "tools.kanban_tools.current_worker_run_terminal_state_from_env",
            return_value=terminal_run,
            create=True,
        ),
    ):
        result = agent.run_conversation("work kanban task t_blocked")

    assert provider_calls == 1
    assert result["turn_exit_reason"] == "kanban_run_terminal(blocked)"
    assert result["api_calls"] == 1
