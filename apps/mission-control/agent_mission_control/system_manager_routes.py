"""Bounded Agent Mission Control surface for Hermes System Manager.

System Manager owns its own SQLite database. Mission Control is only an HTTP
client/editor and never opens or mirrors that database into the local AgentOS
control store.
"""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .clients import UpstreamError
from .security import build_request_summary
from .system_manager_client import SystemManagerClient

TABLES = frozenset({"hosts", "services", "api", "accounts", "notes"})
SERVICE_ACTIONS = frozenset({"start", "stop", "restart", "enable", "disable", "refresh"})


def _json_error(status: int, code: str, message: str, request_id: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "code": code}, "request_id": request_id},
        status_code=status,
    )


def _upstream_status(status: int) -> int:
    return status if 400 <= int(status or 0) < 600 else 502


def _where_from_query(request: Request) -> dict[str, Any] | None:
    out: dict[str, Any] = {}
    for key, value in request.query_params.multi_items():
        if key in {"q", "limit", "profile"}:
            continue
        if key in out:
            # The daemon's compact read contract uses equality predicates, not
            # multi-value filters. Duplicate keys are rejected rather than
            # silently choosing one.
            raise ValueError(f"duplicate filter: {key}")
        out[key] = value
    return out or None


def build_system_manager_router(core: Any) -> APIRouter:
    """Build routes ahead of Router's generic ``/api/{path:path}`` catch-all."""

    router = APIRouter()
    client = SystemManagerClient()

    async def health(request: Request) -> Response:
        rid = request.state.request_id
        try:
            status, body = await client.request("GET", "/health", request_id=rid)
        except UpstreamError as exc:
            return _json_error(_upstream_status(exc.status), "system_manager_unavailable", exc.detail or "unavailable", rid)
        freshness = "live" if status < 400 else "unavailable"
        return JSONResponse(
            core._envelope(  # noqa: SLF001 - same package composition boundary
                body,
                source_id="system-manager",
                profile_id=None,
                freshness=freshness,
                request_id=rid,
                degraded_reason=None if status < 400 else f"upstream_status:{status}",
            ),
            status_code=status,
        )

    async def table_read(request: Request, table: str) -> Response:
        rid = request.state.request_id
        if table not in TABLES:
            return _json_error(404, "system_manager_table_unknown", "unknown table", rid)
        try:
            where = _where_from_query(request)
            limit = max(1, min(int(request.query_params.get("limit", "100")), 500))
        except ValueError as exc:
            return _json_error(400, "invalid_query", str(exc), rid)
        payload: dict[str, Any] = {"table": table, "limit": limit}
        query = (request.query_params.get("q") or "").strip()
        if query:
            payload["query"] = query
        if where:
            payload["where"] = where
        try:
            status, body = await client.request("POST", "/v1/db/read", request_id=rid, json_body=payload)
        except UpstreamError as exc:
            return _json_error(_upstream_status(exc.status), "system_manager_unavailable", exc.detail or "unavailable", rid)
        freshness = "live" if status < 400 else "unavailable"
        return JSONResponse(
            core._envelope(  # noqa: SLF001
                body,
                source_id="system-manager",
                profile_id=None,
                freshness=freshness,
                request_id=rid,
                read_only=False,
                mutations_supported=["upsert", "delete"],
                degraded_reason=None if status < 400 else f"upstream_status:{status}",
            ),
            status_code=status,
        )

    async def table_update(request: Request, table: str) -> Response:
        rid = request.state.request_id
        if table not in TABLES:
            return _json_error(404, "system_manager_table_unknown", "unknown table", rid)
        core._guard_mutation(request)  # noqa: SLF001
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json_error(400, "invalid_body", "JSON body required", rid)
        if not isinstance(body, dict) or not isinstance(body.get("values"), dict):
            return _json_error(400, "invalid_body", "body must contain values object", rid)
        payload = {"table": table, "values": body["values"]}
        if isinstance(body.get("where"), dict):
            payload["where"] = body["where"]

        target = f"/api/system-manager/{table}"
        try:
            core.store.append_audit(
                request_id=rid,
                actor="owner",
                action="system_manager.db_update",
                target=target,
                profile_id=core._request_profile(request),  # noqa: SLF001
                request_summary=build_request_summary(request.method, target, dict(request.query_params)),
                upstream_status=None,
                result="pending",
            )
        except Exception as exc:  # noqa: BLE001
            return _json_error(503, "audit_failed", f"audit write failed: {type(exc).__name__}", rid)

        try:
            status, upstream = await client.request("POST", "/v1/db/update", request_id=rid, json_body=payload)
        except UpstreamError as exc:
            core._record_audit_result(rid, exc.status, f"error:{exc.detail}")  # noqa: SLF001
            return _json_error(_upstream_status(exc.status), "system_manager_unavailable", exc.detail or "unavailable", rid)
        core._record_audit_result(rid, status, "ok" if status < 400 else f"upstream:{status}")  # noqa: SLF001
        if status < 400:
            entity = upstream.get("row") if isinstance(upstream, dict) else None
            entity_id = str(entity.get("id") or "") if isinstance(entity, dict) else ""
            await core.event_bus.safe_publish(
                "system-manager.changed",
                "system-manager",
                table,
                entity_id,
                {"event": upstream.get("operation", "updated") if isinstance(upstream, dict) else "updated"},
                coverage="native",
                profile_id=core._request_profile(request) or "",  # noqa: SLF001
            )
        return JSONResponse(
            core._envelope(  # noqa: SLF001
                upstream,
                source_id="system-manager",
                profile_id=None,
                freshness="live" if status < 400 else "unavailable",
                request_id=rid,
                read_only=False,
                mutations_supported=["upsert", "delete"],
                degraded_reason=None if status < 400 else f"upstream_status:{status}",
            ),
            status_code=status,
        )

    async def service_action(request: Request, service_id: str) -> Response:
        rid = request.state.request_id
        core._guard_mutation(request)  # noqa: SLF001
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json_error(400, "invalid_body", "JSON body required", rid)
        action = str(body.get("action") if isinstance(body, dict) else "").lower().strip()
        if action not in SERVICE_ACTIONS:
            return _json_error(400, "unsupported_action", "unsupported service action", rid)

        target = f"/api/system-manager/services/{service_id}/action"
        try:
            core.store.append_audit(
                request_id=rid,
                actor="owner",
                action=f"system_manager.service.{action}",
                target=target,
                profile_id=core._request_profile(request),  # noqa: SLF001
                request_summary=build_request_summary(request.method, target, dict(request.query_params)),
                upstream_status=None,
                result="pending",
            )
        except Exception as exc:  # noqa: BLE001
            return _json_error(503, "audit_failed", f"audit write failed: {type(exc).__name__}", rid)

        try:
            status, upstream = await client.request(
                "POST", f"/v1/services/{service_id}/action", request_id=rid, json_body={"action": action}
            )
        except UpstreamError as exc:
            core._record_audit_result(rid, exc.status, f"error:{exc.detail}")  # noqa: SLF001
            return _json_error(_upstream_status(exc.status), "system_manager_unavailable", exc.detail or "unavailable", rid)
        core._record_audit_result(rid, status, "ok" if status < 400 else f"upstream:{status}")  # noqa: SLF001
        if status < 400:
            await core.event_bus.safe_publish(
                "system-manager.changed",
                "system-manager",
                "service",
                service_id,
                {"event": action},
                coverage="native",
                profile_id=core._request_profile(request) or "",  # noqa: SLF001
            )
        return JSONResponse(
            core._envelope(  # noqa: SLF001
                upstream,
                source_id="system-manager",
                profile_id=None,
                freshness="live" if status < 400 else "unavailable",
                request_id=rid,
                read_only=False,
                mutations_supported=sorted(SERVICE_ACTIONS),
                degraded_reason=None if status < 400 else f"upstream_status:{status}",
            ),
            status_code=status,
        )

    async def sync_now(request: Request) -> Response:
        rid = request.state.request_id
        core._guard_mutation(request)  # noqa: SLF001
        target = "/api/system-manager/sync"
        try:
            core.store.append_audit(
                request_id=rid,
                actor="owner",
                action="system_manager.sync",
                target=target,
                profile_id=core._request_profile(request),  # noqa: SLF001
                request_summary=build_request_summary(request.method, target, dict(request.query_params)),
                upstream_status=None,
                result="pending",
            )
        except Exception as exc:  # noqa: BLE001
            return _json_error(503, "audit_failed", f"audit write failed: {type(exc).__name__}", rid)
        try:
            status, upstream = await client.request("POST", "/v1/sync", request_id=rid, json_body={})
        except UpstreamError as exc:
            core._record_audit_result(rid, exc.status, f"error:{exc.detail}")  # noqa: SLF001
            return _json_error(_upstream_status(exc.status), "system_manager_unavailable", exc.detail or "unavailable", rid)
        core._record_audit_result(rid, status, "ok" if status < 400 else f"upstream:{status}")  # noqa: SLF001
        if status < 400:
            await core.event_bus.safe_publish(
                "system-manager.changed", "system-manager", "inventory", "all",
                {"event": "sync"}, coverage="native",
                profile_id=core._request_profile(request) or "",  # noqa: SLF001
            )
        return JSONResponse(
            core._envelope(  # noqa: SLF001
                upstream,
                source_id="system-manager",
                profile_id=None,
                freshness="live" if status < 400 else "unavailable",
                request_id=rid,
                read_only=False,
                mutations_supported=["sync"],
                degraded_reason=None if status < 400 else f"upstream_status:{status}",
            ),
            status_code=status,
        )

    router.add_api_route("/api/system-manager/health", health, methods=["GET"])
    router.add_api_route("/api/system-manager/sync", sync_now, methods=["POST"])
    router.add_api_route("/api/system-manager/services/{service_id}/action", service_action, methods=["POST"])
    router.add_api_route("/api/system-manager/{table}", table_read, methods=["GET"])
    router.add_api_route("/api/system-manager/{table}", table_update, methods=["PUT"])
    return router
