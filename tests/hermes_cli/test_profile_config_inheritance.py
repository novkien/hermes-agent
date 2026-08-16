"""Tests for profile config inheritance from the root config.yaml.

Contract (top-level presence semantics):

* A profile inherits a root config.yaml key ONLY when the profile's own
  config.yaml does not define that top-level key.
* A profile-owned key wins wholesale: root sub-keys are never merged into it.
* Root telegram-scoped keys (``telegram``, ``platforms``, ``session``,
  ``sessions.channel_overrides`` and numeric thread ids) never reach a
  profile's effective config.
* ``inherit_root_config: false`` opts a profile out entirely.
* The three loaders (``load_config``, gateway, CLI) must agree.
* ``save_config`` must not materialize inherited root keys into the
  profile's own config.yaml.
"""

import yaml
import pytest

import hermes_cli.config as cfgmod
from hermes_cli.config import load_config

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
"""


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
    yield root, profile_home, worker
    cfgmod._LOAD_CONFIG_CACHE.clear()


def test_inherits_root_keys_absent_from_profile(profile_env):
    cfg = load_config()
    assert cfg["cron"]["allow_agent_scheduling"] is True
    assert cfg["fleet"] == {"enabled": True}
    assert cfg["memory"]["memory_char_limit"] == 7777
    assert cfg["memory"]["write_approval"] is False
    assert cfg["quick_commands"] == {"/demo": "runs a demo command"}
    assert "inherit_root_config" not in cfg


def test_profile_owned_key_wins_wholesale(profile_env):
    cfg = load_config()
    assert cfg["model"] == {"context_length": 1000000}
    monitor = cfg["auxiliary"]["monitor"]
    assert monitor["api_key"] == ""
    assert monitor["timeout"] == 60
    assert "root-monitor-key" not in str(monitor)
    assert cfg["auxiliary"]["background_review"].get("interval_hours") != 99


def test_telegram_and_channel_overrides_never_inherited(profile_env):
    cfg = load_config()
    tg = str(cfg["telegram"])
    assert cfg["telegram"]["allowed_chats"] == ["-1001"]
    for bad in ("group_topics", "room_slots", "room_chat_id", "channel_overrides", "system_prompt"):
        assert bad not in tg
    assert "platforms" not in cfg
    assert "session" not in cfg
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


def test_cli_loader_applies_inheritance(profile_env, monkeypatch):
    import cli as cli_mod

    _, profile_home, _ = profile_env
    monkeypatch.setattr(cli_mod, "_hermes_home", profile_home)
    cfg = cli_mod.load_cli_config()
    assert cfg["cron"]["allow_agent_scheduling"] is True
    assert cfg["fleet"] == {"enabled": True}
    assert cfg["telegram"]["allowed_chats"] == ["-1001"]
    assert "group_topics" not in str(cfg.get("telegram", {}))