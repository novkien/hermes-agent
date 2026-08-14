#!/usr/bin/env python3
"""Targeted regression tests for the AgentOS dashboard stabilization repair.

These tests deliberately exercise the cross-layer contracts behind the owner-
reported failures. They do not require live Hermes or adapter services.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

from starlette.requests import Request

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from agent_mission_control import redact as redact_mod  # noqa: E402
from agent_mission_control.clients import UpstreamError  # noqa: E402
from agent_mission_control.routes import (  # noqa: E402
    CONFIG_WRITE_ALLOW_TREE,
    GATEWAY_READ_PATHS,
    MUTATION_ALLOWLIST,
    READ_PATH_MUTATIONS,
    UPSTREAM_MUTATION_METHODS,
    UPSTREAM_MUTATION_SPECS,
    Router,
    _describe_allow_tree,
    _prune_to_allow_tree,
    is_allowed_adapter_path,
    is_allowed_read_path,
    match_upstream_mutation,
    resolve_upstream_method,
    split_upstream_envelope,
    upstream_error_status,
)


class FakeDashboard:
    def __init__(self, *, status: int = 200, body=None, error: UpstreamError | None = None):
        self.status = status
        self.body = body
        self.error = error
        self.calls: list[dict] = []

    async def get(self, path, *, params=None, inbound_request_id=None):
        self.calls.append({
            "path": path,
            "params": params,
            "request_id": inbound_request_id,
        })
        if self.error:
            raise self.error
        return self.status, self.body, {"x-source-version": "test-version"}


class FakeAdapter:
    def __init__(self, *, status: int = 200, body=None, error: UpstreamError | None = None):
        self.status = status
        self.body = body
        self.error = error
        self.calls: list[dict] = []

    async def request(self, method, path, *, params=None, inbound_request_id=None, **_kwargs):
        self.calls.append({
            "method": method,
            "path": path,
            "params": params,
            "request_id": inbound_request_id,
        })
        if self.error:
            raise self.error
        return self.status, self.body, {}


def make_request(query: str) -> Request:
    request = Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/test",
        "raw_path": b"/test",
        "query_string": query.encode("utf-8"),
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "state": {},
    })
    request.state.request_id = "test-request-id"
    return request


def bare_router(*, dashboard=None, adapter=None) -> Router:
    router = object.__new__(Router)
    router.dashboard = dashboard
    router.adapter = adapter
    return router


def response_json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_adapter_allowlist() -> None:
    accepted = [
        "/health",
        "/capabilities",
        "/kanban/tasks",
        "/kanban/tasks/task_123",
        "/kanban/tasks/task_123/events",
        "/sessions/session_123/timeline",
        "/sources/kanban/fingerprint",
        "/room-binding",
        "/room-sessions",
        # Per-thread card attribution: the join spans the kanban board
        # databases and state.db, so only the adapter can compute it.
        "/room-cards",
    ]
    rejected = [
        "/sessions/session_123/timeline/extra",
        "/room-cards/all",
        "/kanban/tasks/task_123/delete",
        "/../etc/passwd",
        "/sql",
    ]
    for path in accepted:
        assert is_allowed_adapter_path(path), f"expected adapter route to be accepted: {path}"
    for path in rejected:
        assert not is_allowed_adapter_path(path), f"expected adapter route to be rejected: {path}"


def test_envelope_split() -> None:
    data, meta = split_upstream_envelope({"data": [1, 2], "meta": {"source": "adapter"}})
    assert data == [1, 2]
    assert meta == {"source": "adapter"}

    body = {"sessions": [{"id": "s1"}], "total": 1}
    data, meta = split_upstream_envelope(body)
    assert data == body
    assert meta is None


def test_error_status() -> None:
    assert upstream_error_status(404) == 404
    assert upstream_error_status(503) == 503
    assert upstream_error_status(0) == 502
    assert upstream_error_status(200) == 502


async def test_dashboard_profile_forward_and_flatten() -> None:
    dashboard = FakeDashboard(body={
        "data": {"sessions": [{"id": "s1"}], "total": 1},
        "meta": {"source_id": "hermes-dashboard", "schema_fingerprint": "abc"},
    })
    router = bare_router(dashboard=dashboard)
    response = await Router.proxy_dashboard_read(
        router,
        make_request("profile=management&limit=5&offset=0"),
        "api/sessions",
    )
    assert response.status_code == 200
    assert dashboard.calls == [{
        "path": "/api/sessions",
        "params": {"profile": "management", "limit": "5", "offset": "0"},
        "request_id": "test-request-id",
    }]
    payload = response_json(response)
    assert payload["data"] == {"sessions": [{"id": "s1"}], "total": 1}
    assert not (isinstance(payload["data"], dict) and "data" in payload["data"] and "meta" in payload["data"])
    assert payload["meta"]["profile_id"] == "management"
    assert payload["meta"]["upstream_meta"]["source_id"] == "hermes-dashboard"


async def test_adapter_profile_is_provenance_not_filter() -> None:
    adapter = FakeAdapter(body={
        "data": {"tasks": [{"id": "t1"}]},
        "meta": {"source_id": "adapter"},
    })
    router = bare_router(adapter=adapter)
    response = await Router.proxy_adapter_read(
        router,
        make_request("profile=management&status=running&limit=10"),
        "kanban/tasks",
    )
    assert response.status_code == 200
    assert adapter.calls == [{
        "method": "GET",
        "path": "/kanban/tasks",
        "params": {"status": "running", "limit": "10"},
        "request_id": "test-request-id",
    }]
    payload = response_json(response)
    assert payload["data"] == {"tasks": [{"id": "t1"}]}
    assert payload["meta"]["profile_id"] == "management"


async def test_adapter_issues_defaults_limit() -> None:
    adapter = FakeAdapter(body={
        "data": {"issues": [{"id": "i1"}]},
        "meta": {"source_id": "adapter"},
    })
    router = bare_router(adapter=adapter)
    response = await Router.proxy_adapter_read(
        router,
        make_request("profile=management"),
        "issues",
    )
    assert response.status_code == 200
    assert adapter.calls == [{
        "method": "GET",
        "path": "/issues",
        "params": {"limit": "25"},
        "request_id": "test-request-id",
    }]

    response = await Router.proxy_adapter_read(
        router,
        make_request("profile=management&limit=20"),
        "issues",
    )
    assert adapter.calls[-1]["params"] == {"limit": "20"}


async def test_adapter_issues_limit_is_bounded() -> None:
    adapter = FakeAdapter(body={
        "data": {"issues": [{"id": "i1"}]},
        "meta": {"source_id": "adapter"},
    })
    router = bare_router(adapter=adapter)
    response = await Router.proxy_adapter_read(
        router,
        make_request("profile=management&limit=500"),
        "issues",
    )
    assert response.status_code == 200
    assert adapter.calls[-1]["params"] == {"limit": "100"}


async def test_upstream_failure_is_not_http_200() -> None:
    dashboard = FakeDashboard(error=UpstreamError(503, {}, "dashboard unavailable"))
    router = bare_router(dashboard=dashboard)
    response = await Router.proxy_dashboard_read(
        router,
        make_request("profile=default"),
        "api/sessions",
    )
    assert response.status_code == 503
    payload = response_json(response)
    assert payload["meta"]["freshness"] == "unavailable"
    assert payload["meta"]["degraded_reason"] == "upstream_error:503"


def test_read_allowlist_matches_full_dashboard_path() -> None:
    # The allowlist is written in dashboard terms, so it only ever matches a
    # path that still carries /api/. A bare suffix must not slip through.
    assert is_allowed_read_path("/api/skills")
    assert is_allowed_read_path("/api/skills/content")
    assert is_allowed_read_path("/api/model/info")
    assert not is_allowed_read_path("/skills")
    assert not is_allowed_read_path("/api/fs/list")
    assert not is_allowed_read_path("/api/credentials")


async def test_session_context_read_is_proxied_unchanged() -> None:
    # The chat tab's context-window gauge reads the dashboard's
    # GET /api/sessions/{id}/context. It rides the existing "/api/sessions"
    # read allowlist rather than a dedicated BFF route, so the contract worth
    # locking is: allowlisted, profile forwarded, and the Hermes breakdown
    # reaching the SPA verbatim under `data` (the panel reads
    # `categories`/`context_max` by name).
    assert is_allowed_read_path("/api/sessions/20260812_112040_def97a/context")

    breakdown = {
        "categories": [
            {"id": "conversation", "label": "Conversation", "tokens": 11030},
            {"id": "skills", "label": "Skills", "tokens": 12711},
        ],
        "context_max": 1000000,
        "context_used": 96411,
        "estimated_total": 96411,
        "model": "normal",
    }
    dashboard = FakeDashboard(body=breakdown)
    router = bare_router(dashboard=dashboard)
    response = await Router.proxy_upstream_read(
        router,
        make_request("profile=comfyui-worker"),
        "api/sessions/20260812_112040_def97a/context",
    )
    assert response.status_code == 200, response_json(response)
    payload = response_json(response)
    assert payload["data"] == breakdown
    assert payload["meta"]["read_only"] is True
    assert payload["meta"]["mutations_supported"] == []
    assert dashboard.calls[0]["path"] == "/api/sessions/20260812_112040_def97a/context"
    # Sessions belong to a profile; dropping the scope reads the wrong store.
    assert dashboard.calls[0]["params"].get("profile") == "comfyui-worker"


async def test_direct_read_reattaches_api_prefix() -> None:
    # /api/{path:path} strips the prefix, so proxy_dashboard_direct has to put
    # it back before the allowlist sees the path. Dropping it 404s every direct
    # dashboard read (skills, model/info, mcp/servers, memory, logs, ...).
    dashboard = FakeDashboard(body={"data": [{"name": "jarvis-report"}], "meta": {}})
    router = bare_router(dashboard=dashboard)
    response = await Router.proxy_dashboard_direct(
        router,
        make_request("profile=default"),
        "skills",
    )
    assert response.status_code == 200, response_json(response)
    assert dashboard.calls[0]["path"] == "/api/skills"


async def test_direct_read_rejects_non_allowlisted_path() -> None:
    dashboard = FakeDashboard(body={})
    router = bare_router(dashboard=dashboard)
    response = await Router.proxy_dashboard_direct(
        router,
        make_request(""),
        "credentials",
    )
    assert response.status_code == 404
    assert response_json(response)["error"]["message"] == "path not in read allowlist"
    assert dashboard.calls == []


def test_skill_writes_match_the_upstream_openapi_declaration() -> None:
    # Taken from the 9119 dashboard's own /openapi.json (read 2026-08-09). The
    # verbs matter: toggle is PUT, not POST, and removal lives under the hub.
    expected = {
        ("/api/skills/content", "PUT"): {"name", "content"},
        ("/api/skills/toggle", "PUT"): {"name", "enabled"},
        ("/api/skills/hub/uninstall", "POST"): {"name"},
    }
    for (path, method), body_keys in expected.items():
        matched = match_upstream_mutation(path, method)
        assert matched is not None, f"{method} {path} should be allowlisted"
        spec, _tokens = matched
        assert spec["upstream_path"] == path
        assert set(spec["body_keys_allow"]) == body_keys
        assert "profile" in spec["forward_query"]

    # Verbs the routes do not declare stay closed...
    assert match_upstream_mutation("/api/skills/toggle", "POST") is None
    assert match_upstream_mutation("/api/skills/content", "POST") is None
    assert match_upstream_mutation("/api/skills/hub/uninstall", "PUT") is None
    # ...and archive has no upstream route at all.
    for path in ("/api/skills/archive", "/api/skills/delete"):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert match_upstream_mutation(path, method) is None


def test_mutation_route_accepts_every_allowlisted_verb() -> None:
    # A verb declared in the specs but missing from the route registration is
    # answered by FastAPI with a bare 405 before the allowlist ever runs, so the
    # capability looks supported to the SPA and fails at click time.
    declared = {m for spec in UPSTREAM_MUTATION_SPECS.values() for m in spec["methods"]}
    assert declared <= set(UPSTREAM_MUTATION_METHODS)
    assert "PUT" in UPSTREAM_MUTATION_METHODS
    assert "GET" not in UPSTREAM_MUTATION_METHODS  # reads keep their own route


async def test_skill_read_advertises_write_capabilities() -> None:
    # The UI enables controls from meta.mutations_supported, so the read has to
    # declare exactly what the BFF will forward.
    dashboard = FakeDashboard(body=[{"name": "jarvis-report", "enabled": True}])
    router = bare_router(dashboard=dashboard)
    response = await Router.proxy_dashboard_direct(
        router, make_request("profile=default"), "skills",
    )
    meta = response_json(response)["meta"]
    assert meta["mutations_supported"] == ["save", "enable", "disable", "delete"]
    assert meta["read_only"] is False
    # archive is deliberately absent: upstream declares no route for it.
    assert "archive" not in READ_PATH_MUTATIONS["/api/skills"]


async def test_unadvertised_read_stays_read_only() -> None:
    # /api/logs has no READ_PATH_MUTATIONS entry and no write route anywhere in
    # the BFF, so its envelope must keep saying so.
    dashboard = FakeDashboard(body={"logs": []})
    router = bare_router(dashboard=dashboard)
    response = await Router.proxy_dashboard_direct(
        router, make_request(""), "logs",
    )
    meta = response_json(response)["meta"]
    assert meta["mutations_supported"] == []
    assert meta["read_only"] is True


async def test_session_read_advertises_its_real_writes() -> None:
    """Sessions carry write routes, so the read must stop claiming read-only.

    MUTATION_ALLOWLIST (gateway) and UPSTREAM_MUTATION_SPECS (dashboard) both
    declare session writes, but READ_PATH_MUTATIONS had no /api/sessions key —
    so every session read advertised `read_only: true, mutations_supported: []`
    and the chat UI had no honest signal to gate rename/fork/model-lock on.
    """
    dashboard = FakeDashboard(body={"sessions": []})
    router = bare_router(dashboard=dashboard)
    response = await Router.proxy_dashboard_direct(
        router, make_request(""), "sessions",
    )
    meta = response_json(response)["meta"]
    assert meta["read_only"] is False
    for action in ("chat", "rename", "fork", "model_lock", "stop", "delete"):
        assert action in meta["mutations_supported"], action
    # Each advertised action has to correspond to a real allowlisted route.
    assert "session_fork" in MUTATION_ALLOWLIST
    assert "session_model_lock" in MUTATION_ALLOWLIST
    assert "run_stop" in MUTATION_ALLOWLIST


async def test_chat_relay_forwards_bytes_as_they_arrive() -> None:
    """A partial frame must leave the BFF immediately, not wait for its blank line.

    The old relay reassembled lines and only yielded on a frame boundary, so a
    turn whose terminator was delayed sat in memory and landed in one lump after
    the stream closed — the "nothing appears until I refresh" symptom. This
    pins the passthrough: whatever httpx hands over goes straight out.
    """
    from agent_mission_control import chat_proxy

    chunks = [
        b"event: run.started\ndata: {\"run_id\": \"run_1\"}\n\n",
        b"event: assistant.delta\nda",           # split mid-field on purpose
        b"ta: {\"delta\": \"hel\"}\n\n",
        b": keepalive\n\n",
    ]

    class FakeStreamResponse:
        def __init__(self):
            self.closed = False

        async def aiter_bytes(self):
            for chunk in chunks:
                yield chunk

        async def aclose(self):
            self.closed = True

    resp = FakeStreamResponse()
    seen = [frame async for frame in chat_proxy.iter_forwarded_frames(resp, "rid")]
    assert seen == chunks, "relay must not reassemble or reorder the body"
    assert resp.closed is True


async def test_chat_relay_closes_upstream_when_the_client_hangs_up() -> None:
    """Abandoning the generator has to close upstream — that is the Stop button.

    The gateway cancels the agent task on ConnectionResetError from its SSE
    write (api_server.py `_handle_session_chat_stream`). That only happens if
    the BFF actually drops the upstream connection, which the previous relay
    never did: it closed after the loop, so an early exit leaked the run.
    """
    from agent_mission_control import chat_proxy

    class NeverEndingResponse:
        def __init__(self):
            self.closed = False

        async def aiter_bytes(self):
            while True:
                yield b"event: assistant.delta\ndata: {\"delta\": \"x\"}\n\n"
                await asyncio.sleep(0)

        async def aclose(self):
            self.closed = True

    resp = NeverEndingResponse()
    stream = chat_proxy.iter_forwarded_frames(resp, "rid")
    assert await stream.__anext__()
    await stream.aclose()  # what Starlette does when the client disconnects
    assert resp.closed is True, "upstream run would keep burning tokens"


def test_created_session_id_reads_the_nested_gateway_shape() -> None:
    """Live-verified: create answers {"object":..., "session":{"id":...}}.

    Reading only the top level produced an empty id, so the bus event for a new
    session carried entity_id "" and no subscriber could act on it.
    """
    assert Router._created_session_id(  # noqa: SLF001
        {"object": "hermes.session", "session": {"id": "api_1", "source": "api_server"}}
    ) == "api_1"
    # Older/flat gateways still resolve.
    assert Router._created_session_id({"id": "flat"}) == "flat"  # noqa: SLF001
    assert Router._created_session_id({"session_key": "k"}) == "k"  # noqa: SLF001
    assert Router._created_session_id({"session": {}}) == ""  # noqa: SLF001
    assert Router._created_session_id("not a dict") == ""  # noqa: SLF001


def test_chat_open_frame_is_a_wellformed_named_sse_event() -> None:
    from agent_mission_control import chat_proxy

    frame = chat_proxy.open_frame("rid-1", "upstream-9")
    assert frame.startswith("event: bff.open\ndata: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload == {"request_id": "rid-1", "upstream_request_id": "upstream-9"}


def test_gateway_read_allowlist_is_exact_not_prefix() -> None:
    """The gateway serves run dispatch on /v1 too — a prefix match would leak it."""
    assert "/v1/toolsets" in GATEWAY_READ_PATHS
    assert "/v1/skills" in GATEWAY_READ_PATHS
    assert "/v1/capabilities" in GATEWAY_READ_PATHS
    for forbidden in ("/v1/runs", "/v1/chat/completions", "/api/sessions",
                      "/v1/toolsets/../runs"):
        assert forbidden not in GATEWAY_READ_PATHS


def test_verb_suffixed_mutations_do_not_substitute_the_verb_for_the_id() -> None:
    """`{id}` is the last path segment only for the flat session routes.

    /api/sessions/{id}/fork, /api/sessions/{id}/model and /v1/runs/{id}/stop all
    end in a verb, so the generic `route.format(id=path.rsplit('/')[-1])` would
    have produced /api/sessions/fork/fork. Their wrappers pass the resolved
    upstream path instead — this test pins that they must.
    """
    for action in ("chat_send", "session_fork", "session_model_lock", "run_stop"):
        route = MUTATION_ALLOWLIST[action]["route"]
        assert not route.endswith("{id}"), (
            f"{action} has a verb suffix; its wrapper must pass upstream_path"
        )


async def test_alert_acknowledge_publishes_alert_changed() -> None:
    """acknowledge() publishes alert.changed to the bus; KeyError still raises."""
    from agent_mission_control import alerts as alerts_mod

    published: list[dict] = []

    class FakeBus:
        async def publish(self, event_type, source_id, entity_type="", entity_id="",
                          payload=None, coverage="polled", profile_id="", event_id=None):
            published.append({
                "event_type": event_type, "source_id": source_id,
                "entity_id": entity_id, "payload": payload,
            })
            return {"event_id": "e1"}

    class FakeStore:
        def acknowledge_alert(self, alert_id, action, expires_at=None) -> None:
            pass

    engine = alerts_mod.AlertEngine(FakeStore(), object(), bus=FakeBus())
    engine.alerts["a1"] = {"rule_id": "R1", "severity": "critical",
                           "source_id": "health", "entity_type": "source",
                           "entity_id": "gateway", "reason": "down",
                           "first_seen_at": 1, "last_seen_at": 1}

    result = await engine.acknowledge("a1", alerts_mod.ACK)
    assert result["state"] == alerts_mod.ACK
    assert len(published) == 1
    assert published[0]["event_type"] == "alert.changed"
    assert published[0]["entity_id"] == "a1"
    assert published[0]["payload"] == {"state": alerts_mod.ACK}

    try:
        await engine.acknowledge("missing", alerts_mod.ACK)
        raise AssertionError("expected KeyError for unknown alert")
    except KeyError:
        pass
    assert len(published) == 1  # failed ack publishes nothing


async def test_event_bus_cache_invalidated_emits_only_on_drop() -> None:
    """cache.invalidated runs the cache invalidation and only emits when
    entries were actually dropped; the derived event carries the count."""
    from agent_mission_control.event_bus import EventBus

    class DropCache:
        def __init__(self) -> None:
            self.calls = 0

        def invalidate(self, source_id=None, key=None) -> int:
            self.calls += 1
            return 1  # something was dropped

    class FakeStore:
        def insert_event_replay(self, *_args, **_kwargs) -> bool:
            return True

        def replay_last_event_id(self) -> str:
            return ""

    bus = EventBus(FakeStore(), cache=DropCache())
    ev = await bus.publish("cache.invalidated", "adapter", "schema", "adapter",
                           {"fingerprint": "fp"}, coverage="derived")
    assert ev is not None
    assert ev["coverage"] == "derived"
    assert ev["payload"]["removed"] == 1
    assert bus.cache.calls == 1

    class NoDropCache:
        def invalidate(self, source_id=None, key=None) -> int:
            return 0  # nothing cached for this source

    bus2 = EventBus(FakeStore(), cache=NoDropCache())
    ev2 = await bus2.publish("cache.invalidated", "adapter")
    assert ev2 is None  # no entries dropped -> no event


def test_redaction_regex_matches_the_frontend_copy() -> None:
    """The BFF and the SPA must mask the same key shapes.

    The pattern is necessarily duplicated (no build step shares a literal
    between Python and the zero-build ES modules), so pin them together: a
    drift here silently reopens the credential leak on /api/config.
    """
    js = (APP_ROOT / "frontend/dist/tabs/settings.js").read_text(encoding="utf-8")
    marker = "const REDACTED_KEYS = /"
    start = js.index(marker) + len(marker)
    js_pattern = js[start:js.index("/i;", start)]
    assert js_pattern == redact_mod.REDACTED_KEY_PATTERN, (
        f"frontend regex {js_pattern!r} != backend {redact_mod.REDACTED_KEY_PATTERN!r}"
    )


def test_redaction_masks_nested_secrets_and_detects_the_sentinel() -> None:
    raw = {
        "providers": {"9router": {"api_key": "sk-live-real", "base_url": "http://x"}},
        "items": [{"auth_token": "t"}, {"plain": "keep"}],
        "agent": {"disabled_toolsets": ["image_gen"]},
    }
    out = redact_mod.redact_config(raw)
    assert out["providers"]["9router"]["api_key"] == redact_mod.REDACTED_SENTINEL
    assert out["providers"]["9router"]["base_url"] == "http://x"
    assert out["items"][0]["auth_token"] == redact_mod.REDACTED_SENTINEL
    assert out["items"][1]["plain"] == "keep"
    assert out["agent"]["disabled_toolsets"] == ["image_gen"]
    # The original is untouched: redaction must not mutate the upstream payload.
    assert raw["providers"]["9router"]["api_key"] == "sk-live-real"
    assert redact_mod.contains_redacted_sentinel(out) is True
    assert redact_mod.contains_redacted_sentinel(raw) is False


async def test_config_read_is_redacted_server_side() -> None:
    """A masked value must never leave the BFF for /api/config."""
    secret = "sk-live-must-not-ship"
    dashboard = FakeDashboard(body={"providers": {"p": {"api_key": secret}}})
    response = await Router.proxy_dashboard_read(
        bare_router(dashboard=dashboard), make_request(""), "api/config"
    )
    payload = response_json(response)
    assert secret not in json.dumps(payload)
    assert payload["data"]["providers"]["p"]["api_key"] == redact_mod.REDACTED_SENTINEL


def test_path_tokens_cannot_span_a_slash() -> None:
    """A {token} matches one segment, so no spec can become a path proxy."""
    assert match_upstream_mutation("/api/cron/jobs/a/b", "DELETE") is None
    assert match_upstream_mutation("/api/profiles/a/b", "DELETE") is None
    assert match_upstream_mutation("/api/mcp/servers/a/b", "DELETE") is None
    # …and a real single-segment id still resolves.
    matched = match_upstream_mutation("/api/mcp/servers/ctx7", "DELETE")
    assert matched is not None
    spec, tokens = matched
    assert spec["upstream_path"].format(**tokens) == "/api/mcp/servers/ctx7"


def test_literal_specs_win_over_token_specs() -> None:
    """"active" is a route, not a profile name — declaration order guarantees it."""
    spec, _ = match_upstream_mutation("/api/profiles/active", "POST")
    assert spec["summary"] == "upstream.profile.activate"


def test_per_verb_upstream_method_translation() -> None:
    """One path, two verbs: PATCH is rewritten to PUT, DELETE passes through."""
    patch_spec, _ = match_upstream_mutation("/api/cron/jobs/j1", "PATCH")
    assert resolve_upstream_method(patch_spec, "PATCH") == "PUT"
    assert resolve_upstream_method(patch_spec, "DELETE") == "DELETE"
    # The legacy single-value form still works for specs that use it.
    assert resolve_upstream_method({"upstream_method": "PUT"}, "PATCH") == "PUT"
    assert resolve_upstream_method({}, "POST") == "POST"


def test_destructive_specs_are_confirm_gated() -> None:
    """Every irreversible upstream write demands an explicit confirm."""
    must_confirm = [
        ("/api/cron/jobs/j1", "DELETE"),
        ("/api/profiles/p1", "DELETE"),
        ("/api/mcp/servers/m1", "DELETE"),
        ("/api/webhooks/w1", "DELETE"),
        ("/api/memory/reset", "POST"),
        ("/api/gateway/restart", "POST"),
        ("/api/gateway/stop", "POST"),
        ("/api/gateway/start", "POST"),
        ("/api/gateway/drain", "POST"),
        ("/api/ops/hooks", "DELETE"),
        ("/api/ops/checkpoints/prune", "POST"),
    ]
    for path, method in must_confirm:
        matched = match_upstream_mutation(path, method)
        assert matched is not None, f"{method} {path} is not allowlisted"
        gate = matched[0].get("require_confirm")
        covered = gate is True or (
            isinstance(gate, (tuple, list, set, frozenset)) and method in gate)
        assert covered, f"{method} {path} is destructive but not confirm-gated"

    # A non-destructive verb on a shared path must NOT be gated, or every edit
    # would need a confirm dialog.
    edit_spec, _ = match_upstream_mutation("/api/cron/jobs/j1", "PATCH")
    assert "PATCH" not in edit_spec["require_confirm"]


def test_cron_update_uses_the_upstream_nested_body() -> None:
    """Upstream CronJobUpdate is {"updates": {...}}; flat fields 422 there."""
    spec, _ = match_upstream_mutation("/api/cron/jobs/j1", "PATCH")
    assert spec["body_keys_allow"] == ("updates",)
    inner = spec["body_nested_keys_allow"]["updates"]
    assert "schedule" in inner and "prompt" in inner
    # Pause/resume are real upstream routes, not a faked state field.
    assert match_upstream_mutation("/api/cron/jobs/j1/pause", "POST") is not None
    assert match_upstream_mutation("/api/cron/jobs/j1/resume", "POST") is not None
    assert "state" not in inner


def test_credential_carrying_specs_reject_the_sentinel() -> None:
    """Env/config writes must refuse a body still holding the read-time mask."""
    for path, method in [
        ("/api/tools/toolsets/web/env", "PUT"),
        ("/api/memory/providers/mem0/config", "PUT"),
        ("/api/messaging/platforms/telegram", "PUT"),
    ]:
        spec, _ = match_upstream_mutation(path, method)
        assert spec.get("reject_sentinel") is True, f"{method} {path} unguarded"


def test_config_write_tree_excludes_every_credential_branch() -> None:
    """The writable scope is a few known Telegram/agent branches — no secrets.

    `group_topics` appears twice on purpose: the gateway's own
    `toolset_policy._topic_extra` prefers platforms.telegram.extra when that
    dict carries the key and otherwise falls back to the legacy top-level
    telegram.extra, so both have to be writable or a save lands in a shadow
    copy the gateway never reads.
    """
    assert sorted(_describe_allow_tree(CONFIG_WRITE_ALLOW_TREE)) == [
        "agent.disabled_toolsets",
        "platforms.telegram.channel_overrides",
        "platforms.telegram.extra.group_topics",
        "telegram.extra.group_topics",
    ]
    # channel_overrides is a per-thread dict, so a single-thread edit prunes to
    # exactly that thread and cannot disturb its neighbours.
    assert _prune_to_allow_tree(
        {"platforms": {"telegram": {"channel_overrides": {"1497": {"system_prompt": "x"}}}}},
        CONFIG_WRITE_ALLOW_TREE,
    ) == {"platforms": {"telegram": {"channel_overrides": {"1497": {"system_prompt": "x"}}}}}
    # The legacy branch is writable, but only that one key under it.
    assert _prune_to_allow_tree(
        {"telegram": {"extra": {"group_topics": [], "room_slots": [{"slot": 1}]}}},
        CONFIG_WRITE_ALLOW_TREE,
    ) == {"telegram": {"extra": {"group_topics": []}}}
    # Anything outside the tree is dropped rather than forwarded.
    assert _prune_to_allow_tree(
        {"providers": {"9router": {"api_key": "sk-live"}}}, CONFIG_WRITE_ALLOW_TREE
    ) == {}
    assert _prune_to_allow_tree(
        {"agent": {"disabled_toolsets": ["image_gen"], "model": "override"}},
        CONFIG_WRITE_ALLOW_TREE,
    ) == {"agent": {"disabled_toolsets": ["image_gen"]}}
    # A named-but-empty branch collapses so the upstream merge stays a no-op.
    assert _prune_to_allow_tree({"agent": {"model": "x"}}, CONFIG_WRITE_ALLOW_TREE) == {}
    # PUT /api/config/raw is never allowlisted: it would carry sentinels back.
    assert match_upstream_mutation("/api/config/raw", "PUT") is None
    assert match_upstream_mutation("/api/config", "PUT") is None


def test_new_write_surfaces_are_advertised_to_the_ui() -> None:
    """meta.mutations_supported is the UI's gate, so it must not drift."""
    for read_path in READ_PATH_MUTATIONS:
        assert is_allowed_read_path(read_path), f"{read_path} advertised but unreadable"
    for group in ("/api/cron/jobs", "/api/profiles", "/api/mcp/servers",
                  "/api/tools/toolsets", "/api/webhooks", "/api/memory",
                  "/api/dashboard/agent-plugins", "/api/messaging/platforms"):
        assert READ_PATH_MUTATIONS.get(group), f"{group} has no advertised writes"


def test_plugin_toggles_flag_the_restart_requirement() -> None:
    """Toggling writes config.yaml but does not reload the live gateway."""
    for action in ("enable", "disable"):
        spec, _ = match_upstream_mutation(
            f"/api/dashboard/agent-plugins/permits/{action}", "POST")
        assert spec["response_meta"]["restart_required"] is True


async def test_preferences_are_profile_scoped_and_key_gated() -> None:
    """Shell prefs overlay global with profile, and reject unknown keys."""
    import tempfile

    from agent_mission_control.store import Store

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "prefs.db")
        store.set_preference("density", "comfortable", None)
        store.set_preference("theme", "deck", None)
        store.set_preference("density", "compact", "work")

        assert store.list_preferences(None) == {"density": "comfortable", "theme": "deck"}
        # Profile value wins over the global one; unscoped keys still show up.
        assert store.list_preferences("work") == {"density": "compact", "theme": "deck"}

        router = bare_router()
        router.store = store
        response = await Router.preferences_read(router, make_request("profile=work"))
        payload = response_json(response)
        assert payload["data"]["density"] == "compact"
        assert payload["meta"]["source_id"] == "local-store"

    # The write gate is an allowlist, so a credential-shaped key cannot be
    # smuggled into the local store and echoed back to every client.
    assert "api_key" not in Router.PREFERENCE_KEYS
    assert "density" in Router.PREFERENCE_KEYS


async def test_session_persona_stores_only_the_profile_name() -> None:
    """The one fact upstream cannot keep, and nothing else, survives locally."""
    import tempfile

    from agent_mission_control.session_persona_store import SessionPersonaStore

    with tempfile.TemporaryDirectory() as tmp:
        store = SessionPersonaStore(Path(tmp) / "store.db")
        router = bare_router()
        router.dashboard_store = store
        router._require_session = lambda request: {"id": "sess"}
        router._guard_mutation = lambda request: {"id": "sess"}

        # Unknown session: a null name, not a 404 — a session simply may not
        # have been started from another profile's persona.
        response = await Router.session_persona_read(router, make_request(""), "s1")
        assert response_json(response)["data"]["profile_name"] is None

        request = make_request("profile=work")
        request._body = json.dumps({"profile_name": "jarvis"}).encode("utf-8")
        response = await Router.session_persona_write(router, request, "s1")
        assert response_json(response)["data"]["profile_name"] == "jarvis"

        response = await Router.session_persona_read(router, make_request(""), "s1")
        payload = response_json(response)
        assert payload["data"]["profile_name"] == "jarvis"
        assert payload["meta"]["source_id"] == "local-store"

        # A name is the whole contract; anything else is a client bug, not a row.
        request = make_request("")
        request._body = json.dumps({"profile_name": ""}).encode("utf-8")
        response = await Router.session_persona_write(router, request, "s2")
        assert response.status_code == 400

        # The table holds a pointer, never a copy of what Hermes already serves.
        columns = {
            row[1] for row in
            store._conn.execute("PRAGMA table_info(session_persona)").fetchall()
        }
        assert columns == {"session_id", "profile_name", "created_at"}
        store.close()


async def test_events_recent_is_bounded_and_separate_from_the_audit_ledger() -> None:
    """Activity reads the event bus; Action Audit reads operator mutations."""
    import tempfile

    from agent_mission_control.store import Store

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "events.db")
        for index in range(12):
            store.insert_event_replay(
                event_id=f"e{index}", event_type="task.changed",
                # epoch seconds, exactly what EventBus.publish writes.
                occurred_at=1786400000 + index, source_id="kanban",
                entity_type="task", entity_id=f"t{index}",
                payload={"status": "running"}, coverage="native",
            )

        router = bare_router()
        router.store = store

        response = await Router.events_recent_endpoint(router, make_request("limit=5"))
        payload = response_json(response)
        assert payload["meta"]["source_id"] == "event-bus"
        assert payload["meta"]["read_only"] is True
        assert payload["data"]["total"] == 12
        # Newest-last ordering is the replay contract, and the slice is the
        # tail, not the head — a client asking for 5 wants the recent 5.
        events = payload["data"]["events"]
        assert len(events) == 5
        assert [e["event_id"] for e in events] == ["e7", "e8", "e9", "e10", "e11"]
        assert events[0]["payload"] == {"status": "running"}

        # An oversized or garbage limit clamps before it reaches SQL. Asserting
        # on the returned row count would pass either way with only 12 rows in
        # the table, so the bound is observed where it is actually applied.
        seen: list[int] = []
        real = store.replay_latest
        store.replay_latest = lambda limit=2000: (seen.append(limit), real(limit=limit))[1]

        await Router.events_recent_endpoint(router, make_request("limit=99999"))
        await Router.events_recent_endpoint(router, make_request("limit=abc"))
        await Router.events_recent_endpoint(router, make_request("limit=0"))
        await Router.events_recent_endpoint(router, make_request(""))
        assert seen == [500, 200, 1, 200], seen
        store.replay_latest = real


async def test_run_inspector_returns_real_edges_not_an_empty_graph() -> None:
    """Both engines were built with providers={} and always answered empty."""
    from agent_mission_control.correlation_providers import build_correlation_providers
    from agent_mission_control.correlation import CorrelationEngine
    from agent_mission_control.run_inspector import RunInspector

    class Adapter:
        async def kanban_task_detail(self, task_id, request_id=None):
            return 200, {"task": {"id": task_id, "session_id": "s-1",
                                  "assignee": "coder", "current_run_id": 7}}, {}

        async def kanban_task_runs(self, task_id, request_id=None):
            return 200, {"runs": [{"id": 7, "status": "done", "outcome": "ok",
                                   "started_at": "2026-08-10T01:00:00Z"}]}, {}

        async def kanban_task_events(self, task_id, params=None, request_id=None):
            return 200, {"events": [{"kind": "moved", "created_at": "2026-08-10T00:00:00Z"}]}, {}

        async def kanban_task_attachments(self, task_id, request_id=None):
            return 200, {"attachments": [{"id": 3, "filename": "log.txt"}]}, {}

        async def issues_list(self, limit=100, **params):
            return 200, {"issues": [
                {"id": 148, "occurrences": [{"task_ref": "t-1", "event_type": "observed"}]},
            ]}, {}

        async def permits_list(self, limit=100, **params):
            return 200, {"permits": [{"id": "p-1", "issue_title": "Issue 148",
                                      "status": "pending_approval"}]}, {}

        async def issue_detail(self, issue_id, params=None, request_id=None):
            return 200, {"issue": {"id": issue_id, "occurrences": []}}, {}

    class Dash:
        async def get(self, path, *, params=None, inbound_request_id=None):
            return 200, {"session": {"id": "s-1", "thread_id": "th-9"}}, {}

    providers = build_correlation_providers(Adapter(), Dash())
    engine = CorrelationEngine(providers=providers)
    inspector = RunInspector(engine, providers=providers)

    result = await inspector.inspect_task("t-1")
    edges = result["tree"]["edges"]
    assert edges, "task inspection still produces an empty graph"
    assert result["tree"]["coverage"] == "complete"
    pairs = {(e["source_type"], e["target_type"]) for e in edges}
    assert ("task", "session") in pairs
    assert ("artifact", "task") in pairs
    assert ("issue", "task") in pairs
    # The trajectory merges events and runs, sorted, from the same providers.
    kinds = [item["kind"] for item in result["trajectory"]]
    assert "moved" in kinds and "run" in kinds

    # Permit->issue is the free-text "Issue N" parse, so it stays `inferred`.
    issue_graph = await engine.correlate("issue", "148")
    permit_edges = [e for e in issue_graph["edges"] if e["source_type"] == "permit"]
    assert permit_edges and permit_edges[0]["kind"] == "inferred"

    # A down source degrades to "no data", never a 500, and stays quiet.
    class Down(Adapter):
        async def kanban_task_detail(self, task_id, request_id=None):
            raise UpstreamError(503, None, "adapter down")

    down = CorrelationEngine(providers=build_correlation_providers(Down(), Dash()))
    assert (await down.correlate("task", "t-1"))["coverage"] == "unsupported"

    # An unexpected exception also degrades, but must be logged rather than
    # swallowed — silence here is what hid the broken `request_id` keyword.
    class Broken(Adapter):
        async def kanban_task_detail(self, task_id, request_id=None):
            raise TypeError("bug in this file, not a source outage")

    import logging as _logging

    records: list[_logging.LogRecord] = []

    class _Capture(_logging.Handler):
        def emit(self, record):
            records.append(record)

    provider_log = _logging.getLogger("agent_mission_control.correlation_providers")
    handler = _Capture()
    provider_log.addHandler(handler)
    previous = provider_log.propagate
    provider_log.propagate = False
    try:
        broken = CorrelationEngine(providers=build_correlation_providers(Broken(), Dash()))
        assert (await broken.correlate("task", "t-1"))["coverage"] == "unsupported"
    finally:
        provider_log.removeHandler(handler)
        provider_log.propagate = previous
    assert records, "an unexpected provider failure was swallowed without a log"


def test_alert_rules_receive_the_data_they_evaluate() -> None:
    """11 rules were implemented but only "health" was ever fed."""
    import inspect as _inspect

    from agent_mission_control.workers import SourceWorkers

    fed = {
        name
        for _, src in [("", _inspect.getsource(SourceWorkers))]
        for name in re.findall(r'_feed_alerts\(\s*"([a-z_]+)"', src)
    }
    # Every source_data key the rules read must have a writer.
    for key in ("health", "capabilities", "cron", "tasks", "permits", "issues",
                "analytics"):
        assert key in fed, f"no worker feeds alert source_data[{key!r}]"

    # The tick loop must not write "health" too — two writers with different
    # key sets silently erased each other and made R1 flap.
    from agent_mission_control import app as app_mod

    tick_src = _inspect.getsource(app_mod._alert_tick_loop)
    assert 'set_source_data("health"' not in tick_src
    assert 'set_source_data(\n' in tick_src or 'set_source_data(' in tick_src
    assert "freshness" in tick_src, "tick loop must still supply the R3 feed"
    assert not hasattr(app_mod, "_snapshot_health"), \
        "the conflicting health snapshot should be gone, not just unused"


async def test_poll_workers_record_success_time_not_delta_time() -> None:
    """R3 asks "when did this source last answer", not "when did it change"."""
    from agent_mission_control.workers import PollWorker

    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return {"stable": True}

    async def on_delta(_data, _fp):
        return None

    worker = PollWorker("t", 1, fetch, on_delta, lambda d: "same")

    await worker.tick_once()
    first = worker.last_success_at
    assert first is not None
    await worker.tick_once()
    # The second tick produced no delta, but the source did answer — a stable
    # source must not be reported stale.
    assert worker.last_success_at is not None and worker.last_success_at >= first
    assert calls["n"] == 2


async def test_chat_stream_forwards_the_model_lock_flag() -> None:
    """The composer's model pick only runs if `require_model_lock` reaches the
    gateway.

    The gateway ranks a per-request `model` BELOW the model persisted on the
    session row and logs "request selection skipped: session-persisted model
    wins". A turn sent from the dashboard with `model: "normal"` was verified
    live to have been billed against `local`, with nothing in the UI saying so.
    `require_model_lock` is the documented way past that, so dropping it at the
    BFF silently restores the bug.
    """
    from agent_mission_control import chat_proxy

    router = bare_router()
    router.store = type("S", (), {
        "append_audit": lambda self, **kw: None,
        "update_audit_result": lambda self, **kw: None,
    })()
    router.mutation_limiter = type("L", (), {"allow": lambda self, _k: True})()
    router.gateway = object()
    router._require_session = lambda request: {"id": "sess"}
    router._require_csrf = lambda request, session: None
    router._request_profile = lambda request: "default"
    router._record_audit_result = lambda *a, **k: None

    seen: dict = {}

    async def fake_stream_chat(gateway, session_id, body):
        seen["body"] = body
        raise chat_proxy.UpstreamError(409, "model_lock_unavailable")

    original = chat_proxy.stream_chat
    chat_proxy.stream_chat = fake_stream_chat
    try:
        sent = {
            "session_id": "s1", "message": "hi", "model": "normal",
            "provider": "9router", "model_options": {"reasoning": {"effort": "xhigh"}},
            "require_model_lock": True,
            "not_allowed": "drop me",
        }

        async def receive():
            return {"type": "http.request", "body": json.dumps(sent).encode(), "more_body": False}

        request = Request(
            {"type": "http", "method": "POST", "path": "/api/chat/stream", "headers": []},
            receive,
        )
        request.state.request_id = "rid-1"
        await router.chat_stream(request)
    finally:
        chat_proxy.stream_chat = original

    body = seen["body"]
    assert body["require_model_lock"] is True, "the lock flag must reach the gateway"
    assert body["model"] == "normal" and body["provider"] == "9router"
    assert body["model_options"] == {"reasoning": {"effort": "xhigh"}}
    # Still an allowlist, not a passthrough.
    assert "not_allowed" not in body
    assert "session_id" not in body, "the id travels in the path, not the body"


async def test_session_changed_names_the_session_that_moved() -> None:
    """A subscriber watching ONE conversation has to be able to tell whether
    the session in front of it is the one that advanced.

    The poller used to publish a single list-level event with an empty
    `entity_id`, so the chat tab — which filters on that id — discarded every
    event the poller ever sent. A turn driven from Telegram, cron or the CLI
    therefore never reached an open thread, and the only way to see it was to
    refresh. The list-level event stays (tabs that render the whole list want
    it); the targeted ones are what make the topic usable per session.
    """
    from agent_mission_control.workers import SourceWorkers

    published: list[tuple] = []

    class FakeBus:
        async def publish(self, event_type, source_id, entity_type, entity_id,
                          payload, coverage=None):
            published.append((event_type, entity_id, payload))

    workers = SourceWorkers.__new__(SourceWorkers)
    workers.bus = FakeBus()
    workers._sessions_by_id = {}

    first = [
        {"id": "s1", "last_activity_at": 100, "message_count": 4},
        {"id": "s2", "last_activity_at": 100, "message_count": 9},
    ]
    await workers._on_sessions(first, None)
    # Nothing to compare against yet: only the list-level event, or every
    # session on the fleet would look like it just moved.
    assert [e[1] for e in published] == [""]

    published.clear()
    await workers._on_sessions(
        [
            {"id": "s1", "last_activity_at": 100, "message_count": 4},
            {"id": "s2", "last_activity_at": 180, "message_count": 11},
        ],
        None,
    )
    targeted = [e for e in published if e[1]]
    assert [e[1] for e in targeted] == ["s2"], "only the session that moved is named"
    assert targeted[0][2]["message_count"] == 11
    assert "" in [e[1] for e in published], "the list-level event still fires"


async def test_session_changed_burst_is_capped() -> None:
    """A cron sweep touching hundreds of sessions must not flood the bus; the
    list-level event still covers whatever the cap drops."""
    from agent_mission_control.workers import SourceWorkers

    published: list[str] = []

    class FakeBus:
        async def publish(self, event_type, source_id, entity_type, entity_id,
                          payload, coverage=None):
            published.append(entity_id)

    workers = SourceWorkers.__new__(SourceWorkers)
    workers.bus = FakeBus()
    workers._sessions_by_id = {f"s{i}": (0, 0, None) for i in range(200)}

    await workers._on_sessions(
        [{"id": f"s{i}", "last_activity_at": 5, "message_count": 1} for i in range(200)],
        None,
    )
    assert len([e for e in published if e]) == 25
    assert "" in published


def test_decision_writes_stay_off_the_get_only_adapter_proxy() -> None:
    """The adapter proxy is GET-only; decisions must use their own handlers."""
    assert is_allowed_adapter_path("/permits/p-1/decision") is False
    assert is_allowed_adapter_path("/issues/1/update") is False
    # The reads they sit next to are still proxied.
    assert is_allowed_adapter_path("/permits/p-1") is True
    assert is_allowed_adapter_path("/issues/1") is True


def test_issue_transition_rules_match_the_upstream_script() -> None:
    """The form validates locally so it can explain a rejection instantly."""
    validate = Router._validate_issue_update
    router = bare_router()

    assert validate(router, {"status": "bogus"}).startswith("status must be one of")
    assert validate(router, {"event_type": "nope"}).startswith("event_type must be one of")
    # resolved needs both fields; dismissed needs a reason; merged needs a target.
    assert validate(router, {"status": "resolved", "resolution": "fixed"})
    assert validate(router, {"status": "resolved", "verification": "checked"})
    assert validate(router, {"status": "dismissed"})
    assert validate(router, {"status": "merged"})
    # Valid transitions pass.
    assert validate(router, {
        "status": "resolved", "resolution": "fixed", "verification": "rerun green"}) is None
    assert validate(router, {"status": "merged", "merge_into_id": 12}) is None
    assert validate(router, {"event_type": "recurred"}) is None


def test_issue_delete_rides_the_update_path() -> None:
    """Deletion is one more `issue_update` transition (`delete: true`), not a
    second capability — Hermes agents only ever call the one `issue_update`
    tool. It is soft (deleted_at/deleted_reason, the row is kept) but still
    needs a reason, same rule as every other terminal status."""
    validate = Router._validate_issue_update
    router = bare_router()

    assert validate(router, {"delete": True}) == "reason is required"
    assert validate(router, {"delete": True, "reason": "   "}) == "reason is required"
    assert validate(router, {"delete": True, "reason": 12}) == "reason is required"
    assert validate(router, {"delete": True, "reason": "duplicate of issue 82"}) is None
    # A delete does not need to also satisfy the status-transition rules.
    assert validate(router, {"delete": True, "reason": "dup", "status": "bogus"}) is None


def test_decision_field_allowlists_are_closed() -> None:
    """A caller cannot introduce a field the adapter would forward blindly."""
    assert "delete" in Router.PERMIT_DECISION_FIELDS
    assert "evil" not in Router.PERMIT_DECISION_FIELDS
    # The issue set matches the upstream update payload exactly, including the
    # delete/reason pair folded in from the removed issue_delete tool.
    assert Router.ISSUE_UPDATE_FIELDS == frozenset({
        "status", "resolution", "verification", "merge_into_id", "event_type",
        "context", "severity", "delete", "reason",
    })
    assert set(Router.ISSUE_STATUSES) == {"open", "resolved", "dismissed", "merged"}


def test_audit_summary_logs_decisions_but_never_free_text() -> None:
    """"permit approved" is a useful audit line; "permit touched" is not.

    Only the short closed-vocabulary fields render their values. Free-text
    fields contribute their key name and nothing else, so an operator note or a
    resolution write-up can never land in the audit trail.
    """
    from agent_mission_control.security import AUDIT_VALUE_FIELDS, build_request_summary

    summary = build_request_summary(
        "POST", "/permits/p-1/decision", None,
        body={
            "status": "approved",
            "approved": True,
            "approval_note": "ship it — spoke to the owner on the phone",
            "execution_result": "long free text\nwith a newline",
        },
    )
    assert "status=approved" in summary
    assert "approved=true" in summary
    # Key names are listed; the free-text values are not.
    assert "approval_note" in summary and "ship it" not in summary
    assert "execution_result" in summary and "long free text" not in summary
    assert "\n" not in summary

    # An allowlisted key still cannot carry free text through by being long.
    smuggled = build_request_summary(
        "POST", "/issues/1/update", None, body={"status": "x" * 200})
    assert "x" * 50 not in smuggled

    # The credential-shaped names are not in the value allowlist.
    assert not (AUDIT_VALUE_FIELDS & {
        "api_key", "token", "secret", "password", "env", "value"})


async def test_every_typed_adapter_method_accepts_request_id_by_keyword() -> None:
    """`request_id=` must reach the transport on every typed AdapterClient method.

    Six of them passed `request_id=` to a `_get()` whose keyword was named
    `inbound_request_id`, so each raised TypeError at call time. Callers wrapped
    in broad excepts (the correlation providers) turned that into a silently
    empty graph rather than an error, so nothing surfaced it.
    """
    import inspect as _inspect

    from agent_mission_control.clients import AdapterClient

    seen: list[str] = []

    class _Recorder(AdapterClient):
        async def request(self, method, path, **kwargs):  # type: ignore[override]
            seen.append(kwargs.get("inbound_request_id"))
            return 200, {}, {}

    client = _Recorder("http://adapter.invalid", "token")
    checked = 0
    for name in dir(client):
        if name.startswith("_"):
            continue
        fn = getattr(client, name)
        if not _inspect.iscoroutinefunction(fn):
            continue
        sig = _inspect.signature(fn)
        if "request_id" not in sig.parameters:
            continue
        # Fill every other required parameter with a throwaway string.
        args = [
            "x" for p in sig.parameters.values()
            if p.name != "request_id"
            and p.default is _inspect.Parameter.empty
            and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        seen.clear()
        await fn(*args, request_id="probe-id")
        assert seen and seen[-1] == "probe-id", f"{name} dropped request_id"
        checked += 1
    assert checked >= 15, f"expected the full typed surface, only saw {checked}"
    await client.aclose()


async def main() -> None:
    test_redaction_regex_matches_the_frontend_copy()
    test_redaction_masks_nested_secrets_and_detects_the_sentinel()
    await test_config_read_is_redacted_server_side()
    test_adapter_allowlist()
    test_envelope_split()
    test_error_status()
    test_read_allowlist_matches_full_dashboard_path()
    await test_session_context_read_is_proxied_unchanged()
    await test_direct_read_reattaches_api_prefix()
    await test_direct_read_rejects_non_allowlisted_path()
    test_skill_writes_match_the_upstream_openapi_declaration()
    test_mutation_route_accepts_every_allowlisted_verb()
    await test_skill_read_advertises_write_capabilities()
    await test_unadvertised_read_stays_read_only()
    await test_session_read_advertises_its_real_writes()
    await test_chat_relay_forwards_bytes_as_they_arrive()
    await test_chat_relay_closes_upstream_when_the_client_hangs_up()
    await test_chat_stream_forwards_the_model_lock_flag()
    await test_session_changed_names_the_session_that_moved()
    await test_session_changed_burst_is_capped()
    test_created_session_id_reads_the_nested_gateway_shape()
    test_chat_open_frame_is_a_wellformed_named_sse_event()
    test_gateway_read_allowlist_is_exact_not_prefix()
    test_verb_suffixed_mutations_do_not_substitute_the_verb_for_the_id()
    await test_dashboard_profile_forward_and_flatten()
    await test_adapter_profile_is_provenance_not_filter()
    await test_adapter_issues_defaults_limit()
    await test_adapter_issues_limit_is_bounded()
    await test_upstream_failure_is_not_http_200()
    await test_alert_acknowledge_publishes_alert_changed()
    await test_event_bus_cache_invalidated_emits_only_on_drop()
    await test_preferences_are_profile_scoped_and_key_gated()
    await test_session_persona_stores_only_the_profile_name()
    test_path_tokens_cannot_span_a_slash()
    test_literal_specs_win_over_token_specs()
    test_per_verb_upstream_method_translation()
    test_destructive_specs_are_confirm_gated()
    test_cron_update_uses_the_upstream_nested_body()
    test_credential_carrying_specs_reject_the_sentinel()
    test_config_write_tree_excludes_every_credential_branch()
    test_new_write_surfaces_are_advertised_to_the_ui()
    test_plugin_toggles_flag_the_restart_requirement()
    await test_events_recent_is_bounded_and_separate_from_the_audit_ledger()
    await test_run_inspector_returns_real_edges_not_an_empty_graph()
    test_alert_rules_receive_the_data_they_evaluate()
    await test_poll_workers_record_success_time_not_delta_time()
    test_decision_writes_stay_off_the_get_only_adapter_proxy()
    test_issue_transition_rules_match_the_upstream_script()
    test_issue_delete_rides_the_update_path()
    test_decision_field_allowlists_are_closed()
    await test_every_typed_adapter_method_accepts_request_id_by_keyword()
    test_audit_summary_logs_decisions_but_never_free_text()
    print("RUNTIME_CONTRACT_TESTS=PASS")


if __name__ == "__main__":
    asyncio.run(main())
