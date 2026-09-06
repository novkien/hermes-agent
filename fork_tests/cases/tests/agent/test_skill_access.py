from types import SimpleNamespace

import pytest

from agent.skill_access import SkillAccess, materialize_profile_preloads, resolve_skill_access
from agent.skill_policy_context import bind_enabled_skills, current_enabled_skills


def test_absent_policy_preserves_legacy_but_empty_policy_denies_all():
    assert resolve_skill_access({}) == SkillAccess()
    assert resolve_skill_access({"skills": {"enabled": []}}).enabled == ()


def test_topic_can_only_narrow_profile():
    config = {"skills": {"enabled": ["coder-worker", "godot"], "preload": ["coder-worker"]}}
    policy = resolve_skill_access(config, enabled=["coder-worker", "retired-controller"])
    assert policy == SkillAccess(("coder-worker",), ("coder-worker",))
    with pytest.raises(ValueError, match="subset"):
        resolve_skill_access(config, enabled=["godot"])


@pytest.mark.parametrize("names", ["coder-worker", ["../coder-worker"], ["coder-worker", "coder-worker"], [None]])
def test_invalid_policy_does_not_widen(names):
    with pytest.raises(ValueError):
        resolve_skill_access({"skills": {"enabled": names}})


def test_unknown_configured_skill_is_rejected_even_when_topic_removes_it():
    with pytest.raises(ValueError, match="Unknown configured"):
        resolve_skill_access({"skills": {"enabled": ["typo", "core"]}}, enabled=["core"], known=["core"])


def test_visibility_does_not_grant_or_remove_access():
    policy = resolve_skill_access({"skills": {"mode": {"invisible": ["core"]}, "enabled": ["core"]}})
    assert policy.enabled == ("core",)


def test_required_preload_once_across_explicit_and_profile_paths(monkeypatch):
    import agent.skill_commands as commands

    monkeypatch.setattr(commands, "get_skill_commands", lambda: {"/core": {"name": "core"}})
    calls = []

    def build(names):
        assert current_enabled_skills() == ("core",)
        calls.append(names)
        return "CORE PROCEDURE", names, []

    monkeypatch.setattr(commands, "build_preloaded_skills_prompt", build)
    config = {"skills": {"enabled": ["core"], "preload": ["core"]}}
    agent = SimpleNamespace(enabled_skills=None, _auto_loaded_skill_prompt="")
    materialize_profile_preloads(agent, config)
    materialize_profile_preloads(agent, config, agent.preloaded_skill_names)
    assert calls == [["core"]]
    assert agent._auto_loaded_skill_prompt == "CORE PROCEDURE"
    explicit = SimpleNamespace(enabled_skills=None, _auto_loaded_skill_prompt="EXPLICIT CORE")
    materialize_profile_preloads(explicit, config, ["core"])
    assert calls == [["core"]]


def test_missing_required_preload_fails_before_work(monkeypatch):
    import agent.skill_commands as commands

    monkeypatch.setattr(commands, "get_skill_commands", lambda: {"/core": {"name": "core"}})
    monkeypatch.setattr(commands, "build_preloaded_skills_prompt", lambda names: ("", [], names))
    with pytest.raises(ValueError, match="Required skill preload"):
        materialize_profile_preloads(
            SimpleNamespace(enabled_skills=None, _auto_loaded_skill_prompt=""),
            {"skills": {"enabled": ["core"], "preload": ["core"]}},
        )


def test_bindings_restore_between_concurrent_contexts():
    from contextvars import copy_context

    with bind_enabled_skills(["manager"]):
        other = copy_context()
        def worker():
            with bind_enabled_skills(["worker"]):
                assert current_enabled_skills() == ("worker",)
        other.run(worker)
        assert current_enabled_skills() == ("manager",)
    assert current_enabled_skills() is None
