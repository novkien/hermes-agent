"""In-process implementation of the bounded Hermes data backend."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any, Callable

from .config import (
    KANBAN_TASK_SORTS,
    SourceSpec,
    Settings,
)
from .db import KanbanBoardRegistry, SourceStore, StateProfileRegistry
from .decisions import DecisionError, apply_issue_update, apply_permit_decision
from .protocol import BackendHealth, BackendResult, DataBackendError, JsonObject
from .queries import (
    board_summary_extras,
    issue_detail,
    issues_list,
    kanban_board_summary,
    kanban_board_summary_all,
    kanban_boards_capabilities,
    kanban_boards_list,
    kanban_list_tasks,
    kanban_resolve_task,
    kanban_task_attachments,
    kanban_task_detail,
    kanban_task_events,
    kanban_task_runs,
    permit_detail,
    permits_list,
    resolve_worker_session,
    state_room_sessions,
    state_room_thread_sessions,
    state_search_sessions,
    state_session_timeline,
    state_session_tips,
    state_thread_live_sessions,
)
from .room_binding import read_room_binding
from .room_occupancy import read_live_occupancy


LOCAL_BACKEND_VERSION = "0.1.0"
MAX_MEMORY_FILE_BYTES = 1_000_000
MEMORY_FILE_ALIASES = {
    "memory": "MEMORY.md",
    "memory.md": "MEMORY.md",
    "user": "USER.md",
    "user.md": "USER.md",
}


class LocalDataBackend:
    """Typed, in-process equivalent of the deployed HTTP Adapter.

    The object has no HTTP authentication surface. Mission Control routes keep
    ownership of session, CSRF, origin, rate-limit and audit enforcement.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.started_at = time.time()
        self.stores = {
            spec.id: SourceStore(spec) for spec in self.settings.sources.values()
        }
        self.kanban_registry = KanbanBoardRegistry(
            boards_dir=self.settings.kanban_boards_dir,
            default_board=self.settings.default_kanban_board,
            spec_builder=self._board_spec,
        )
        self.profile_registry = StateProfileRegistry(
            profiles_dir=self.settings.state_profiles_dir,
            spec_builder=self._profile_state_spec,
        )

    def _board_spec(self, board: str, path: str) -> SourceSpec:
        return SourceSpec(
            id=f"kanban/{board}",
            path=path,
            tables=(
                "tasks",
                "task_links",
                "task_comments",
                "task_events",
                "task_runs",
                "task_attachments",
                "kanban_notify_subs",
            ),
            allowed_filters=("status", "assignee", "profile"),
            allowed_sorts=KANBAN_TASK_SORTS,
            query_budget_ms=self.settings.sources["kanban"].query_budget_ms,
            row_counts=False,
            note=f"kanban board: {board} (read-only, under kanban/boards/)",
        )

    def _profile_state_spec(self, profile: str, path: str) -> SourceSpec:
        return SourceSpec(
            id=f"state/{profile}",
            path=path,
            tables=("sessions", "messages", "messages_fts"),
            query_budget_ms=self.settings.sources["state"].query_budget_ms,
            row_counts=False,
            note=f"worker profile state: {profile} (read-only)",
        )

    def _store(self, source_id: str) -> SourceStore:
        try:
            return self.stores[source_id]
        except KeyError:
            raise DataBackendError(404, "not_found", f"unknown source: {source_id}")

    def _board_store(self, board: str | None) -> SourceStore:
        if board is None or board == "":
            try:
                return self.kanban_registry.default_store()
            except KeyError:
                raise DataBackendError(404, "not_found", "default board not found") from None
        if board == "all":
            raise DataBackendError(
                422,
                "invalid_params",
                "board='all' is only valid for the board summary or task list",
            )
        try:
            return self.kanban_registry.resolve(board)
        except KeyError:
            raise DataBackendError(404, "not_found", f"unknown board: {board}") from None
        except ValueError as exc:
            raise DataBackendError(422, "invalid_params", str(exc)) from None
        except sqlite3.Error:
            raise DataBackendError(500, "backend_error", "data source query failed") from None
        except OSError:
            raise DataBackendError(500, "backend_error", "data source unavailable") from None

    @staticmethod
    def _guard_identifier(value: str, label: str) -> None:
        if not value or "/" in value or ".." in value:
            raise DataBackendError(404, "not_found", f"{label} not found")

    async def _call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except DataBackendError:
            raise
        except DecisionError as exc:
            code = "invalid_params" if exc.status in {400, 413, 422} else "backend_error"
            if exc.status == 404:
                code = "not_found"
            raise DataBackendError(exc.status, code, exc.detail) from None
        except ValueError as exc:
            raise DataBackendError(422, "invalid_params", str(exc)) from None

    async def _result(
        self, function: Callable[..., JsonObject], *args: Any, **kwargs: Any
    ) -> BackendResult:
        envelope = await self._call(function, *args, **kwargs)
        return BackendResult.from_envelope(envelope)

    def _capabilities_meta(self, spec: SourceSpec, store: SourceStore) -> JsonObject:
        fingerprint = store.fingerprint()
        try:
            current = store.fingerprint(recompute=True)
            schema_drift = current != fingerprint
        except Exception:
            current = fingerprint
            schema_drift = False
        result: JsonObject = {
            "schema_fingerprint": current,
            "tables": spec.tables,
            "allowed_filters": list(spec.allowed_filters),
            "allowed_sorts": list(spec.allowed_sorts),
            "query_budget_ms": spec.query_budget_ms,
        }
        if spec.row_counts:
            result["row_counts"] = {}
            for table in spec.tables:
                try:
                    result["row_counts"][table] = store.row_count(table)
                except Exception:
                    result["row_counts"][table] = None
        if schema_drift:
            result["schema_drift"] = True
        return result

    def _health_sync(self) -> BackendHealth:
        sources = []
        for spec in self.settings.sources.values():
            store = self.stores[spec.id]
            present = Path(store.spec.path).exists()
            sources.append(
                {
                    "source_id": spec.id,
                    "present": present,
                    "reachable": store.reachable() if present else False,
                }
            )
        return BackendHealth(
            status="ok",
            version=LOCAL_BACKEND_VERSION,
            sources=sources,
            uptime=round(time.time() - self.started_at, 3),
        )

    async def health(self) -> BackendHealth:
        return await self._call(self._health_sync)

    def _capabilities_sync(self) -> JsonObject:
        data = {
            spec.id: self._capabilities_meta(spec, self.stores[spec.id])
            for spec in self.settings.sources.values()
        }
        data["kanban"]["boards"] = kanban_boards_capabilities(
            self.kanban_registry
        )["boards"]
        try:
            data["kanban"]["schema_fingerprint"] = self.kanban_registry.fingerprint(
                self.settings.default_kanban_board
            )
        except Exception:
            pass
        return {"data": data, "meta": {"count": len(data), "query_ms": 0.0}}

    async def capabilities(self) -> BackendResult:
        return await self._result(self._capabilities_sync)

    def _memory_path(self, file_key: str) -> tuple[str, Path]:
        filename = MEMORY_FILE_ALIASES.get(file_key.strip().lower())
        if filename is None:
            raise DataBackendError(404, "not_found", "unknown memory file")
        path = (self.settings.memory_dir / filename).resolve()
        if path.parent != self.settings.memory_dir:
            raise DataBackendError(404, "not_found", "unknown memory file")
        return filename, path

    def _memory_file_sync(self, file_key: str) -> JsonObject:
        filename, path = self._memory_path(file_key)
        if not path.is_file():
            raise DataBackendError(404, "not_found", "memory file not found")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            raise DataBackendError(500, "backend_error", "memory file read failed") from None
        return {
            "data": {"file": filename, "path": str(path), "content": content},
            "meta": {
                "source_id": "hermes-memory",
                "read_only": False,
                "mutations_supported": ["save"],
                "query_ms": 0.0,
            },
        }

    async def memory_file(self, file_key: str) -> BackendResult:
        return await self._result(self._memory_file_sync, file_key)

    def _save_memory_file_sync(self, file_key: str, content: str) -> JsonObject:
        filename, path = self._memory_path(file_key)
        if not isinstance(content, str):
            raise DataBackendError(400, "invalid_params", "content must be a string")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_MEMORY_FILE_BYTES:
            raise DataBackendError(413, "invalid_params", "memory file exceeds size limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: str | None = None
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                dir=path.parent, prefix=f".{filename}.tmp."
            )
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(encoded)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        except OSError:
            raise DataBackendError(500, "backend_error", "memory file write failed") from None
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
        return {
            "data": {
                "file": filename,
                "path": str(path),
                "size": len(encoded),
                "content": content,
            },
            "meta": {
                "source_id": "hermes-memory",
                "read_only": False,
                "mutations_supported": ["save"],
                "query_ms": 0.0,
            },
        }

    async def save_memory_file(self, file_key: str, content: str) -> BackendResult:
        return await self._result(self._save_memory_file_sync, file_key, content)

    async def kanban_boards(self) -> BackendResult:
        return await self._result(kanban_boards_list, self.kanban_registry)

    def _kanban_tasks_sync(self, **filters: Any) -> JsonObject:
        allowed = {"board", "status", "assignee", "profile", "sort", "page", "limit"}
        unknown = sorted(set(filters) - allowed)
        if unknown:
            raise DataBackendError(
                422, "invalid_params", f"unsupported filters: {', '.join(unknown)}"
            )
        board = filters.get("board")
        if board == "all":
            merged: list[JsonObject] = []
            fingerprints: list[str] = []
            for name in self.kanban_registry.board_names():
                part = kanban_list_tasks(
                    self.kanban_registry.resolve(name),
                    status=filters.get("status"),
                    assignee=filters.get("assignee"),
                    profile=filters.get("profile"),
                    sort=filters.get("sort"),
                    page=filters.get("page"),
                    limit=filters.get("limit"),
                    board=name,
                )
                merged.extend(part.get("data") or [])
                fingerprint = (part.get("meta") or {}).get("schema_fingerprint")
                if fingerprint:
                    fingerprints.append(fingerprint)
            sort_key = filters.get("sort") or "created_at"
            merged.sort(
                key=lambda row: (row.get(sort_key) is None, row.get(sort_key)),
                reverse=True,
            )
            return {
                "data": merged,
                "meta": {
                    "source_id": "kanban",
                    "board": "all",
                    "schema_fingerprint": fingerprints[0] if fingerprints else None,
                    "row_count": len(merged),
                    "page": filters.get("page") or 1,
                    "limit": filters.get("limit"),
                    "query_ms": 0.0,
                    "read_only": True,
                    "mutations_supported": [],
                },
            }
        return kanban_list_tasks(
            self._board_store(board),
            status=filters.get("status"),
            assignee=filters.get("assignee"),
            profile=filters.get("profile"),
            sort=filters.get("sort"),
            page=filters.get("page"),
            limit=filters.get("limit"),
            board=board or self.settings.default_kanban_board,
        )

    async def kanban_tasks(self, **filters: Any) -> BackendResult:
        return await self._result(self._kanban_tasks_sync, **filters)

    def _resolve_task(self, task_id: str) -> tuple[SourceStore, str, JsonObject]:
        self._guard_identifier(task_id, "task")
        resolved = kanban_resolve_task(self.kanban_registry, task_id)
        if resolved is None:
            raise DataBackendError(404, "not_found", "task not found")
        return resolved

    async def kanban_task(self, task_id: str) -> BackendResult:
        def load() -> JsonObject:
            return self._resolve_task(task_id)[2]

        return await self._result(load)

    async def kanban_task_events(
        self, task_id: str, *, cursor: int = 0, limit: int = 200
    ) -> BackendResult:
        def load() -> JsonObject:
            store, board, _ = self._resolve_task(task_id)
            result = kanban_task_events(store, task_id, cursor, limit)
            if result is None:
                raise DataBackendError(404, "not_found", "task not found")
            result["meta"]["board"] = board
            return result

        return await self._result(load)

    async def kanban_task_runs(self, task_id: str, *, limit: int = 50) -> BackendResult:
        def load() -> JsonObject:
            store, board, _ = self._resolve_task(task_id)
            result = kanban_task_runs(store, task_id, limit)
            if result is None:
                raise DataBackendError(404, "not_found", "task not found")
            result["meta"]["board"] = board
            return result

        return await self._result(load)

    async def kanban_task_attachments(
        self, task_id: str, *, limit: int = 20
    ) -> BackendResult:
        def load() -> JsonObject:
            store, board, _ = self._resolve_task(task_id)
            result = kanban_task_attachments(store, task_id, limit)
            if result is None:
                raise DataBackendError(404, "not_found", "task not found")
            result["meta"]["board"] = board
            return result

        return await self._result(load)

    def _kanban_worker_session_sync(self, task_id: str) -> JsonObject:
        self._guard_identifier(task_id, "task")
        started = time.perf_counter()
        result = resolve_worker_session(
            self._store("state"), self.profile_registry.stores(), task_id
        )
        if result is None:
            raise DataBackendError(404, "not_found", "worker session not found")
        return {
            "data": result,
            "meta": {
                "source_id": "state",
                "query_ms": (time.perf_counter() - started) * 1000,
                "read_only": True,
                "mutations_supported": [],
            },
        }

    async def kanban_worker_session(self, task_id: str) -> BackendResult:
        return await self._result(self._kanban_worker_session_sync, task_id)

    def _kanban_summary_sync(self, board: str | None) -> JsonObject:
        if board == "all":
            return kanban_board_summary_all(
                self.kanban_registry, self._store("permits"), self._store("issues")
            )
        kanban = kanban_board_summary(
            self._board_store(board),
            board=board or self.settings.default_kanban_board,
        )
        kanban["data"].update(
            board_summary_extras(self._store("permits"), self._store("issues"))
        )
        return kanban

    async def kanban_summary(self, *, board: str | None = None) -> BackendResult:
        return await self._result(self._kanban_summary_sync, board)

    async def permits(self, **filters: Any) -> BackendResult:
        allowed = {"status", "severity", "approved", "executed", "sort", "page", "limit"}
        unknown = sorted(set(filters) - allowed)
        if unknown:
            raise DataBackendError(422, "invalid_params", f"unsupported filters: {', '.join(unknown)}")
        return await self._result(permits_list, self._store("permits"), **filters)

    async def permit(self, permit_id: str) -> BackendResult:
        self._guard_identifier(permit_id, "permit")

        def load() -> JsonObject:
            result = permit_detail(self._store("permits"), permit_id)
            if result is None:
                raise DataBackendError(404, "not_found", "permit not found")
            return result

        return await self._result(load)

    async def decide_permit(self, permit_id: str, body: JsonObject) -> BackendResult:
        result = await self._call(
            apply_permit_decision,
            permit_id,
            body,
            scripts_dir=self.settings.scripts_dir,
        )
        return BackendResult(
            data=result,
            meta={
                "source_id": "permits",
                "read_only": False,
                "mutations_supported": ["decide"],
                "query_ms": 0.0,
            },
        )

    async def issues(self, **filters: Any) -> BackendResult:
        allowed = {"status", "severity", "sort", "page", "limit"}
        unknown = sorted(set(filters) - allowed)
        if unknown:
            raise DataBackendError(422, "invalid_params", f"unsupported filters: {', '.join(unknown)}")
        return await self._result(issues_list, self._store("issues"), **filters)

    async def issue(self, issue_id: int, *, occurrence_limit: int = 50) -> BackendResult:
        def load() -> JsonObject:
            result = issue_detail(self._store("issues"), issue_id, occurrence_limit)
            if result is None:
                raise DataBackendError(404, "not_found", "issue not found")
            return result

        return await self._result(load)

    async def update_issue(self, issue_id: str, body: JsonObject) -> BackendResult:
        result = await self._call(
            apply_issue_update,
            issue_id,
            body,
            scripts_dir=self.settings.scripts_dir,
        )
        return BackendResult(
            data=result,
            meta={
                "source_id": "issues",
                "read_only": False,
                "mutations_supported": ["decide"],
                "query_ms": 0.0,
            },
        )

    async def search_sessions(self, query: str, *, limit: int = 200) -> BackendResult:
        return await self._result(
            state_search_sessions,
            self._store("state"),
            query,
            limit,
            budget_ms=10_000,
        )

    def _room_sessions_sync(self, chat_id: str, limit: int, history: bool) -> JsonObject:
        rows = state_room_sessions(self._store("state"), chat_id, limit)
        extra: JsonObject = {}
        if history:
            extra["thread_sessions"] = state_room_thread_sessions(
                self._store("state"), chat_id
            )
        return {
            "data": {"sessions": rows, "total": len(rows), **extra},
            "meta": self._read_meta("state"),
        }

    async def room_sessions(
        self, chat_id: str, *, limit: int = 200, history: bool = False
    ) -> BackendResult:
        return await self._result(self._room_sessions_sync, chat_id, limit, history)

    @staticmethod
    def _read_meta(source_id: str) -> JsonObject:
        return {
            "source_id": source_id,
            "query_ms": 0.0,
            "read_only": True,
            "mutations_supported": [],
        }

    def _session_tips_sync(self, session_ids: list[str]) -> JsonObject:
        tips = state_session_tips(self._store("state"), session_ids)
        return {
            "data": {"tips": tips, "total": len(tips)},
            "meta": self._read_meta("state"),
        }

    async def session_tips(self, session_ids: list[str]) -> BackendResult:
        return await self._result(self._session_tips_sync, session_ids)

    def _room_cards_sync(self, chat_id: str, per_thread: int) -> JsonObject:
        per_thread = max(0, min(int(per_thread), 100))
        thread_of_session: dict[str, str] = {}
        for row in state_room_thread_sessions(self._store("state"), chat_id):
            session_id = row.get("id")
            thread_id = row.get("thread_id")
            if session_id is not None and thread_id is not None:
                thread_of_session[str(session_id)] = str(thread_id)
        counts: dict[str, int] = {}
        by_thread: dict[str, list[JsonObject]] = {}
        for board in self.kanban_registry.board_names():
            store = self.kanban_registry.resolve(board)
            try:
                rows = store.query(
                    "SELECT id, title, status, priority, assignee, created_by, "
                    "created_at, session_id FROM tasks WHERE session_id IS NOT NULL"
                )
            except Exception:
                continue
            for raw in rows:
                row = dict(raw)
                thread = thread_of_session.get(str(row.get("session_id")))
                if thread is None:
                    continue
                row["board"] = board
                counts[thread] = counts.get(thread, 0) + 1
                by_thread.setdefault(thread, []).append(row)
        for thread, rows in by_thread.items():
            rows.sort(
                key=lambda row: (row.get("created_at") is None, row.get("created_at")),
                reverse=True,
            )
            by_thread[thread] = rows[:per_thread]
        return {
            "data": {"counts": counts, "cards": by_thread},
            "meta": self._read_meta("kanban"),
        }

    async def room_cards(self, chat_id: str, *, per_thread: int = 10) -> BackendResult:
        return await self._result(self._room_cards_sync, chat_id, per_thread)

    def _thread_sessions_sync(self, chat_id: str, thread_ids: list[str]) -> JsonObject:
        sessions = state_thread_live_sessions(self._store("state"), chat_id, thread_ids)
        return {
            "data": {"sessions_by_thread": sessions},
            "meta": self._read_meta("state"),
        }

    async def thread_sessions(
        self, chat_id: str, thread_ids: list[str]
    ) -> BackendResult:
        return await self._result(self._thread_sessions_sync, chat_id, thread_ids)

    async def session_timeline(
        self, session_id: str, *, limit: int = 200
    ) -> BackendResult:
        self._guard_identifier(session_id, "session")

        def load() -> JsonObject:
            result = state_session_timeline(self._store("state"), session_id, limit)
            if result is None:
                raise DataBackendError(404, "not_found", "session not found")
            return result

        return await self._result(load)

    def _source_fingerprint_sync(self, source_id: str) -> JsonObject:
        store = self._store(source_id)
        spec = self.settings.sources[source_id]
        if source_id == "state":
            data = {
                "source_id": source_id,
                "schema_fingerprint": None,
                "note": spec.note,
            }
        else:
            data = {
                "source_id": source_id,
                "schema_fingerprint": store.fingerprint(recompute=True),
            }
        return {"data": data, "meta": {"source_id": source_id, "query_ms": 0.0}}

    async def source_fingerprint(self, source_id: str) -> BackendResult:
        return await self._result(self._source_fingerprint_sync, source_id)

    def _room_binding_sync(self) -> JsonObject:
        binding = read_room_binding(self.settings.config_path)
        occupancy = read_live_occupancy(self.settings.room_bindings_db)
        if binding is None:
            return {
                "data": {
                    "degraded": True,
                    "note": "room binding config unavailable",
                    **occupancy,
                },
                "meta": {"source_id": "config", "query_ms": 0.0},
            }
        return {
            "data": {**binding, **occupancy},
            "meta": self._read_meta("config"),
        }

    async def room_binding(self) -> BackendResult:
        return await self._result(self._room_binding_sync)

    def _close_sync(self) -> None:
        for store in self.stores.values():
            store.close()
        self.kanban_registry.close()
        self.profile_registry.close()

    async def aclose(self) -> None:
        await asyncio.to_thread(self._close_sync)
