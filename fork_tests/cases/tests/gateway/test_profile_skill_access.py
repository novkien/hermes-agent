from types import SimpleNamespace

from gateway.skill_policy import SkillPolicyStatus, resolve_enabled_skills_policy


def test_cli_profile_and_telegram_topic_share_access_resolution(monkeypatch):
    from gateway.platforms import base

    monkeypatch.setattr(base, "resolve_group_topic", lambda *a: {"thread_id": "10", "enabled_skills": ["core", "other"]})
    config = {"skills": {"enabled": ["core"], "preload": ["core"]}}
    cli = resolve_enabled_skills_policy(SimpleNamespace(platform="cli"), config, skill_names=["core", "other"])
    topic = resolve_enabled_skills_policy(
        SimpleNamespace(platform="telegram", chat_id="group", thread_id="10"),
        config, skill_names=["core", "other"],
    )
    assert cli.status == topic.status == SkillPolicyStatus.CONFIGURED_VALID
    assert cli.identities == topic.identities == ("core",)


def test_invalid_profile_is_not_unconfigured_on_cli():
    result = resolve_enabled_skills_policy(
        SimpleNamespace(platform="cli"), {"skills": {"enabled": "core"}}, skill_names=["core"]
    )
    assert result.status == SkillPolicyStatus.RESOLUTION_ERROR
    assert not result.permits("core")
