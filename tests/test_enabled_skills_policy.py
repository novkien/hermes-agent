"""Thread-scoped enabled_skills policy tests."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.platforms.base import resolve_group_topic
from gateway.session_context import clear_session_vars, set_session_vars


CHAT_ID = "-1003914667905"
CEO_THREADS = ["32857", "70678", "70680", "70681", "70682"]
ENABLED = [
    "agent2agent-ceo-operation",
    "agent2agent-operation",
    "plan",
    "research-work-order",
    "room-task-binding",
    "orchestrator-swarm",
    "routing-operations",
]


def _extra(enabled=ENABLED):
    return {
        "group_topics": [
            {
                "chat_id": CHAT_ID,
                "topics": [
                    {
                        "thread_id": 32857,
                        "cross_thread": [70678, 70680, 70681, 70682],
                        "skills": ["agent2agent-ceo-operation"],
                        "enabled_skills": enabled,
                    }
                ],
            }
        ]
    }


def _config(enabled=ENABLED):
    return {"telegram": {"extra": _extra(enabled)}}


def test_all_five_physical_threads_resolve_same_canonical_policy():
    for thread_id in CEO_THREADS:
        topic = resolve_group_topic(_extra(), CHAT_ID, thread_id)
        assert topic["thread_id"] == 32857
        assert topic["skills"] == ["agent2agent-ceo-operation"]
        assert topic["enabled_skills"] == ENABLED


def test_same_thread_id_in_another_chat_does_not_resolve():
    assert resolve_group_topic(_extra(), "-100999", "70678") is None


def test_direct_child_entry_wins_over_cross_thread_inheritance():
    extra = _extra()
    extra["group_topics"][0]["topics"].append(
        {"thread_id": 70678, "enabled_skills": ["plan"]}
    )
    assert resolve_group_topic(extra, CHAT_ID, "70678")["enabled_skills"] == ["plan"]


def test_prompt_index_filters_to_enabled_skills(monkeypatch, tmp_path):
    from agent import prompt_builder

    skills_dir = tmp_path / "skills"
    for name in ("allowed", "blocked"):
        path = skills_dir / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} description\n---\n# {name}\n"
        )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    prompt_builder._SKILLS_PROMPT_CACHE.clear()
    rendered = prompt_builder.build_skills_system_prompt(enabled_skills={"allowed"})
    assert "- allowed:" in rendered
    assert "blocked" not in rendered


def test_prompt_cache_is_partitioned_by_enabled_skills(monkeypatch, tmp_path):
    from agent import prompt_builder

    skills_dir = tmp_path / "skills"
    for name in ("one", "two"):
        path = skills_dir / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n"
        )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    prompt_builder._SKILLS_PROMPT_CACHE.clear()
    one = prompt_builder.build_skills_system_prompt(enabled_skills={"one"})
    two = prompt_builder.build_skills_system_prompt(enabled_skills={"two"})
    assert "    - one: one" in one and "    - two: two" not in one
    assert "    - two: two" in two and "    - one: one" not in two


def test_skills_list_and_view_are_scoped(monkeypatch):
    from tools import skills_tool

    fake_skills = [
        {"name": "plan", "description": "p", "category": "x"},
        {"name": "blocked", "description": "b", "category": "x"},
    ]
    tokens = set_session_vars(platform="telegram", chat_id=CHAT_ID, thread_id="70678")
    try:
        monkeypatch.setattr(skills_tool, "_find_all_skills", lambda: fake_skills)
        with patch("hermes_cli.config.load_config_readonly", return_value=_config(["plan"])):
            listed = json.loads(skills_tool.skills_list())
            assert [item["name"] for item in listed["skills"]] == ["plan"]
            denied = json.loads(skills_tool.skill_view("blocked"))
            assert denied["success"] is False
            assert "not enabled" in denied["error"]
    finally:
        clear_session_vars(tokens)


def test_unconfigured_thread_preserves_legacy_list(monkeypatch):
    from tools import skills_tool

    fake_skills = [
        {"name": "plan", "description": "p", "category": "x"},
        {"name": "blocked", "description": "b", "category": "x"},
    ]
    tokens = set_session_vars(platform="telegram", chat_id=CHAT_ID, thread_id="999")
    try:
        monkeypatch.setattr(skills_tool, "_find_all_skills", lambda: fake_skills)
        with patch("hermes_cli.config.load_config_readonly", return_value=_config()):
            listed = json.loads(skills_tool.skills_list())
            assert {item["name"] for item in listed["skills"]} == {"plan", "blocked"}
    finally:
        clear_session_vars(tokens)


@pytest.mark.parametrize("bad", [[], "plan", [""], [1]])
def test_malformed_enabled_skills_fails_closed(bad):
    from tools import skills_tool

    tokens = set_session_vars(platform="telegram", chat_id=CHAT_ID, thread_id="32857")
    try:
        with patch("hermes_cli.config.load_config_readonly", return_value=_config(bad)):
            result = json.loads(skills_tool.skills_list())
            assert result["success"] is False
            assert "enabled_skills" in result["error"]
    finally:
        clear_session_vars(tokens)


def test_agent_init_rejects_unknown_enabled_skill(monkeypatch):
    from agent.agent_init import init_agent

    monkeypatch.setattr(
        "tools.skills_tool._find_all_skills",
        lambda: [{"name": "known"}],
    )
    with pytest.raises(ValueError, match="unknown enabled_skills"):
        init_agent(SimpleNamespace(), enabled_skills=["missing"])


def test_gateway_resolves_enabled_skills_for_synthetic_source(monkeypatch):
    from gateway.config import Platform
    from gateway.run import _resolve_source_enabled_skills
    from gateway.session import SessionSource

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=CHAT_ID,
        chat_type="group",
        thread_id="70681",
    )
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: _config())
    assert _resolve_source_enabled_skills(source) == ENABLED


def test_gateway_source_policy_legacy_for_unconfigured_thread(monkeypatch):
    from gateway.config import Platform
    from gateway.run import _resolve_source_enabled_skills
    from gateway.session import SessionSource

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=CHAT_ID,
        chat_type="group",
        thread_id="999",
    )
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: _config())
    assert _resolve_source_enabled_skills(source) is None


def test_agent_signature_changes_with_enabled_skills():
    from gateway.run import GatewayRunner

    base = GatewayRunner._agent_config_signature("m", {}, [], "", enabled_skills=["one"])
    same = GatewayRunner._agent_config_signature("m", {}, [], "", enabled_skills=["one"])
    changed = GatewayRunner._agent_config_signature("m", {}, [], "", enabled_skills=["two"])
    legacy = GatewayRunner._agent_config_signature("m", {}, [], "", enabled_skills=None)
    assert base == same
    assert base != changed
    assert base != legacy


def test_run_agent_forwards_enabled_skills_to_inner():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    captured = {}

    async def fake_run_agent_inner(*args, **kwargs):
        captured.update(kwargs)
        return {"final_response": "ok"}

    runner._run_agent_inner = fake_run_agent_inner

    result = asyncio.run(
        runner._run_agent(
            message="ping",
            context_prompt="",
            history=[],
            source=SimpleNamespace(),
            session_id="session-1",
            enabled_skills=["plan"],
        )
    )

    assert result["final_response"] == "ok"
    assert captured["enabled_skills"] == ["plan"]
