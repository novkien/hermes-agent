"""Exact bounded query shapes for the four allowlisted sources.

Every query here is fixed at authoring time: identifiers are literals, values
are bound parameters, every result is LIMIT-bounded in SQL, and only documented
indexed columns are used for filters/sorts. state.db queries are restricted to
the six shapes from u03-query-plans.md and never select content columns.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .config import (
    ATTACHMENT_META_COLUMNS,
    ISSUE_SORTS,
    KANBAN_TASK_SORTS,
    LLM_REQUEST_META_COLUMNS,
    MESSAGE_META_COLUMNS,
    PERMIT_SORTS,
    RUN_META_COLUMNS,
    TASK_CORE_COLUMNS,
    SourceSpec,
)
from .db import KanbanBoardRegistry, SourceStore

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _row_to_dict(row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _parse_events_payload(payload: str | None) -> Any:
    """Return payload JSON, or None when the event carries a REDACTED marker."""
    if payload is None:
        return None
    if "REDACTED" in payload:
        return None
    try:
        return json.loads(payload)
    except (ValueError, TypeError):
        return payload


def _apply_sort_allowlist(sort: str | None, allow: tuple[str, ...]) -> str:
    if sort is None:
        return ""
    if sort not in allow:
        raise ValueError(f"unsupported sort: {sort}")
    return sort


def _page_limit(page: int | None, limit: int | None, max_limit: int) -> tuple[int, int]:
    try:
        p = int(page) if page is not None else 1
        l = int(limit) if limit is not None else 50
    except (TypeError, ValueError):
        raise ValueError("page/limit must be integers")
    if p < 1:
        raise ValueError("page must be >= 1")
    if l < 1 or l > max_limit:
        raise ValueError(f"limit must be between 1 and {max_limit}")
    return p, l


def _clamp_int(value: int | None, default: int, lo: int, hi: int, name: str) -> int:
    try:
        v = int(value) if value is not None else default
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")
    if v < lo or v > hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return v


def _iso(value: float | int | None) -> str | None:
    if value is None:
        return None
    import datetime

    try:
        ts = float(value)
        if ts > 10_000_000_000:  # ms
            ts /= 1000.0
        return datetime.datetime.fromtimestamp(
            ts, tz=datetime.timezone(datetime.timedelta(hours=7))
        ).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _meta(
    source_id: str,
    fingerprint: str,
    count: int,
    page: int,
    limit: int,
    query_ms: float,
    schema_drift: bool = False,
    note: str | None = None,
) -> dict[str, Any]:
    m: dict[str, Any] = {
        "source_id": source_id,
        "schema_fingerprint": fingerprint,
        "fetched_at": _iso(time.time()),
        "count": count,
        "page": page,
        "limit": limit,
        "query_ms": round(query_ms, 3),
    }
    if schema_drift:
        m["schema_drift"] = True
    if note:
        m["note"] = note
    return m


# ---------------------------------------------------------------------------
# kanban
# ---------------------------------------------------------------------------

KANBAN_CORE_SQL = ", ".join(TASK_CORE_COLUMNS)


def kanban_list_tasks(
    store: SourceStore,
    status: str | None = None,
    assignee: str | None = None,
    profile: str | None = None,
    sort: str | None = None,
    page: int | None = None,
    limit: int | None = None,
    budget_ms: int | None = None,
    board: str | None = None,
) -> dict[str, Any]:
    p, l = _page_limit(page, limit, 100)
    sort_col = _apply_sort_allowlist(sort, KANBAN_TASK_SORTS) or "created_at"
    where: list[str] = []
    params: list[Any] = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if assignee is not None:
        where.append("assignee = ?")
        params.append(assignee)
    if profile is not None:
        where.append("assignee = ?")
        params.append(profile)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    order = f" ORDER BY {sort_col} DESC"
    offset = (p - 1) * l
    t0 = time.perf_counter()
    rows = store.query(
        f"SELECT {KANBAN_CORE_SQL} FROM tasks{where_sql}{order} LIMIT ? OFFSET ?",
        params + [l, offset],
        timeout_ms=budget_ms,
    )
    query_ms = (time.perf_counter() - t0) * 1000
    data = []
    for r in rows:
        d = _row_to_dict(r)
        if board is not None:
            d["board"] = board
        data.append(d)
    meta = _meta("kanban", store.fingerprint(), len(rows), p, l, query_ms)
    if board is not None:
        meta["board"] = board
    return {"data": data, "meta": meta}


# ---------------------------------------------------------------------------
# worker-session resolver
# ---------------------------------------------------------------------------

# Every Kanban worker session's first stored `user` message is exactly this
# prefix followed by the card id. It is the only reliable link between a task
# row and the session that actually executed it — see the docstring on
# resolve_worker_session for why `tasks.session_id` cannot be used instead.
_WORKER_PROMPT_PREFIX = "work kanban task "


def _worker_anchor_matches(store: SourceStore, card_id: str) -> list[tuple[int, str, str]]:
    """Return (message_id, session_id, content) for stored user messages whose
    text is the worker anchor prompt for card_id, content used only to verify
    the match in-process and never returned to a caller of this module."""
    has_fts = store.query(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    )
    if has_fts:
        try:
            phrase = f'content : "{_WORKER_PROMPT_PREFIX}{card_id}"'
            rows = store.query(
                "SELECT m.id, m.session_id, m.content FROM messages_fts AS f "
                "JOIN messages AS m ON m.id = f.rowid "
                "WHERE messages_fts MATCH ? AND m.role = 'user'",
                (phrase,),
            )
            return [(int(r["id"]), str(r["session_id"]), r["content"] or "") for r in rows]
        except sqlite3.Error:
            pass
    rows = store.query(
        "SELECT id, session_id, content FROM messages WHERE role = 'user' AND content LIKE ?",
        (f"{_WORKER_PROMPT_PREFIX}{card_id}%",),
    )
    return [(int(r["id"]), str(r["session_id"]), r["content"] or "") for r in rows]


def _worker_anchor_matches_card(content: str, card_id: str) -> bool:
    stripped = content.strip()
    if not stripped.lower().startswith(_WORKER_PROMPT_PREFIX):
        return False
    remainder = stripped[len(_WORKER_PROMPT_PREFIX):].strip()
    if not remainder.startswith(card_id):
        return False
    tail = remainder[len(card_id):]
    return tail == "" or not (tail[0].isalnum() or tail[0] == "_")


def resolve_worker_session(
    default_store: SourceStore,
    profile_stores: list[tuple[str, SourceStore]],
    card_id: str,
) -> dict[str, Any] | None:
    """Resolve the real Hermes session that executed a Kanban worker card.

    `tasks.session_id` (the column the Kanban tab shows today) records the
    agent session that *created* the card, not the worker session that ran
    it — a worker's real conversation lives in a separate per-profile
    database (~/.hermes/profiles/<profile>/state.db), which nothing else in
    this adapter reads. Every worker session's first stored `user` message is
    exactly `work kanban task <card_id>`; that anchor is the only reliable
    link, per ~/.hermes/skills/system-handler/session-logs-trace on the
    Hermes host (this mirrors that skill's L1 resolution level only — no
    log-footer/metadata corroboration, no time-window fallback).
    """
    candidates: list[dict[str, Any]] = []
    for label, store in [("default", default_store), *profile_stores]:
        try:
            matches = _worker_anchor_matches(store, card_id)
        except sqlite3.Error:
            continue
        for message_id, session_id, content in matches:
            if not _worker_anchor_matches_card(content, card_id):
                continue
            session_rows = store.query(
                "SELECT id, source, session_key, profile_name, started_at, "
                "ended_at, end_reason, message_count, title FROM sessions "
                "WHERE id = ?",
                (session_id,),
            )
            if not session_rows:
                continue
            first_rows = store.query(
                "SELECT MIN(id) AS first_id FROM messages "
                "WHERE session_id = ? AND role = 'user'",
                (session_id,),
            )
            first_id = first_rows[0]["first_id"] if first_rows else None
            is_first = first_id == message_id
            session = _row_to_dict(session_rows[0])
            for field in ("started_at", "ended_at"):
                if session.get(field) is not None:
                    session[f"{field}_iso_utc"] = _epoch_to_iso(session[field])
            candidates.append(
                {
                    "session_id": session_id,
                    "profile": label,
                    "method": "worker_prompt_anchor" if is_first else "worker_prompt_mention",
                    "confidence": "high" if is_first else "medium",
                    "session": session,
                }
            )
    if not candidates:
        return None
    candidates.sort(
        key=lambda c: (
            0 if c["confidence"] == "high" else 1,
            -(c["session"].get("started_at") or 0),
        )
    )
    best = candidates[0]
    return {
        "session_id": best["session_id"],
        "profile": best["profile"],
        "method": best["method"],
        "confidence": best["confidence"],
        "session": best["session"],
        "candidate_count": len(candidates),
    }


def _epoch_to_iso(value: Any) -> str | None:
    try:
        import datetime

        return (
            datetime.datetime.fromtimestamp(float(value), tz=datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, OSError):
        return None


def kanban_task_detail(
    store: SourceStore, task_id: str, board: str | None = None
) -> dict[str, Any] | None:
    t0 = time.perf_counter()
    rows = store.query("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not rows:
        return None
    task = _row_to_dict(rows[0])
    if board is not None:
        task["board"] = board

    children = [
        r["child_id"]
        for r in store.query(
            "SELECT child_id FROM task_links WHERE parent_id = ? "
            "ORDER BY child_id",
            (task_id,),
        )
    ]
    parents = [
        r["parent_id"]
        for r in store.query(
            "SELECT parent_id FROM task_links WHERE child_id = ? "
            "ORDER BY parent_id",
            (task_id,),
        )
    ]
    comments = [
        _row_to_dict(r)
        for r in store.query(
            "SELECT id, task_id, author, body, created_at FROM task_comments "
            "WHERE task_id = ? ORDER BY created_at, id",
            (task_id,),
        )
    ]
    attachments = [
        _row_to_dict(r)
        for r in store.query(
            "SELECT id, task_id, filename, content_type, size, uploaded_by, "
            "created_at FROM task_attachments WHERE task_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (task_id,),
        )
    ]
    task["children"] = children
    task["parents"] = parents
    task["comments"] = comments
    task["attachments"] = attachments
    query_ms = (time.perf_counter() - t0) * 1000
    meta = _meta("kanban", store.fingerprint(), 1, 1, 1, query_ms)
    if board is not None:
        meta["board"] = board
    return {"data": task, "meta": meta}


def kanban_task_events(
    store: SourceStore,
    task_id: str,
    cursor: int = 0,
    limit: int = 200,
    budget_ms: int | None = None,
) -> dict[str, Any] | None:
    limit = _clamp_int(limit, 200, 1, 200, "limit")
    cursor = _clamp_int(cursor, 0, 0, 2**63 - 1, "cursor")
    t0 = time.perf_counter()
    exists = store.query("SELECT 1 FROM tasks WHERE id = ?", (task_id,))
    if not exists:
        return None
    rows = store.query(
        "SELECT id, task_id, run_id, kind, created_at, payload FROM task_events "
        "WHERE task_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
        (task_id, cursor, limit),
        timeout_ms=budget_ms,
    )
    events = []
    for r in rows:
        d = _row_to_dict(r)
        d["payload"] = _parse_events_payload(r["payload"])
        events.append(d)
    query_ms = (time.perf_counter() - t0) * 1000
    return {
        "data": events,
        "meta": _meta("kanban", store.fingerprint(), len(events), 1, limit, query_ms),
    }


def kanban_task_runs(
    store: SourceStore,
    task_id: str,
    limit: int = 50,
    budget_ms: int | None = None,
) -> dict[str, Any] | None:
    limit = _clamp_int(limit, 50, 1, 50, "limit")
    t0 = time.perf_counter()
    exists = store.query("SELECT 1 FROM tasks WHERE id = ?", (task_id,))
    if not exists:
        return None
    cols = ", ".join(RUN_META_COLUMNS)
    rows = store.query(
        f"SELECT {cols} FROM task_runs WHERE task_id = ? "
        "ORDER BY started_at DESC, id DESC LIMIT ?",
        (task_id, limit),
        timeout_ms=budget_ms,
    )
    query_ms = (time.perf_counter() - t0) * 1000
    return {
        "data": [_row_to_dict(r) for r in rows],
        "meta": _meta("kanban", store.fingerprint(), len(rows), 1, limit, query_ms),
    }


def kanban_task_attachments(
    store: SourceStore,
    task_id: str,
    limit: int = 20,
    budget_ms: int | None = None,
) -> dict[str, Any] | None:
    limit = _clamp_int(limit, 20, 1, 20, "limit")
    t0 = time.perf_counter()
    exists = store.query("SELECT 1 FROM tasks WHERE id = ?", (task_id,))
    if not exists:
        return None
    cols = ", ".join(ATTACHMENT_META_COLUMNS)
    rows = store.query(
        f"SELECT {cols} FROM task_attachments WHERE task_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (task_id, limit),
        timeout_ms=budget_ms,
    )
    query_ms = (time.perf_counter() - t0) * 1000
    return {
        "data": [_row_to_dict(r) for r in rows],
        "meta": _meta("kanban", store.fingerprint(), len(rows), 1, limit, query_ms),
    }


def kanban_board_summary(
    store: SourceStore,
    budget_ms: int | None = None,
    board: str | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    by_status = [
        {"status": r[0], "count": r[1]}
        for r in store.query(
            "SELECT status, COUNT(*) FROM tasks GROUP BY status "
            "ORDER BY COUNT(*) DESC, status",
            timeout_ms=budget_ms,
        )
    ]
    total = int(store.query("SELECT COUNT(*) FROM tasks")[0][0])
    by_assignee = [
        {"assignee": r[0], "count": r[1]}
        for r in store.query(
            "SELECT assignee, COUNT(*) FROM tasks GROUP BY assignee "
            "ORDER BY COUNT(*) DESC, assignee LIMIT 10",
            timeout_ms=budget_ms,
        )
    ]
    running = int(
        store.query("SELECT COUNT(*) FROM tasks WHERE status = 'running'")[0][0]
    )
    blocked = int(
        store.query("SELECT COUNT(*) FROM tasks WHERE status = 'blocked'")[0][0]
    )
    query_ms = (time.perf_counter() - t0) * 1000
    data = {
        "tasks_by_status": by_status,
        "total_tasks": total,
        "tasks_by_assignee": by_assignee,
        "running_count": running,
        "blocked_count": blocked,
    }
    meta = _meta("kanban", store.fingerprint(), 1, 1, 1, query_ms)
    if board is not None:
        meta["board"] = board
    return {"data": data, "meta": meta}


# ---------------------------------------------------------------------------
# multi-board kanban (S8-I-FIX D1)
# ---------------------------------------------------------------------------

KANBAN_BOARD_TABLES = (
    "tasks",
    "task_links",
    "task_comments",
    "task_events",
    "task_runs",
    "task_attachments",
    "kanban_notify_subs",
)


def kanban_boards_list(registry: KanbanBoardRegistry) -> dict[str, Any]:
    """GET /kanban/boards — indexed per-board counts + fingerprints."""
    t0 = time.perf_counter()
    boards = []
    for name in registry.board_names():
        store = registry._store(name)
        fp = store.fingerprint()
        try:
            task_count = int(store.query("SELECT COUNT(*) FROM tasks")[0][0])
            running = int(
                store.query(
                    "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
                )[0][0]
            )
            blocked = int(
                store.query(
                    "SELECT COUNT(*) FROM tasks WHERE status = 'blocked'"
                )[0][0]
            )
        except Exception:
            task_count = running = blocked = 0
        boards.append(
            {
                "board": name,
                "task_count": task_count,
                "running_count": running,
                "blocked_count": blocked,
                "fingerprint": fp,
            }
        )
    query_ms = (time.perf_counter() - t0) * 1000
    return {
        "data": boards,
        "meta": {
            "source_id": "kanban",
            "count": len(boards),
            "query_ms": round(query_ms, 3),
            "note": "boards discovered under ~/.hermes/kanban/boards/",
        },
    }


def kanban_boards_capabilities(registry: KanbanBoardRegistry) -> dict[str, Any]:
    """Per-board fingerprints + bounded row counts for /capabilities."""
    boards = []
    for name in registry.board_names():
        store = registry._store(name)
        fp = store.fingerprint()
        entry: dict[str, Any] = {"board": name, "schema_fingerprint": fp}
        try:
            current = store.fingerprint(recompute=True)
            entry["schema_drift"] = current != fp
        except Exception:
            entry["schema_drift"] = False
        entry["row_counts"] = {}
        for table in KANBAN_BOARD_TABLES:
            try:
                entry["row_counts"][table] = store.row_count(table)
            except Exception:
                entry["row_counts"][table] = None
        boards.append(entry)
    return {"boards": boards}


def kanban_resolve_task(
    registry: KanbanBoardRegistry, task_id: str, budget_ms: int | None = None
) -> tuple[SourceStore, str, dict[str, Any]] | None:
    """Resolve a task across all boards; first match wins (board order).

    Returns (store, board, detail-payload) so the caller can serve the same
    board's events/runs/attachments, or None when the id exists nowhere.
    """
    for name in registry.board_names():
        store = registry._store(name)
        if store.query("SELECT 1 FROM tasks WHERE id = ?", (task_id,)):
            detail = kanban_task_detail(store, task_id, board=name)
            # The task database is live and can change between these two
            # reads. Treat a concurrently removed task as not found instead
            # of raising and destabilizing the request worker.
            if detail is not None:
                return store, name, detail
    return None


def kanban_board_summary_all(
    registry: KanbanBoardRegistry,
    permits_store: SourceStore,
    issues_store: SourceStore,
    budget_ms: int | None = None,
) -> dict[str, Any]:
    """GET /kanban/board/summary?board=all — per-board summaries + totals."""
    t0 = time.perf_counter()
    boards = []
    total_tasks = 0
    total_running = 0
    total_blocked = 0
    for name in registry.board_names():
        summary = kanban_board_summary(registry._store(name), budget_ms)
        d = summary["data"]
        boards.append({"board": name, **d})
        total_tasks += d["total_tasks"]
        total_running += d["running_count"]
        total_blocked += d["blocked_count"]
    extras = board_summary_extras(permits_store, issues_store, budget_ms)
    totals = {
        "total_tasks": total_tasks,
        "running_count": total_running,
        "blocked_count": total_blocked,
        **extras,
    }
    query_ms = (time.perf_counter() - t0) * 1000
    return {
        "data": {
            "boards": boards,
            "totals": totals,
            "note": "per-board summaries for all boards under ~/.hermes/kanban/boards/",
        },
        "meta": {
            "source_id": "kanban",
            "board": "all",
            "count": len(boards),
            "query_ms": round(query_ms, 3),
        },
    }


# ---------------------------------------------------------------------------
# permits
# ---------------------------------------------------------------------------


def _permits_where(
    status: str | None,
    severity: str | None,
    approved: str | None,
    executed: str | None,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if severity is not None:
        where.append("severity = ?")
        params.append(severity)
    if approved is not None:
        where.append("approved = ?")
        params.append(approved)
    if executed is not None:
        where.append("executed = ?")
        params.append(executed)
    return ((" WHERE " + " AND ".join(where)) if where else ""), params


def permits_list(
    store: SourceStore,
    status: str | None = None,
    severity: str | None = None,
    approved: str | None = None,
    executed: str | None = None,
    sort: str | None = None,
    page: int | None = None,
    limit: int | None = None,
    budget_ms: int | None = None,
) -> dict[str, Any]:
    p, l = _page_limit(page, limit, 100)
    sort_col = _apply_sort_allowlist(sort, PERMIT_SORTS) or "created_at"
    where, params = _permits_where(status, severity, approved, executed)
    offset = (p - 1) * l
    t0 = time.perf_counter()
    rows = store.query(
        f"SELECT * FROM permits{where} ORDER BY {sort_col} DESC LIMIT ? OFFSET ?",
        params + [l, offset],
        timeout_ms=budget_ms,
    )
    query_ms = (time.perf_counter() - t0) * 1000
    return {
        "data": [_row_to_dict(r) for r in rows],
        "meta": _meta("permits", store.fingerprint(), len(rows), p, l, query_ms),
    }


def permit_detail(store: SourceStore, permit_id: str) -> dict[str, Any] | None:
    t0 = time.perf_counter()
    rows = store.query("SELECT * FROM permits WHERE permit_id = ?", (permit_id,))
    if not rows:
        return None
    query_ms = (time.perf_counter() - t0) * 1000
    return {
        "data": _row_to_dict(rows[0]),
        "meta": _meta("permits", store.fingerprint(), 1, 1, 1, query_ms),
    }


# ---------------------------------------------------------------------------
# issues
# ---------------------------------------------------------------------------


def issues_list(
    store: SourceStore,
    status: str | None = None,
    severity: str | None = None,
    sort: str | None = None,
    page: int | None = None,
    limit: int | None = None,
    budget_ms: int | None = None,
) -> dict[str, Any]:
    p, l = _page_limit(page, limit, 100)
    sort_col = _apply_sort_allowlist(sort, ISSUE_SORTS) or "id"
    # A soft-deleted issue (deleted_at set by agent_notes_db.py `update
    # --json {"delete":true}`) must never resurface in a list, regardless of
    # what status/severity the caller asks for.
    where: list[str] = ["deleted_at IS NULL"]
    params: list[Any] = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if severity is not None:
        where.append("severity = ?")
        params.append(severity)
    where_sql = " WHERE " + " AND ".join(where)
    # occurrence_count lives on issues; the list returns all issue columns.
    offset = (p - 1) * l
    t0 = time.perf_counter()
    rows = store.query(
        f"SELECT * FROM issues{where_sql} ORDER BY {sort_col} DESC LIMIT ? OFFSET ?",
        params + [l, offset],
        timeout_ms=budget_ms,
    )
    query_ms = (time.perf_counter() - t0) * 1000
    return {
        "data": [_row_to_dict(r) for r in rows],
        "meta": _meta("issues", store.fingerprint(), len(rows), p, l, query_ms),
    }


def issue_detail(
    store: SourceStore,
    issue_id: int,
    occurrence_limit: int = 50,
    budget_ms: int | None = None,
) -> dict[str, Any] | None:
    occurrence_limit = _clamp_int(occurrence_limit, 50, 1, 50, "occurrence_limit")
    t0 = time.perf_counter()
    rows = store.query("SELECT * FROM issues WHERE id = ?", (issue_id,))
    if not rows:
        return None
    issue = _row_to_dict(rows[0])
    occurrences = [
        _row_to_dict(r)
        for r in store.query(
            "SELECT * FROM issue_occurrences WHERE issue_id = ? "
            "ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (issue_id, occurrence_limit),
            timeout_ms=budget_ms,
        )
    ]
    issue["occurrences"] = occurrences
    query_ms = (time.perf_counter() - t0) * 1000
    return {
        "data": issue,
        "meta": _meta("issues", store.fingerprint(), 1, 1, 1, query_ms),
    }


def issues_open_count(store: SourceStore, budget_ms: int | None = None) -> int:
    return int(
        store.query(
            "SELECT COUNT(*) FROM issues WHERE status = 'open' AND deleted_at IS NULL",
            timeout_ms=budget_ms,
        )[0][0]
    )


def issues_by_severity(store: SourceStore, budget_ms: int | None = None) -> list[dict]:
    return [
        {"severity": r[0], "count": r[1]}
        for r in store.query(
            "SELECT severity, COUNT(*) FROM issues GROUP BY severity "
            "ORDER BY COUNT(*) DESC, severity",
            timeout_ms=budget_ms,
        )
    ]


def permits_open_count(store: SourceStore, budget_ms: int | None = None) -> int:
    return int(
        store.query(
            "SELECT COUNT(*) FROM permits WHERE status = 'pending_approval'",
            timeout_ms=budget_ms,
        )[0][0]
    )


def permits_by_severity(store: SourceStore, budget_ms: int | None = None) -> list[dict]:
    return [
        {"severity": r[0], "count": r[1]}
        for r in store.query(
            "SELECT severity, COUNT(*) FROM permits GROUP BY severity "
            "ORDER BY COUNT(*) DESC, severity",
            timeout_ms=budget_ms,
        )
    ]


def board_summary_extras(
    permits_store: SourceStore,
    issues_store: SourceStore,
    budget_ms: int | None = None,
) -> dict[str, Any]:
    return {
        "open_permits": permits_open_count(permits_store, budget_ms),
        "open_issues": issues_open_count(issues_store, budget_ms),
        "issues_by_severity": issues_by_severity(issues_store, budget_ms),
        "permits_by_severity": permits_by_severity(permits_store, budget_ms),
    }


# ---------------------------------------------------------------------------
# state.db - STRICT bounded queries only
# ---------------------------------------------------------------------------

SESSION_META_SQL = (
    "id, source, user_id, session_key, chat_id, chat_type, thread_id, "
    "display_name, expiry_finalized, model, model_config, system_prompt_hash, "
    "parent_session_id, started_at, ended_at, end_reason, message_count, "
    "tool_call_count, input_tokens, output_tokens, cache_read_tokens, "
    "cache_write_tokens, reasoning_tokens, cwd, git_branch, git_repo_root, "
    "billing_provider, billing_base_url, billing_mode, estimated_cost_usd, "
    "actual_cost_usd, cost_status, cost_source, pricing_version, title, "
    "last_activity_at, last_activity_description, last_activity_provenance, "
    "api_call_count, handoff_state, handoff_platform, handoff_error, "
    "profile_name, archived, pinned, last_read_at"
)
MESSAGE_META_SQL = ", ".join(MESSAGE_META_COLUMNS)
LLM_META_SQL = ", ".join(LLM_REQUEST_META_COLUMNS)


def state_session_by_id(store: SourceStore, session_id: str) -> dict[str, Any] | None:
    rows = store.query(
        f"SELECT {SESSION_META_SQL} FROM sessions WHERE id = ?",
        (session_id,),
    )
    return _row_to_dict(rows[0]) if rows else None


def state_recent_sessions(
    store: SourceStore, limit: int = 50
) -> list[dict[str, Any]]:
    limit = _clamp_int(limit, 50, 1, 50, "limit")
    rows = store.query(
        f"SELECT {SESSION_META_SQL} FROM sessions "
        "ORDER BY started_at DESC LIMIT ?",
        (limit,),
    )
    return [_row_to_dict(r) for r in rows]


def state_room_thread_sessions(
    store: SourceStore, chat_id: str, limit: int = 4000
) -> list[dict[str, Any]]:
    """Every session in a room chat, as id -> thread_id pairs.

    `state_room_sessions` answers occupancy — "who holds this thread now" — so
    it deliberately returns one chain tip per thread. Attribution is a different
    question with a different answer: a kanban card records the session that
    created it, and a manager thread accumulates hundreds of sessions across its
    resets and compressions (thread 36319 on this deployment has created 205
    cards across five boards). Tips alone can therefore attribute only the
    handful of cards made since the last reset, and every older card looks
    ownerless.

    This returns the two columns needed to join by thread and nothing else, so
    the caller can attribute rows for a whole room in one bounded read.
    """
    limit = _clamp_int(limit, 4000, 1, 20000, "limit")
    rows = store.query(
        "SELECT id, thread_id FROM sessions "
        "WHERE chat_id = ? AND thread_id IS NOT NULL "
        "ORDER BY started_at DESC LIMIT ?",
        (chat_id, limit),
    )
    return [_row_to_dict(r) for r in rows]


def state_room_sessions(
    store: SourceStore, chat_id: str, limit: int = 200
) -> list[dict[str, Any]]:
    """One live session per thread for a room chat, resolving chain tips.

    Hermes ends a thread's session with end_reason='session_reset' (or a
    compression continuation) and starts a successor carrying
    parent_session_id. The dashboard's own list hides those successors and
    surfaces the dead chain root, so a thread appears frozen at the moment it
    was last reset. Occupancy is a "which session is live on this thread now"
    question, so this returns the newest-started row per thread_id — which is
    exactly the tip of whatever reset/compression chain the thread is on —
    plus how many earlier sessions that thread has accumulated.
    """
    limit = _clamp_int(limit, 200, 1, 500, "limit")
    rows = store.query(
        f"SELECT {SESSION_META_SQL} FROM sessions s "
        "WHERE chat_id = ? AND thread_id IS NOT NULL "
        "AND started_at = (SELECT MAX(started_at) FROM sessions t "
        "                  WHERE t.chat_id = s.chat_id "
        "                    AND t.thread_id = s.thread_id) "
        "ORDER BY started_at DESC LIMIT ?",
        (chat_id, limit),
    )
    out = []
    for r in rows:
        row = _row_to_dict(r)
        prior = store.query(
            "SELECT COUNT(*) AS n FROM sessions "
            "WHERE chat_id = ? AND thread_id = ? AND id != ?",
            (chat_id, row.get("thread_id"), row.get("id")),
        )
        row["prior_sessions"] = _row_to_dict(prior[0]).get("n", 0) if prior else 0
        # state.db has no is_active column; a session is live exactly while it
        # has not been ended. The dashboard list contract exposes this as
        # is_active, so mirror that name for callers joining the two sources.
        row["is_active"] = row.get("ended_at") is None
        out.append(row)
    return out


def state_messages_meta(
    store: SourceStore, session_id: str, limit: int = 200
) -> list[dict[str, Any]]:
    limit = _clamp_int(limit, 200, 1, 200, "limit")
    rows = store.query(
        f"SELECT {MESSAGE_META_SQL} FROM messages WHERE session_id = ? "
        "ORDER BY timestamp, id LIMIT ?",
        (session_id, limit),
    )
    return [_row_to_dict(r) for r in rows]


def state_model_usage(
    store: SourceStore, session_id: str
) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT session_id, model, billing_provider, billing_base_url, "
        "billing_mode, task, api_call_count, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens, reasoning_tokens, "
        "estimated_cost_usd, actual_cost_usd, cost_status, cost_source, "
        "first_seen, last_seen FROM session_model_usage WHERE session_id = ?",
        (session_id,),
    )
    return [_row_to_dict(r) for r in rows]


def state_llm_requests(
    store: SourceStore, session_id: str, limit: int = 100
) -> list[dict[str, Any]]:
    limit = _clamp_int(limit, 100, 1, 100, "limit")
    try:
        rows = store.query(
            f"SELECT {LLM_META_SQL} FROM llm_provider_requests WHERE session_id = ? "
            "ORDER BY captured_at, id LIMIT ?",
            (session_id, limit),
        )
    except sqlite3.OperationalError:
        # llm_provider_requests is written by a newer Hermes than some live
        # state.db files carry. Provider-request metadata is an enrichment of
        # the timeline, not its spine, so a deployment without the table gets a
        # timeline without that section instead of a 500 that hides the
        # transcript entirely.
        return []
    return [_row_to_dict(r) for r in rows]


def state_session_timeline(
    store: SourceStore,
    session_id: str,
    limit: int = 200,
    budget_ms: int | None = None,
) -> dict[str, Any] | None:
    limit = _clamp_int(limit, 200, 1, 200, "limit")
    t0 = time.perf_counter()
    session = state_session_by_id(store, session_id)
    if session is None:
        return None
    session.pop("system_prompt", None)
    messages = state_messages_meta(store, session_id, limit)
    model_usage = state_model_usage(store, session_id)
    provider_requests = state_llm_requests(store, session_id, 100)
    query_ms = (time.perf_counter() - t0) * 1000
    return {
        "data": {
            "session": session,
            "messages": messages,
            "model_usage": model_usage,
            "provider_requests": provider_requests,
        },
        "meta": _meta(
            "state",
            store.fingerprint(),
            len(messages),
            1,
            limit,
            query_ms,
            note="state.db timeline (metadata only; message content never read)",
        ),
    }


def state_search_sessions(
    store: SourceStore,
    q: str,
    limit: int = 200,
    budget_ms: int | None = None,
) -> dict[str, Any]:
    """FTS5 search over messages content via messages_fts MATCH.

    Returns session_id/timestamp/role metadata joined back through the FTS
    rowid. No LIKE fallback is ever used.
    """
    limit = _clamp_int(limit, 200, 1, 200, "limit")
    if q is None or len(q.strip()) < 3:
        raise ValueError("q must be at least 3 characters")
    query = q.strip()
    t0 = time.perf_counter()
    rows = store.query(
        "SELECT m.id, m.session_id, m.role, m.tool_call_id, m.tool_name, "
        "m.timestamp, m.token_count, m.finish_reason, "
        "snippet(messages_fts, 0, '[', ']', '...', 12) AS snippet "
        "FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid "
        "WHERE messages_fts MATCH ? LIMIT ?",
        (query, limit),
        timeout_ms=budget_ms or 10_000,
    )
    query_ms = (time.perf_counter() - t0) * 1000
    data = []
    for r in rows:
        d = _row_to_dict(r)
        d.pop("content", None)
        data.append(d)
    return {
        "data": data,
        "meta": _meta("state", store.fingerprint(), len(data), 1, limit, query_ms),
    }


def state_session_tips(
    store: SourceStore, session_ids: list[str], max_depth: int = 50
) -> dict[str, dict[str, Any]]:
    """Resolve each given session id to the live tip of its reset chain.

    Hermes ends a thread's session with end_reason='session_reset' (or a
    compression continuation) and starts a successor carrying
    parent_session_id. The dashboard's list surface hides those successors and
    returns the chain ROOT, so an actively-running thread shows up frozen at
    the moment it was last reset, carrying the root's own stale message count
    and timestamps.

    This walks each root down to its leaves and returns the leaf with the most
    recent activity, so a caller holding a page of dashboard rows can display
    what each of those conversations actually is right now. The dashboard stays
    the list authority — this only re-identifies rows it already returned.

    Returns {root_id: {...tip fields...}}. A root with no children maps to
    itself at chain_depth 0.
    """
    ids = [str(s) for s in session_ids if s]
    if not ids:
        return {}
    if len(ids) > 200:
        raise ValueError("session_ids accepts at most 200 ids per call")
    max_depth = _clamp_int(max_depth, 50, 1, 100, "max_depth")

    placeholders = ",".join("?" for _ in ids)
    rows = store.query(
        "WITH RECURSIVE chain(root_id, id, depth) AS ("
        f"  SELECT id, id, 0 FROM sessions WHERE id IN ({placeholders})"
        "  UNION ALL"
        "  SELECT c.root_id, s.id, c.depth + 1"
        "    FROM sessions s JOIN chain c ON s.parent_session_id = c.id"
        # Depth bound is a cycle guard: parent_session_id is not enforced
        # acyclic by the schema, and a cycle would spin this CTE forever.
        "   WHERE c.depth < ?"
        ") "
        "SELECT c.root_id AS root_id, c.depth AS chain_depth, s.id AS tip_id, "
        "       s.message_count AS message_count, s.last_activity_at AS last_activity_at, "
        "       s.started_at AS started_at, s.ended_at AS ended_at, "
        "       s.end_reason AS end_reason, s.title AS title, s.thread_id AS thread_id "
        "  FROM chain c JOIN sessions s ON s.id = c.id",
        (*ids, max_depth),
    )

    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        row = _row_to_dict(r)
        root = row.get("root_id")
        if root is None:
            continue
        # state.db has no is_active column; a session is live exactly while it
        # has not been ended.
        row["is_active"] = row.get("ended_at") is None
        current = best.get(root)
        if current is None or _tip_rank(row) > _tip_rank(current):
            best[root] = row
    return best


def _tip_rank(row: dict[str, Any]) -> tuple[int, float, int]:
    """Order tips within one chain: live first, then most recent activity.

    A chain can fork (two children of one parent), so "the tip" is a choice,
    not a lookup. A session that has not ended outranks any ended one; among
    equals the freshest activity wins, and depth breaks the remaining ties.
    """
    activity = row.get("last_activity_at") or row.get("started_at") or 0
    try:
        activity = float(activity)
    except (TypeError, ValueError):
        activity = 0.0
    return (0 if row.get("ended_at") else 1, activity, int(row.get("chain_depth") or 0))


def state_thread_live_sessions(
    store: SourceStore, chat_id: str, thread_ids: list[str], limit_per_thread: int = 5
) -> dict[str, list[dict[str, Any]]]:
    """Every currently-live session bound to each of the given threads.

    "Live" here needs no chain-tip walk: a session with ended_at IS NULL is by
    definition the open head of its own history, so unlike state_session_tips
    (which resolves a dashboard-listed ROOT id down to whatever tip replaced
    it) this is a direct filter. A thread can legitimately have more than one
    live session at once — e.g. a cron-sourced run and a human-driven chat
    both bound to the same forum topic — so this returns a list per thread
    rather than picking one.
    """
    ids = [str(t) for t in thread_ids if t is not None and str(t) != ""]
    if not ids:
        return {}
    if len(ids) > 200:
        raise ValueError("thread_ids accepts at most 200 ids per call")
    limit_per_thread = _clamp_int(limit_per_thread, 5, 1, 20, "limit_per_thread")

    placeholders = ",".join("?" for _ in ids)
    rows = store.query(
        f"SELECT {SESSION_META_SQL} FROM sessions "
        f"WHERE chat_id = ? AND thread_id IN ({placeholders}) AND ended_at IS NULL "
        "ORDER BY thread_id, COALESCE(last_activity_at, started_at) DESC",
        (chat_id, *ids),
    )

    out: dict[str, list[dict[str, Any]]] = {t: [] for t in ids}
    for r in rows:
        row = _row_to_dict(r)
        tid = str(row.get("thread_id"))
        bucket = out.get(tid)
        if bucket is None or len(bucket) >= limit_per_thread:
            continue
        row["is_active"] = True
        bucket.append(row)
    return out
