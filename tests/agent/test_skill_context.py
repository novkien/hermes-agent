import pytest

from agent.skill_context import (
    empty_skills_mode,
    prune_skill_description,
    resolve_profile_skills_mode,
    serialize_skills_mode,
    skill_mode_for_name,
    validate_skills_mode,
)


def test_profile_skills_mode_accepts_per_skill_lists_and_canonicalizes():
    configured = {
        "prune": ["research", "coder", "research"],
        "invisible": ["private"],
    }

    expected = {
        "prune": ("coder", "research"),
        "invisible": ("private",),
    }
    assert resolve_profile_skills_mode({"skills": {"mode": configured}}) == expected
    assert validate_skills_mode(configured) == expected
    assert serialize_skills_mode(expected) == {
        "prune": ["coder", "research"],
        "invisible": ["private"],
    }


def test_profile_skills_mode_defaults_every_skill_visible():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["skills"]["mode"] == {"prune": [], "invisible": []}
    assert resolve_profile_skills_mode({}) == empty_skills_mode()
    assert resolve_profile_skills_mode({"skills": {}}) == empty_skills_mode()
    assert skill_mode_for_name(empty_skills_mode(), "anything") == "visible"


@pytest.mark.parametrize(
    "value",
    [
        "prune",
        {"visible": ["skill-a"]},
        {"prune": "skill-a"},
        {"invisible": [""]},
        {"prune": [" skill-a"]},
        None,
        60,
    ],
)
def test_profile_skills_mode_rejects_invalid_shapes(value):
    with pytest.raises(ValueError, match="skills.mode"):
        resolve_profile_skills_mode({"skills": {"mode": value}})


def test_profile_skills_mode_rejects_conflicting_skill_assignment():
    with pytest.raises(ValueError, match="both prune and invisible"):
        validate_skills_mode({"prune": ["same"], "invisible": ["same"]})


def test_prune_is_60_unicode_code_points_and_preserves_short_text():
    short = "Đây là mô tả ngắn."
    long = "🧠" * 59 + "đuôi không được lộ"

    assert prune_skill_description(short) == short
    assert prune_skill_description(long) == "🧠" * 59 + "…"
    assert len(prune_skill_description(long)) == 60
    assert "đuôi" not in prune_skill_description(long)
