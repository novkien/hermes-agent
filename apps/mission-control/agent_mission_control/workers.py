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
    ) -> None:
        self.name = name
        self.interval_seconds = interval_seconds
        self.fetch = fetch
        self.on_delta = on_delta
        self.fingerprint_of = fingerprint_of
        self.backoff_max = backoff_max
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
            if fp != self.last_fingerprint:
                await self.on_delta(data, fp)
                self.last_fingerprint = fp
        except Exception as exc:  # noqa: BLE001 - worker must survive
            self.last_error = str(exc)
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
        self.workers: dict[str, PollWorker] = {}
        self._tasks: list[asyncio.Task] = []
        self._source_health: dict[str, bool] = {}
        self._kanban_tasks: list[dict] = []
        # id -> (last_activity, message_count, ended_at) from the previous poll.
        # Empty on the first poll, which is why the first tick emits no targeted
        # session events: everything would look "changed" against nothing.
        self._sessions_by_id: dict[str, tuple] = {}

    def _feed_alerts(self, key: str, data: Any) -> None:
        if self.alert_engine is None:
            return
        try:
            self.alert_engine.set_source_data(key, data)
        except Exception:  # noqa: BLE001 - a poll must never die on alert wiring
            pass

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
            "summary": summary[1] if isinstance(summary, tuple) and summary[0] < 400 else {},
            "tasks": tasks[1] if isinstance(tasks, tuple) and tasks[0] < 400 else [],
        }

    def _fp_kanban(self, data: dict) -> str:
        running = [
            t for t in data.get("tasks", [])
            if t.get("status") in ("running", "ready", "blocked", "todo")
        ]
        return fingerprint_tasks(running)

    async def _on_kanban(self, data: dict, fp: Optional[str]) -> None:
        self._feed_alerts("tasks", data.get("tasks", []))
        prev_tasks = self._kanban_tasks
        cur_tasks = data.get("tasks", [])
        # task.changed on lifecycle field delta per running task
        by_id = {t.get("id"): t for t in cur_tasks}
        prev_by_id = {t.get("id"): t for t in prev_tasks}
        for tid, t in by_id.items():
            if t.get("status") not in ("running", "ready", "blocked", "todo"):
                continue
            prev = prev_by_id.get(tid)
            if prev is None:
                await self.bus.publish(
                    "task.changed", "kanban", "task", tid,
                    {"status": t.get("status"), "current_run_id": t.get("current_run_id"),
                     "last_heartbeat_at": t.get("last_heartbeat_at")},
                )
            else:
                changed = any(
                    t.get(k) != prev.get(k)
                    for k in ("status", "current_run_id", "last_heartbeat_at")
                )
                if changed:
                    await self.bus.publish(
                        "task.changed", "kanban", "task", tid,
                        {"status": t.get("status"), "current_run_id": t.get("current_run_id"),
                         "last_heartbeat_at": t.get("last_heartbeat_at")},
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
        return r[1] if isinstance(r, tuple) and r[0] < 400 else []

    def _fp_permits(self, data: list) -> str:
        return fingerprint_json(
            [(p.get("permit_id"), p.get("status"), p.get("severity"),
              p.get("updated_at"), p.get("expires_at")) for p in data]
        )

    async def _on_permits(self, data: list, fp: Optional[str]) -> None:
        self._feed_alerts("permits", data)
        for p in data:
            await self.bus.publish(
                "permit.changed", "permits", "permit", p.get("permit_id") or "",
                {"status": p.get("status"), "severity": p.get("severity"),
                 "updated_at": p.get("updated_at")},
            )

    # ---- issues ------------------------------------------------------------
    async def _fetch_issues(self) -> list[dict]:
        r = await self.adapter.issues_list(limit=100)
        return r[1] if isinstance(r, tuple) and r[0] < 400 else []

    def _fp_issues(self, data: list) -> str:
        return fingerprint_json(
            [(i.get("id"), i.get("status"), i.get("severity"),
              i.get("last_seen_at"), i.get("updated_at")) for i in data]
        )

    async def _on_issues(self, data: list, fp: Optional[str]) -> None:
        self._feed_alerts("issues", data)
        for i in data:
            await self.bus.publish(
                "issue.changed", "issues", "issue", str(i.get("id") or ""),
                {"status": i.get("status"), "severity": i.get("severity"),
                 "last_seen_at": i.get("last_seen_at")},
            )

    # ---- cron --------------------------------------------------------------
    async def _fetch_cron(self) -> list[dict]:
        s, body, _ = await self.dashboard.get("/api/cron/jobs")
        if s >= 400:
            raise RuntimeError(f"cron fetch failed: {s}")
        jobs = body.get("jobs", body if isinstance(body, list) else [])
        return jobs

    def _fp_cron(self, data: list) -> str:
        return fingerprint_json(
            [(j.get("id"), j.get("state"), j.get("last_run_at"),
              j.get("next_run_at"), j.get("last_status")) for j in data]
        )

    async def _on_cron(self, data: list, fp: Optional[str]) -> None:
        self._feed_alerts("cron", data)
        for j in data:
            await self.bus.publish(
                "cron.changed", "cron", "cron_job", j.get("id") or "",
                {"state": j.get("state"), "last_run_at": j.get("last_run_at"),
                 "next_run_at": j.get("next_run_at"), "last_status": j.get("last_status")},
            )

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
        # The list-level event: "sessions moved, somebody's did". Tabs that
        # render the whole list refresh on this one.
        await self.bus.publish(
            "session.changed", "sessions", "session", "",
            {"count": len(data)}, coverage="polled",
        )
        # Plus one targeted event per row that actually moved. Without these
        # the topic carried an empty `entity_id` and nothing else, so a
        # subscriber watching ONE open conversation — the chat tab does exactly
        # that — had no way to tell whether the session in front of it was the
        # one that advanced. Capped so a fleet-wide burst (a cron sweep touching
        # hundreds of sessions at once) cannot flood the bus; the list-level
        # event above still covers the overflow.
        previous = self._sessions_by_id
        current = {}
        emitted = 0
        for row in data:
            sid = row.get("id") or row.get("session_id")
            if not sid:
                continue
            state = (
                row.get("last_activity_at") or row.get("last_active"),
                row.get("message_count"),
                row.get("ended_at"),
            )
            current[sid] = state
            if previous and previous.get(sid) != state and emitted < 25:
                emitted += 1
                await self.bus.publish(
                    "session.changed", "sessions", "session", str(sid),
                    {"last_activity_at": state[0], "message_count": state[1],
                     "ended_at": state[2]}, coverage="polled",
                )
        self._sessions_by_id = current

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

    # ---- setup -------------------------------------------------------------
    def build(self) -> None:
        self.workers["kanban"] = PollWorker(
            "kanban", self.cfg.poll_kanban_seconds, self._fetch_kanban,
            self._on_kanban, self._fp_kanban, self.cfg.poll_backoff_max_seconds,
        )
        self.workers["permits"] = PollWorker(
            "permits", self.cfg.poll_permits_seconds, self._fetch_permits,
            self._on_permits, self._fp_permits, self.cfg.poll_backoff_max_seconds,
        )
        self.workers["issues"] = PollWorker(
            "issues", self.cfg.poll_issues_seconds, self._fetch_issues,
            self._on_issues, self._fp_issues, self.cfg.poll_backoff_max_seconds,
        )
        self.workers["cron"] = PollWorker(
            "cron", self.cfg.poll_cron_seconds, self._fetch_cron,
            self._on_cron, self._fp_cron, self.cfg.poll_backoff_max_seconds,
        )
        self.workers["sessions"] = PollWorker(
            "sessions", self.cfg.poll_sessions_seconds, self._fetch_sessions,
            self._on_sessions, self._fp_sessions, self.cfg.poll_backoff_max_seconds,
        )
        # Faster than the other pollers on purpose: this drives a "running now"
        # indicator, and an indicator that lags the turn it describes by fifteen
        # seconds is worse than none. The response is a short list and costs the
        # gateway a dict walk.
        self.workers["running"] = PollWorker(
            "running", self.cfg.poll_running_seconds, self._fetch_running,
            self._on_running, self._fp_running, self.cfg.poll_backoff_max_seconds,
        )
        self.workers["health"] = PollWorker(
            "health", self.cfg.poll_health_seconds, self._fetch_health,
            self._on_health, self._fp_health, self.cfg.poll_backoff_max_seconds,
        )
        self.workers["capabilities"] = PollWorker(
            "capabilities", self.cfg.poll_adapter_health_seconds, self._fetch_capabilities,
            self._on_capabilities, self._fp_capabilities, self.cfg.poll_backoff_max_seconds,
        )
        self.workers["analytics"] = PollWorker(
            "analytics", self.cfg.poll_analytics_seconds, self._fetch_analytics,
            self._on_analytics, self._fp_analytics, self.cfg.poll_backoff_max_seconds,
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
