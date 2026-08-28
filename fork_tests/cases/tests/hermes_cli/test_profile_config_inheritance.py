"""Tests for profile config inheritance from the root config.yaml.

Contract (top-level ownership with narrow field-wise exceptions):

* A profile inherits root top-level sections it does not explicitly declare.
* A declared profile section replaces its root counterpart. ``model`` is the
  narrow exception: its fields merge so a sparse model override remains usable.
* Root telegram-scoped keys (``telegram``, ``platforms``, ``session``,
  ``sessions.channel_overrides`` and numeric thread ids) never reach a
  profile's effective config.
* ``inherit_root_config: false`` opts a profile out entirely.
* The three loaders (``load_config``, gateway, CLI) must agree.
* ``save_config`` must not materialize inherited root keys into the
  profile's own config.yaml.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import hermes_cli.config as cfgmod
from hermes_cli.config import load_config
from hermes_cli.env_loader import load_hermes_dotenv

ROOT_YAML = """
model:
  default: normal
  provider: 9router
memory:
  memory_char_limit: 7777
  write_approval: false
cron:
  allow_agent_scheduling: true
fleet:
  enabled: true
auxiliary:
  monitor:
    timeout: 999999
    api_key: root-monitor-key
  background_review:
    enabled: true
    interval_hours: 99
mcp:
  model: normal
mcp_servers:
  godot:
    url: http://godot.example.test/mcp
  computer-use-linux:
    command: /usr/bin/computer-use-linux-mcp
display:
  runtime_footer:
    enabled: true
    fields: [model, tokens]
fallback_providers:
  - provider: deepseek
    model: inherited-fallback
quick_commands:
  /demo: runs a demo command
telegram:
  extra:
    group_topics: true
    room_slots: 2
  room_chat_id: "123"
  channel_overrides:
    "-1001":
      system_prompt: root-channel-prompt
platforms:
  telegram:
    token: root-token
session: root-session
sessions:
  "62769":
    model: gpt-4o
  channel_overrides:
    "-1001":
      system_prompt: root-thread-prompt
"""

PROFILE_YAML = """
_config_version: 29
telegram:
  allowed_chats:
    - "-1001"
  require_mention: true
auxiliary:
  monitor:
    api_key: ""
model:
  context_length: 1000000
mcp_servers:
  comfyui:
    url: http://comfyui.example.test/mcp
"""


def _seed_inherited_env_profile(tmp_path, monkeypatch, *, profile_env: str = ""):
    root = tmp_path / "hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    (root / "config.yaml").write_text(
        "providers:\n"
        "  shared:\n"
        "    base_url: https://provider.example/v1\n"
        "    api_key: ${env:HERMES_CONFIG_SHARED_API_KEY}\n"
        "platforms:\n"
        "  discord:\n"
        "    token: ${env:ROOT_DISCORD_TOKEN}\n",
        encoding="utf-8",
    )
    (root / ".env").write_text(
        "HERMES_CONFIG_SHARED_API_KEY=root-shared-key\n"
        "ROOT_DISCORD_TOKEN=root-discord-token\n"
        "UNRELATED_ROOT_API_KEY=must-not-load\n",
        encoding="utf-8",
    )
    (profile / "config.yaml").write_text(
        "model:\n  default: worker-model\n", encoding="utf-8"
    )
    (profile / ".env").write_text(profile_env, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    for name in (
        "HERMES_CONFIG_SHARED_API_KEY",
        "ROOT_DISCORD_TOKEN",
        "UNRELATED_ROOT_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    return root, profile


def _run_profile_config_cli(root, *, env_key: str, config_key: str):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(root)
    env.pop(env_key, None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "-p",
            "worker",
            "config",
            "get",
            config_key,
        ],
        cwd=Path(__file__).resolve().parents[4],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_named_profile_loads_only_env_refs_needed_by_inherited_root_config(
    tmp_path, monkeypatch
):
    """Inherited root config carries only its referenced dotenv values."""
    _, profile = _seed_inherited_env_profile(tmp_path, monkeypatch)

    load_hermes_dotenv(hermes_home=profile)

    assert os.environ["HERMES_CONFIG_SHARED_API_KEY"] == "root-shared-key"
    assert "ROOT_DISCORD_TOKEN" not in os.environ
    assert "UNRELATED_ROOT_API_KEY" not in os.environ
    assert load_config()["providers"]["shared"]["api_key"] == "root-shared-key"


def test_named_profile_env_overrides_inherited_root_config_env_ref(
    tmp_path, monkeypatch
):
    """A profile can diverge from the shared root-config credential alias."""
    _, profile = _seed_inherited_env_profile(
        tmp_path,
        monkeypatch,
        profile_env="HERMES_CONFIG_SHARED_API_KEY=profile-key\n",
    )

    load_hermes_dotenv(hermes_home=profile)

    assert os.environ["HERMES_CONFIG_SHARED_API_KEY"] == "profile-key"


def test_profile_root_config_opt_out_does_not_load_root_dotenv(
    tmp_path, monkeypatch
):
    """``inherit_root_config: false`` remains a complete isolation boundary."""
    _, profile = _seed_inherited_env_profile(tmp_path, monkeypatch)
    (profile / "config.yaml").write_text(
        "inherit_root_config: false\nmodel:\n  default: worker-model\n",
        encoding="utf-8",
    )

    load_hermes_dotenv(hermes_home=profile)

    assert "HERMES_CONFIG_SHARED_API_KEY" not in os.environ


def test_named_profile_cli_does_not_warn_before_inherited_env_bridge(
    tmp_path, monkeypatch
):
    """Eager parser imports must not warn about a ref dotenv later resolves."""
    root, _ = _seed_inherited_env_profile(tmp_path, monkeypatch)

    result = _run_profile_config_cli(
        root,
        env_key="HERMES_CONFIG_SHARED_API_KEY",
        config_key="providers.shared.api_key",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "root-shared-key"
    assert "Config ref '${env:HERMES_CONFIG_SHARED_API_KEY}'" not in result.stderr


def test_provisional_warning_suppression_ends_after_dotenv_bootstrap(
    monkeypatch, caplog
):
    """The startup marker suppresses only provisional, never real, misses."""
    from hermes_cli import _startup_fast
    from hermes_cli.config import _expand_env_vars

    monkeypatch.delenv("HERMES_CONFIG_MISSING_API_KEY", raising=False)
    caplog.set_level("WARNING", logger="hermes_cli.config")
    try:
        _startup_fast._DOTENV_BOOTSTRAP_PENDING = True
        _expand_env_vars("${env:HERMES_CONFIG_MISSING_API_KEY}")
        assert "HERMES_CONFIG_MISSING_API_KEY is not set" not in caplog.text

        _startup_fast._DOTENV_BOOTSTRAP_PENDING = False
        _expand_env_vars("${env:HERMES_CONFIG_MISSING_API_KEY}")
        assert "HERMES_CONFIG_MISSING_API_KEY is not set" in caplog.text
    finally:
        _startup_fast._DOTENV_BOOTSTRAP_PENDING = False


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Isolated root + worker profile under tmp_path/.hermes."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "worker").mkdir(parents=True)
    (root / "config.yaml").write_text(ROOT_YAML, encoding="utf-8")
    worker = root / "profiles" / "worker" / "config.yaml"
    worker.write_text(PROFILE_YAML, encoding="utf-8")
    profile_home = root / "profiles" / "worker"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setattr(cfgmod, "get_hermes_home", lambda: profile_home)
    monkeypatch.setattr(cfgmod, "get_config_path", lambda: worker)
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfgmod._PROFILE_INHERITED_RAW_CACHE.clear()
    yield root, profile_home, worker
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfgmod._PROFILE_INHERITED_RAW_CACHE.clear()


def test_inherits_root_keys_absent_from_profile(profile_env):
    cfg = load_config()
    assert cfg["cron"]["allow_agent_scheduling"] is True
    assert cfg["fleet"] == {"enabled": True}
    assert cfg["memory"]["memory_char_limit"] == 7777
    assert cfg["memory"]["write_approval"] is False
    assert cfg["quick_commands"] == {"/demo": "runs a demo command"}
    assert "inherit_root_config" not in cfg


def test_profile_model_fields_merge_but_other_declared_sections_are_owned(profile_env):
    cfg = load_config()
    assert cfg["model"]["default"] == "normal"
    assert cfg["model"]["provider"] == "9router"
    assert cfg["model"]["context_length"] == 1000000
    monitor = cfg["auxiliary"]["monitor"]
    assert monitor["api_key"] == ""
    assert "root-monitor-key" not in str(monitor)

    # Defaults may supply auxiliary keys, but root-owned siblings must not be
    # present in the profile/root layer once the profile declares auxiliary.
    raw = cfgmod.read_config_with_profile_inheritance(profile_env[2])
    assert raw["auxiliary"] == {"monitor": {"api_key": ""}}

    # mcp_servers is a profile-owned capability boundary.  Root MCPs must not
    # become available merely because this profile declares its own server.
    assert cfg["mcp_servers"] == {
        "comfyui": {"url": "http://comfyui.example.test/mcp"}
    }


def test_telegram_and_channel_overrides_never_inherited(profile_env):
    cfg = load_config()
    tg = str(cfg["telegram"])
    assert cfg["telegram"]["allowed_chats"] == ["-1001"]
    for bad in ("group_topics", "room_slots", "room_chat_id", "channel_overrides", "system_prompt"):
        assert bad not in tg
    assert "platforms" not in cfg
    assert cfg.get("session") != "root-session"
    assert cfg.get("session") == {"terminal_continue": True}
    sessions = cfg.get("sessions", {})
    assert "62769" not in sessions
    assert "channel_overrides" not in sessions


def test_defaults_merged_beneath_inherited_section(profile_env):
    cfg = load_config()
    assert cfg["mcp"] == {"auto_reload_on_config_change": True, "model": "normal"}


def test_opt_out_flag_disables_inheritance(profile_env, tmp_path, monkeypatch):
    _, profile_home, worker = profile_env
    opt_out = PROFILE_YAML + "inherit_root_config: false\n"
    worker.write_text(opt_out, encoding="utf-8")
    monkeypatch.setattr(cfgmod, "get_config_path", lambda: worker)
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfg = load_config()
    assert cfg["cron"]["allow_agent_scheduling"] is False
    assert "fleet" not in cfg
    assert cfg["model"] == {"context_length": 1000000}
    assert "inherit_root_config" not in cfg


def test_non_profile_home_does_not_inherit(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "config.yaml").write_text(ROOT_YAML, encoding="utf-8")
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    (standalone / "config.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(standalone))
    monkeypatch.setattr(cfgmod, "get_hermes_home", lambda: standalone)
    monkeypatch.setattr(cfgmod, "get_config_path", lambda: standalone / "config.yaml")
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfg = load_config()
    assert cfg["cron"]["allow_agent_scheduling"] is False
    assert "fleet" not in cfg


def test_root_config_change_invalidates_profile_cache(profile_env):
    root, _, _ = profile_env
    assert load_config()["cron"]["allow_agent_scheduling"] is True
    slim_root = (
        ROOT_YAML.replace("cron:\n  allow_agent_scheduling: true\n", "")
        .replace("fleet:\n  enabled: true\n", "")
    )
    (root / "config.yaml").write_text(slim_root, encoding="utf-8")
    cfg = load_config()
    assert cfg["cron"]["allow_agent_scheduling"] is False
    assert "fleet" not in cfg


def test_profile_change_drops_inherited_key(profile_env, monkeypatch):
    _, _, worker = profile_env
    own_cron = PROFILE_YAML + "cron:\n  allow_agent_scheduling: false\n"
    worker.write_text(own_cron, encoding="utf-8")
    monkeypatch.setattr(cfgmod, "get_config_path", lambda: worker)
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfg = load_config()
    assert cfg["cron"]["allow_agent_scheduling"] is False
    assert cfg["fleet"] == {"enabled": True}


def test_save_config_does_not_materialize_inherited_keys(profile_env):
    cfg = load_config()
    cfgmod.save_config(cfg)
    raw = yaml.safe_load((profile_env[2]).read_text(encoding="utf-8"))
    for inherited in ("cron", "fleet", "memory", "mcp", "quick_commands"):
        assert inherited not in raw
    assert raw["auxiliary"]["monitor"] == {"api_key": ""}
    assert raw["telegram"]["allowed_chats"] == ["-1001"]
    assert raw["model"] == {"context_length": 1000000}
    reloaded = load_config()
    assert reloaded["cron"]["allow_agent_scheduling"] is True


def test_empty_owned_section_survives_save_round_trip(profile_env, monkeypatch):
    _, _, worker = profile_env
    own_mcp = PROFILE_YAML + "mcp:\n"
    worker.write_text(own_mcp, encoding="utf-8")
    monkeypatch.setattr(cfgmod, "get_config_path", lambda: worker)
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfg = load_config()
    assert cfg["mcp"] == {"auto_reload_on_config_change": True}
    cfgmod.save_config(cfg)
    raw = yaml.safe_load(worker.read_text(encoding="utf-8"))
    assert "mcp" in raw
    reloaded = load_config()
    assert "model" not in reloaded["mcp"]


def test_gateway_loader_applies_inheritance(profile_env, monkeypatch):
    import gateway.config as gwmod

    _, profile_home, _ = profile_env
    monkeypatch.setattr(gwmod, "get_hermes_home", lambda: profile_home)
    gw = gwmod.load_gateway_config()
    assert gw.quick_commands == {"/demo": "runs a demo command"}
    plat = gw.platforms[gwmod.Platform.TELEGRAM]
    assert "allowed_chats" in str(plat)
    assert plat.channel_overrides == {}
    for bad in ("group_topics", "room_slots", "room_chat_id"):
        assert bad not in str(plat.extra)
    assert "root-channel-prompt" not in str(plat)


def test_gateway_runtime_behavioral_loader_applies_inheritance(profile_env, monkeypatch):
    from gateway import run as gateway_run
    from gateway.runtime_footer import resolve_footer_config

    _, profile_home, _ = profile_env
    monkeypatch.setattr(gateway_run, "_hermes_home", profile_home)
    cfg = gateway_run._load_gateway_config()

    assert cfg["cron"]["allow_agent_scheduling"] is True
    assert cfg["model"]["default"] == "normal"
    assert cfg["model"]["provider"] == "9router"
    assert cfg["model"]["context_length"] == 1000000
    assert resolve_footer_config(cfg, "telegram") == {
        "enabled": True,
        "fields": ["model", "tokens"],
    }
    assert "inherit_root_config" not in cfg


def test_gateway_runtime_behavioral_loader_honors_opt_out(profile_env, monkeypatch):
    from gateway import run as gateway_run
    from gateway.runtime_footer import resolve_footer_config

    _, profile_home, worker = profile_env
    worker.write_text(PROFILE_YAML + "inherit_root_config: false\n", encoding="utf-8")
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfgmod._PROFILE_INHERITED_RAW_CACHE.clear()
    monkeypatch.setattr(gateway_run, "_hermes_home", profile_home)

    cfg = gateway_run._load_gateway_config()
    assert "cron" not in cfg
    assert "fleet" not in cfg
    assert resolve_footer_config(cfg, "telegram")["enabled"] is False
    assert "inherit_root_config" not in cfg


def test_presence_sensitive_profile_merge_has_no_defaults(profile_env):
    _, _, worker = profile_env
    cfg = cfgmod.read_config_with_profile_inheritance(worker)

    assert cfg["cron"]["allow_agent_scheduling"] is True
    assert cfg["model"] == {
        "default": "normal",
        "provider": "9router",
        "context_length": 1000000,
    }
    assert "auto_reload_on_config_change" not in cfg["mcp"]
    assert cfg["mcp_servers"] == {
        "comfyui": {"url": "http://comfyui.example.test/mcp"}
    }
    assert "inherit_root_config" not in cfg


def test_gateway_fallback_refresh_keeps_inherited_root_chain(profile_env, monkeypatch):
    from types import SimpleNamespace

    from gateway import run as gateway_run

    _, profile_home, _ = profile_env
    monkeypatch.setattr(gateway_run, "_hermes_home", profile_home)
    runner = SimpleNamespace(_fallback_model=None)
    bound = gateway_run.GatewayRunner._refresh_fallback_model.__get__(runner)

    assert bound() == [
        {"provider": "deepseek", "model": "inherited-fallback"}
    ]


def test_presence_sensitive_merge_keeps_last_good_on_torn_root(profile_env):
    root, _, worker = profile_env
    first = cfgmod.read_config_with_profile_inheritance(worker)
    (root / "config.yaml").write_text("display: [\n", encoding="utf-8")

    assert cfgmod.read_config_with_profile_inheritance(worker) == first


def test_cli_loader_applies_inheritance(profile_env, monkeypatch):
    import cli as cli_mod

    _, profile_home, _ = profile_env
    monkeypatch.setattr(cli_mod, "_hermes_home", profile_home)
    cfg = cli_mod.load_cli_config()
    assert cfg["cron"]["allow_agent_scheduling"] is True
    assert cfg["fleet"] == {"enabled": True}
    assert cfg["model"]["default"] == "normal"
    assert cfg["model"]["provider"] == "9router"
    assert cfg["model"]["context_length"] == 1000000
    assert cfg["telegram"]["allowed_chats"] == ["-1001"]
    assert "group_topics" not in str(cfg.get("telegram", {}))
