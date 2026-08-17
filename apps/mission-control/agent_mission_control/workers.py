"""Source workers — server-side polling (Stage 5 item 2, merged into
agent_mission_control by S8).

Single loop per source (no per-tab polling), exponential backoff on error
(up to 5 min), bounded concurrency, delta-only emission (no events when
nothing changed). Coverage label: polled.

Sources:
- kanban: board summary + running tasks every 10s -> task.changed / run.changed
- permits: fingerprint-set compare every 20s -> permit.changed
- issues: fingerprint-set compare every 20s -> issue.changed
- cron: dashboard 9119 GET /api/cron/jobs every 45s -> cron.changed
- sessions: dashboard 9119 GET /api/sessions every 15s -> session.changed
- health: gateway 8642 /health + dashboard 9119 /api/health + /api/status
  every 15s -> source.health on state transitions
- adapter health/capabilities every 30s -> source.health + schema fingerprint
  change detection (R2 input)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, Awaitable, Callable, Optional

from .cache import Cache
from .clients import AdapterClient, DashboardClient, GatewayClient
from .event_bus import EventBus
from .read_model import READ_MODEL_PROJECTOR_VERSION, ReadModel, project_entity
from .store import Store

DEFAULT_BACKOFF_MAX = 300  # 5 minutes

FetchFn = Callable[[], Awaitable[Any]]


class PollWorker:
    """Generic poll loop: fetch, diff via fingerprint, emit event on delta."""

    def __init__(
        self,
        name: str,
        interval_seconds: int,
        fetch: FetchFn,
        on_delta: Callable[[Any, Any], Awaitable[None]],
        fingerprint_of: Callable[[Any], str],
        backoff_max: int = DEFAULT_BACKOFF_MAX,
        on_success: Callable[[Any, str], Awaitable[None]] | None = None,
        on_failure: Callable[[Exception], Awaitable[None]] | None = None,
    ) -> None:
        self.name = name
        self.interval_seconds = interval_seconds
        self.fetch = fetch
        self.on_delta = on_delta
        self.fingerprint_of = fingerprint_of
        self.backoff_max = backoff_max
        self.on_success = on_success
        self.on_failure = on_failure
        self.last_fingerprint: Optional[str] = None
        self.last_error: Optional[str] = None
        self.backoff_seconds = 0
        self.paused = False
        self.health_transitions: list[dict] = []
        # Success time, not delta time: a source that keeps returning the same
        # rows is fresh, not stale. R3 reads this via the freshness feed.
        self.last_success_at: Optional[float] = None

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    async def tick_once(self) -> None:
        """One fetch/diff/backoff cycle (no sleep) — testable unit."""
        try:
            data = await self.fetch()
            self.backoff_seconds = 0
            self.last_error = None
            self.last_success_at = time.time()
            fp = self.fingerprint_of(data)
            if self.on_success is not None:
                await self.on_success(data, fp)
            if fp != self.last_fingerprint:
                await self.on_delta(data, fp)
                self.last_fingerprint = fp
        except Exception as exc:  # noqa: BLE001 - worker must survive
            self.last_error = str(exc)
            if self.on_failure is not None:
                try:
                    await self.on_failure(exc)
                except Exception:
                    pass
            self.backoff_seconds = min(
                self.backoff_seconds * 2 or 2, self.backoff_max
            )

    async def run(self) -> None:
        while True:
            if not self.paused:
                await self.tick_once()
                await asyncio.sleep(
                    self.interval_seconds + self.backoff_seconds
                )
            else:
                await asyncio.sleep(1)


def fingerprint_json(data: Any) -> str:
    """Deterministic sha256 of JSON payload (set-compare friendly)."""
    try:
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, default=str).encode()
        ).hexdigest()
    except Exception:
        return hashlib.sha256(str(data).encode()).hexdigest()


def fingerprint_tasks(tasks: list[dict]) -> str:
    """Fingerprint over running-task lifecycle fields only (status/run/heartbeat)."""
    key = [
        (t.get("id"), t.get("status"), t.get("current_run_id"),
         t.get("last_heartbeat_at"), t.get("started_at"), t.get("completed_at"))
        for t in tasks
    ]
    return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()


class SourceWorkers:
    """All poll workers + alert tick, started/stopped by lifespan."""

    def __init__(
        self,
        bus: EventBus,
        store: Store,
        cache: Cache,
        dashboard: DashboardClient,
        gateway: GatewayClient,
        adapter: AdapterClient,
        cfg: Any,
        alert_engine: Any = None,
        read_model: ReadModel | None = None,
    ) -> None:
        self.bus = bus
        self.store = store
        self.cache = cache
        self.dashboard = dashboard
        self.gateway = gateway
        self.adapter = adapter
        self.cfg = cfg
        # The alert rules for cron/tasks/permits/issues/analytics can only fire
        # against data someone feeds them. These workers already fetch exactly
        # that data, so they feed it here instead of a second poller doing the
        # same round trips.
        self.alert_engine = alert_engine
        self.read_model = read_model
        self.workers: dict[str, PollWorker] = {}
        self._tasks: list[asyncio.Task] = []
        self._source_health: dict[str, bool] = {}
        self._kanban_tasks: list[dict] = []
        self._permit_entities: dict[str, dict] = {}
        self._issue_entities: dict[str, dict] = {}
        self._cron_entities: dict[str, dict] = {}
        self._session_entities: dict[str, dict] = {}
        self._initialized_sources: set[str] = set()

    def _feed_alerts(self, key: str, data: Any) -> None:
        if self.alert_engine is None:
            return
        try:
            self.alert_engine.set_source_data(key, data)
        except Exception:  # noqa: BLE001 - a poll must never die on alert wiring
            pass

    def _ensure_delta_state(self) -> None:
        if not hasattr(self, "_initialized_sources"):
            self._initialized_sources = set()
        for name in ("_permit_entities", "_issue_entities", "_cron_entities", "_session_entities"):
            if not hasattr(self, name):
                setattr(self, name, {})

    def freshness_snapshot(self) -> dict[str, dict]:
        """{worker: {fetched_at, last_error}} for alert rule R3."""
        return {
            name: {"fetched_at": w.last_success_at, "last_error": w.last_error}
            for name, w in self.workers.items()
            if w.last_success_at is not None
        }

    # ---- kanban -----------------------------------------------------------
    async def _fetch_kanban(self) -> dict:
        summary = await self.adapter.board_summary()
        tasks = await self.adapter.tasks(limit=100)
        return {
            "summary": self._adapter_data(summary, {}),
            "tasks": self._adapter_data(tasks, []),
        }

    @staticmethod
    def _adapter_data(response: Any, default: Any) -> Any:
        if not isinstance(response, tuple) or len(response) < 2 or response[0] >= 400:
            return default
        body = response[1]
        if isinstance(body, dict) and "data" in body:
            return body.get("data", default)
        return body

    def _fp_kanban(self, data: dict) -> str:
        running = [
            t for t in data.get("tasks", [])
            if t.get("status") in ("running", "ready", "blocked", "todo")
        ]
        return fingerprint_tasks(running)

    async def _on_kanban(self, data: dict, fp: Optional[str]) -> None:
        self._ensure_delta_state()
        self._feed_alerts("tasks", data.get("tasks", []))
        prev_tasks = self._kanban_tasks
        cur_tasks = data.get("tasks", [])
        # task.changed on lifecycle field delta per running task
        by_id = {t.get("id"): t for t in cur_tasks}
        prev_by_id = {t.get("id"): t for t in prev_tasks}
        if "kanban" not in self._initialized_sources:
            self._initialized_sources.add("kanban")
            self._kanban_tasks = cur_tasks
            return
        for tid, t in by_id.items():
            prev = prev_by_id.get(tid)
            projected = project_entity("kanban.tasks", t)
            if prev is None or project_entity("kanban.tasks", prev) != projected:
                await self.bus.publish(
                    "task.changed", "kanban", "task", tid,
                    projected,
                )
        for tid in set(prev_by_id) - set(by_id):
            if tid:
                await self.bus.publish(
                    "task.changed", "kanban", "task", str(tid), {}, operation="delete"
                )
        # run.changed when a running task's run ended
        for tid, t in by_id.items():
            prev = prev_by_id.get(tid)
            if prev is None:
                continue
            if prev.get("status") in ("running",) and t.get("status") != "running":
                await self.bus.publish(
                    "run.changed", "kanban", "run", str(t.get("current_run_id") or ""),
                    {"task_id": tid, "status": t.get("status"),
                     "completed_at": t.get("completed_at")},
                )
        self._kanban_tasks = cur_tasks

    # ---- permits -----------------------------------------------------------
    async def _fetch_permits(self) -> list[dict]:
        r = await self.adapter.permits_list(limit=100)
        data = self._adapter_data(r, [])
        return data if isinstance(data, list) else []

    def _fp_permits(self, data: list) -> str:
        return fingerprint_json(
            [(p.get("permit_id"), p.get("status"), p.get("severity"),
              p.get("updated_at"), p.get("expires_at")) for p in data]
        )

    async def _on_permits(self, data: list, fp: Optional[str]) -> None:
        self._ensure_delta_state()
        self._feed_alerts("permits", data)
        current = {str(p.get("permit_id") or p.get("id")): p for p in data if p.get("permit_id") or p.get("id")}
        if "permits" in self._initialized_sources:
            for entity_id, row in current.items():
                projected = project_entity("permits", row)
                previous = self._permit_entities.get(entity_id)
                if previous is None or project_entity("permits", previous) != projected:
                    await self.bus.publish("permit.changed", "permits", "permit", entity_id, projected)
            for entity_id in set(self._permit_entities) - set(current):
                await self.bus.publish("permit.changed", "permits", "permit", entity_id, {}, operation="delete")
        self._initialized_sources.add("permits")
        self._permit_entities = current

    # ---- issues ------------------------------------------------------------
    async def _fetch_issues(self) -> list[dict]:
        r = await self.adapter.issues_list(limit=100)
        data = self._adapter_data(r, [])
        return data if isinstance(data, list) else []

    def _fp_issues(self, data: list) -> str:
        return fingerprint_json(
            [(i.get("id"), i.get("status"), i.get("severity"),
              i.get("last_seen_at"), i.get("updated_at")) for i in data]
        )

    async def _on_issues(self, data: list, fp: Optional[str]) -> None:
        self._ensure_delta_state()
        self._feed_alerts("issues", data)
        current = {str(row.get("id")): row for row in data if row.get("id") is not None}
        if "issues" in self._initialized_sources:
            for entity_id, row in current.items():
                projected = project_entity("issues", row)
                previous = self._issue_entities.get(entity_id)
                if previous is None or project_entity("issues", previous) != projected:
                    await self.bus.publish("issue.changed", "issues", "issue", entity_id, projected)
            for entity_id in set(self._issue_entities) - set(current):
                await self.bus.publish("issue.changed", "issues", "issue", entity_id, {}, operation="delete")
        self._initialized_sources.add("issues")
        self._issue_entities = current

    # ---- cron --------------------------------------------------------------
    async def _fetch_cron(self) -> list[dict]:
        s, body, _ = await self.dashboard.get("/api/cron/jobs")
        if s >= 400:
            raise RuntimeError(f"cron fetch failed: {s}")
        if isinstance(body, dict):
            jobs = body.get("jobs") or body.get("data") or []
        else:
            jobs = body if isinstance(body, list) else []
        return jobs

    def _fp_cron(self, data: list) -> str:
        return fingerprint_json(
            [(j.get("id"), j.get("state"), j.get("last_run_at"),
              j.get("next_run_at"), j.get("last_status")) for j in data]
        )

    async def _on_cron(self, data: list, fp: Optional[str]) -> None:
        self._ensure_delta_state()
        self._feed_alerts("cron", data)
        current = {str(row.get("id")): row for row in data if row.get("id") is not None}
        if "cron" in self._initialized_sources:
            for entity_id, row in current.items():
                projected = project_entity("cron.jobs", row)
                previous = self._cron_entities.get(entity_id)
                if previous is None or project_entity("cron.jobs", previous) != projected:
                    await self.bus.publish("cron.changed", "cron", "cron_job", entity_id, projected)
            for entity_id in set(self._cron_entities) - set(current):
                await self.bus.publish("cron.changed", "cron", "cron_job", entity_id, {}, operation="delete")
        self._initialized_sources.add("cron")
        self._cron_entities = current

    # ---- sessions ----------------------------------------------------------
    # `session.changed` used to be emitted only by the two chat handlers in
    # routes.py, so a conversation driven from Telegram or a cron run never
    # reached the SPA and the Sessions tab only refreshed on navigation. This
    # is the poller that makes the event mean "sessions moved", whoever moved
    # them.
    async def _fetch_sessions(self) -> list[dict]:
        s, body, _ = await self.dashboard.get(
            "/api/sessions?limit=100&offset=0&order=recent"
        )
        if s >= 400:
            raise RuntimeError(f"sessions fetch failed: {s}")
        if isinstance(body, dict):
            rows = body.get("sessions") or body.get("data") or body.get("items") or []
        else:
            rows = body
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def _fp_sessions(self, data: list) -> str:
        # Activity + message count, not just ids: a live conversation keeps the
        # same id while its transcript grows, and that growth is the delta the
        # SPA needs to hear about.
        return fingerprint_json(
            [(r.get("id") or r.get("session_id"), r.get("last_activity_at") or r.get("last_active"),
              r.get("message_count"), r.get("ended_at"), r.get("archived")) for r in data]
        )

    async def _on_sessions(self, data: list, fp: Optional[str]) -> None:
        self._ensure_delta_state()
        current: dict[str, dict] = {}
        emitted = 0
        for row in data:
            sid = row.get("id") or row.get("session_id")
            if not sid:
                continue
            sid = str(sid)
            current[sid] = row
            projected = project_entity("sessions", row)
            previous = self._session_entities.get(sid)
            if "sessions" in self._initialized_sources and (
                previous is None or project_entity("sessions", previous) != projected
            ) and emitted < 100:
                emitted += 1
                await self.bus.publish(
                    "session.changed", "sessions", "session", sid,
                    projected, coverage="polled",
                )
        if "sessions" in self._initialized_sources:
            for sid in list(set(self._session_entities) - set(current))[:100]:
                await self.bus.publish(
                    "session.changed", "sessions", "session", sid, {},
                    coverage="polled", operation="delete",
                )
            if emitted >= 100 or len(set(self._session_entities) - set(current)) > 100:
                await self.bus.publish(
                    "session.changed", "sessions", "session", "",
                    {"reason": "delta-bound-exceeded"}, coverage="derived",
                    operation="resync-required",
                )
        self._initialized_sources.add("sessions")
        self._session_entities = current

    # ---- running turns -----------------------------------------------------
    # Nothing in the session list says whether a turn is in flight — `is_active`
    # only means the session record has not ended, which is true of nearly every
    # session ever created. The gateway is the only place that knows, and it
    # only knows while the turn is actually running, so this is polled fast and
    # published as its own topic rather than folded into `session.changed`.
    async def _fetch_running(self) -> list[dict]:
        s, body, _ = await self.gateway.get("/api/sessions/running")
        if s >= 400:
            raise RuntimeError(f"running fetch failed: {s}")
        rows = body.get("running") if isinstance(body, dict) else None
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def _fp_running(self, data: list) -> str:
        return fingerprint_json(sorted(str(r.get("session_id") or "") for r in data))

    async def _on_running(self, data: list, fp: Optional[str]) -> None:
        await self.bus.publish(
            "session.running", "sessions", "session", "",
            {"running": [
                {"session_id": r.get("session_id"), "run_id": r.get("run_id"),
                 "started_at": r.get("started_at"), "platform": r.get("platform")}
                for r in data if r.get("session_id")
            ]},
            coverage="polled",
        )

    # ---- health ------------------------------------------------------------
    async def _fetch_health(self) -> dict:
        results = {}
        try:
            s, _, _ = await self.gateway.get("/health")
            results["gateway"] = s < 400
        except Exception:
            results["gateway"] = False
        try:
            s, _, _ = await self.dashboard.get("/api/health")
            results["dashboard"] = s < 400
        except Exception:
            results["dashboard"] = False
        try:
            # /api/status, not /api/system/status: the latter has never existed
            # on the dashboard, so probing it pinned R1 critical from startup.
            # /api/status is the dashboard's machine-level liveness probe and is
            # in its PUBLIC_API_PATHS (hermes_cli/web_server.py).
            s, _, _ = await self.dashboard.get("/api/status")
            results["dashboard_status"] = s < 400
        except Exception:
            results["dashboard_status"] = False
        return results

    def _fp_health(self, data: dict) -> str:
        return fingerprint_json(data)

    async def _on_health(self, data: dict, fp: Optional[str]) -> None:
        # Sole writer of the "health" key. The alert tick loop used to write it
        # too, from a differently-keyed registry snapshot, so the two silently
        # erased each other's sources and R1 flapped.
        self._feed_alerts("health", data)
        for source, ok in data.items():
            prev = self._source_health.get(source)
            if prev is None:
                self._source_health[source] = ok
                continue
            if prev != ok:
                await self.bus.publish(
                    "source.health", "health", "source", source,
                    {"healthy": ok, "previous": prev},
                    coverage="polled",
                )
                self._source_health[source] = ok

    # ---- adapter capabilities (R2 fingerprint detection) --------------------
    async def _fetch_capabilities(self) -> dict:
        s, body, _ = await self.adapter.capabilities()
        if s >= 400:
            raise RuntimeError(f"capabilities fetch failed: {s}")
        return body

    def _fp_capabilities(self, data: dict) -> str:
        fp = data.get("schema_fingerprint") or data.get("fingerprint") or data.get("global_fingerprint") or ""
        if isinstance(fp, dict):
            fp = fp.get("sha256_ddl", "") or fp.get("global", "")
        return str(fp)

    async def _on_capabilities(self, data: dict, fp: Optional[str]) -> None:
        self._feed_alerts("capabilities", data)
        if fp:
            self.store.record_fingerprint("adapter", fp)
            # Emit a derived event; the bus invalidates the backend cache for
            # the source and forwards the event so clients clear their own
            # prefetch caches (app.js cache.invalidated handler).
            await self.bus.publish(
                "cache.invalidated", "adapter", "schema", "adapter",
                {"fingerprint": fp}, coverage="derived",
            )

    # ---- analytics (R10 token-spike input) ----------------------------------
    async def _fetch_analytics(self) -> dict:
        s, body, _ = await self.dashboard.get("/api/analytics/usage")
        if s >= 400:
            raise RuntimeError(f"analytics fetch failed: {s}")
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _total_tokens(data: dict) -> float:
        """Upstream has shipped several shapes for this payload, so read the
        first field that exists rather than assuming one."""
        for key in ("total_tokens", "tokens", "token_count"):
            value = data.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        totals = data.get("totals")
        if isinstance(totals, dict):
            for key in ("total_tokens", "tokens", "input_tokens"):
                value = totals.get(key)
                if isinstance(value, (int, float)):
                    return float(value)
        return 0.0

    def _fp_analytics(self, data: dict) -> str:
        return fingerprint_json(self._total_tokens(data))

    async def _on_analytics(self, data: dict, fp: Optional[str]) -> None:
        # R10 stays inert unless the operator configured a threshold — there is
        # no defensible default for "too many tokens".
        self._feed_alerts("analytics", {
            "tokens": self._total_tokens(data),
            "token_threshold": getattr(self.cfg, "alert_token_threshold", 0),
        })

    # ---- persistent read model -------------------------------------------
    _WORKER_RESOURCES = {
        "kanban": ("kanban.tasks",),
        "permits": ("permits",),
        "issues": ("issues",),
        "cron": ("cron.jobs",),
        "sessions": ("sessions",),
        "running": ("sessions.running",),
        "health": ("source.health",),
        "analytics": ("analytics.usage",),
    }

    async def _persist_success(self, name: str, data: Any, fingerprint: str) -> None:
        model = self.read_model
        if model is None or not model.available:
            return
        profile = getattr(self.cfg, "live_default_profile", "default")
        fingerprint = f"p{READ_MODEL_PROJECTOR_VERSION}:{fingerprint}"
        if name == "kanban":
            model.replace_entities("kanban.tasks", data.get("tasks", []), profile_id=profile, fingerprint=fingerprint)
        elif name in {"permits", "issues", "cron", "sessions", "running"}:
            key = self._WORKER_RESOURCES[name][0]
            model.replace_entities(key, data if isinstance(data, list) else [], profile_id=profile, fingerprint=fingerprint)
        elif name == "health":
            rows = [
                {"source_id": source, "healthy": bool(healthy), "checked_at": time.time()}
                for source, healthy in (data.items() if isinstance(data, dict) else ())
            ]
            model.replace_entities("source.health", rows, profile_id=profile, fingerprint=fingerprint)
        elif name == "analytics":
            summary = dict(data) if isinstance(data, dict) else {}
            if isinstance(summary.get("totals"), dict):
                summary = {**summary, **summary["totals"]}
            model.replace_summary("analytics.usage", summary, profile_id=profile, fingerprint=fingerprint)

    async def _persist_failure(self, name: str, exc: Exception) -> None:
        if self.read_model is None:
            return
        resources = self._WORKER_RESOURCES.get(name, ())
        if resources:
            self.read_model.record_failure(
                resources, exc, profile_id=getattr(self.cfg, "live_default_profile", "default")
            )

    def _worker(self, name: str, interval: int, fetch: FetchFn, on_delta, fingerprint_of) -> PollWorker:
        return PollWorker(
            name, interval, fetch, on_delta, fingerprint_of,
            self.cfg.poll_backoff_max_seconds,
            on_success=lambda data, fp: self._persist_success(name, data, fp),
            on_failure=lambda exc: self._persist_failure(name, exc),
        )

    # ---- setup -------------------------------------------------------------
    def build(self) -> None:
        self.workers["kanban"] = self._worker("kanban", self.cfg.poll_kanban_seconds, self._fetch_kanban, self._on_kanban, self._fp_kanban)
        self.workers["permits"] = self._worker("permits", self.cfg.poll_permits_seconds, self._fetch_permits, self._on_permits, self._fp_permits)
        self.workers["issues"] = self._worker("issues", self.cfg.poll_issues_seconds, self._fetch_issues, self._on_issues, self._fp_issues)
        self.workers["cron"] = self._worker("cron", self.cfg.poll_cron_seconds, self._fetch_cron, self._on_cron, self._fp_cron)
        self.workers["sessions"] = self._worker("sessions", self.cfg.poll_sessions_seconds, self._fetch_sessions, self._on_sessions, self._fp_sessions)
        # Faster than the other pollers on purpose: this drives a "running now"
        # indicator, and an indicator that lags the turn it describes by fifteen
        # seconds is worse than none. The response is a short list and costs the
        # gateway a dict walk.
        self.workers["running"] = self._worker("running", self.cfg.poll_running_seconds, self._fetch_running, self._on_running, self._fp_running)
        self.workers["health"] = self._worker("health", self.cfg.poll_health_seconds, self._fetch_health, self._on_health, self._fp_health)
        self.workers["capabilities"] = PollWorker(
            "capabilities", self.cfg.poll_adapter_health_seconds, self._fetch_capabilities,
            self._on_capabilities, self._fp_capabilities, self.cfg.poll_backoff_max_seconds,
        )
        self.workers["analytics"] = self._worker("analytics", self.cfg.poll_analytics_seconds, self._fetch_analytics, self._on_analytics, self._fp_analytics)

    async def start(self) -> None:
        self.build()
        for w in self.workers.values():
            self._tasks.append(asyncio.create_task(w.run()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks = []
