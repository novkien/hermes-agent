from agent.session_event_relay import gateway_ingest_target


def test_gateway_ingest_target_prefers_pinned_observation_env(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_EVENT_RELAY_URL", "http://127.0.0.1:8642/")
    monkeypatch.setenv("HERMES_SESSION_EVENT_RELAY_KEY", "dispatcher-key")
    monkeypatch.setenv("API_SERVER_PORT", "49152")
    monkeypatch.setenv("API_SERVER_KEY", "profile-key")
    assert gateway_ingest_target() == ("http://127.0.0.1:8642", "dispatcher-key")


def test_gateway_ingest_target_keeps_legacy_api_server_fallback(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_EVENT_RELAY_URL", raising=False)
    monkeypatch.delenv("HERMES_SESSION_EVENT_RELAY_KEY", raising=False)
    monkeypatch.setenv("API_SERVER_PORT", "8765")
    monkeypatch.setenv("API_SERVER_KEY", "legacy-key")
    assert gateway_ingest_target() == ("http://127.0.0.1:8765", "legacy-key")
