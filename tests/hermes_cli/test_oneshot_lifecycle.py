from types import SimpleNamespace

import pytest

from hermes_cli.oneshot_lifecycle import oneshot_run


def test_ordinary_oneshot_does_not_invoke_plugin_lifetime():
    agent = SimpleNamespace(run_conversation=lambda message: {"final_response": message})
    with oneshot_run(agent, {}) as run:
        assert run("ordinary") == {"final_response": "ordinary"}


def test_delegated_synthesis_precedes_cleanup(monkeypatch):
    import hermes_cli.plugins as plugins

    events = []
    def managed(prompt):
        events.extend(["dispatch", "yield", "authenticated return", "synthesis"])
        return {"final_response": "actual product"}
    monkeypatch.setattr(plugins, "invoke_hook", lambda *a, **kw: [
        {"owner": "worker-pool", "run": managed, "close": lambda: events.append("settle")}
    ])
    with oneshot_run(object(), {"oneshot": {"lifecycle": "worker-pool"}}) as run:
        result = run("handoff")
        assert events[-1] == "synthesis"
    assert result["final_response"] == "actual product"
    assert events[-1] == "settle"


@pytest.mark.parametrize("handles", [[], [{"owner": "worker-pool"}]])
def test_missing_or_broken_required_lifetime_cannot_fall_back(monkeypatch, handles):
    import hermes_cli.plugins as plugins

    monkeypatch.setattr(plugins, "invoke_hook", lambda *a, **kw: handles)
    with pytest.raises(RuntimeError, match="unavailable or ambiguous"):
        with oneshot_run(object(), {"oneshot": {"lifecycle": "worker-pool"}}):
            pytest.fail("configured lifetime was bypassed")


def test_cleanup_runs_on_cancel_and_errors_propagate(monkeypatch):
    import hermes_cli.plugins as plugins

    closed = []
    def cancelled(prompt):
        raise KeyboardInterrupt()
    monkeypatch.setattr(plugins, "invoke_hook", lambda *a, **kw: [
        {"owner": "worker-pool", "run": cancelled, "close": lambda: closed.append(True)}
    ])
    with pytest.raises(KeyboardInterrupt):
        with oneshot_run(object(), {"oneshot": {"lifecycle": "worker-pool"}}) as run:
            run("handoff")
    assert closed == [True]
