"""Application factory + middleware for agent-mission-control.

Middleware order (outermost first):
1. Request ID correlation (inbound X-Request-Id or generated)
2. Body size limit
3. Security headers
4. IP allowlist + auto-session gate (every route; rejects disallowed peers
   with 403 and auto-issues a session cookie for allowed peers — the
   S8-ALT auth model, no login step).

Startup guard (fail-closed): refuse to bind 0.0.0.0 without ALLOWED_CIDRS.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import alerts as alerts_mod
from .cache import Cache
from .capabilities import CapabilityRegistry
from .clients import AdapterClient, DashboardClient, GatewayClient, new_request_id
from .config import Settings, should_refuse_start
from .correlation import CorrelationEngine
from .correlation_providers import build_correlation_providers
from .event_bus import EventBus
from .ip_utils import resolve_client_ip
from .pulse import Pulse
from .routes import ApiError, Router
from .run_inspector import RunInspector
from .runner_manager import RunnerManager
from .session_persona_store import SessionPersonaStore
from .store import Store
from .system_manager_routes import build_system_manager_router
from .workers import SourceWorkers

logger = logging.getLogger("agent_mission_control")

_STATIC_ASSET_PREFIXES = (
    "/assets/",
    "/tabs/",
    "/pure/",
    # External dashboards are same-origin, allowlisted, GET-only proxy
    # surfaces.  They must not auto-issue a new AgentOS session for every
    # iframe asset/API request when the browser has no cookie yet.
    "/api/proxy/external/",
)
_STATIC_ASSET_SUFFIXES = (
    ".js",
    ".css",
    ".json",
    ".map",
    ".ico",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
)
_STATIC_ASSET_FILES = {
    "/app.js",
    "/index.html",
    "/styles.css",
    "/api.js",
    "/events.js",
    "/palette.js",
    "/preload.js",
    "/profile.js",
    "/provenance.js",
    "/ui.js",
    "/favicon.ico",
}
_BOOTSTRAP_PATHS = {"/"}


class _AllowlistGate:
    """IP allowlist + auto-session gate (S8-ALT auth model).

    Every request (including / and static) passes through this middleware:
    - The effective client IP is resolved (peer IP unless TRUST_PROXY_HEADERS
      enables X-Forwarded-For).
    - A peer outside ALLOWED_CIDRS is rejected 403 before any session,
      CSRF, or upstream work — fail-closed.
    - A peer inside the allowlist gets a server-side session auto-issued on
      first contact (random 32+ hex id, HttpOnly + SameSite=Strict cookie,
      Secure when COOKIE_SECURE=1) unless a valid session cookie is already
      present. The session then carries the CSRF token + preferences exactly
      as the old password login did.
    """

    def __init__(self, router: Router):
        self._router = router

    async def __call__(self, request: Request, call_next) -> Response:
        path = request.url.path
        peer = request.client.host if request.client else None
        effective = resolve_client_ip(
            peer,
            request.headers.get("X-Forwarded-For"),
            self._router.s.trust_proxy_headers,
        )
        if not self._router.allowlist.contains(effective):
            return JSONResponse(
                {"error": {"message": "source IP not allowed", "code": "ip_forbidden"},
                 "request_id": getattr(request.state, "request_id", None)},
                status_code=403,
            )
        if request.method in {"OPTIONS", "HEAD"}:
            return await call_next(request)
        if self._is_static_asset_path(path) or path in _BOOTSTRAP_PATHS:
            return await call_next(request)
        session = self._router._session_from_request(request)  # noqa: SLF001
        if session is None:
            # Auto-issue a session for the allowed peer (rate-limited per IP).
            session = self._router.auto_issue_session(request)
            if session is None:
                return JSONResponse(
                    {"error": {"message": "session issue rate limit exceeded",
                               "code": "rate_limited"},
                     "request_id": getattr(request.state, "request_id", None)},
                    status_code=429,
                )
            request.state.auto_session = session
            response = await call_next(request)
            # Only set the cookie on responses that are not streaming (SSE
            # uses the ?token= fallback; setting a cookie on an SSE response
            # is unreliable). Streaming handlers still get their own check.
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                self._router._attach_session_cookie(response, session, request)  # noqa: SLF001
            return response
        return await call_next(request)

    @staticmethod
    def _is_static_asset_path(path: str) -> bool:
        if path in _STATIC_ASSET_FILES:
            return True
        if any(path.startswith(prefix) for prefix in _STATIC_ASSET_PREFIXES):
            return True
        return path.endswith(_STATIC_ASSET_SUFFIXES)


async def _request_id_middleware(request: Request, call_next) -> Response:
    rid = request.headers.get("X-Request-Id") or new_request_id()
    request.state.request_id = rid
    response = await call_next(request)
    response.headers.setdefault("X-Request-Id", rid)
    return response


async def _body_limit_middleware(request: Request, call_next) -> Response:
    # Only enforce on routes that can carry a body.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > request.app.state.settings.body_limit_bytes:
                    return JSONResponse(
                        {"error": {"message": "request body too large",
                                   "code": "body_too_large"}},
                        status_code=413,
                    )
            except ValueError:
                pass
    return await call_next(request)


async def _security_headers_middleware(request: Request, call_next) -> Response:
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    path = request.url.path
    if path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": exc.message, "code": exc.code},
         "request_id": getattr(request.state, "request_id", None)},
        status_code=exc.status,
    )


class AppDeps:
    """Bundle of app-level collaborators for tests/factory."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        dashboard: DashboardClient,
        gateway: GatewayClient,
        adapter: AdapterClient,
        cache: Cache,
        registry: CapabilityRegistry,
        router: Router,
        event_bus: EventBus | None = None,
        correlation_engine: CorrelationEngine | None = None,
        run_inspector: RunInspector | None = None,
        alert_engine: alerts_mod.AlertEngine | None = None,
        pulse: Pulse | None = None,
        workers: SourceWorkers | None = None,
        dashboard_store: SessionPersonaStore | None = None,
        runner_manager: RunnerManager | None = None,
    ):
        self.settings = settings
        self.store = store
        self.dashboard_store = dashboard_store or getattr(
            router, "dashboard_store", None
        )
        self.runner_manager = runner_manager or getattr(router, "runner_manager", None)
        self.dashboard = dashboard
        self.gateway = gateway
        self.adapter = adapter
        self.cache = cache
        self.registry = registry
        self.router = router
        self.event_bus = event_bus or getattr(router, "event_bus", None)
        self.correlation_engine = correlation_engine or getattr(
            router, "correlation_engine", None
        )
        self.run_inspector = run_inspector or getattr(router, "run_inspector", None)
        self.alert_engine = alert_engine or getattr(router, "alert_engine", None)
        self.pulse = pulse or getattr(router, "pulse", None)
        self.workers = workers


def create_app(deps: AppDeps | None = None, settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app. Pass ``deps`` (fully built) or ``settings``
    (builds real collaborators against env defaults)."""
    s = settings or Settings.from_env()

    if deps is None:
        store = Store(s.store_path)
        dashboard_store = SessionPersonaStore(s.dashboard_store_path)
        dashboard = DashboardClient(s.dashboard_url, s.dashboard_basic_auth_password)
        gateway = GatewayClient(
            s.gateway_url, s.gateway_token, nas_jwt_secret=s.nas_jwt_secret,
            stream_read_timeout=s.chat_stream_read_timeout_seconds,
        )
        adapter = AdapterClient(s.adapter_url, s.adapter_token)
        runner_manager = RunnerManager(
            hermes_executable=s.runner_hermes_executable,
            pool_max=s.runner_pool_max,
            idle_seconds=s.runner_pool_idle_seconds,
            keepalive_fresh_seconds=s.runner_pool_keepalive_fresh_seconds,
            port_announce_timeout_seconds=s.runner_port_announce_timeout_seconds,
        )
        cache = Cache(ttl_seconds=s.cache_ttl_seconds, max_concurrency=s.cache_max_concurrency)
        bus = EventBus(
            store,
            ring_buffer_size=s.event_ring_buffer_size,
            db_replay_limit=s.event_db_replay_limit,
            heartbeat_seconds=s.sse_heartbeat_seconds,
            retry_ms=s.sse_retry_ms,
            cache=cache,
        )
        registry = CapabilityRegistry(adapter, dashboard, gateway, store)
        # One provider map, shared: the inspector's trajectory and the
        # engine's graph must describe the same rows or they contradict.
        correlation_providers = build_correlation_providers(adapter, dashboard)
        engine = CorrelationEngine(providers=correlation_providers)
        inspector = RunInspector(engine, providers=correlation_providers)
        alert_engine = alerts_mod.AlertEngine(store, s, bus=bus)
        pulse = Pulse(store)
        workers = SourceWorkers(
            bus, store, cache, dashboard, gateway, adapter, s,
            alert_engine=alert_engine,
        )
        router = Router(
            s, store, dashboard, gateway, adapter, cache, registry,
            event_bus=bus, correlation_engine=engine, run_inspector=inspector,
            alert_engine=alert_engine, pulse=pulse, dashboard_store=dashboard_store,
            runner_manager=runner_manager,
        )
        deps = AppDeps(
            s, store, dashboard, gateway, adapter, cache, registry, router,
            event_bus=bus, correlation_engine=engine, run_inspector=inspector,
            alert_engine=alert_engine, pulse=pulse, workers=workers,
            dashboard_store=dashboard_store, runner_manager=runner_manager,
        )

    app = FastAPI(title="agent-mission-control", version="0.1.0", docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.state.settings = s
    app.state.deps = deps

    app.middleware("http")(_security_headers_middleware)
    app.middleware("http")(_body_limit_middleware)
    app.middleware("http")(_request_id_middleware)
    app.add_exception_handler(ApiError, _api_error_handler)

    # IP allowlist + auto-session gate — LAST-registered middleware runs
    # FIRST on request, so this is the outermost gate.
    app.middleware("http")(_AllowlistGate(deps.router).__call__)

    # System Manager owns a dedicated BFF namespace. Register it before the
    # core router because Router.build() contains a generic /api/{path:path}
    # dashboard-read catch-all near the end of its route table.
    app.include_router(build_system_manager_router(deps.router))
    app.include_router(deps.router.build())

    # Lifespan: start source workers + alert tick + registry probe; stop
    # cleanly (Stage 5 merge requirement).
    @app.on_event("startup")
    async def _startup() -> None:
        workers = getattr(deps, "workers", None)
        if workers is not None:
            try:
                await workers.start()
            except Exception:  # noqa: BLE001 — workers must never break boot
                logger.warning("worker start failed (continuing): %s", _exc_name())
        try:
            await deps.registry.refresh()
        except Exception:  # noqa: BLE001
            logger.warning("registry probe failed at startup: %s", _exc_name())
        deps._alert_tick_task = asyncio.create_task(_alert_tick_loop(deps))

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        tick = getattr(deps, "_alert_tick_task", None)
        if tick is not None:
            tick.cancel()
            try:
                await tick
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        workers = getattr(deps, "workers", None)
        if workers is not None:
            try:
                await workers.stop()
            except Exception:  # noqa: BLE001
                pass
        for client in (deps.dashboard, deps.gateway, deps.adapter):
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
        runner_manager = getattr(deps, "runner_manager", None)
        if runner_manager is not None:
            try:
                await runner_manager.stop_all()
            except Exception:  # noqa: BLE001
                pass
        try:
            deps.store.close()
        except Exception:  # noqa: BLE001
            pass
        dashboard_store = getattr(deps, "dashboard_store", None)
        if dashboard_store is not None:
            try:
                dashboard_store.close()
            except Exception:  # noqa: BLE001
                pass

    return app


async def _alert_tick_loop(deps: AppDeps) -> None:
    """Periodically re-evaluate the alert rules.

    The loop no longer writes ``source_data["health"]``. It used to, from a
    registry snapshot keyed by source id, while the health poll worker wrote
    the same key from its own probe results keyed by upstream name — so the two
    erased each other's sources every tick and R1 flapped. The worker is now
    the sole writer; this loop only supplies the freshness feed (which nothing
    else owns) and drives the time-window rules that need a periodic re-run
    even when no source changed.
    """
    s = deps.settings
    try:
        while True:
            await asyncio.sleep(s.alerts_tick_seconds)
            try:
                workers = getattr(deps, "workers", None)
                if workers is not None:
                    deps.alert_engine.set_source_data(
                        "freshness", workers.freshness_snapshot())
                deps.alert_engine.evaluate()
                await deps.alert_engine.publish_resolved()
            except Exception:  # noqa: BLE001
                logger.warning("alert tick error: %s", _exc_name())
    except asyncio.CancelledError:
        pass


def _exc_name() -> str:
    import sys
    return type(sys.exc_info()[1]).__name__ if sys.exc_info()[1] else "unknown"


def refuse_start_if_needed(settings: Settings) -> bool:
    """Fail-closed startup guard. True => process must exit non-zero."""
    if should_refuse_start(settings.bind_host, bool(settings.allowed_cidrs)):
        logger.error(
            "REFUSING TO START: binding %s without ALLOWED_CIDRS configured "
            "(set ALLOWED_CIDRS, e.g. 192.168.0.0/24,100.64.0.0/10). Fail-closed.",
            settings.bind_host,
        )
        return True
    return False
