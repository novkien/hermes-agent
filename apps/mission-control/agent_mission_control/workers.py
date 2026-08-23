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
from .clients import DashboardClient, GatewayClient
from .data_backend import BackendResult, DataBackend
from .event_bus import EventBus
from .live_resources import RESOURCE_SPECS
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
            self.backoff_seconds = min(self.backoff_seconds * 2 or 2, self.backoff_max)

    async def run(self) -> None:
        while True:
            if not self.paused:
                await self.tick_once()
                await asyncio.sleep(self.interval_seconds + self.backoff_seconds)
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
        (
            t.get("id"),
            t.get("status"),
            t.get("current_run_id"),
            t.get("last_heartbeat_at"),
            t.get("started_at"),
            t.get("completed_at"),
        )
        for t in tasks
    ]
    return hashlib.sha256(
        json.dumps(key, sort_keys=True, default=str).encode()
    ).hexdigest()


class SourceWorkers:
    """All poll workers + alert tick, started/stopped by lifespan."""

    def __init__(
        self,
        bus: EventBus,
        store: Store,
        cache: Cache,
        dashboard: DashboardClient,
        gateway: GatewayClient,
        adapter: DataBackend,
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
        self._repository_entities: dict[str, dict] = {}
        self._system_manager_entities: dict[str, dict] = {}
        self._inventory_entities: dict[str, dict[str, dict]] = {}
        self._initialized_sources: set[str] = set()
        # Filled by app composition after the dedicated route modules create
        # their bounded service/client instances and before startup runs.
        self.repository_service: Any = None
        self.system_manager_client: Any = None

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
        for name in (
            "_permit_entities",
            "_issue_entities",
            "_cron_entities",
            "_session_entities",
            "_repository_entities",
            "_system_manager_entities",
        ):
            if not hasattr(self, name):
                setattr(self, name, {})
        if not hasattr(self, "_inventory_entities"):
            self._inventory_entities = {}

    def freshness_snapshot(self) -> dict[str, dict]:
        """{worker: {fetched_at, last_error}} for alert rule R3."""
        return {
            name: {"fetched_at": w.last_success_at, "last_error": w.last_error}
            for name, w in self.workers.items()
            if w.last_success_at is not None
        }

    # ---- kanban -----------------------------------------------------------
    async def _fetch_kanban(self) -> dict:
        summary = await self.adapter.kanban_summary()
        tasks = await self.adapter.kanban_tasks(limit=100)
        return {
            "summary": self._backend_data(summary, {}),
            "tasks": self._backend_data(tasks, []),
        }

    @staticmethod
    def _backend_data(response: Any, default: Any) -> Any:
        return response.data if isinstance(response, BackendResult) else default

    def _fp_kanban(self, data: dict) -> str:
        running = [
            t
            for t in data.get("tasks", [])
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
                    "task.changed",
                    "kanban",
                    "task",
                    tid,
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
                    "run.changed",
                    "kanban",
                    "run",
                    str(t.get("current_run_id") or ""),
                    {
                        "task_id": tid,
                        "status": t.get("status"),
                        "completed_at": t.get("completed_at"),
                    },
                )
        self._kanban_tasks = cur_tasks

    # ---- permits -----------------------------------------------------------
    async def _fetch_permits(self) -> list[dict]:
        r = await self.adapter.permits(limit=100)
        data = self._backend_data(r, [])
        return data if isinstance(data, list) else []

    def _fp_permits(self, data: list) -> str:
        return fingerprint_json([
            (
                p.get("permit_id"),
                p.get("status"),
                p.get("severity"),
                p.get("updated_at"),
                p.get("expires_at"),
            )
            for p in data
        ])

    async def _on_permits(self, data: list, fp: Optional[str]) -> None:
        self._ensure_delta_state()
        self._feed_alerts("permits", data)
        current = {
            str(p.get("permit_id") or p.get("id")): p
            for p in data
            if p.get("permit_id") or p.get("id")
        }
        if "permits" in self._initialized_sources:
            for entity_id, row in current.items():
                projected = project_entity("permits", row)
                previous = self._permit_entities.get(entity_id)
                if previous is None or project_entity("permits", previous) != projected:
                    await self.bus.publish(
                        "permit.changed", "permits", "permit", entity_id, projected
                    )
            for entity_id in set(self._permit_entities) - set(current):
                await self.bus.publish(
                    "permit.changed",
                    "permits",
                    "permit",
                    entity_id,
                    {},
                    operation="delete",
                )
        self._initialized_sources.add("permits")
        self._permit_entities = current

    # ---- issues ------------------------------------------------------------
    async def _fetch_issues(self) -> list[dict]:
        r = await self.adapter.issues(limit=100)
        data = self._backend_data(r, [])
        return data if isinstance(data, list) else []

    def _fp_issues(self, data: list) -> str:
        return fingerprint_json([
            (
                i.get("id"),
                i.get("status"),
                i.get("severity"),
                i.get("last_seen_at"),
                i.get("updated_at"),
            )
            for i in data
        ])

    async def _on_issues(self, data: list, fp: Optional[str]) -> None:
        self._ensure_delta_state()
        self._feed_alerts("issues", data)
        current = {str(row.get("id")): row for row in data if row.get("id") is not None}
        if "issues" in self._initialized_sources:
            for entity_id, row in current.items():
                projected = project_entity("issues", row)
                previous = self._issue_entities.get(entity_id)
                if previous is None or project_entity("issues", previous) != projected:
                    await self.bus.publish(
                        "issue.changed", "issues", "issue", entity_id, projected
                    )
            for entity_id in set(self._issue_entities) - set(current):
                await self.bus.publish(
                    "issue.changed",
                    "issues",
                    "issue",
                    entity_id,
                    {},
                    operation="delete",
                )
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
        return fingerprint_json([
            (
                j.get("id"),
                j.get("state"),
                j.get("last_run_at"),
                j.get("next_run_at"),
                j.get("last_status"),
            )
            for j in data
        ])

    async def _on_cron(self, data: list, fp: Optional[str]) -> None:
        self._ensure_delta_state()
        self._feed_alerts("cron", data)
        current = {str(row.get("id")): row for row in data if row.get("id") is not None}
        if "cron" in self._initialized_sources:
            for entity_id, row in current.items():
                projected = project_entity("cron.jobs", row)
                previous = self._cron_entities.get(entity_id)
                if (
                    previous is None
                    or project_entity("cron.jobs", previous) != projected
                ):
                    await self.bus.publish(
                        "cron.changed", "cron", "cron_job", entity_id, projected
                    )
            for entity_id in set(self._cron_entities) - set(current):
                await self.bus.publish(
                    "cron.changed",
                    "cron",
                    "cron_job",
                    entity_id,
                    {},
                    operation="delete",
                )
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
        return (
            [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        )

    def _fp_sessions(self, data: list) -> str:
        # Activity + message count, not just ids: a live conversation keeps the
        # same id while its transcript grows, and that growth is the delta the
        # SPA needs to hear about.
        return fingerprint_json([
            (
                r.get("id") or r.get("session_id"),
                r.get("last_activity_at") or r.get("last_active"),
                r.get("message_count"),
                r.get("ended_at"),
                r.get("archived"),
            )
            for r in data
        ])

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
            if (
                "sessions" in self._initialized_sources
                and (
                    previous is None
                    or project_entity("sessions", previous) != projected
                )
                and emitted < 100
            ):
                emitted += 1
                await self.bus.publish(
                    "session.changed",
                    "sessions",
                    "session",
                    sid,
                    projected,
                    coverage="polled",
                )
        if "sessions" in self._initialized_sources:
            for sid in list(set(self._session_entities) - set(current))[:100]:
                await self.bus.publish(
                    "session.changed",
                    "sessions",
                    "session",
                    sid,
                    {},
                    coverage="polled",
                    operation="delete",
                )
            if emitted >= 100 or len(set(self._session_entities) - set(current)) > 100:
                await self.bus.publish(
                    "session.changed",
                    "sessions",
                    "session",
                    "",
                    {"reason": "delta-bound-exceeded"},
                    coverage="derived",
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
        return (
            [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        )

    def _fp_running(self, data: list) -> str:
        return fingerprint_json(sorted(str(r.get("session_id") or "") for r in data))

    async def _on_running(self, data: list, fp: Optional[str]) -> None:
        await self.bus.publish(
            "session.running",
            "sessions",
            "session",
            "",
            {
                "running": [
                    {
                        "session_id": r.get("session_id"),
                        "run_id": r.get("run_id"),
                        "started_at": r.get("started_at"),
                        "platform": r.get("platform"),
                    }
                    for r in data
                    if r.get("session_id")
                ]
            },
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
                    "source.health",
                    "health",
                    "source",
                    source,
                    {"healthy": ok, "previous": prev},
                    coverage="polled",
                )
                self._source_health[source] = ok

    # ---- adapter capabilities (R2 fingerprint detection) --------------------
    async def _fetch_capabilities(self) -> dict:
        result = await self.adapter.capabilities()
        return result.data if isinstance(result.data, dict) else {}

    def _fp_capabilities(self, data: dict) -> str:
        fp = (
            data.get("schema_fingerprint")
            or data.get("fingerprint")
            or data.get("global_fingerprint")
            or ""
        )
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
                "cache.invalidated",
                "adapter",
                "schema",
                "adapter",
                {"fingerprint": fp},
                coverage="derived",
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
        self._feed_alerts(
            "analytics",
            {
                "tokens": self._total_tokens(data),
                "token_threshold": getattr(self.cfg, "alert_token_threshold", 0),
            },
        )

    # ---- repository inventory --------------------------------------------
    async def _fetch_repositories(self) -> list[dict]:
        if self.repository_service is None:
            raise RuntimeError("repository service is not composed")
        rows = await asyncio.to_thread(
            self.repository_service.status_all, fetch=True, include_github=True
        )
        return [row for row in rows if isinstance(row, dict)]

    def _fp_repositories(self, data: list[dict]) -> str:
        return fingerprint_json([
            (
                row.get("name"),
                row.get("state"),
                row.get("local_sha"),
                row.get("remote_sha"),
                row.get("ahead"),
                row.get("behind"),
                bool((row.get("working_tree") or {}).get("dirty")),
            )
            for row in data
        ])

    async def _on_repositories(self, data: list[dict], fp: Optional[str]) -> None:
        self._ensure_delta_state()
        current = {str(row.get("name")): row for row in data if row.get("name")}
        if "repositories" in self._initialized_sources:
            for entity_id, row in current.items():
                projected = project_entity("repositories", row)
                previous = self._repository_entities.get(entity_id)
                if (
                    previous is None
                    or project_entity("repositories", previous) != projected
                ):
                    await self.bus.publish(
                        "repository.changed",
                        "repository-worker",
                        "repository",
                        entity_id,
                        projected,
                        coverage="polled",
                        operation="upsert",
                    )
            for entity_id in set(self._repository_entities) - set(current):
                await self.bus.publish(
                    "repository.changed",
                    "repository-worker",
                    "repository",
                    entity_id,
                    {},
                    coverage="polled",
                    operation="delete",
                )
        self._initialized_sources.add("repositories")
        self._repository_entities = current

    # ---- System Manager inventory ----------------------------------------
    _SYSTEM_MANAGER_TABLES = ("services", "api", "accounts", "notes")

    async def _fetch_system_manager(self) -> list[dict]:
        if self.system_manager_client is None:
            raise RuntimeError("system manager client is not composed")

        async def one(table: str) -> list[dict]:
            status, body = await self.system_manager_client.request(
                "POST",
                "/v1/db/read",
                request_id=f"live-worker-{table}",
                json_body={"table": table, "limit": 500},
            )
            if status >= 400:
                raise RuntimeError(f"system manager {table} fetch failed: {status}")
            rows = body.get("rows") if isinstance(body, dict) else None
            out = []
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict) or row.get("id") in (None, ""):
                    continue
                out.append({
                    **row,
                    "table": table,
                    "entity_key": f"{table}:{row['id']}",
                })
            return out

        groups = await asyncio.gather(
            *(one(table) for table in self._SYSTEM_MANAGER_TABLES)
        )
        return [row for group in groups for row in group]

    def _fp_system_manager(self, data: list[dict]) -> str:
        return fingerprint_json([
            (
                row.get("entity_key"),
                row.get("observed_state"),
                row.get("health"),
                row.get("enabled"),
                row.get("updated_at"),
                row.get("revision"),
            )
            for row in data
        ])

    async def _on_system_manager(self, data: list[dict], fp: Optional[str]) -> None:
        self._ensure_delta_state()
        current = {
            str(row.get("entity_key")): row for row in data if row.get("entity_key")
        }
        if "system-manager" in self._initialized_sources:
            for entity_id, row in current.items():
                projected = project_entity("system-manager.inventory", row)
                previous = self._system_manager_entities.get(entity_id)
                if (
                    previous is None
                    or project_entity("system-manager.inventory", previous) != projected
                ):
                    await self.bus.publish(
                        "system-manager.changed",
                        "system-manager",
                        "inventory",
                        entity_id,
                        projected,
                        coverage="polled",
                        operation="upsert",
                    )
            for entity_id in set(self._system_manager_entities) - set(current):
                await self.bus.publish(
                    "system-manager.changed",
                    "system-manager",
                    "inventory",
                    entity_id,
                    {},
                    coverage="polled",
                    operation="delete",
                )
        self._initialized_sources.add("system-manager")
        self._system_manager_entities = current

    # ---- catalog, inventory and metadata ---------------------------------
    _INVENTORY_EVENTS = {
        "catalog.profiles": "profiles.changed",
        "catalog.models": "models.changed",
        "catalog.tools": "toolsets.changed",
        "catalog.mcp": "mcp.changed",
        "catalog.plugins": "plugins.changed",
        "catalog.skills": "skills.changed",
        "memory.inventory": "memory.changed",
        "config.webhooks": "webhooks.changed",
        "config.channels": "channels.changed",
        "artifacts.metadata": "artifacts.changed",
        "files.metadata": "files.changed",
        "rooms.binding": "rooms.changed",
        "rooms.sessions": "room-sessions.changed",
        "action.audit": "audit.changed",
        "command.status": "command.changed",
    }

    @staticmethod
    def _list_from(body: Any, *keys: str) -> list[dict]:
        if isinstance(body, list):
            return [row for row in body if isinstance(row, dict)]
        if not isinstance(body, dict):
            return []
        for key in keys:
            rows = body.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return []

    async def _dashboard_body(self, path: str) -> dict:
        status, body, _headers = await self.dashboard.get(path)
        if status >= 400:
            raise RuntimeError(f"dashboard inventory fetch failed: {path} ({status})")
        return body if isinstance(body, dict) else {}

    async def _fetch_artifact_metadata(self) -> list[dict]:
        result = await self.adapter.kanban_tasks(limit=25)
        payload = result.data
        tasks = self._list_from(payload, "tasks", "items")[:25]
        rows: list[dict] = []
        for task in tasks:
            task_id = task.get("id")
            if not task_id:
                continue
            try:
                detail = await self.adapter.kanban_task_attachments(
                    str(task_id), limit=20
                )
            except Exception:
                continue
            data = detail.data
            for attachment in self._list_from(data, "attachments", "items"):
                attachment_id = attachment.get("id") or attachment.get("name")
                if attachment_id in (None, ""):
                    continue
                rows.append({
                    **attachment,
                    "id": f"{task_id}:{attachment_id}",
                    "attachment_id": attachment.get("id"),
                    "task_id": task_id,
                    "task_title": task.get("title"),
                })
        return rows[:500]

    async def _fetch_room_inventory(self) -> tuple[list[dict], list[dict]]:
        payload = (await self.adapter.room_binding()).data
        slots = self._list_from(payload, "room_slots")
        occupancy = self._list_from(payload, "live_occupancy")
        reservations = self._list_from(payload, "reservations")
        occupied = {str(row.get("room_slot")): row for row in occupancy}
        reserved = {str(row.get("room_slot")): row for row in reservations}
        bindings: list[dict] = []
        for slot in slots:
            key = str(slot.get("slot") or "")
            if not key:
                continue
            live = occupied.get(key, {})
            reservation = reserved.get(key, {})
            thread_ids = [
                value
                for name, value in slot.items()
                if name.endswith("_thread_id") and value not in (None, "")
            ]
            bindings.append({
                "slot": key,
                "state": live.get("status") or ("occupied" if live else "free"),
                "status": live.get("status"),
                "task_id": live.get("task_id"),
                "chat_id": live.get("chat_id"),
                "thread_ids": thread_ids,
                "held_since": live.get("held_since"),
                "bound_at": live.get("bound_at"),
                "occupied": bool(live),
                "reserved": bool(reservation),
                "reserved_task": reservation.get("task_id"),
                "seat_count": len(thread_ids),
            })

        sessions: list[dict] = []
        for chat_id in sorted({
            str(row.get("chat_id")) for row in occupancy if row.get("chat_id")
        }):
            try:
                result = await self.adapter.room_sessions(chat_id, limit=200)
            except Exception:
                continue
            data = result.data
            for row in self._list_from(data, "sessions", "items"):
                session_id = row.get("session_id") or row.get("id")
                if session_id in (None, ""):
                    continue
                sessions.append({**row, "session_id": session_id, "chat_id": chat_id})
        return bindings, sessions[:1000]

    def _memory_inventory(self) -> list[dict]:
        memory_dir = getattr(
            getattr(self.adapter, "settings", None), "memory_dir", None
        )
        if memory_dir is None:
            return []
        rows = []
        for file_key, name in (("memory", "MEMORY.md"), ("user", "USER.md")):
            path = memory_dir / name
            try:
                stat = path.stat()
                rows.append({
                    "file_key": file_key,
                    "name": name,
                    "exists": True,
                    "size": stat.st_size,
                    "modified_at": stat.st_mtime,
                })
            except OSError:
                rows.append({
                    "file_key": file_key,
                    "name": name,
                    "exists": False,
                    "size": 0,
                })
        return rows

    async def _fetch_inventory(self) -> dict[str, list[dict]]:
        (
            profiles,
            info,
            options,
            tools,
            mcp,
            plugins,
            skills,
            webhooks,
            channels,
            files,
            health,
            status,
        ) = await asyncio.gather(
            self._dashboard_body("/api/profiles"),
            self._dashboard_body("/api/model/info"),
            self._dashboard_body("/api/model/options"),
            self._dashboard_body("/api/tools/toolsets"),
            self._dashboard_body("/api/mcp/servers"),
            self._dashboard_body("/api/dashboard/plugins/hub"),
            self._dashboard_body("/api/skills"),
            self._dashboard_body("/api/webhooks"),
            self._dashboard_body("/api/messaging/platforms"),
            self._dashboard_body("/api/files"),
            self._dashboard_body("/api/health"),
            self._dashboard_body("/api/status"),
        )
        model_rows: list[dict] = []
        current_model = info.get("model") or options.get("model")
        current_provider = info.get("provider") or options.get("provider")
        for provider in self._list_from(options, "providers"):
            slug = provider.get("slug") or provider.get("id") or provider.get("name")
            capabilities = (
                provider.get("capabilities")
                if isinstance(provider.get("capabilities"), dict)
                else {}
            )
            for model in (
                provider.get("models")
                if isinstance(provider.get("models"), list)
                else []
            ):
                model_id = (
                    str(model.get("id") or model.get("name"))
                    if isinstance(model, dict)
                    else str(model)
                )
                cap = (
                    capabilities.get(model_id, {})
                    if isinstance(capabilities, dict)
                    else {}
                )
                model_rows.append({
                    "id": f"{slug}::{model_id}",
                    "model": model_id,
                    "provider": slug,
                    "provider_name": provider.get("name") or slug,
                    "featured": model_id in (provider.get("featured_models") or []),
                    "authenticated": provider.get("authenticated") is not False,
                    "is_current": slug == current_provider
                    and model_id == current_model,
                    "fast": bool(cap.get("fast")) if isinstance(cap, dict) else False,
                    "reasoning": bool(cap.get("reasoning"))
                    if isinstance(cap, dict)
                    else False,
                    "context": cap.get("context") if isinstance(cap, dict) else None,
                })
        room_bindings, room_sessions = await self._fetch_room_inventory()
        command = {
            "id": "hermes",
            "gateway_state": status.get("gateway_state"),
            "active_sessions": status.get("active_sessions"),
            "active_agents": status.get("active_agents"),
            "cpu_percent": health.get("cpu_percent") or status.get("cpu_percent"),
            "memory_percent": health.get("memory_percent")
            or status.get("memory_percent"),
            "version": status.get("version"),
            "checked_at": time.time(),
        }
        webhook_rows = self._list_from(webhooks, "subscriptions", "webhooks", "items")
        for row in webhook_rows:
            if not row.get("name") and row.get("id"):
                row["name"] = row["id"]
        channel_rows = self._list_from(channels, "platforms", "items")
        for row in channel_rows:
            if not row.get("id"):
                row["id"] = row.get("platform_id") or row.get("platform")
        return {
            "catalog.profiles": self._list_from(profiles, "profiles", "items"),
            "catalog.models": model_rows,
            "catalog.tools": self._list_from(tools, "toolsets", "items"),
            "catalog.mcp": self._list_from(mcp, "servers", "items"),
            "catalog.plugins": self._list_from(plugins, "plugins", "items"),
            "catalog.skills": self._list_from(skills, "skills", "items"),
            "memory.inventory": self._memory_inventory(),
            "config.webhooks": webhook_rows,
            "config.channels": channel_rows,
            "artifacts.metadata": await self._fetch_artifact_metadata(),
            "files.metadata": self._list_from(files, "entries", "items"),
            "rooms.binding": room_bindings,
            "rooms.sessions": room_sessions,
            "action.audit": self.store.list_audit(limit=200),
            "command.status": [command],
        }

    def _fp_inventory(self, data: dict[str, list[dict]]) -> str:
        projected = {
            key: [project_entity(key, row) for row in rows]
            for key, rows in data.items()
        }
        return fingerprint_json(projected)

    async def _on_inventory(
        self, data: dict[str, list[dict]], fp: Optional[str]
    ) -> None:
        self._ensure_delta_state()
        for resource_key, rows in data.items():
            spec = RESOURCE_SPECS[resource_key]
            current = {
                str(projected[spec.entity_key]): projected
                for row in rows
                if (projected := project_entity(resource_key, row)).get(spec.entity_key)
                not in (None, "")
            }
            previous = self._inventory_entities.get(resource_key, {})
            marker = f"inventory:{resource_key}"
            if marker in self._initialized_sources:
                event_type = self._INVENTORY_EVENTS[resource_key]
                for entity_id, projected in current.items():
                    if previous.get(entity_id) != projected:
                        await self.bus.publish(
                            event_type,
                            spec.authority,
                            resource_key,
                            entity_id,
                            projected,
                            coverage="polled",
                            resource_key=resource_key,
                            operation="upsert",
                        )
                for entity_id in set(previous) - set(current):
                    await self.bus.publish(
                        event_type,
                        spec.authority,
                        resource_key,
                        entity_id,
                        {},
                        coverage="polled",
                        resource_key=resource_key,
                        operation="delete",
                    )
            self._initialized_sources.add(marker)
            self._inventory_entities[resource_key] = current

    # Sensitive/on-demand resources publish only invalidation signals. Raw
    # config and log bodies never enter the read model or replay payload.
    async def _fetch_settings_signal(self) -> dict:
        return await self._dashboard_body("/api/config")

    async def _on_settings_signal(self, _data: dict, _fp: Optional[str]) -> None:
        marker = "signal:settings"
        if marker in self._initialized_sources:
            await self.bus.publish(
                "settings.changed",
                "dashboard",
                "settings",
                "",
                {},
                coverage="polled",
                profile_id=getattr(self.cfg, "live_default_profile", "default"),
                resource_key="system.settings",
                operation="invalidate",
            )
        self._initialized_sources.add(marker)

    async def _fetch_logs_signal(self) -> dict:
        return await self._dashboard_body("/api/logs?lines=500")

    async def _on_logs_signal(self, _data: dict, _fp: Optional[str]) -> None:
        marker = "signal:logs"
        if marker in self._initialized_sources:
            await self.bus.publish(
                "logs.changed",
                "dashboard",
                "logs",
                "",
                {},
                coverage="polled",
                profile_id=getattr(self.cfg, "live_default_profile", "default"),
                resource_key="logs.tail",
                operation="invalidate",
            )
        self._initialized_sources.add(marker)

    @staticmethod
    async def _probe_port(service: str, port: int) -> dict:
        started = time.monotonic()
        healthy = False
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("100.100.127.43", port), timeout=2.0
            )
            del reader
            healthy = True
            writer.close()
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            healthy = False
        return {
            "service": service,
            "healthy": healthy,
            "status": "online" if healthy else "offline",
            "checked_at": time.time(),
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }

    async def _fetch_iframe_health(self) -> list[dict]:
        return list(
            await asyncio.gather(
                self._probe_port("llama-proxy", 8082),
                self._probe_port("9router", 20128),
            )
        )

    def _fp_iframe_health(self, data: list[dict]) -> str:
        return fingerprint_json([
            (row.get("service"), row.get("healthy"), row.get("status")) for row in data
        ])

    async def _on_iframe_health(self, data: list[dict], _fp: Optional[str]) -> None:
        current = {
            str(row["service"]): project_entity("iframe.health", row) for row in data
        }
        previous = self._inventory_entities.get("iframe.health", {})
        marker = "inventory:iframe.health"
        if marker in self._initialized_sources:
            for entity_id, row in current.items():
                if previous.get(entity_id) != row:
                    await self.bus.publish(
                        "iframe.changed",
                        "mission-control",
                        "iframe",
                        entity_id,
                        row,
                        coverage="polled",
                        resource_key="iframe.health",
                        operation="upsert",
                    )
        self._initialized_sources.add(marker)
        self._inventory_entities["iframe.health"] = current

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
        "repositories": ("repositories",),
        "system-manager": ("system-manager.inventory",),
        "inventory": tuple(_INVENTORY_EVENTS),
        "iframe-health": ("iframe.health",),
    }

    async def _persist_success(self, name: str, data: Any, fingerprint: str) -> None:
        model = self.read_model
        if model is None or not model.available:
            return
        profile = getattr(self.cfg, "live_default_profile", "default")
        fingerprint = f"p{READ_MODEL_PROJECTOR_VERSION}:{fingerprint}"
        if name == "kanban":
            model.replace_entities(
                "kanban.tasks",
                data.get("tasks", []),
                profile_id=profile,
                fingerprint=fingerprint,
            )
        elif name in {"permits", "issues", "cron", "sessions", "running"}:
            key = self._WORKER_RESOURCES[name][0]
            model.replace_entities(
                key,
                data if isinstance(data, list) else [],
                profile_id=profile,
                fingerprint=fingerprint,
            )
        elif name == "health":
            rows = [
                {
                    "source_id": source,
                    "healthy": bool(healthy),
                    "checked_at": time.time(),
                }
                for source, healthy in (data.items() if isinstance(data, dict) else ())
            ]
            model.replace_entities(
                "source.health", rows, profile_id=profile, fingerprint=fingerprint
            )
        elif name == "analytics":
            summary = dict(data) if isinstance(data, dict) else {}
            if isinstance(summary.get("totals"), dict):
                summary = {**summary, **summary["totals"]}
            model.replace_summary(
                "analytics.usage", summary, profile_id=profile, fingerprint=fingerprint
            )
        elif name in {"repositories", "system-manager"}:
            key = self._WORKER_RESOURCES[name][0]
            model.replace_entities(
                key,
                data if isinstance(data, list) else [],
                profile_id=profile,
                fingerprint=fingerprint,
            )
        elif name == "inventory" and isinstance(data, dict):
            for key, rows in data.items():
                if key not in self._INVENTORY_EVENTS:
                    continue
                model.replace_entities(
                    key,
                    rows if isinstance(rows, list) else [],
                    profile_id=profile,
                    fingerprint=f"{fingerprint}:{key}",
                )
        elif name == "iframe-health":
            model.replace_entities(
                "iframe.health",
                data if isinstance(data, list) else [],
                profile_id=profile,
                fingerprint=fingerprint,
            )

    async def _persist_failure(self, name: str, exc: Exception) -> None:
        if self.read_model is None:
            return
        resources = self._WORKER_RESOURCES.get(name, ())
        if resources:
            self.read_model.record_failure(
                resources,
                exc,
                profile_id=getattr(self.cfg, "live_default_profile", "default"),
            )

    def _worker(
        self, name: str, interval: int, fetch: FetchFn, on_delta, fingerprint_of
    ) -> PollWorker:
        return PollWorker(
            name,
            interval,
            fetch,
            on_delta,
            fingerprint_of,
            self.cfg.poll_backoff_max_seconds,
            on_success=lambda data, fp: self._persist_success(name, data, fp),
            on_failure=lambda exc: self._persist_failure(name, exc),
        )

    # ---- setup -------------------------------------------------------------
    def build(self) -> None:
        self.workers["kanban"] = self._worker(
            "kanban",
            self.cfg.poll_kanban_seconds,
            self._fetch_kanban,
            self._on_kanban,
            self._fp_kanban,
        )
        self.workers["permits"] = self._worker(
            "permits",
            self.cfg.poll_permits_seconds,
            self._fetch_permits,
            self._on_permits,
            self._fp_permits,
        )
        self.workers["issues"] = self._worker(
            "issues",
            self.cfg.poll_issues_seconds,
            self._fetch_issues,
            self._on_issues,
            self._fp_issues,
        )
        self.workers["cron"] = self._worker(
            "cron",
            self.cfg.poll_cron_seconds,
            self._fetch_cron,
            self._on_cron,
            self._fp_cron,
        )
        self.workers["sessions"] = self._worker(
            "sessions",
            self.cfg.poll_sessions_seconds,
            self._fetch_sessions,
            self._on_sessions,
            self._fp_sessions,
        )
        # Faster than the other pollers on purpose: this drives a "running now"
        # indicator, and an indicator that lags the turn it describes by fifteen
        # seconds is worse than none. The response is a short list and costs the
        # gateway a dict walk.
        self.workers["running"] = self._worker(
            "running",
            self.cfg.poll_running_seconds,
            self._fetch_running,
            self._on_running,
            self._fp_running,
        )
        self.workers["health"] = self._worker(
            "health",
            self.cfg.poll_health_seconds,
            self._fetch_health,
            self._on_health,
            self._fp_health,
        )
        self.workers["capabilities"] = PollWorker(
            "capabilities",
            self.cfg.poll_adapter_health_seconds,
            self._fetch_capabilities,
            self._on_capabilities,
            self._fp_capabilities,
            self.cfg.poll_backoff_max_seconds,
        )
        self.workers["analytics"] = self._worker(
            "analytics",
            self.cfg.poll_analytics_seconds,
            self._fetch_analytics,
            self._on_analytics,
            self._fp_analytics,
        )
        if self.repository_service is not None:
            self.workers["repositories"] = self._worker(
                "repositories",
                max(30, self.cfg.poll_adapter_health_seconds),
                self._fetch_repositories,
                self._on_repositories,
                self._fp_repositories,
            )
        if self.system_manager_client is not None:
            self.workers["system-manager"] = self._worker(
                "system-manager",
                max(10, self.cfg.poll_health_seconds),
                self._fetch_system_manager,
                self._on_system_manager,
                self._fp_system_manager,
            )
        self.workers["inventory"] = self._worker(
            "inventory",
            max(30, self.cfg.poll_adapter_health_seconds),
            self._fetch_inventory,
            self._on_inventory,
            self._fp_inventory,
        )
        self.workers["settings-signal"] = PollWorker(
            "settings-signal",
            max(30, self.cfg.poll_adapter_health_seconds),
            self._fetch_settings_signal,
            self._on_settings_signal,
            fingerprint_json,
            self.cfg.poll_backoff_max_seconds,
        )
        self.workers["logs-signal"] = PollWorker(
            "logs-signal",
            max(2, self.cfg.poll_running_seconds),
            self._fetch_logs_signal,
            self._on_logs_signal,
            fingerprint_json,
            self.cfg.poll_backoff_max_seconds,
        )
        self.workers["iframe-health"] = self._worker(
            "iframe-health",
            max(10, self.cfg.poll_health_seconds),
            self._fetch_iframe_health,
            self._on_iframe_health,
            self._fp_iframe_health,
        )

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
