"""Owner-only repository registry, Hermes sync, and GitHub PR routes."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .repository_runner import RepositoryGitRunner
from .repository_sync import RepositorySyncError, RepositorySyncService
from .security import build_request_summary


def _json_error(
    status: int, code: str, message: str, request_id: str, *, details: Any = None
) -> JSONResponse:
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


async def _body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(value, dict):
        raise RepositorySyncError("invalid_body", "JSON object required")
    return value


def _head_sha(body: dict[str, Any]) -> str | None:
    value = body.get("expected_head_sha")
    if value is None:
        return None
    text = str(value).strip()
    if not 7 <= len(text) <= 64 or any(ch not in "0123456789abcdefABCDEF" for ch in text):
        raise RepositorySyncError("invalid_body", "invalid expected_head_sha")
    return text


async def _parse_expected_head(request: Request) -> str | None:
    return _head_sha(await _body(request))


# The one owner-mutation vocabulary for this surface. It feeds the write
# envelope's meta.mutations_supported and must stay equal to the
# READ_PATH_MUTATIONS["/api/repositories"] entry in routes.py so the SPA's
# action gating and the BFF's capability contract cannot drift.
REPOSITORY_MUTATIONS: tuple[str, ...] = (
    "initialize_layout",
    "sync",
    "codex_review",
    "mark_ready",
    "mark_draft",
    "merge_pr",
    "prepare_superproject_pin",
)


def build_repository_router(core: Any) -> APIRouter:
    """Compose the registry-driven owner repository control plane."""

    router = APIRouter()
    service = RepositorySyncService(runner=RepositoryGitRunner())
    core.repository_service = service

    def envelope(data: Any, request: Request) -> JSONResponse:
        # House convention (routes.proxy_dashboard_read): every envelope on a
        # mutation-capable surface advertises the write vocabulary with
        # read_only=False — including reads — so the SPA gates its controls on
        # what this BFF actually accepts instead of guessing.
        return JSONResponse(
            core._envelope(  # noqa: SLF001
                data,
                source_id="repository-control",
                profile_id=core._request_profile(request),  # noqa: SLF001
                freshness="live",
                request_id=request.state.request_id,
                read_only=False,
                mutations_supported=list(REPOSITORY_MUTATIONS),
            )
        )

    async def run_mutation(
        request: Request,
        *,
        action: str,
        target: str,
        call: Callable[[Any], dict[str, Any]],
        parse_body: Callable[[], Awaitable[Any]] | None = None,
    ) -> Response:
        rid = request.state.request_id
        core._guard_mutation(request)  # noqa: SLF001
        # Body parsing runs after the guard and before the audit write, so a
        # malformed request is rejected in the documented chain order — it can
        # never reach the ledger or the upstream call ahead of the session,
        # CSRF and Origin/Host checks.
        parsed: Any = None
        if parse_body is not None:
            try:
                parsed = await parse_body()
            except RepositorySyncError as exc:
                return _json_error(400, exc.code, str(exc), rid, details=exc.details)
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
            result = await asyncio.to_thread(call, parsed)
        except RepositorySyncError as exc:
            core._record_audit_result(rid, 409, f"error:{exc.code}")  # noqa: SLF001
            return _json_error(409, exc.code, str(exc), rid, details=exc.details)
        except Exception as exc:  # noqa: BLE001
            core._record_audit_result(rid, 500, f"error:{type(exc).__name__}")  # noqa: SLF001
            return _json_error(
                500, "repository_operation_failed", f"{type(exc).__name__}: {exc}", rid
            )

        ok = bool(result.get("ok"))
        partial = bool(result.get("partial_success"))
        error = result.get("error") if isinstance(result, dict) else None
        code = str((error or {}).get("code") or "operation_failed")
        core._record_audit_result(  # noqa: SLF001
            rid,
            200 if (ok or partial) else 409,
            "ok" if ok else ("partial_success" if partial else f"error:{code}"),
        )
        await core.event_bus.safe_publish(
            "repository.changed",
            "repository-control",
            target,
            str(result.get("repo") or target),
            {
                "action": action,
                "ok": ok,
                "partial_success": partial,
                "error_code": None if ok else code,
            },
        )
        # Operational errors and partial success need the full phase receipt in
        # the browser, so they remain a successful HTTP envelope.
        return envelope(result, request)

    def known(repo: str, rid: str) -> JSONResponse | None:
        if repo in service.registry:
            return None
        return _json_error(404, "repo_unknown", "unknown repository", rid)

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
                "registry": {
                    "count": len(service.registry),
                    "names": list(service.registry),
                },
                "automation": service.automation_commands(),
                "recent_operations": service.store.recent(limit=50),
            },
            request,
        )

    @router.get("/api/repositories/{repo}")
    async def repository_detail(request: Request, repo: str) -> Response:
        invalid = known(repo, request.state.request_id)
        if invalid:
            return invalid
        refresh = _bool(request.query_params.get("refresh"), True)
        status = await asyncio.to_thread(
            service.status, repo, fetch=refresh, include_github=True
        )
        return envelope(
            {
                "repository": status,
                "operations": service.store.recent(repo, 100),
                "automation": service.automation_commands(),
            },
            request,
        )

    @router.get("/api/repositories/{repo}/operations")
    async def repository_operations(request: Request, repo: str) -> Response:
        invalid = known(repo, request.state.request_id)
        if invalid:
            return invalid
        try:
            limit = max(1, min(int(request.query_params.get("limit", "100")), 200))
        except ValueError:
            return _json_error(400, "invalid_query", "limit must be an integer", request.state.request_id)
        return envelope({"operations": service.store.recent(repo, limit)}, request)

    @router.post("/api/repositories/{repo}/initialize")
    async def initialize_repository(request: Request, repo: str) -> Response:
        invalid = known(repo, request.state.request_id)
        if invalid:
            return invalid
        return await run_mutation(
            request,
            action="repository.initialize_layout",
            target=f"/api/repositories/{repo}/initialize",
            call=lambda _parsed: service.initialize_layout(repo, trigger="dashboard"),
        )

    @router.post("/api/repositories/{repo}/pull")
    async def pull_repository(request: Request, repo: str) -> Response:
        invalid = known(repo, request.state.request_id)
        if invalid:
            return invalid
        return await run_mutation(
            request,
            action="repository.sync",
            target=f"/api/repositories/{repo}/pull",
            call=lambda _parsed: service.sync(repo, trigger="dashboard:legacy-pull"),
        )

    @router.post("/api/repositories/{repo}/sync")
    async def sync_repository(request: Request, repo: str) -> Response:
        invalid = known(repo, request.state.request_id)
        if invalid:
            return invalid
        return await run_mutation(
            request,
            action="repository.sync",
            target=f"/api/repositories/{repo}/sync",
            call=lambda _parsed: service.sync(repo, trigger="dashboard"),
        )

    @router.post("/api/repositories/{repo}/prepare-superproject-pin")
    async def prepare_superproject_pin(request: Request, repo: str) -> Response:
        invalid = known(repo, request.state.request_id)
        if invalid:
            return invalid
        return await run_mutation(
            request,
            action="repository.prepare_superproject_pin",
            target=f"/api/repositories/{repo}/prepare-superproject-pin",
            call=lambda _parsed: service.prepare_superproject_pin(
                repo, trigger="dashboard"
            ),
        )

    @router.post("/api/repositories/{repo}/pulls/{number}/codex-review")
    async def codex_review(request: Request, repo: str, number: int) -> Response:
        invalid = known(repo, request.state.request_id)
        if invalid:
            return invalid
        return await run_mutation(
            request,
            action="repository.codex_review",
            target=f"/api/repositories/{repo}/pulls/{number}/codex-review",
            call=lambda expected: service.request_codex_review(
                repo, number, expected_head_sha=expected, trigger="dashboard"
            ),
            parse_body=lambda: _parse_expected_head(request),
        )

    @router.post("/api/repositories/{repo}/pulls/{number}/ready")
    async def mark_ready(request: Request, repo: str, number: int) -> Response:
        invalid = known(repo, request.state.request_id)
        if invalid:
            return invalid
        return await run_mutation(
            request,
            action="repository.mark_ready",
            target=f"/api/repositories/{repo}/pulls/{number}/ready",
            call=lambda _parsed: service.change_draft_state(repo, number, ready=True),
        )

    @router.post("/api/repositories/{repo}/pulls/{number}/draft")
    async def mark_draft(request: Request, repo: str, number: int) -> Response:
        invalid = known(repo, request.state.request_id)
        if invalid:
            return invalid
        return await run_mutation(
            request,
            action="repository.mark_draft",
            target=f"/api/repositories/{repo}/pulls/{number}/draft",
            call=lambda _parsed: service.change_draft_state(repo, number, ready=False),
        )

    @router.post("/api/repositories/{repo}/pulls/{number}/merge")
    async def merge_pr(request: Request, repo: str, number: int) -> Response:
        invalid = known(repo, request.state.request_id)
        if invalid:
            return invalid
        return await run_mutation(
            request,
            action="repository.merge_pr",
            target=f"/api/repositories/{repo}/pulls/{number}/merge",
            call=lambda expected: service.merge_pr(
                repo, number, expected_head_sha=expected, trigger="dashboard"
            ),
            parse_body=lambda: _parse_expected_head(request),
        )

    @router.post("/api/repositories/{repo}/pulls/{number}/merge-and-pull")
    async def legacy_merge_and_pull(request: Request, repo: str, number: int) -> Response:
        """Compatibility route: merge GitHub only; never pull a local child repo."""
        invalid = known(repo, request.state.request_id)
        if invalid:
            return invalid
        return await run_mutation(
            request,
            action="repository.merge_pr",
            target=f"/api/repositories/{repo}/pulls/{number}/merge-and-pull",
            call=lambda expected: service.merge_pr(
                repo, number, expected_head_sha=expected, trigger="dashboard:legacy-route"
            ),
            parse_body=lambda: _parse_expected_head(request),
        )

    @router.post("/api/repositories/{repo}/pulls/{number}/rebase-merge")
    async def legacy_merge(request: Request, repo: str, number: int) -> Response:
        invalid = known(repo, request.state.request_id)
        if invalid:
            return invalid
        return await run_mutation(
            request,
            action="repository.merge_pr",
            target=f"/api/repositories/{repo}/pulls/{number}/rebase-merge",
            call=lambda expected: service.merge_pr(
                repo, number, expected_head_sha=expected, trigger="dashboard:legacy-route"
            ),
            parse_body=lambda: _parse_expected_head(request),
        )

    @router.post("/api/repositories/{repo}/commit")
    async def retired_commit(request: Request, repo: str) -> Response:
        invalid = known(repo, request.state.request_id)
        if invalid:
            return invalid
        return await run_mutation(
            request,
            action="repository.commit_retired",
            target=f"/api/repositories/{repo}/commit",
            call=lambda _parsed: service.commit_local(repo),
        )

    @router.post("/api/repositories/{repo}/upstream-sync")
    async def retired_upstream(request: Request, repo: str) -> Response:
        invalid = known(repo, request.state.request_id)
        if invalid:
            return invalid
        return await run_mutation(
            request,
            action="repository.upstream_retired",
            target=f"/api/repositories/{repo}/upstream-sync",
            call=lambda _parsed: service.sync_upstream(repo),
        )

    return router
