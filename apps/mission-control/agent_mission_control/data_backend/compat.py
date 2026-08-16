"""Temporary HTTP parity and legacy-call compatibility for backend cutover."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Awaitable, Callable

from ..clients import AdapterClient, UpstreamError
from .protocol import BackendHealth, BackendResult, DataBackend, DataBackendError, JsonObject


def _normalized(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalized(child)
            for key, child in sorted(value.items())
            if key not in {"query_ms", "fetched_at", "uptime", "request_id"}
        }
    if isinstance(value, (list, tuple)):
        return [_normalized(child) for child in value]
    return value


class HttpDataBackend:
    """DataBackend implementation backed by the temporary :8643 service."""

    def __init__(self, client: AdapterClient) -> None:
        self.client = client

    async def _result(
        self, method: str, path: str, *, params: dict | None = None, body: Any = None
    ) -> BackendResult:
        try:
            status, payload, _ = await self.client.request(
                method, path, params=params, json_body=body
            )
        except UpstreamError as exc:
            raise DataBackendError(exc.status, "upstream_error", exc.detail) from None
        if status >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else str(payload)
            raise DataBackendError(status, "upstream_error", str(detail or "adapter error"))
        if not isinstance(payload, dict):
            raise DataBackendError(502, "invalid_response", "adapter returned a non-object")
        return BackendResult.from_envelope(payload)

    async def health(self) -> BackendHealth:
        status, payload, _ = await self.client.health()
        if status >= 400 or not isinstance(payload, dict):
            raise DataBackendError(status, "upstream_error", "adapter health failed")
        return BackendHealth(
            status=str(payload.get("status", "unknown")),
            version=str(payload.get("version", "")),
            sources=list(payload.get("sources") or []),
            uptime=float(payload.get("uptime") or 0),
        )

    async def capabilities(self) -> BackendResult:
        return await self._result("GET", "/capabilities")

    async def memory_file(self, file_key: str) -> BackendResult:
        return await self._result("GET", f"/memory/files/{file_key}")

    async def save_memory_file(self, file_key: str, content: str) -> BackendResult:
        return await self._result("PUT", f"/memory/files/{file_key}", body={"content": content})

    async def kanban_boards(self) -> BackendResult:
        return await self._result("GET", "/kanban/boards")

    async def kanban_tasks(self, **filters: Any) -> BackendResult:
        return await self._result("GET", "/kanban/tasks", params=filters or None)

    async def kanban_task(self, task_id: str) -> BackendResult:
        return await self._result("GET", f"/kanban/tasks/{task_id}")

    async def kanban_task_events(self, task_id: str, *, cursor: int = 0, limit: int = 200) -> BackendResult:
        return await self._result("GET", f"/kanban/tasks/{task_id}/events", params={"cursor": cursor, "limit": limit})

    async def kanban_task_runs(self, task_id: str, *, limit: int = 50) -> BackendResult:
        return await self._result("GET", f"/kanban/tasks/{task_id}/runs", params={"limit": limit})

    async def kanban_task_attachments(self, task_id: str, *, limit: int = 20) -> BackendResult:
        return await self._result("GET", f"/kanban/tasks/{task_id}/attachments", params={"limit": limit})

    async def kanban_worker_session(self, task_id: str) -> BackendResult:
        return await self._result("GET", f"/kanban/tasks/{task_id}/worker-session")

    async def kanban_summary(self, *, board: str | None = None) -> BackendResult:
        return await self._result("GET", "/kanban/board/summary", params={"board": board} if board else None)

    async def permits(self, **filters: Any) -> BackendResult:
        return await self._result("GET", "/permits", params=filters or None)

    async def permit(self, permit_id: str) -> BackendResult:
        return await self._result("GET", f"/permits/{permit_id}")

    async def decide_permit(self, permit_id: str, body: JsonObject) -> BackendResult:
        return await self._result("POST", f"/permits/{permit_id}/decision", body=body)

    async def issues(self, **filters: Any) -> BackendResult:
        return await self._result("GET", "/issues", params=filters or None)

    async def issue(self, issue_id: int, *, occurrence_limit: int = 50) -> BackendResult:
        return await self._result("GET", f"/issues/{issue_id}", params={"occurrence_limit": occurrence_limit})

    async def update_issue(self, issue_id: str, body: JsonObject) -> BackendResult:
        return await self._result("POST", f"/issues/{issue_id}/update", body=body)

    async def search_sessions(self, query: str, *, limit: int = 200) -> BackendResult:
        return await self._result("GET", "/sessions/search", params={"q": query, "limit": limit})

    async def room_sessions(self, chat_id: str, *, limit: int = 200, history: bool = False) -> BackendResult:
        return await self._result("GET", "/room-sessions", params={"chat_id": chat_id, "limit": limit, "history": int(history)})

    async def session_tips(self, session_ids: list[str]) -> BackendResult:
        return await self._result("GET", "/session-tips", params={"ids": ",".join(session_ids)})

    async def room_cards(self, chat_id: str, *, per_thread: int = 10) -> BackendResult:
        return await self._result("GET", "/room-cards", params={"chat_id": chat_id, "per_thread": per_thread})

    async def thread_sessions(self, chat_id: str, thread_ids: list[str]) -> BackendResult:
        return await self._result("GET", "/thread-sessions", params={"chat_id": chat_id, "thread_ids": ",".join(thread_ids)})

    async def session_timeline(self, session_id: str, *, limit: int = 200) -> BackendResult:
        return await self._result("GET", f"/sessions/{session_id}/timeline", params={"limit": limit})

    async def source_fingerprint(self, source_id: str) -> BackendResult:
        return await self._result("GET", f"/sources/{source_id}/fingerprint")

    async def room_binding(self) -> BackendResult:
        return await self._result("GET", "/room-binding")

    async def aclose(self) -> None:
        await self.client.aclose()


@dataclass(frozen=True)
class ParityReport:
    method: str
    matches: bool
    primary: Any
    shadow: Any


class ParityComparator:
    READ_METHODS = frozenset(
        name for name in DataBackend.__dict__ if not name.startswith("_")
    ) - {"save_memory_file", "decide_permit", "update_issue", "aclose"}

    def __init__(self, primary: DataBackend, shadow: DataBackend) -> None:
        self.primary = primary
        self.shadow = shadow

    async def compare(self, method: str, *args: Any, **kwargs: Any) -> ParityReport:
        if method not in self.READ_METHODS:
            raise ValueError(f"method is not shadow-readable: {method}")

        async def capture(backend: DataBackend) -> Any:
            try:
                value = await getattr(backend, method)(*args, **kwargs)
            except DataBackendError as exc:
                if exc.status == 404:
                    error_class = "not_found"
                elif exc.status in {400, 413, 422}:
                    error_class = "invalid_params"
                elif exc.status in {502, 503, 504}:
                    error_class = "unavailable"
                else:
                    error_class = "internal"
                return {"error": {"status": exc.status, "class": error_class}}
            if isinstance(value, BackendResult):
                return _normalized(value.to_envelope())
            if isinstance(value, BackendHealth):
                return _normalized(value.to_payload())
            return _normalized(value)

        primary, shadow = await asyncio_gather(capture(self.primary), capture(self.shadow))
        return ParityReport(method, primary == shadow, primary, shadow)


async def asyncio_gather(left: Awaitable[Any], right: Awaitable[Any]) -> tuple[Any, Any]:
    import asyncio

    first, second = await asyncio.gather(left, right)
    return first, second


class LegacyDataBackendFacade:
    """Old AdapterClient call shape backed by a typed DataBackend.

    This exists only during migration so routes/workers can cut over without
    changing the browser's `/api/adapter/*` contract in the same phase.
    """

    source_id = "adapter"

    def __init__(self, backend: DataBackend) -> None:
        self.backend = backend

    @staticmethod
    def _int(params: dict, name: str, default: int) -> int:
        value = params.get(name, default)
        try:
            return int(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            raise DataBackendError(422, "invalid_params", f"{name} must be an integer") from None

    @classmethod
    def _integer_params(cls, params: dict, *names: str) -> dict:
        """Match FastAPI's integer coercion at the legacy HTTP boundary."""
        normalized = dict(params)
        for name in names:
            if name in normalized:
                normalized[name] = cls._int(normalized, name, 0)
        return normalized

    async def _tuple(self, call: Awaitable[BackendResult | BackendHealth]):
        try:
            result = await call
        except DataBackendError as exc:
            body = {"error": {"code": exc.code, "message": exc.detail}, "detail": exc.detail}
            return exc.status, body, {}
        if isinstance(result, BackendHealth):
            return 200, result.to_payload(), {}
        return 200, result.to_envelope(), {}

    async def health(self, request_id: str | None = None):
        return await self._tuple(self.backend.health())

    async def capabilities(self, request_id: str | None = None):
        return await self._tuple(self.backend.capabilities())

    async def request(self, method: str, path: str, *, params=None, json_body=None, **_kwargs):
        params = dict(params or {})
        try:
            if method == "GET":
                call = self._read_call(path, params)
            elif method == "PUT" and re.fullmatch(r"/memory/files/[^/]+", path):
                call = self.backend.save_memory_file(path.rsplit("/", 1)[1], (json_body or {}).get("content"))
            elif method == "POST" and re.fullmatch(r"/permits/[^/]+/decision", path):
                call = self.backend.decide_permit(path.split("/")[2], json_body or {})
            elif method == "POST" and re.fullmatch(r"/issues/[^/]+/update", path):
                call = self.backend.update_issue(path.split("/")[2], json_body or {})
            else:
                raise DataBackendError(404, "not_found", "path not in adapter allowlist")
        except DataBackendError as exc:
            return exc.status, {"detail": exc.detail}, {}
        return await self._tuple(call)

    def _read_call(self, path: str, params: dict):
        if path == "/health": return self.backend.health()
        if path == "/capabilities": return self.backend.capabilities()
        if path == "/kanban/boards": return self.backend.kanban_boards()
        if path == "/kanban/tasks": return self.backend.kanban_tasks(**self._integer_params(params, "page", "limit"))
        if path == "/kanban/board/summary": return self.backend.kanban_summary(board=params.get("board"))
        if path == "/permits": return self.backend.permits(**self._integer_params(params, "page", "limit"))
        if path == "/issues": return self.backend.issues(**self._integer_params(params, "page", "limit"))
        if path == "/sessions/search": return self.backend.search_sessions(str(params.get("q", "")), limit=self._int(params, "limit", 200))
        if path == "/room-sessions": return self.backend.room_sessions(str(params.get("chat_id", "")), limit=self._int(params, "limit", 200), history=bool(int(params.get("history", 0))))
        if path == "/session-tips": return self.backend.session_tips([part for part in str(params.get("ids", "")).split(",") if part])
        if path == "/room-cards": return self.backend.room_cards(str(params.get("chat_id", "")), per_thread=self._int(params, "per_thread", 10))
        if path == "/thread-sessions": return self.backend.thread_sessions(str(params.get("chat_id", "")), [part for part in str(params.get("thread_ids", "")).split(",") if part])
        if path == "/room-binding": return self.backend.room_binding()
        match = re.fullmatch(r"/memory/files/([^/]+)", path)
        if match: return self.backend.memory_file(match.group(1))
        match = re.fullmatch(r"/kanban/tasks/([^/]+)/(events|runs|attachments|worker-session)", path)
        if match:
            task_id, suffix = match.groups()
            if suffix == "events": return self.backend.kanban_task_events(task_id, cursor=self._int(params, "cursor", 0), limit=self._int(params, "limit", 200))
            if suffix == "runs": return self.backend.kanban_task_runs(task_id, limit=self._int(params, "limit", 50))
            if suffix == "attachments": return self.backend.kanban_task_attachments(task_id, limit=self._int(params, "limit", 20))
            return self.backend.kanban_worker_session(task_id)
        match = re.fullmatch(r"/kanban/tasks/([^/]+)", path)
        if match: return self.backend.kanban_task(match.group(1))
        match = re.fullmatch(r"/permits/([^/]+)", path)
        if match: return self.backend.permit(match.group(1))
        match = re.fullmatch(r"/issues/(\d+)", path)
        if match: return self.backend.issue(int(match.group(1)), occurrence_limit=self._int(params, "occurrence_limit", 50))
        match = re.fullmatch(r"/sessions/([^/]+)/timeline", path)
        if match: return self.backend.session_timeline(match.group(1), limit=self._int(params, "limit", 200))
        match = re.fullmatch(r"/sources/([^/]+)/fingerprint", path)
        if match: return self.backend.source_fingerprint(match.group(1))
        raise DataBackendError(404, "not_found", "path not in adapter allowlist")

    async def tasks(self, limit: int = 100, **params): return await self.request("GET", "/kanban/tasks", params={**params, "limit": min(int(limit), 100)})
    async def board_summary(self, request_id=None): return await self.request("GET", "/kanban/board/summary")
    async def permits_list(self, limit: int = 100, **params): return await self.request("GET", "/permits", params={**params, "limit": min(int(limit), 100)})
    async def issues_list(self, limit: int = 100, **params): return await self.request("GET", "/issues", params={**params, "limit": min(int(limit), 100)})
    async def session_search(self, q: str, limit: int = 200): return await self.request("GET", "/sessions/search", params={"q": q, "limit": min(int(limit), 200)})
    async def memory_file(self, filename: str, request_id=None): return await self.request("GET", f"/memory/files/{filename}")
    async def memory_file_write(self, filename: str, content: str, request_id=None): return await self.request("PUT", f"/memory/files/{filename}", json_body={"content": content})
    async def kanban_task_detail(self, task_id: str, request_id=None): return await self.request("GET", f"/kanban/tasks/{task_id}")
    async def permit_decision(self, permit_id: str, body: dict, request_id=None): return await self.request("POST", f"/permits/{permit_id}/decision", json_body=body)
    async def issue_update(self, issue_id: str, body: dict, request_id=None): return await self.request("POST", f"/issues/{issue_id}/update", json_body=body)
    async def aclose(self): await self.backend.aclose()
