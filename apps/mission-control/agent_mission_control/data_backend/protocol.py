"""Typed contract for Mission Control's bounded Hermes data backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


JsonObject = dict[str, Any]


@dataclass(frozen=True)
class BackendResult:
    data: Any
    meta: JsonObject = field(default_factory=dict)

    @classmethod
    def from_envelope(cls, envelope: JsonObject) -> "BackendResult":
        if "data" not in envelope:
            raise ValueError("backend result is missing data")
        meta = envelope.get("meta") or {}
        if not isinstance(meta, dict):
            raise ValueError("backend result meta must be an object")
        return cls(data=envelope["data"], meta=dict(meta))

    def to_envelope(self) -> JsonObject:
        return {"data": self.data, "meta": dict(self.meta)}


@dataclass(frozen=True)
class BackendHealth:
    status: str
    version: str
    sources: list[JsonObject]
    uptime: float

    def to_payload(self) -> JsonObject:
        return {
            "status": self.status,
            "version": self.version,
            "sources": self.sources,
            "uptime": self.uptime,
        }


class DataBackendError(Exception):
    """Stable backend failure independent of HTTP or FastAPI."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


@runtime_checkable
class DataBackend(Protocol):
    async def health(self) -> BackendHealth: ...

    async def capabilities(self) -> BackendResult: ...

    async def memory_file(self, file_key: str) -> BackendResult: ...

    async def save_memory_file(self, file_key: str, content: str) -> BackendResult: ...

    async def kanban_boards(self) -> BackendResult: ...

    async def kanban_tasks(self, **filters: Any) -> BackendResult: ...

    async def kanban_task(self, task_id: str) -> BackendResult: ...

    async def kanban_task_events(
        self, task_id: str, *, cursor: int = 0, limit: int = 200
    ) -> BackendResult: ...

    async def kanban_task_runs(
        self, task_id: str, *, limit: int = 50
    ) -> BackendResult: ...

    async def kanban_task_attachments(
        self, task_id: str, *, limit: int = 20
    ) -> BackendResult: ...

    async def kanban_worker_session(self, task_id: str) -> BackendResult: ...

    async def kanban_summary(self, *, board: str | None = None) -> BackendResult: ...

    async def permits(self, **filters: Any) -> BackendResult: ...

    async def permit(self, permit_id: str) -> BackendResult: ...

    async def decide_permit(
        self, permit_id: str, body: JsonObject
    ) -> BackendResult: ...

    async def issues(self, **filters: Any) -> BackendResult: ...

    async def issue(
        self, issue_id: int, *, occurrence_limit: int = 50
    ) -> BackendResult: ...

    async def update_issue(self, issue_id: str, body: JsonObject) -> BackendResult: ...

    async def search_sessions(
        self, query: str, *, limit: int = 200
    ) -> BackendResult: ...

    async def room_sessions(
        self, chat_id: str, *, limit: int = 200, history: bool = False
    ) -> BackendResult: ...

    async def session_tips(self, session_ids: list[str]) -> BackendResult: ...

    async def room_cards(
        self, chat_id: str, *, per_thread: int = 10
    ) -> BackendResult: ...

    async def thread_sessions(
        self, chat_id: str, thread_ids: list[str]
    ) -> BackendResult: ...

    async def session_timeline(
        self, session_id: str, *, limit: int = 200
    ) -> BackendResult: ...

    async def source_fingerprint(self, source_id: str) -> BackendResult: ...

    async def room_binding(self) -> BackendResult: ...

    async def aclose(self) -> None: ...
