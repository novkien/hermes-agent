"""Federated search (Stage 5 item 4) — merged into agent_mission_control (S8).

Bounded per-source fan-out with per-source timeouts and cancellation.
Sources:
- sessions: adapter GET /sessions/search?q=&limit= (FTS5, bounded) — NEVER
  full-scans state.db (dashboard search_router is FTS5/trigram, u03-query-plans Q6)
- tasks: adapter GET /kanban/tasks?limit=100 then client-side title/assignee/status
  filter (bounded, documented)
- issues: adapter GET /issues?limit=100 then client-side issue/context filter
- permits: adapter GET /permits?limit=100 then client-side filter

Each source -> {source_id, results[], count, timed_out, degraded}.
Global envelope + deep-link hrefs (route + entity id).
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from .clients import AdapterClient

DEEP_LINKS = {
    "sessions": ("/sessions/{id}", "session_id"),
    "tasks": ("/tasks/{id}", "id"),
    "issues": ("/issues/{id}", "id"),
    "permits": ("/permits/{id}", "permit_id"),
}

# Per-source timeout (seconds); bounded fan-out never blocks the BFF.
SOURCE_TIMEOUT_SECONDS = 5.0


def _norm(s: Any) -> str:
    return str(s or "").lower()


def _matches(text: str, q: str) -> bool:
    return _norm(q) in _norm(text)


async def _fetch_with_timeout(coro, timeout: float, default: Any):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        return "__TIMEOUT__"
    except Exception:
        return "__ERROR__"


def _status_tuple(result: Any) -> Optional[tuple[int, Any]]:
    """Normalize adapter responses: S4 client returns (status, body, headers);
    fakes return objects with .status_code/.json()."""
    if isinstance(result, tuple):
        if len(result) >= 2:
            return result[0], result[1]
        return None
    if hasattr(result, "status_code"):
        try:
            return result.status_code, result.json()
        except Exception:
            return result.status_code, None
    return None


def _rows_from(body: Any, keys: tuple[str, ...]) -> list[dict]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in keys:
            v = body.get(k)
            if isinstance(v, list):
                return v
    return []


async def search_sessions(adapter: AdapterClient, q: str, limit: int) -> dict:
    timed_out = False
    degraded = False
    results: list[dict] = []
    try:
        r = await _fetch_with_timeout(
            adapter.session_search(q, limit=limit), SOURCE_TIMEOUT_SECONDS, None
        )
        if r == "__TIMEOUT__":
            timed_out = True
        elif r == "__ERROR__":
            degraded = True
        else:
            st = _status_tuple(r)
            if st is None or st[0] >= 400:
                degraded = True
            else:
                raw = _rows_from(st[1], ("results", "data"))
                for item in raw[:limit]:
                    sid = item.get("session_id") or item.get("id") or ""
                    results.append({
                        "id": sid,
                        "title": item.get("title", ""),
                        "snippet": item.get("snippet", "") or item.get("preview", ""),
                        "href": DEEP_LINKS["sessions"][0].format(id=sid),
                    })
    except Exception:
        degraded = True
    return {
        "source_id": "sessions",
        "results": results[:limit],
        "count": len(results[:limit]),
        "timed_out": timed_out,
        "degraded": degraded,
    }


def _filter_rows(rows: list[dict], q: str, fields: list[str]) -> list[dict]:
    out = []
    for row in rows:
        hay = " ".join(str(row.get(f, "")) for f in fields)
        if _matches(hay, q):
            out.append(row)
    return out


async def search_tasks(adapter: AdapterClient, q: str, limit: int) -> dict:
    timed_out = False
    degraded = False
    results: list[dict] = []
    try:
        r = await _fetch_with_timeout(adapter.tasks(limit=100), SOURCE_TIMEOUT_SECONDS, None)
        if r == "__TIMEOUT__":
            timed_out = True
        elif r == "__ERROR__":
            degraded = True
        else:
            st = _status_tuple(r)
            if st is None or st[0] >= 400:
                degraded = True
            else:
                rows = _rows_from(st[1], ("data", "tasks", "items"))
                matched = _filter_rows(rows, q, ["title", "assignee", "status", "body"])
                for t in matched[:limit]:
                    tid = t.get("id", "")
                    results.append({
                        "id": tid,
                        "title": t.get("title", ""),
                        "assignee": t.get("assignee", ""),
                        "status": t.get("status", ""),
                        "href": DEEP_LINKS["tasks"][0].format(id=tid),
                    })
    except Exception:
        degraded = True
    return {
        "source_id": "tasks",
        "results": results[:limit],
        "count": len(results[:limit]),
        "timed_out": timed_out,
        "degraded": degraded,
    }


async def search_issues(adapter: AdapterClient, q: str, limit: int) -> dict:
    timed_out = False
    degraded = False
    results: list[dict] = []
    try:
        r = await _fetch_with_timeout(adapter.issues_list(limit=100), SOURCE_TIMEOUT_SECONDS, None)
        if r == "__TIMEOUT__":
            timed_out = True
        elif r == "__ERROR__":
            degraded = True
        else:
            st = _status_tuple(r)
            if st is None or st[0] >= 400:
                degraded = True
            else:
                rows = _rows_from(st[1], ("data", "issues", "items"))
                matched = _filter_rows(rows, q, ["issue", "context", "status", "severity"])
                for i in matched[:limit]:
                    iid = i.get("id", "")
                    results.append({
                        "id": str(iid),
                        "issue": i.get("issue", ""),
                        "status": i.get("status", ""),
                        "severity": i.get("severity", ""),
                        "href": DEEP_LINKS["issues"][0].format(id=iid),
                    })
    except Exception:
        degraded = True
    return {
        "source_id": "issues",
        "results": results[:limit],
        "count": len(results[:limit]),
        "timed_out": timed_out,
        "degraded": degraded,
    }


async def search_permits(adapter: AdapterClient, q: str, limit: int) -> dict:
    timed_out = False
    degraded = False
    results: list[dict] = []
    try:
        r = await _fetch_with_timeout(adapter.permits_list(limit=100), SOURCE_TIMEOUT_SECONDS, None)
        if r == "__TIMEOUT__":
            timed_out = True
        elif r == "__ERROR__":
            degraded = True
        else:
            st = _status_tuple(r)
            if st is None or st[0] >= 400:
                degraded = True
            else:
                rows = _rows_from(st[1], ("data", "permits", "items"))
                matched = _filter_rows(rows, q, ["permit_id", "issue_title", "status", "severity", "source"])
                for p in matched[:limit]:
                    pid = p.get("permit_id", "")
                    results.append({
                        "id": pid,
                        "permit_id": pid,
                        "issue_title": p.get("issue_title", ""),
                        "status": p.get("status", ""),
                        "severity": p.get("severity", ""),
                        "href": DEEP_LINKS["permits"][0].format(id=pid),
                    })
    except Exception:
        degraded = True
    return {
        "source_id": "permits",
        "results": results[:limit],
        "count": len(results[:limit]),
        "timed_out": timed_out,
        "degraded": degraded,
    }


async def federated_search(adapter: AdapterClient, q: str, limit: int = 20) -> dict:
    """Fan out to all 4 sources with bounded per-source timeouts + cancellation."""
    per_source_limit = max(1, min(limit, 50))
    coros = [
        search_sessions(adapter, q, per_source_limit),
        search_tasks(adapter, q, per_source_limit),
        search_issues(adapter, q, per_source_limit),
        search_permits(adapter, q, per_source_limit),
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)
    sources: list[dict] = []
    for r in results:
        if isinstance(r, Exception):
            sources.append({
                "source_id": "unknown", "results": [], "count": 0,
                "timed_out": False, "degraded": True,
            })
        else:
            sources.append(r)
    total = sum(s["count"] for s in sources)
    any_degraded = any(s["degraded"] for s in sources)
    any_timeout = any(s["timed_out"] for s in sources)
    return {
        "query": q,
        "limit": limit,
        "total": total,
        "sources": sources,
        "degraded": any_degraded,
        "timed_out": any_timeout,
        "state_db_full_scan": False,  # audited: sessions search is FTS5, others bounded lists
    }
