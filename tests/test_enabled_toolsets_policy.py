"""Thread-scoped enabled_toolsets policy tests."""
from types import SimpleNamespace

from gateway.platforms.base import resolve_group_topic
from gateway.toolset_policy import ToolsetPolicyStatus, resolve_enabled_toolsets_policy

CHAT_ID = "-1003914667905"
CEO_THREADS = ["32857", "70678", "70680", "70681", "70682"]
SELECTED = ["skills_read", "todo", "clarify", "a2a_coordination", "kanban_coordination", "permit_request", "room_coordination"]


def _source(thread_id, chat_id=CHAT_ID):
    return SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id=chat_id, thread_id=str(thread_id))


def _config(value=SELECTED):
    return {"telegram": {"extra": {"group_topics": [{"chat_id": CHAT_ID, "topics": [{
        "thread_id": 32857, "cross_thread": [70678, 70680, 70681, 70682],
        "enabled_toolsets": value,
    }]}]}}}


def _known():
    return set(SELECTED) | {"other"}


def test_five_physical_threads_inherit_canonical_policy():
    for thread_id in CEO_THREADS:
        topic = resolve_group_topic(_config()["telegram"]["extra"], CHAT_ID, thread_id)
        assert topic["thread_id"] == 32857
        result = resolve_enabled_toolsets_policy(_source(thread_id), _config(), known_toolsets=_known())
        assert result.status is ToolsetPolicyStatus.CONFIGURED_VALID
        assert list(result.toolsets) == SELECTED
        assert result.resolved_topic_id == "32857"


def test_same_thread_id_other_chat_is_unconfigured():
    assert resolve_enabled_toolsets_policy(_source(70678, "-100999"), _config(), known_toolsets=_known()).status is ToolsetPolicyStatus.UNCONFIGURED


def test_direct_child_overrides_inheritance():
    config = _config()
    config["telegram"]["extra"]["group_topics"][0]["topics"].append({"thread_id": 70678, "enabled_toolsets": ["todo"]})
    result = resolve_enabled_toolsets_policy(_source(70678), config, known_toolsets=_known())
    assert result.toolsets == ("todo",)
    assert result.resolved_topic_id == "70678"


def test_missing_key_is_legacy_but_empty_is_strict_zero_tools():
    config = _config()
    del config["telegram"]["extra"]["group_topics"][0]["topics"][0]["enabled_toolsets"]
    assert resolve_enabled_toolsets_policy(_source(32857), config, known_toolsets=_known()).status is ToolsetPolicyStatus.UNCONFIGURED
    result = resolve_enabled_toolsets_policy(_source(32857), _config([]), known_toolsets=_known())
    assert result.status is ToolsetPolicyStatus.CONFIGURED_VALID
    assert result.toolsets == ()


def test_invalid_shapes_duplicates_unknown_and_hard_disable_fail_closed():
    for value in ("todo", ["todo", 1], ["todo", "todo"], ["unknown"]):
        assert resolve_enabled_toolsets_policy(_source(32857), _config(value), known_toolsets=_known()).status is ToolsetPolicyStatus.CONFIGURED_INVALID
    config = _config(["todo"])
    config["agent"] = {"disabled_toolsets": ["todo"]}
    assert resolve_enabled_toolsets_policy(_source(32857), config, known_toolsets=_known()).status is ToolsetPolicyStatus.CONFIGURED_INVALID


def test_config_read_error_is_never_legacy():
    assert resolve_enabled_toolsets_policy(_source(32857), None, config_loaded=False).status is ToolsetPolicyStatus.RESOLUTION_ERROR


def test_reordered_list_has_same_fingerprint_and_physical_identity_partitions():
    a = resolve_enabled_toolsets_policy(_source(32857), _config(["todo", "clarify"]), known_toolsets=_known())
    b = resolve_enabled_toolsets_policy(_source(32857), _config(["clarify", "todo"]), known_toolsets=_known())
    child = resolve_enabled_toolsets_policy(_source(70678), _config(["todo", "clarify"]), known_toolsets=_known())
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != child.fingerprint


def test_gateway_helper_strict_replacement_and_legacy(monkeypatch):
    from gateway.run import _resolve_source_enabled_toolsets
    policy, fingerprint = _resolve_source_enabled_toolsets(_source(70681), _config(["todo"]))
    assert policy == ["todo"] and fingerprint != "legacy"
    legacy, fingerprint = _resolve_source_enabled_toolsets(_source(999), _config())
    assert legacy is None and fingerprint == "legacy"


def test_gateway_defaults_exclude_kanban_until_explicitly_enabled(monkeypatch):
    import hermes_cli.tools_config as tools_config
    from gateway.run import _resolve_gateway_enabled_toolsets

    monkeypatch.setattr(
        tools_config,
        "_get_platform_tools",
        lambda config, platform_key: {
            "web", "todo", "kanban", "kanban_coordination",
        },
    )

    defaults, fingerprint = _resolve_gateway_enabled_toolsets(
        _source(999), _config(), "telegram",
    )
    assert defaults == ["todo", "web"]
    assert fingerprint.startswith("gateway-default-no-kanban-v1:")

    manager, manager_fingerprint = _resolve_gateway_enabled_toolsets(
        _source(32857), _config(["skills_read", "kanban"]), "telegram",
    )
    assert manager == ["skills_read", "kanban"]
    assert not manager_fingerprint.startswith("gateway-default-no-kanban-v1:")


def test_gateway_explicit_empty_policy_stays_zero_tools(monkeypatch):
    import hermes_cli.tools_config as tools_config
    from gateway.run import _resolve_gateway_enabled_toolsets

    monkeypatch.setattr(
        tools_config,
        "_get_platform_tools",
        lambda config, platform_key: {"web", "kanban"},
    )
    enabled, _ = _resolve_gateway_enabled_toolsets(
        _source(32857), _config([]), "telegram",
    )
    assert enabled == []


def test_agent_cache_signature_distinguishes_empty_and_legacy():
    from gateway.run import GatewayRunner
    legacy = GatewayRunner._agent_config_signature("m", {}, [], "", enabled_toolsets_policy_fingerprint="legacy")
    zero = GatewayRunner._agent_config_signature("m", {}, [], "", enabled_toolsets_policy_fingerprint="zero")
    assert legacy != zero


def test_toolset_registry_defines_ceo_read_only_skill_surface():
    from toolsets import resolve_toolset
    assert set(resolve_toolset("skills_read")) == {"skills_list", "skill_view"}
    assert "skill_manage" not in resolve_toolset("skills_read")
