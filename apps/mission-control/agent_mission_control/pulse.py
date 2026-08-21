"""Pulse derivation (Stage 5 item 6).

GET /api/pulse?window=1h|24h|7d deriving {event_count, failures,
active_sessions, running_tasks, pending_permits, open_issues, tokens_in,
tokens_out, cost_estimated, cost_class} from cached adapter/API data via
bounded queries only — NO state.db count(*).
"""
from __future__ import annotations

import time
from typing import Any, Optional

from .store import Store

WINDOWS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}


def cost_class_of(cost_status: Optional[str], estimated: Any) -> str:
    """U-09 mapping: provider-reported | Hermes-calculated |
    estimated-from-verified-rate | unavailable."""
    if cost_status == "included":
        return "Hermes-calculated"
    if cost_status in ("estimated", "actual"):
        return "Hermes-calculated"
    if estimated and float(estimated) > 0:
        return "estimated-from-verified-rate"
    return "unavailable"


class Pulse:
    def __init__(self, store: Store, source_data: Optional[dict] = None) -> None:
        self.store = store
        # source_data injected by routes/workers; tests inject fixtures
        self.source_data: dict = source_data or {}

    def set_source_data(self, source_id: str, data: Any) -> None:
        self.source_data[source_id] = data

    def derive(self, window: str = "24h") -> dict:
        if window not in WINDOWS:
            window = "24h"
        seconds = WINDOWS[window]
        now = int(time.time())

        # event count from the event_replay table (bounded: WHERE occurred_at >= now-seconds)
        cutoff = now - seconds
        event_count = 0
        try:
            row = self.store.conn().execute(
                "SELECT COUNT(*) AS c FROM event_replay WHERE occurred_at>=?", (cutoff,)
            ).fetchone()
            event_count = row["c"] if row else 0
        except Exception:
            event_count = 0

        # failures: R4/R5-style flags from cached data
        failures = 0
        cron_jobs = self.source_data.get("cron", [])
        for j in cron_jobs:
            if j.get("last_status") == "error":
                failures += 1
        tasks = self.source_data.get("tasks", [])
        for t in tasks:
            if t.get("status") in ("failed", "timed_out", "crashed", "gave_up"):
                failures += 1

        sessions = self.source_data.get("sessions", [])
        active_sessions = sum(
            1 for s in sessions if s.get("ended_at") in (None, "", 0)
        )

        running_tasks = sum(1 for t in tasks if t.get("status") == "running")

        permits = self.source_data.get("permits", [])
        pending_permits = sum(
            1 for p in permits if p.get("status") in ("pending_approval",)
        )

        issues = self.source_data.get("issues", [])
        open_issues = sum(1 for i in issues if i.get("status") in ("open", "recurring"))

        # tokens/cost from analytics meta (U-09): tokens are provider-reported;
        # USD is Hermes-calculated estimate.
        analytics = self.source_data.get("analytics", {})
        tokens_in = int(analytics.get("tokens_in", 0) or 0)
        tokens_out = int(analytics.get("tokens_out", 0) or 0)
        cost_estimated = float(analytics.get("cost_estimated", 0) or 0)
        cost_status = analytics.get("cost_status") or "unknown"

        cost_class = cost_class_of(cost_status, cost_estimated)

        return {
            "window": window,
            "generated_at": now,
            "event_count": event_count,
            "failures": failures,
            "active_sessions": active_sessions,
            "running_tasks": running_tasks,
            "pending_permits": pending_permits,
            "open_issues": open_issues,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_estimated": round(cost_estimated, 6),
            "cost_class": cost_class,
            "bounded_queries_only": True,  # no state.db count(*) ever
        }
