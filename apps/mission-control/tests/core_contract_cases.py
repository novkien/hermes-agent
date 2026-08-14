"""Broad unit contracts for core control-plane components.

The main runtime suite imports and executes ``run_all`` so these cases remain
part of the repository's canonical, dependency-light validation command.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from starlette.requests import Request


def _json(response: Any) -> dict[str, Any]:
    return json.loads(response.body.decode("utf-8"))


def _request(
    method: str,
    path: str,
    *,
    query: str = "",
    body: Any = None,
) -> Request:
    encoded = b"" if body is None else json.dumps(body).encode("utf-8")
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": encoded, "more_body": False}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": query.encode("utf-8"),
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "state": {},
        },
        receive,
    )
    request.state.request_id = "core-contract-request"
    return request


async def test_system_manager_cache_and_mutation_contracts() -> None:
    from agent_mission_control import system_manager_routes as sm

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
            self.rows = 0

        async def request(self, method, path, *, request_id, json_body=None):
            self.calls.append((method, path, json_body))
            if path == "/v1/db/read":
                self.rows += 1
                return 200, {"rows": [{"id": f"row-{self.rows}"}]}
            if path == "/v1/db/update":
                return 200, {"operation": "updated", "row": {"id": "svc-1"}}
            raise AssertionError(f"unexpected System Manager path: {path}")

    sequence: list[str] = []

    class Store:
        def append_audit(self, **_kwargs):
            sequence.append("audit")

    class Bus:
        def __init__(self) -> None:
            self.events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        async def safe_publish(self, *args, **kwargs):
            self.events.append((args, kwargs))

    class Core:
        def __init__(self) -> None:
            self.store = Store()
            self.event_bus = Bus()
            self.audit_results: list[tuple[Any, ...]] = []

        @staticmethod
        def _request_profile(request):
            return request.query_params.get("profile") or "default"

        @staticmethod
        def _guard_mutation(_request):
            sequence.append("guard")

        def _record_audit_result(self, *args):
            self.audit_results.append(args)

        @staticmethod
        def _envelope(data, **meta):
            return {"data": data, "meta": meta}

    original_client = sm.SystemManagerClient
    original_max = sm.SM_CACHE_MAX_ENTRIES
    client = Client()
    sm.SystemManagerClient = lambda: client
    core = Core()
    await sm._system_manager_cache_invalidate()
    try:
        router = sm.build_system_manager_router(core)
        read = next(
            route.endpoint
            for route in router.routes
            if route.path == "/api/system-manager/{table}" and "GET" in route.methods
        )
        update = next(
            route.endpoint
            for route in router.routes
            if route.path == "/api/system-manager/{table}" and "PUT" in route.methods
        )

        query = "profile=alpha&q=api&limit=999&host_id=h1"
        first = await read(_request("GET", "/api/system-manager/services", query=query), "services")
        assert first.status_code == 200
        assert client.calls[-1] == (
            "POST",
            "/v1/db/read",
            {"table": "services", "limit": 500, "query": "api", "where": {"host_id": "h1"}},
        )
        assert _json(first)["meta"]["profile_id"] == "alpha"

        cached = await read(_request("GET", "/api/system-manager/services", query=query), "services")
        assert len(client.calls) == 1, "a fresh read must be served from the profile-scoped cache"
        assert _json(cached)["data"] == _json(first)["data"]
        assert _json(cached)["meta"]["freshness"] == "live"

        other_profile = query.replace("alpha", "beta")
        await read(_request("GET", "/api/system-manager/services", query=other_profile), "services")
        assert len(client.calls) == 2, "cache entries must never cross profile boundaries"

        duplicate = await read(
            _request("GET", "/api/system-manager/services", query="host_id=a&host_id=b"),
            "services",
        )
        assert duplicate.status_code == 400
        assert _json(duplicate)["error"]["code"] == "invalid_query"
        unknown = await read(_request("GET", "/api/system-manager/nope"), "nope")
        assert unknown.status_code == 404

        key = sm._system_manager_cache_key("services", "alpha", "api", 500, {"host_id": "h1"})
        async with sm._SYSTEM_MANAGER_CACHE_LOCK:
            sm._SYSTEM_MANAGER_TABLE_CACHE[key]["stale_after"] = 0
        stale = await read(_request("GET", "/api/system-manager/services", query=query), "services")
        assert _json(stale)["meta"]["freshness"] == "stale"
        assert _json(stale)["meta"]["degraded_reason"] == "upstream_refresh_pending"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(client.calls) == 3, "a stale hit must trigger one background refresh"

        result = await update(
            _request(
                "PUT",
                "/api/system-manager/services",
                query="profile=alpha",
                body={"where": {"id": "svc-1"}, "values": {"display_name": "API"}},
            ),
            "services",
        )
        assert result.status_code == 200
        assert sequence[:2] == ["guard", "audit"], "mutation guards and pending audit precede upstream I/O"
        assert not any(
            entry.get("table") == "services" for entry in sm._SYSTEM_MANAGER_TABLE_CACHE.values()
        ), "successful writes must invalidate every cached view of the table"
        assert core.event_bus.events[0][0][:4] == (
            "system-manager.changed", "system-manager", "services", "svc-1"
        )

        sm.SM_CACHE_MAX_ENTRIES = 1
        await sm._system_manager_cache_set("old", "notes", {"rows": [1]})
        await asyncio.sleep(0)
        await sm._system_manager_cache_set("new", "accounts", {"rows": [2]})
        assert await sm._system_manager_cache_get("old") is None
        copied = await sm._system_manager_cache_get("new")
        assert copied is not None
        copied["table"] = "changed"
        assert (await sm._system_manager_cache_get("new"))["table"] == "accounts"
    finally:
        sm.SystemManagerClient = original_client
        sm.SM_CACHE_MAX_ENTRIES = original_max
        await sm._system_manager_cache_invalidate()


async def test_cache_stale_while_revalidate_contracts() -> None:
    from agent_mission_control.cache import Cache

    cache = Cache(ttl_seconds=0.01, max_concurrency=1)
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return {"version": calls}, {"marker": "ok"}

    first = await cache.get("k", "adapter", "fp-1", fetch)
    assert first.payload == {"version": 1}
    assert first.meta["source_id"] == "adapter"
    assert (await cache.get("k", "adapter", "fp-1", fetch)).payload == {"version": 1}
    assert calls == 1

    first.stale_after = 0
    stale = await cache.get("k", "adapter", "fp-2", fetch)
    assert stale.payload == {"version": 1}, "stale-while-revalidate must not block the read"
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert cache._entries["k"].payload == {"version": 2}
    assert cache.snapshot() == {"entries": 1, "inflight": 0, "sources_touched": ["adapter"]}
    assert cache.invalidate(source_id="adapter") == 1
    assert cache.invalidate(key="missing") == 1

    async def broken():
        raise RuntimeError("offline")

    unavailable = await cache.get("broken", "gateway", None, broken)
    assert unavailable.payload is None
    assert unavailable.meta == {"error": "upstream_unavailable"}


def test_network_security_and_configuration_contracts() -> None:
    from agent_mission_control.config import Settings, should_refuse_start, usable_secret
    from agent_mission_control.ip_utils import CidrList, resolve_client_ip
    from agent_mission_control.security import (
        SlidingWindowRateLimiter,
        build_request_summary,
        constant_time_equal,
        redact_headers,
        redact_query_params,
        redact_text,
        sha256_hex,
    )

    cidrs = CidrList.parse("192.168.1.99/24, 100.64.0.0/10, invalid")
    assert len(cidrs) == 2 and cidrs.contains("192.168.1.8") and cidrs.contains("100.90.1.2")
    assert not cidrs.contains("8.8.8.8") and not CidrList.parse(None).contains("127.0.0.1")
    assert cidrs.describe() == "192.168.1.0/24,100.64.0.0/10"
    assert resolve_client_ip("10.0.0.2", "192.168.1.8, 10.0.0.1", True) == "192.168.1.8"
    assert resolve_client_ip("10.0.0.2", "192.168.1.8", False) == "10.0.0.2"

    assert constant_time_equal("same", "same") and not constant_time_equal("a", "b")
    assert len(sha256_hex("value")) == 64
    headers = redact_headers({"Authorization": "Bearer secret-token", "Accept": "json"})
    assert headers == {"Authorization": "Bearer <redacted>", "Accept": "json"}
    assert redact_query_params({"token": "secret", "limit": "5"}) == {
        "token": "<redacted>", "limit": "5"
    }
    scrubbed = redact_text("Authorization: Bearer abcdefgh token=supersecret password=hunter2")
    assert "abcdefgh" not in scrubbed and "supersecret" not in scrubbed and "hunter2" not in scrubbed
    summary = build_request_summary(
        "POST", "/x", {"token": "secret", "page": "2"}, body={"status": "open", "note": "private"}
    )
    assert "secret" not in summary and "private" not in summary and "status=open" in summary

    limiter = SlidingWindowRateLimiter(2, 60)
    assert limiter.allow("owner") and limiter.allow("owner") and not limiter.allow("owner")
    limiter.reset("owner")
    assert limiter.allow("owner")
    limiter.reset()

    old_env = dict(os.environ)
    try:
        for name in ("BIND_HOST", "ALLOWED_CIDRS", "ALLOWED_ORIGIN", "ALLOWED_HOST"):
            os.environ.pop(name, None)
        settings = Settings.from_env(overrides={"cache_ttl_seconds": 7})
        assert settings.allowed_cidrs.contains("127.0.0.1")
        assert settings.resolved_allowed_host == "192.168.1.9:51763"
        assert settings.cache_ttl_seconds == 7
    finally:
        os.environ.clear()
        os.environ.update(old_env)
    assert usable_secret("real-secret", 8) and not usable_secret("changeme")
    assert should_refuse_start("0.0.0.0", False) and not should_refuse_start("127.0.0.1", False)


async def test_event_bus_replay_and_sse_contracts() -> None:
    from agent_mission_control.event_bus import EventBus, sse_frame, sse_frame_named, sse_heartbeat

    class Store:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def insert_event_replay(self, event_id, event_type, occurred_at, source_id,
                                entity_type, entity_id, payload, coverage):
            self.events.append({
                "event_id": event_id, "event_type": event_type, "occurred_at": occurred_at,
                "source_id": source_id, "entity_type": entity_type, "entity_id": entity_id,
                "payload": payload, "coverage": coverage,
            })

        def replay_latest(self, limit):
            return self.events[-limit:]

        def replay_events_after(self, event_id, limit):
            ids = [event["event_id"] for event in self.events]
            if event_id not in ids:
                return []
            return self.events[ids.index(event_id) + 1:][:limit]

        def replay_last_event_id(self):
            return self.events[-1]["event_id"] if self.events else ""

    store = Store()
    bus = EventBus(store, ring_buffer_size=2, db_replay_limit=10)
    delivered: list[str] = []

    async def subscriber(event):
        delivered.append(event["event_id"])

    async def broken(_event):
        raise RuntimeError("subscriber failure is isolated")

    bus.subscribe("*", subscriber)
    bus.subscribe("changed", broken)
    assert (await bus.publish("changed", "adapter", event_id="e1"))["coverage"] == "polled"
    assert await bus.publish("changed", "adapter", event_id="e1") is None
    await bus.publish("changed", "adapter", event_id="e2")
    await bus.publish("changed", "adapter", event_id="e3")
    assert delivered == ["e1", "e2", "e3"]
    assert [event["event_id"] for event in bus.ring_events()] == ["e2", "e3"]
    assert [event["event_id"] for event in await bus.replay_after("e2")] == ["e3"]
    assert [event["event_id"] for event in await bus.replay_after("e1")] == ["e2", "e3"]
    assert bus.last_event_id() == "e3"
    bus.unsubscribe("*", subscriber)

    frame = sse_frame(store.events[0], retry_ms=1234)
    assert frame.startswith("retry: 1234\nid: e1\nevent: changed\ndata: ")
    assert sse_frame_named("delta", {"text": "hi"}).startswith("event: delta\ndata: ")
    assert sse_heartbeat() == ": ping\n\n"


async def test_search_capabilities_alerts_and_pulse_contracts() -> None:
    from agent_mission_control.alerts import ACK, AlertEngine
    from agent_mission_control.capabilities import CapabilityRegistry
    from agent_mission_control.pulse import Pulse, cost_class_of
    from agent_mission_control.search import federated_search

    class Adapter:
        async def session_search(self, q, limit):
            assert q == "api" and limit == 50
            return 200, {"results": [{"session_id": "s1", "title": "API session"}]}, {}

        async def tasks(self, limit):
            assert limit == 100
            return 200, {"tasks": [{"id": "t1", "title": "Build API", "status": "running"}]}, {}

        async def issues_list(self, limit):
            return 200, {"issues": [{"id": 7, "issue": "API failure", "severity": "high"}]}, {}

        async def permits_list(self, limit):
            raise RuntimeError("permits offline")

        async def health(self):
            return 200, {}, {}

        async def capabilities(self):
            return 200, {"schema_fingerprint": "fp-new"}, {}

    search = await federated_search(Adapter(), "api", limit=500)
    assert search["total"] == 3 and search["degraded"] is True
    assert search["sources"][0]["results"][0]["href"] == "/sessions/s1"
    assert search["sources"][2]["results"][0]["href"] == "/issues/7"
    assert search["state_db_full_scan"] is False

    class FingerprintStore:
        def __init__(self) -> None:
            self.fingerprint = "fp-old"

        def record_fingerprint(self, _source, fingerprint):
            self.fingerprint = fingerprint

        def get_fingerprint(self, _source):
            return self.fingerprint

    class Dashboard:
        async def get(self, _path):
            raise ValueError("unexpected probe failure")

    class Gateway:
        _nas_jwt_secret = "configured"

        async def request(self, _method, path):
            return (200 if path == "/health" else 401), {}, {}

    fingerprints = FingerprintStore()
    registry = CapabilityRegistry(Adapter(), Dashboard(), Gateway(), fingerprints)
    capabilities = await registry.refresh()
    assert capabilities["adapter"]["schema_fingerprint"] == "fp-new"
    assert capabilities["adapter"]["healthy"] is True
    assert capabilities["hermes-dashboard"]["healthy"] is False
    assert "unexpected probe failure" in capabilities["hermes-dashboard"]["error"]
    assert capabilities["cron"]["healthy"] is True
    assert registry.snapshot() == capabilities

    class AlertStore:
        def __init__(self) -> None:
            self.acks: dict[str, dict[str, Any]] = {}
            self.cleared: list[str] = []

        def get_fingerprint(self, _source):
            return "fp-old"

        def audit_failures_since(self, _window, _minimum):
            return True

        def get_acknowledgement(self, alert_id):
            return self.acks.get(alert_id)

        def acknowledge_alert(self, alert_id, action, expires_at):
            self.acks[alert_id] = {"action": action, "expires_at": expires_at}

        def clear_acknowledgements(self, alert_id):
            self.acks.pop(alert_id, None)
            self.cleared.append(alert_id)

    class Bus:
        def __init__(self) -> None:
            self.events: list[tuple[Any, ...]] = []

        async def publish(self, *args, **_kwargs):
            self.events.append(args)

    now = int(time.time())
    alert_store = AlertStore()
    alert_bus = Bus()
    cfg = SimpleNamespace(
        alert_stale_seconds=10,
        alert_heartbeat_stale_seconds=10,
        alert_permit_expiry_hours=24,
        alert_permit_pending_days=7,
        alert_mutation_fail_window_seconds=600,
        alert_mutation_fail_min=3,
    )
    engine = AlertEngine(alert_store, cfg, bus=alert_bus, source_data={
        "health": {"adapter": False},
        "capabilities": {"schema_fingerprint": "fp-new"},
        "freshness": {"gateway": {"fetched_at": now - 20}},
        "cron": [{"id": "c1", "name": "nightly", "last_status": "error"}],
        "tasks": [{"id": "t1", "status": "failed"},
                  {"id": "t2", "status": "running", "last_heartbeat_at": now - 20}],
        "runs": [{"id": "r1", "outcome": "crashed"}],
        "permits": [{"permit_id": "p1", "status": "pending_approval", "expires_at": now + 60}],
        "issues": [{"id": 7, "status": "open", "severity": "critical"}],
        "analytics": {"token_threshold": 100, "tokens": 101},
    })
    alerts = engine.evaluate()
    assert {alert["rule_id"] for alert in alerts} == {
        "R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R10", "R11"
    }
    alert_id = alerts[0]["id"]
    assert (await engine.acknowledge(alert_id, ACK))["state"] == ACK
    assert engine.list_active()[0]["state"] == ACK
    engine.source_data = {}
    engine.evaluate()
    assert engine.resolved_pending and alert_store.cleared
    await engine.publish_resolved()
    assert alert_bus.events[-1][0] == "alert.changed"

    class Row(dict):
        pass

    class Conn:
        @staticmethod
        def execute(_sql, _params):
            return SimpleNamespace(fetchone=lambda: Row(c=4))

    class PulseStore:
        @staticmethod
        def conn():
            return Conn()

    pulse = Pulse(PulseStore(), {
        "cron": [{"last_status": "error"}],
        "tasks": [{"status": "running"}, {"status": "failed"}],
        "sessions": [{"ended_at": None}, {"ended_at": "done"}],
        "permits": [{"status": "pending_approval"}],
        "issues": [{"status": "open"}, {"status": "resolved"}],
        "analytics": {"tokens_in": 11, "tokens_out": 3, "cost_estimated": 1.2345678,
                      "cost_status": "estimated"},
    }).derive("bogus")
    assert pulse["window"] == "24h" and pulse["event_count"] == 4
    assert (pulse["failures"], pulse["active_sessions"], pulse["running_tasks"]) == (2, 1, 1)
    assert (pulse["pending_permits"], pulse["open_issues"]) == (1, 1)
    assert pulse["cost_estimated"] == 1.234568 and pulse["cost_class"] == "Hermes-calculated"
    assert cost_class_of(None, 0.5) == "estimated-from-verified-rate"
    assert cost_class_of(None, 0) == "unavailable"


async def test_upstream_client_contracts() -> None:
    import httpx

    from agent_mission_control.clients import (
        DashboardClient,
        GatewayClient,
        UpstreamError,
        _BaseClient,
    )
    from agent_mission_control import system_manager_client as sm_client

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/text":
            return httpx.Response(200, text="plain", headers={"X-Upstream": "yes"})
        return httpx.Response(200, json={"ok": True}, headers={"X-Upstream": "yes"})

    client = _BaseClient(
        "http://upstream.invalid/",
        timeout=3,
        route_timeouts={"/slow": 9},
        headers={"Authorization": "Bearer secret"},
    )
    client._client = httpx.AsyncClient(
        base_url="http://upstream.invalid", transport=httpx.MockTransport(handler)
    )
    status, body, headers = await client.request("GET", "/json", inbound_request_id="rid")
    assert status == 200 and body == {"ok": True} and headers["x-upstream"] == "yes"
    assert seen[-1].headers["x-request-id"] == "rid"
    assert client._timeout_for("/slow/item") == 9 and client._timeout_for("/other") == 3
    assert (await client.request("GET", "/text"))[1] == "plain"
    assert (await client.request("GET", "/text", raw=True))[1] == b"plain"
    streamed = await client.stream("GET", "/json", inbound_request_id="stream-rid")
    assert streamed.status_code == 200
    await streamed.aclose()
    assert "secret" not in client.describe_headers({"Authorization": "Bearer secret"})
    await client.aclose()

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("late", request=request)

    failed = _BaseClient("http://upstream.invalid")
    failed._client = httpx.AsyncClient(
        base_url="http://upstream.invalid", transport=httpx.MockTransport(timeout_handler)
    )
    try:
        await failed.request("GET", "/timeout")
        raise AssertionError("timeout should be translated")
    except UpstreamError as exc:
        assert exc.status == 504 and exc.detail == "timeout"
    await failed.aclose()

    login_count = 0
    protected_count = 0

    def dashboard_handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_count, protected_count
        if request.url.path == "/auth/password-login":
            login_count += 1
            return httpx.Response(200, json={"ok": True}, headers={"Set-Cookie": f"sid={login_count}; HttpOnly"})
        protected_count += 1
        if protected_count == 1:
            return httpx.Response(401, json={"error": "expired"})
        assert request.headers["cookie"] == "sid=2"
        return httpx.Response(200, json={"data": "ok"})

    dashboard = DashboardClient("http://dashboard.invalid", "password")
    dashboard._client = httpx.AsyncClient(
        base_url="http://dashboard.invalid", transport=httpx.MockTransport(dashboard_handler)
    )
    assert (await dashboard.get("/api/health"))[:2] == (200, {"data": "ok"})
    assert (login_count, protected_count) == (2, 2), "an expired cookie is re-authenticated exactly once"
    await dashboard.aclose()

    unconfigured = DashboardClient("http://dashboard.invalid", None)
    try:
        await unconfigured.get("/api/health")
        raise AssertionError("missing dashboard auth should fail closed")
    except UpstreamError as exc:
        assert exc.status == 503

    class GatewayRecorder(GatewayClient):
        def __init__(self, secret):
            super().__init__("http://gateway.invalid", "api-key", nas_jwt_secret=secret)
            self.recorded = None

        async def request(self, method, path, **kwargs):
            self.recorded = (method, path, kwargs)
            return 202, {"accepted": True}, {}

    gateway = GatewayRecorder("nas-secret")
    await gateway.cron_fire({"job": "j1"}, inbound_request_id="rid", idempotency_key="idem")
    assert gateway.recorded[2]["extra_headers"] == {
        "Authorization": "Bearer nas-secret", "Idempotency-Key": "idem"
    }
    try:
        await GatewayRecorder(None).cron_fire({})
        raise AssertionError("cron fire should require its dedicated secret")
    except UpstreamError as exc:
        assert exc.status == 503

    original_async_client = sm_client.httpx.AsyncClient
    original_env = dict(os.environ)
    captured: dict[str, Any] = {}

    class FakeResponse:
        status_code = 200
        text = "not-json"

        @staticmethod
        def json():
            raise ValueError("not json")

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, path, **kwargs):
            captured["request"] = (method, path, kwargs)
            return FakeResponse()

    try:
        os.environ["SYSTEM_MANAGER_URL"] = "http://system-manager.invalid/"
        os.environ["SYSTEM_MANAGER_TOKEN"] = "token"
        os.environ["SYSTEM_MANAGER_TIMEOUT_SECONDS"] = "0.1"
        sm_client.httpx.AsyncClient = FakeAsyncClient
        system_manager = sm_client.SystemManagerClient()
        status, body = await system_manager.request("GET", "/health", request_id="rid")
        assert status == 200 and body["error"] == "not-json"
        assert captured["init"]["base_url"] == "http://system-manager.invalid"
        assert captured["init"]["timeout"] == 1.0
        assert captured["request"][2]["headers"] == {
            "X-Request-Id": "rid", "Authorization": "Bearer token"
        }
    finally:
        sm_client.httpx.AsyncClient = original_async_client
        os.environ.clear()
        os.environ.update(original_env)


async def test_app_middleware_contracts() -> None:
    from fastapi.responses import JSONResponse, StreamingResponse

    from agent_mission_control.app import (
        _AllowlistGate,
        _api_error_handler,
        _body_limit_middleware,
        _request_id_middleware,
        _security_headers_middleware,
    )
    from agent_mission_control.ip_utils import CidrList
    from agent_mission_control.routes import ApiError

    async def ok(_request):
        return JSONResponse({"ok": True})

    request = _request("GET", "/api/test")
    response = await _request_id_middleware(request, ok)
    assert response.headers["x-request-id"] == request.state.request_id
    secured = await _security_headers_middleware(request, ok)
    assert secured.headers["x-content-type-options"] == "nosniff"
    assert secured.headers["referrer-policy"] == "no-referrer"
    assert secured.headers["cache-control"] == "no-store"

    too_large = _request("POST", "/api/test")
    too_large.scope["headers"] = [(b"content-length", b"11")]
    too_large.scope["app"] = SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(body_limit_bytes=10)
    ))
    limited = await _body_limit_middleware(too_large, ok)
    assert limited.status_code == 413
    error = await _api_error_handler(request, ApiError(409, "conflict", "cannot continue"))
    assert error.status_code == 409 and _json(error)["error"]["code"] == "conflict"

    class Router:
        def __init__(self):
            self.s = SimpleNamespace(trust_proxy_headers=False)
            self.allowlist = CidrList.parse("127.0.0.1/32")
            self.attached = 0
            self.auto = {"id": "sid", "csrf_token": "csrf"}

        @staticmethod
        def _session_from_request(_request):
            return None

        def auto_issue_session(self, _request):
            return self.auto

        def _attach_session_cookie(self, _response, session, _request):
            assert session is self.auto
            self.attached += 1

    router = Router()
    gate = _AllowlistGate(router)
    allowed = await gate(_request("GET", "/api/test"), ok)
    assert allowed.status_code == 200 and router.attached == 1
    assert _AllowlistGate._is_static_asset_path("/tabs/chat.js")
    assert _AllowlistGate._is_static_asset_path("/styles.css")
    await gate(_request("GET", "/styles.css"), ok)
    assert router.attached == 1, "static assets do not allocate sessions"

    async def stream(_request):
        async def content():
            yield b"event: ready\n\n"
        return StreamingResponse(content(), media_type="text/event-stream")

    await gate(_request("GET", "/api/events"), stream)
    assert router.attached == 1, "SSE responses must not receive a session cookie"
    router.allowlist = CidrList.parse("10.0.0.0/8")
    forbidden = await gate(_request("GET", "/api/test"), ok)
    assert forbidden.status_code == 403
    router.allowlist = CidrList.parse("127.0.0.1/32")
    router.auto = None
    rate_limited = await gate(_request("GET", "/api/test"), ok)
    assert rate_limited.status_code == 429


async def test_source_worker_contracts() -> None:
    from agent_mission_control.workers import PollWorker, SourceWorkers, fingerprint_json, fingerprint_tasks

    class Bus:
        def __init__(self):
            self.events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        async def publish(self, *args, **kwargs):
            self.events.append((args, kwargs))

    class Store:
        def __init__(self):
            self.fingerprints: list[tuple[str, str]] = []

        def record_fingerprint(self, source, fingerprint):
            self.fingerprints.append((source, fingerprint))

    class Adapter:
        async def board_summary(self):
            return 200, {"running": 1}, {}

        async def tasks(self, limit):
            return 200, [{"id": "t1", "status": "running", "current_run_id": "r1"}], {}

        async def permits_list(self, limit):
            return 200, [{"permit_id": "p1", "status": "pending_approval"}], {}

        async def issues_list(self, limit):
            return 200, [{"id": 7, "status": "open", "severity": "high"}], {}

        async def capabilities(self):
            return 200, {"schema_fingerprint": {"sha256_ddl": "fp-1"}}, {}

    class Dashboard:
        async def get(self, path):
            if path == "/api/cron/jobs":
                return 200, {"jobs": [{"id": "c1", "state": "on"}]}, {}
            if path.startswith("/api/sessions"):
                return 200, {"sessions": [{"id": "s1", "message_count": 1}]}, {}
            if path == "/api/analytics/usage":
                return 200, {"totals": {"input_tokens": 12}}, {}
            return 200, {"ok": True}, {}

    class Gateway:
        async def get(self, path):
            if path == "/api/sessions/running":
                return 200, {"running": [{"session_id": "s1", "run_id": "r1"}]}, {}
            return 503, {}, {}

    class AlertEngine:
        def __init__(self):
            self.data: dict[str, Any] = {}

        def set_source_data(self, key, value):
            self.data[key] = value

    cfg = SimpleNamespace(
        poll_kanban_seconds=1, poll_permits_seconds=1, poll_issues_seconds=1,
        poll_cron_seconds=1, poll_sessions_seconds=1, poll_running_seconds=1,
        poll_health_seconds=1, poll_adapter_health_seconds=1, poll_analytics_seconds=1,
        poll_backoff_max_seconds=8, alert_token_threshold=10,
    )
    bus, store, alerts = Bus(), Store(), AlertEngine()
    workers = SourceWorkers(bus, store, SimpleNamespace(), Dashboard(), Gateway(), Adapter(), cfg, alerts)
    workers.build()
    assert set(workers.workers) == {
        "kanban", "permits", "issues", "cron", "sessions", "running", "health",
        "capabilities", "analytics",
    }

    kanban = await workers._fetch_kanban()
    await workers._on_kanban(kanban, workers._fp_kanban(kanban))
    await workers._on_kanban(
        {"summary": {}, "tasks": [{"id": "t1", "status": "done", "current_run_id": "r1"}]},
        None,
    )
    assert any(event[0][0] == "task.changed" for event in bus.events)
    assert any(event[0][0] == "run.changed" for event in bus.events)
    permits = await workers._fetch_permits()
    await workers._on_permits(permits, workers._fp_permits(permits))
    issues = await workers._fetch_issues()
    await workers._on_issues(issues, workers._fp_issues(issues))
    cron = await workers._fetch_cron()
    await workers._on_cron(cron, workers._fp_cron(cron))
    sessions = await workers._fetch_sessions()
    await workers._on_sessions(sessions, workers._fp_sessions(sessions))
    running = await workers._fetch_running()
    await workers._on_running(running, workers._fp_running(running))

    health = await workers._fetch_health()
    assert health == {"gateway": False, "dashboard": True, "dashboard_status": True}
    await workers._on_health(health, workers._fp_health(health))
    changed_health = dict(health, gateway=True)
    await workers._on_health(changed_health, workers._fp_health(changed_health))
    assert any(event[0][0] == "source.health" for event in bus.events)

    capabilities = await workers._fetch_capabilities()
    fingerprint = workers._fp_capabilities(capabilities)
    assert fingerprint == "fp-1"
    await workers._on_capabilities(capabilities, fingerprint)
    assert store.fingerprints == [("adapter", "fp-1")]
    analytics = await workers._fetch_analytics()
    assert workers._total_tokens(analytics) == 12
    await workers._on_analytics(analytics, workers._fp_analytics(analytics))
    assert alerts.data["analytics"] == {"tokens": 12.0, "token_threshold": 10}
    assert workers.freshness_snapshot() == {}

    assert fingerprint_json({"b": 2, "a": 1}) == fingerprint_json({"a": 1, "b": 2})
    assert fingerprint_tasks([{"id": "t", "status": "running"}]) != fingerprint_tasks([
        {"id": "t", "status": "done"}
    ])

    failures = 0

    async def broken_fetch():
        nonlocal failures
        failures += 1
        raise RuntimeError("offline")

    failed_worker = PollWorker("failed", 1, broken_fetch, lambda *_args: None, fingerprint_json, 4)
    failed_worker.pause()
    assert failed_worker.paused
    failed_worker.resume()
    await failed_worker.tick_once()
    await failed_worker.tick_once()
    await failed_worker.tick_once()
    assert failures == 3 and failed_worker.backoff_seconds == 4 and failed_worker.last_error == "offline"


def test_store_crud_and_replay_contracts() -> None:
    from agent_mission_control.store import Store

    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "contracts.db")
        try:
            assert store.schema_version() == 3
            assert set(store.table_names()) == {
                "action_audit", "alert_acknowledgements", "alert_rules", "cache_metadata",
                "event_replay", "preferences", "saved_views", "schema_fingerprints", "sessions",
            }
            store.create_session("sid", "csrf", 60)
            assert store.get_session("sid")["csrf_token"] == "csrf"
            assert [row["id"] for row in store.list_sessions()] == ["sid"]
            store.delete_session("sid")
            assert store.get_session("sid") is None

            store.append_audit("rid", "owner", "update", "/x", None, "POST /x", None, "pending")
            store.complete_audit("rid", 503, "upstream:503")
            assert store.list_audit()[0]["upstream_status"] == 503
            assert store.count_audit() == 1 and store.audit_failures_since(60, 1)

            store.set_preference("theme", "light")
            store.set_preference("theme", "dark", "alpha")
            assert store.get_preference("theme", "alpha") == "dark"
            assert store.list_preferences("alpha") == {"theme": "dark"}
            store.save_view("v1", "Open", "issues", {"status": "open"}, "alpha")

            store.upsert_cache_metadata("k", "adapter", "fp", 1, 2)
            assert store.get_cache_metadata("k")["fingerprint"] == "fp"
            store.record_fingerprint("adapter", "fp-1")
            assert store.get_fingerprint("adapter") == "fp-1"
            assert store.list_fingerprints()[0]["source_id"] == "adapter"

            store.upsert_alert_rule("R1", {"threshold": 1}, enabled=True)
            assert store.is_alert_rule_enabled("R1")
            assert store.list_alert_rules()[0]["rule_id"] == "R1"
            store.acknowledge_alert("R1:x", "acknowledged")
            assert store.get_acknowledgement("R1:x")["action"] == "acknowledged"
            store.clear_acknowledgements("R1:x")
            assert store.get_acknowledgement("R1:x") is None

            for event_id in ("e1", "e2", "e3"):
                assert store.insert_event_replay(
                    event_id, "changed", 1, "adapter", "task", event_id, {"id": event_id}, "native"
                )
            assert not store.insert_event_replay(
                "e1", "changed", 1, "adapter", "task", "e1", {}, "native"
            )
            assert [event["event_id"] for event in store.replay_events_after("e1", 10)] == ["e2", "e3"]
            assert [event["event_id"] for event in store.replay_latest(2)] == ["e2", "e3"]
            assert store.replay_last_event_id() == "e3" and store.event_replay_count() == 3
        finally:
            store.close()


async def run_all() -> None:
    await test_system_manager_cache_and_mutation_contracts()
    await test_cache_stale_while_revalidate_contracts()
    test_network_security_and_configuration_contracts()
    await test_event_bus_replay_and_sse_contracts()
    await test_search_capabilities_alerts_and_pulse_contracts()
    await test_upstream_client_contracts()
    await test_app_middleware_contracts()
    await test_source_worker_contracts()
    test_store_crud_and_replay_contracts()
