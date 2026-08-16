import pytest

from agent.skill_context import (
    prune_skill_description,
    resolve_profile_skills_mode,
    validate_skills_mode,
)


@pytest.mark.parametrize("mode", ["visible", "prune", "invisible"])
def test_profile_skills_mode_accepts_only_declared_enum(mode):
    assert resolve_profile_skills_mode({"skills": {"mode": mode}}) == mode
    assert validate_skills_mode(mode) == mode


def test_profile_skills_mode_defaults_visible_for_legacy_config():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["skills"]["mode"] == "visible"
    assert resolve_profile_skills_mode({}) == "visible"
    assert resolve_profile_skills_mode({"skills": {}}) == "visible"


@pytest.mark.parametrize("value", ["compact", "PRUNE", " prune", "", None, 60])
def test_profile_skills_mode_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="skills.mode must be one of"):
        resolve_profile_skills_mode({"skills": {"mode": value}})


def test_prune_is_60_unicode_code_points_and_preserves_short_text():
    short = "Đây là mô tả ngắn."
    long = "🧠" * 59 + "đuôi không được lộ"

    assert prune_skill_description(short) == short
    assert prune_skill_description(long) == "🧠" * 59 + "…"
    assert len(prune_skill_description(long)) == 60
    assert "đuôi" not in prune_skill_description(long)
