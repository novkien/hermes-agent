"""Owner role/bootstrap guidance on the real skill catalog builder."""

from agent.prompt_builder import build_skills_system_prompt


def test_category_is_not_a_plugin_namespace(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    skill = tmp_path / "skills" / "comfyui" / "comfyui"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: comfyui\ndescription: Route media work\n---\n"
    )
    result = build_skills_system_prompt()
    assert "- comfyui: Route media work" in result
    assert "not category:name" in result
    assert "file_path=" in result
    assert result.index("bootstrap") < result.index("<available_skills>")
    assert "never bypass the policy" in result
    assert "If a skill has issues, fix it" not in result


def test_delegated_yield_preserves_owned_process_waiting():
    from agent.prompt_builder import OPENAI_MODEL_EXECUTION_GUIDANCE

    assert "pending delegated handoff" in OPENAI_MODEL_EXECUTION_GUIDANCE
    assert "only when the active role contract requires it" in OPENAI_MODEL_EXECUTION_GUIDANCE
    assert "your own tool-launched processes" in OPENAI_MODEL_EXECUTION_GUIDANCE
    assert "process poll/wait" in OPENAI_MODEL_EXECUTION_GUIDANCE
    assert "when a required return is pending" not in OPENAI_MODEL_EXECUTION_GUIDANCE
