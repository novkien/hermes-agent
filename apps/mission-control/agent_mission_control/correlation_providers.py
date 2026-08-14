"""Live data providers for the correlation engine and run inspector.

Both engines were constructed with ``providers={}``, so every
``/api/run-inspector/*`` response was an empty graph. The typed adapter methods
they need already existed and were simply never wired.

Rules kept from the engines themselves:
  * every lookup is bounded (a primary key, or a capped list scan);
  * a provider that has no backing upstream method is left out entirely rather
    than faked — the engines treat a missing provider as "no data", which is
    honest, where a fabricated one would not be;
  * nothing here infers a relation. Providers return rows; the engines decide.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from .clients import UpstreamError

logger = logging.getLogger(__name__)

# Reverse lookups have no indexed upstream route, so they scan a bounded page
# and filter client-side — the same pattern workers.py already uses.
_REVERSE_SCAN_LIMIT = 100


def _rows(body: Any, *keys: str) -> list[dict]:
    """Pull a row list out of whatever shape the upstream returned."""
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    if not isinstance(body, dict):
        return []
    for key in keys:
        value = body.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    return []


def _unwrap(body: Any) -> Any:
    """Adapter responses may or may not carry a `data` envelope."""
    if isinstance(body, dict) and "data" in body and "meta" in body:
        return body["data"]
    return body


def build_correlation_providers(adapter, dashboard) -> dict[str, Callable]:
    """One provider map shared by CorrelationEngine and RunInspector.

    Kinds with no backing method (``messages_by_session``,
    ``api_requests_by_session``, ``delegations_by_session``) are deliberately
    absent: the engines' ``_get`` degrades to None/[] for an unknown kind, so
    their edges simply do not appear instead of appearing wrong.
    """

    async def _adapter(call, *args, **kwargs) -> Any:
        try:
            status, body, _ = await call(*args, **kwargs)
        except UpstreamError:
            # A down or slow source degrades to "no data"; that is not an error.
            return None
        except Exception:  # noqa: BLE001
            # Anything else is a bug in this file, not a source outage. Swallowing
            # it silently once cost a fully empty graph, so it gets logged.
            logger.exception("correlation provider failed: %s", getattr(call, "__name__", call))
            return None
        if status >= 400:
            return None
        return _unwrap(body)

    # ---- single-entity lookups (primary key) ----------------------------
    async def task(task_id) -> Optional[dict]:
        body = await _adapter(adapter.kanban_task_detail, str(task_id))
        if isinstance(body, dict):
            return body.get("task") if isinstance(body.get("task"), dict) else body
        return None

    async def session(session_id) -> Optional[dict]:
        # No adapter method exists for a single session; the dashboard has one.
        try:
            status, body, _ = await dashboard.get(f"/api/sessions/{session_id}")
        except Exception:  # noqa: BLE001
            return None
        if status >= 400:
            return None
        body = _unwrap(body)
        if isinstance(body, dict):
            return body.get("session") if isinstance(body.get("session"), dict) else body
        return None

    async def issue(issue_id) -> Optional[dict]:
        body = await _adapter(adapter.issue_detail, str(issue_id))
        if isinstance(body, dict):
            return body.get("issue") if isinstance(body.get("issue"), dict) else body
        return None

    # ---- bounded child lists --------------------------------------------
    async def task_runs(task_id) -> list[dict]:
        return _rows(await _adapter(adapter.kanban_task_runs, str(task_id)), "runs", "items")

    async def task_events(task_id) -> list[dict]:
        return _rows(await _adapter(adapter.kanban_task_events, str(task_id)), "events", "items")

    async def task_attachments(task_id) -> list[dict]:
        return _rows(
            await _adapter(adapter.kanban_task_attachments, str(task_id)),
            "attachments", "items",
        )

    # ---- reverse lookups (bounded scan + client-side filter) ------------
    def _occurrences(issue_row: dict) -> list[dict]:
        occ = issue_row.get("occurrences")
        return [o for o in occ if isinstance(o, dict)] if isinstance(occ, list) else []

    async def _issues_page() -> list[dict]:
        body = await _adapter(adapter.issues_list, limit=_REVERSE_SCAN_LIMIT)
        return _rows(body, "issues", "items")

    async def issue_occurrences_by_task(task_id) -> list[dict]:
        wanted = str(task_id)
        out: list[dict] = []
        for row in await _issues_page():
            for occ in _occurrences(row):
                if str(occ.get("task_ref") or "") == wanted:
                    out.append({**occ, "issue_id": row.get("id")})
        return out

    async def issue_occurrences_by_session(session_id) -> list[dict]:
        wanted = str(session_id)
        out: list[dict] = []
        for row in await _issues_page():
            for occ in _occurrences(row):
                if str(occ.get("session_ref") or "") == wanted:
                    out.append({**occ, "issue_id": row.get("id")})
        return out

    async def permits_for_issue(issue_id) -> list[dict]:
        """Permit→issue is a free-text "Issue N" parse upstream, so the engine
        labels these edges `inferred`. Matching here uses the same parse."""
        from .correlation import parse_permit_issue

        wanted = str(issue_id)
        body = await _adapter(adapter.permits_list, limit=_REVERSE_SCAN_LIMIT)
        out: list[dict] = []
        for row in _rows(body, "permits", "items"):
            title = row.get("issue_title") or row.get("title") or ""
            if parse_permit_issue(str(title)) == wanted:
                out.append({
                    "permit_id": row.get("id") or row.get("permit_id"),
                    "status": row.get("status", ""),
                })
        return out

    async def run(run_id) -> Optional[dict]:
        """No upstream route fetches a run by id; the engine treats None as
        `unsupported`, which is the truthful answer."""
        return None

    return {
        "task": task,
        "session": session,
        "issue": issue,
        "run": run,
        "task_runs": task_runs,
        "task_events": task_events,
        "task_attachments": task_attachments,
        "issue_occurrences_by_task": issue_occurrences_by_task,
        "issue_occurrences_by_session": issue_occurrences_by_session,
        "permits_for_issue": permits_for_issue,
    }
