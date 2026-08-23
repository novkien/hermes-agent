"""Bounded browser-path dispatch onto the typed in-process backend.

The browser keeps the historical ``/api/adapter/*`` URLs, but no production
code constructs an HTTP adapter client or forwards arbitrary paths.  This
module is the single explicit compatibility map from those allowlisted paths
to ``DataBackend`` methods.
"""

from __future__ import annotations

import re
from typing import Any, Awaitable

from .protocol import BackendHealth, BackendResult, DataBackend, DataBackendError


def _integer(params: dict[str, Any], name: str, default: int) -> int:
    value = params.get(name, default)
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        raise DataBackendError(
            422, "invalid_params", f"{name} must be an integer"
        ) from None


def _integer_params(params: dict[str, Any], *names: str) -> dict[str, Any]:
    normalized = dict(params)
    for name in names:
        if name in normalized:
            normalized[name] = _integer(normalized, name, 0)
    return normalized


def read_call(
    backend: DataBackend, path: str, params: dict[str, Any] | None = None
) -> Awaitable[BackendResult | BackendHealth]:
    """Resolve one allowlisted legacy read path to a typed backend call."""
    values = dict(params or {})
    if path == "/health":
        return backend.health()
    if path == "/capabilities":
        return backend.capabilities()
    if path == "/kanban/boards":
        return backend.kanban_boards()
    if path == "/kanban/tasks":
        return backend.kanban_tasks(**_integer_params(values, "page", "limit"))
    if path == "/kanban/board/summary":
        return backend.kanban_summary(board=values.get("board"))
    if path == "/permits":
        return backend.permits(**_integer_params(values, "page", "limit"))
    if path == "/issues":
        return backend.issues(**_integer_params(values, "page", "limit"))
    if path == "/sessions/search":
        return backend.search_sessions(
            str(values.get("q", "")), limit=_integer(values, "limit", 200)
        )
    if path == "/room-sessions":
        try:
            history = bool(int(values.get("history", 0)))
        except (TypeError, ValueError):
            raise DataBackendError(
                422, "invalid_params", "history must be an integer"
            ) from None
        return backend.room_sessions(
            str(values.get("chat_id", "")),
            limit=_integer(values, "limit", 200),
            history=history,
        )
    if path == "/session-tips":
        return backend.session_tips([
            part for part in str(values.get("ids", "")).split(",") if part
        ])
    if path == "/room-cards":
        return backend.room_cards(
            str(values.get("chat_id", "")),
            per_thread=_integer(values, "per_thread", 10),
        )
    if path == "/thread-sessions":
        return backend.thread_sessions(
            str(values.get("chat_id", "")),
            [part for part in str(values.get("thread_ids", "")).split(",") if part],
        )
    if path == "/room-binding":
        return backend.room_binding()

    match = re.fullmatch(r"/memory/files/([^/]+)", path)
    if match:
        return backend.memory_file(match.group(1))
    match = re.fullmatch(
        r"/kanban/tasks/([^/]+)/(events|runs|attachments|worker-session)", path
    )
    if match:
        task_id, suffix = match.groups()
        if suffix == "events":
            return backend.kanban_task_events(
                task_id,
                cursor=_integer(values, "cursor", 0),
                limit=_integer(values, "limit", 200),
            )
        if suffix == "runs":
            return backend.kanban_task_runs(
                task_id, limit=_integer(values, "limit", 50)
            )
        if suffix == "attachments":
            return backend.kanban_task_attachments(
                task_id, limit=_integer(values, "limit", 20)
            )
        return backend.kanban_worker_session(task_id)
    match = re.fullmatch(r"/kanban/tasks/([^/]+)", path)
    if match:
        return backend.kanban_task(match.group(1))
    match = re.fullmatch(r"/permits/([^/]+)", path)
    if match:
        return backend.permit(match.group(1))
    match = re.fullmatch(r"/issues/(\d+)", path)
    if match:
        return backend.issue(
            int(match.group(1)),
            occurrence_limit=_integer(values, "occurrence_limit", 50),
        )
    match = re.fullmatch(r"/sessions/([^/]+)/timeline", path)
    if match:
        return backend.session_timeline(
            match.group(1), limit=_integer(values, "limit", 200)
        )
    match = re.fullmatch(r"/sources/([^/]+)/fingerprint", path)
    if match:
        return backend.source_fingerprint(match.group(1))
    raise DataBackendError(404, "not_found", "path not in adapter allowlist")
