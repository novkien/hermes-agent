"""Run Inspector (Stage 5 item 5).

GET /api/run-inspector/task/{task_id} and /api/run-inspector/session/{session_id}
returning a correlation tree + chronological trajectory.

Timeline builder: merge task_events + run events + session messages metadata +
provider requests + issue occurrences into chronological trajectory with
per-item {source_id, coverage} labels. NEVER infers relations from timestamps.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .correlation import CorrelationEngine, TASK, SESSION


def _json_meta(row: Any) -> dict:
    m = row.get("metadata") if isinstance(row, dict) else {}
    if isinstance(m, str):
        try:
            return json.loads(m)
        except Exception:
            return {}
    return m or {}


def _iso_or_ts(item: Any) -> int:
    """Return an epoch int for sorting; timestamps never used for correlation."""
    if isinstance(item, dict):
        v = item.get("occurred_at") or item.get("timestamp") or item.get("created_at") or item.get("started_at")
        if v is None:
            return 0
        if isinstance(v, (int, float)):
            return int(v)
        # ISO string -> try parse (approximate epoch; sort-only)
        s = str(v)
        try:
            import datetime as _dt

            s2 = s.replace("Z", "+00:00")
            if s2.endswith("+07:00") or "+" in s2:
                return int(_dt.datetime.fromisoformat(s2).timestamp())
            return int(_dt.datetime.fromisoformat(s2).replace(tzinfo=_dt.timezone.utc).timestamp())
        except Exception:
            return 0
    return 0


def _source_coverage(item: dict, default_source: str) -> dict:
    return {
        "source_id": item.get("source_id", default_source),
        "coverage": item.get("coverage", "native"),
    }


class RunInspector:
    def __init__(self, engine: CorrelationEngine, providers: Optional[dict] = None) -> None:
        self.engine = engine
        self.providers = providers or {}
        self.timeline_limit = 200

    async def _get(self, kind: str, entity_id: Any) -> list:
        fn = self.providers.get(kind)
        if fn is None:
            return []
        try:
            v = await fn(entity_id)
            return v if isinstance(v, list) else (v or [])
        except Exception:
            return []

    async def trajectory_for_task(self, task_id: str) -> list[dict]:
        items: list[dict] = []
        for ev in await self._get("task_events", task_id):
            items.append({
                "occurred_at": ev.get("created_at") or ev.get("occurred_at"),
                "kind": ev.get("kind", "task_event"),
                "entity_type": "task",
                "entity_id": task_id,
                "payload": ev.get("payload", {}),
                **{"source_id": "kanban", "coverage": "native"},
            })
        for run in await self._get("task_runs", task_id):
            items.append({
                "occurred_at": run.get("started_at"),
                "kind": "run",
                "entity_type": "run",
                "entity_id": str(run.get("id")),
                "payload": {"status": run.get("status"), "outcome": run.get("outcome"),
                            "profile": run.get("profile")},
                **{"source_id": "kanban", "coverage": "native"},
            })
        for o in await self._get("issue_occurrences_by_task", task_id):
            items.append({
                "occurred_at": o.get("occurred_at"),
                "kind": "issue_occurrence",
                "entity_type": "issue",
                "entity_id": str(o.get("issue_id")),
                "payload": {"event_type": o.get("event_type"), "reporter": o.get("reporter")},
                **{"source_id": "issues", "coverage": "native"},
            })
        items.sort(key=lambda x: _iso_or_ts(x) if x.get("occurred_at") is not None else 0)
        return items[: self.timeline_limit]

    async def trajectory_for_session(self, session_id: str) -> list[dict]:
        items: list[dict] = []
        for m in await self._get("messages_by_session", session_id):
            items.append({
                "occurred_at": m.get("timestamp"),
                "kind": "message",
                "entity_type": "message",
                "entity_id": str(m.get("id")),
                "payload": {"role": m.get("role"), "tool_name": m.get("tool_name"),
                            "tool_call_id": m.get("tool_call_id")},
                **{"source_id": "state.db", "coverage": "native"},
            })
        for r in await self._get("api_requests_by_session", session_id):
            items.append({
                "occurred_at": r.get("captured_at"),
                "kind": "provider_request",
                "entity_type": "api_request",
                "entity_id": str(r.get("api_request_id")),
                "payload": {"provider": r.get("provider"), "model": r.get("model"),
                            "api_mode": r.get("api_mode"), "attempt": r.get("attempt")},
                **{"source_id": "state.db", "coverage": "native"},
            })
        for o in await self._get("issue_occurrences_by_session", session_id):
            items.append({
                "occurred_at": o.get("occurred_at"),
                "kind": "issue_occurrence",
                "entity_type": "issue",
                "entity_id": str(o.get("issue_id")),
                "payload": {"event_type": o.get("event_type")},
                **{"source_id": "issues", "coverage": "native"},
            })
        items.sort(key=lambda x: _iso_or_ts(x) if x.get("occurred_at") is not None else 0)
        return items[: self.timeline_limit]

    async def inspect_task(self, task_id: str) -> dict:
        corr = await self.engine.correlate(TASK, task_id)
        return {
            "root": {"type": TASK, "id": task_id},
            "tree": corr,
            "trajectory": await self.trajectory_for_task(task_id),
        }

    async def inspect_session(self, session_id: str) -> dict:
        corr = await self.engine.correlate(SESSION, session_id)
        return {
            "root": {"type": SESSION, "id": session_id},
            "tree": corr,
            "trajectory": await self.trajectory_for_session(session_id),
        }
