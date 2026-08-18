from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one match, found {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "agent/session_event_relay.py",
    '''    key = _read("API_SERVER_KEY", "")\n    if not key:\n        return None\n    port = _read("API_SERVER_PORT", "8642")\n    # Deliberately not API_SERVER_HOST: that is the BIND address, and a gateway\n    # bound to 0.0.0.0 is still reached from this machine over loopback.\n    return f"http://127.0.0.1:{port}", key\n''',
    '''    # Detached workers can activate another profile before this relay starts.\n    # That profile loads its own .env with override=True, so API_SERVER_* can no\n    # longer be trusted to identify the gateway that spawned the worker. A\n    # dispatcher pins an observation-only target in dedicated process env.\n    relay_url = os.environ.get("HERMES_SESSION_EVENT_RELAY_URL", "").strip()\n    relay_key = os.environ.get("HERMES_SESSION_EVENT_RELAY_KEY", "").strip()\n    if relay_url and relay_key:\n        return relay_url.rstrip("/"), relay_key\n\n    # Backward compatibility for ordinary CLI turns and deployments that have\n    # not pinned a dedicated observation target.\n    key = _read("API_SERVER_KEY", "")\n    if not key:\n        return None\n    port = _read("API_SERVER_PORT", "8642")\n    # Deliberately not API_SERVER_HOST: that is the BIND address, and a gateway\n    # bound to 0.0.0.0 is still reached from this machine over loopback.\n    return f"http://127.0.0.1:{port}", key\n''',
)

replace_once(
    "hermes_cli/kanban_db.py",
    '''    prompt = f"work kanban task {task.id}"\n    env = dict(os.environ)\n    # The dispatcher is detached from every conversation. Its worker must never\n''',
    '''    prompt = f"work kanban task {task.id}"\n    env = dict(os.environ)\n\n    # Pin live-turn telemetry to the gateway that owns this dispatcher BEFORE\n    # the child activates its assignee profile. Profile startup loads .env with\n    # override=True and may replace API_SERVER_KEY/PORT; these dedicated vars\n    # survive that profile switch and affect observation only.\n    relay_url = str(os.environ.get("HERMES_SESSION_EVENT_RELAY_URL") or "").strip()\n    relay_key = str(os.environ.get("HERMES_SESSION_EVENT_RELAY_KEY") or "").strip()\n    if not (relay_url and relay_key):\n        parent_api_key = str(os.environ.get("API_SERVER_KEY") or "").strip()\n        if parent_api_key:\n            parent_api_port = str(os.environ.get("API_SERVER_PORT") or "8642").strip() or "8642"\n            relay_url = f"http://127.0.0.1:{parent_api_port}"\n            relay_key = parent_api_key\n    if relay_url and relay_key:\n        env["HERMES_SESSION_EVENT_RELAY_URL"] = relay_url.rstrip("/")\n        env["HERMES_SESSION_EVENT_RELAY_KEY"] = relay_key\n\n    # The dispatcher is detached from every conversation. Its worker must never\n''',
)

replace_once(
    "apps/mission-control/frontend/dist/tabs/chat.js",
    '''    if (sse) sse.watch(sessionId, sessionProfile);\n''',
    '''    // Detached Kanban workers publish frames to the dispatch-owning gateway's\n    // observation hub, not Mission Control's isolated profile runner. History\n    // remains profile-scoped; only the live watch target drops the profile.\n    if (sse) sse.watch(sessionId, workerLink ? null : sessionProfile);\n''',
)

replace_once(
    "tests/hermes_cli/test_kanban_worker_session_source.py",
    '''    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: tmp_path / "logs")\n\n    task = kb.Task(\n''',
    '''    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: tmp_path / "logs")\n    monkeypatch.setenv("API_SERVER_KEY", "dispatcher-observation-key")\n    monkeypatch.setenv("API_SERVER_PORT", "8642")\n\n    task = kb.Task(\n''',
)
replace_once(
    "tests/hermes_cli/test_kanban_worker_session_source.py",
    '''    assert captured["env"]["HERMES_SESSION_SOURCE"] == "kanban"\n''',
    '''    assert captured["env"]["HERMES_SESSION_SOURCE"] == "kanban"\n    assert captured["env"]["HERMES_SESSION_EVENT_RELAY_URL"] == "http://127.0.0.1:8642"\n    assert captured["env"]["HERMES_SESSION_EVENT_RELAY_KEY"] == "dispatcher-observation-key"\n''',
)

Path("tests/agent/test_session_event_relay.py").write_text(
    '''from agent.session_event_relay import gateway_ingest_target\n\n\ndef test_gateway_ingest_target_prefers_pinned_observation_env(monkeypatch):\n    monkeypatch.setenv("HERMES_SESSION_EVENT_RELAY_URL", "http://127.0.0.1:8642/")\n    monkeypatch.setenv("HERMES_SESSION_EVENT_RELAY_KEY", "dispatcher-key")\n    monkeypatch.setenv("API_SERVER_PORT", "49152")\n    monkeypatch.setenv("API_SERVER_KEY", "profile-key")\n    assert gateway_ingest_target() == ("http://127.0.0.1:8642", "dispatcher-key")\n\n\ndef test_gateway_ingest_target_keeps_legacy_api_server_fallback(monkeypatch):\n    monkeypatch.delenv("HERMES_SESSION_EVENT_RELAY_URL", raising=False)\n    monkeypatch.delenv("HERMES_SESSION_EVENT_RELAY_KEY", raising=False)\n    monkeypatch.setenv("API_SERVER_PORT", "8765")\n    monkeypatch.setenv("API_SERVER_KEY", "legacy-key")\n    assert gateway_ingest_target() == ("http://127.0.0.1:8765", "legacy-key")\n''',
    encoding="utf-8",
)

replace_once(
    "apps/mission-control/tests/live_route_contracts.mjs",
    '''assert.match(chat, /WATCH_SILENT_CATCHUP_MS/, 'chat does not promptly catch up a silent remote watcher');\n''',
    '''assert.match(chat, /WATCH_SILENT_CATCHUP_MS/, 'chat does not promptly catch up a silent remote watcher');\nassert.match(\n  chat,\n  /sse\\.watch\\(sessionId, workerLink \\? null : sessionProfile\\)/,\n  'Kanban worker chat is not watching the dispatcher observation hub',\n);\n''',
)
