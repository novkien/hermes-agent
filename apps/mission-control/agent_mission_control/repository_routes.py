"""Bounded AgentOS repository monitoring and synchronization routes."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .repository_runner import RepositoryGitRunner
from .repository_sync import RepositorySyncError, RepositorySyncService
from .security import build_request_summary


def _json_error(status: int, code: str, message: str, request_id: str, *, details: Any = None) -> JSONResponse:
    payload: dict[str, Any] = {
        "error": {"message": message, "code": code},
        "request_id": request_id,
    }
    if details is not None:
        payload["error"]["details"] = details
    return JSONResponse(payload, status_code=status)


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _clean_message(value: Any, *, default: str | None = None) -> str | None:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    if len(text) > 240:
        raise ValueError("commit message must be 240 characters or fewer")
    if "\n" in text or "\r" in text:
        raise ValueError("commit message must be one line")
    return text


def build_repository_router(core: Any) -> APIRouter:
    """Build the six-repository control surface ahead of the generic catch-all."""

    router = APIRouter()
    service = RepositorySyncService(runner=RepositoryGitRunner())

    def envelope(data: Any, request: Request, *, read_only: bool) -> JSONResponse:
        return JSONResponse(
            core._envelope(  # noqa: SLF001 - package composition boundary
                data,
                source_id="repository-sync",
                profile_id=core._request_profile(request),  # noqa: SLF001
                freshness="live",
                request_id=request.state.request_id,
                read_only=read_only,
                mutations_supported=(
                    []
                    if read_only
                    else ["sync", "commit", "sync_upstream", "rebase_merge_pr"]
                ),
            )
        )

    async def run_mutation(
        request: Request,
        *,
        action: str,
        target: str,
        call: Callable[[], dict[str, Any]],
    ) -> Response:
        rid = request.state.request_id
        core._guard_mutation(request)  # noqa: SLF001
        profile_id = core._request_profile(request)  # noqa: SLF001
        try:
            core.store.append_audit(
                request_id=rid,
                actor="owner",
                action=action,
                target=target,
                profile_id=profile_id,
                request_summary=build_request_summary(
                    request.method, target, dict(request.query_params)
                ),
                upstream_status=None,
                result="pending",
            )
        except Exception as exc:  # noqa: BLE001
            return _json_error(
                503, "audit_failed", f"audit write failed: {type(exc).__name__}", rid
            )

        try:
            result = await asyncio.to_thread(call)
        except RepositorySyncError as exc:
            core._record_audit_result(rid, 409, f"error:{exc.code}")  # noqa: SLF001
            return _json_error(409, exc.code, str(exc), rid, details=exc.details)
        except Exception as exc:  # noqa: BLE001
            core._record_audit_result(rid, 500, f"error:{type(exc).__name__}")  # noqa: SLF001
            return _json_error(
                500, "repository_operation_failed", f"{type(exc).__name__}: {exc}", rid
            )

        ok = bool(result.get("ok"))
        error = result.get("error") if isinstance(result, dict) else None
        code = str((error or {}).get("code") or "operation_failed")
        core._record_audit_result(  # noqa: SLF001
            rid, 200 if ok else 409, "ok" if ok else f"error:{code}"
        )
        await core.event_bus.safe_publish(
            "repository.changed",
            "repository-sync",
            target,
            str(result.get("repo") or target),
            {
                "action": action,
                "ok": ok,
                "trigger": result.get("trigger") or "dashboard",
                "error_code": None if ok else code,
            },
        )
        # Repository conflicts are operational results, not transport failures. A
        # 200 envelope lets the SPA display the full recovery payload (stash SHA,
        # backup branch and conflicting files) instead of collapsing it to a toast.
        return envelope(result, request, read_only=False)

    @router.get("/api/repositories")
    async def repositories(request: Request) -> Response:
        refresh = _bool(request.query_params.get("refresh"), True)
        github = _bool(request.query_params.get("github"), True)
        rows = await asyncio.to_thread(
            service.status_all, fetch=refresh, include_github=github
        )
        return envelope(
            {
                "repositories": rows,
                "automation": service.automation_commands(),
                "recent_operations": service.store.recent(limit=30),
            },
            request,
            read_only=True,
        )

    @router.get("/api/repositories/{repo}")
    async def repository_detail(request: Request, repo: str) -> Response:
        if repo not in service.registry:
            return _json_error(404, "repo_unknown", "unknown repository", request.state.request_id)
        refresh = _bool(request.query_params.get("refresh"), True)
        status = await asyncio.to_thread(
            service.status, repo, fetch=refresh, include_github=True
        )
        return envelope(
            {
                "repository": status,
                "operations": service.store.recent(repo, 50),
                "automation": service.automation_commands(),
            },
            request,
            read_only=True,
        )

    @router.get("/api/repositories/{repo}/operations")
    async def repository_operations(request: Request, repo: str) -> Response:
        if repo not in service.registry:
            return _json_error(404, "repo_unknown", "unknown repository", request.state.request_id)
        try:
            limit = max(1, min(int(request.query_params.get("limit", "50")), 200))
        except ValueError:
            return _json_error(400, "invalid_query", "limit must be an integer", request.state.request_id)
        return envelope(
            {"operations": service.store.recent(repo, limit)}, request, read_only=True
        )

    @router.post("/api/repositories/sync-all")
    async def sync_all(request: Request) -> Response:
        rid = request.state.request_id
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            return _json_error(400, "invalid_body", "JSON object required", rid)
        auto_commit = _bool(body.get("auto_commit"), True)
        try:
            message = _clean_message(body.get("commit_message"))
        except ValueError as exc:
            return _json_error(400, "invalid_body", str(exc), rid)

        def run() -> dict[str, Any]:
            def _run_sync(name: str) -> dict[str, Any]:
                try:
                    return service.sync(
                        name,
                        auto_commit=auto_commit,
                        commit_message=message,
                        trigger="dashboard",
                    )
                except Exception as exc:  # noqa: BLE001 - sync failures should be per-repo and reported together
                    return {
                        "repo": name,
                        "action": "sync",
                        "trigger": "dashboard",
                        "ok": False,
                        "error": {
                            "code": "sync_failed",
                            "message": f"{type(exc).__name__}: {exc}",
                        },
                    }

            names = list(service.registry)
            max_workers = max(1, min(len(names), 6))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                results = list(executor.map(_run_sync, names))
            return {
                "repo": "all",
                "action": "sync_all",
                "trigger": "dashboard",
                "ok": all(bool(item.get("ok")) for item in results),
                "results": results,
            }

        return await run_mutation(
            request,
            action="repository.sync_all",
            target="/api/repositories/sync-all",
            call=run,
        )

    @router.post("/api/repositories/{repo}/sync")
    async def sync_repository(request: Request, repo: str) -> Response:
        rid = request.state.request_id
        if repo not in service.registry:
            return _json_error(404, "repo_unknown", "unknown repository", rid)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            return _json_error(400, "invalid_body", "JSON object required", rid)
        try:
            message = _clean_message(body.get("commit_message"))
        except ValueError as exc:
            return _json_error(400, "invalid_body", str(exc), rid)
        auto_commit = _bool(body.get("auto_commit"), True)
        return await run_mutation(
            request,
            action="repository.sync",
            target=f"/api/repositories/{repo}/sync",
            call=lambda: service.sync(
                repo,
                auto_commit=auto_commit,
                commit_message=message,
                trigger="dashboard",
            ),
        )

    @router.post("/api/repositories/{repo}/commit")
    async def commit_repository(request: Request, repo: str) -> Response:
        rid = request.state.request_id
        if repo not in service.registry:
            return _json_error(404, "repo_unknown", "unknown repository", rid)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            return _json_error(400, "invalid_body", "JSON object required", rid)
        try:
            message = _clean_message(body.get("message"))
        except ValueError as exc:
            return _json_error(400, "invalid_body", str(exc), rid)
        return await run_mutation(
            request,
            action="repository.commit",
            target=f"/api/repositories/{repo}/commit",
            call=lambda: service.commit_local(repo, message=message, trigger="dashboard"),
        )

    @router.post("/api/repositories/{repo}/upstream-sync")
    async def upstream_sync(request: Request, repo: str) -> Response:
        rid = request.state.request_id
        if repo not in service.registry:
            return _json_error(404, "repo_unknown", "unknown repository", rid)
        if not service.spec(repo).is_fork:
            return _json_error(400, "not_a_fork", "repository has no configured upstream", rid)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            return _json_error(400, "invalid_body", "JSON object required", rid)
        auto_commit = _bool(body.get("auto_commit"), True)
        return await run_mutation(
            request,
            action="repository.sync_upstream",
            target=f"/api/repositories/{repo}/upstream-sync",
            call=lambda: service.sync_upstream(
                repo, trigger="dashboard", pull_after=True, auto_commit=auto_commit
            ),
        )

    @router.post("/api/repositories/{repo}/pulls/{number}/rebase-merge")
    async def rebase_merge_pull(request: Request, repo: str, number: int) -> Response:
        rid = request.state.request_id
        if repo not in service.registry:
            return _json_error(404, "repo_unknown", "unknown repository", rid)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            return _json_error(400, "invalid_body", "JSON object required", rid)
        head_sha = body.get("expected_head_sha")
        if head_sha is not None:
            head_sha = str(head_sha).strip()
            if len(head_sha) < 7 or len(head_sha) > 64:
                return _json_error(400, "invalid_body", "invalid expected_head_sha", rid)
        auto_commit = _bool(body.get("auto_commit"), True)
        return await run_mutation(
            request,
            action="repository.rebase_merge_pr",
            target=f"/api/repositories/{repo}/pulls/{int(number)}/rebase-merge",
            call=lambda: service.merge_pull_request_rebase(
                repo,
                int(number),
                expected_head_sha=head_sha,
                trigger="dashboard",
                pull_after=True,
                auto_commit=auto_commit,
            ),
        )

    return router
