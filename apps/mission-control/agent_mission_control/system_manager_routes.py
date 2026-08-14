"""Bounded Agent Mission Control surface for Hermes System Manager.

System Manager owns its own SQLite database. Mission Control is only an HTTP
client/editor and never opens or mirrors that database into the local AgentOS
control store.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .clients import UpstreamError
from .security import build_request_summary
from .system_manager_client import SystemManagerClient

TABLES = frozenset({"hosts", "services", "api", "accounts", "notes"})
SERVICE_ACTIONS = frozenset({"start", "stop", "restart", "enable", "disable", "refresh"})
def _sm_cache_ttl() -> float:
    value = os.getenv("SYSTEM_MANAGER_CACHE_TTL_SECONDS", "90") or "90"
    try:
        return max(5.0, float(value))
    except ValueError:
        return 90.0


def _sm_cache_max_entries() -> int:
    value = os.getenv("SYSTEM_MANAGER_CACHE_MAX_ENTRIES", "128") or "128"
    try:
        return max(1, min(int(value), 1024))
    except ValueError:
        return 128


SM_CACHE_TTL_SECONDS = _sm_cache_ttl()
SM_CACHE_MAX_ENTRIES = _sm_cache_max_entries()

_SYSTEM_MANAGER_TABLE_CACHE: dict[str, dict[str, Any]] = {}
_SYSTEM_MANAGER_CACHE_LOCK = asyncio.Lock()
_SYSTEM_MANAGER_REFRESHING = set[str]()


def _system_manager_cache_key(
    table: str,
    profile: str | None,
    query: str,
    limit: int,
    where: dict[str, Any] | None,
) -> str:
    payload = {
        "table": table,
        "profile": profile or "",
        "q": query,
        "limit": limit,
        "where": where or {},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _system_manager_cache_is_stale(entry: dict[str, Any], now: float) -> bool:
    return bool(entry.get("stale_after") is not None and now >= float(entry.get("stale_after", 0.0)))


async def _system_manager_cache_get(cache_key: str) -> dict[str, Any] | None:
    async with _SYSTEM_MANAGER_CACHE_LOCK:
        entry = _SYSTEM_MANAGER_TABLE_CACHE.get(cache_key)
        if entry is None:
            return None
        return dict(entry)


async def _system_manager_cache_set(cache_key: str, table: str, payload: Any) -> None:
    now = time.time()
    stale_after = now + SM_CACHE_TTL_SECONDS
    async with _SYSTEM_MANAGER_CACHE_LOCK:
        _SYSTEM_MANAGER_TABLE_CACHE[cache_key] = {
            "table": table,
            "payload": payload,
            "fetched_at": now,
            "stale_after": stale_after,
        }
        # Evict oldest if cache grows too large.
        if len(_SYSTEM_MANAGER_TABLE_CACHE) > SM_CACHE_MAX_ENTRIES:
            ordered = sorted(
                _SYSTEM_MANAGER_TABLE_CACHE.items(),
                key=lambda item: float(item[1].get("fetched_at", 0.0)),
            )
            for key, _ in ordered[: len(_SYSTEM_MANAGER_TABLE_CACHE) - SM_CACHE_MAX_ENTRIES]:
                _SYSTEM_MANAGER_TABLE_CACHE.pop(key, None)


async def _system_manager_cache_invalidate(table: str | None = None) -> None:
    async with _SYSTEM_MANAGER_CACHE_LOCK:
        if table is None:
            _SYSTEM_MANAGER_TABLE_CACHE.clear()
            return
        for key, entry in list(_SYSTEM_MANAGER_TABLE_CACHE.items()):
            if entry.get("table") == table:
                _SYSTEM_MANAGER_TABLE_CACHE.pop(key, None)


async def _refresh_system_manager_cache_entry(
    client: Any,
    cache_key: str,
    table: str,
    query_payload: dict[str, Any],
    request_id: str,
) -> None:
    if cache_key in _SYSTEM_MANAGER_REFRESHING:
        return
    _SYSTEM_MANAGER_REFRESHING.add(cache_key)
    try:
        status, body = await client.request("POST", "/v1/db/read", request_id=request_id, json_body=query_payload)
        if status < 400:
            await _system_manager_cache_set(cache_key, table, body)
    except Exception:
        pass
    finally:
        _SYSTEM_MANAGER_REFRESHING.discard(cache_key)



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
        profile_id = core._request_profile(request)
        query = (request.query_params.get("q") or "").strip()
        cache_key = _system_manager_cache_key(
            table=table,
            profile=profile_id,
            query=query,
            limit=limit,
            where=where,
        )
        payload: dict[str, Any] = {"table": table, "limit": limit}
        if query:
            payload["query"] = query
        if where:
            payload["where"] = where

        now = time.time()
        cached = await _system_manager_cache_get(cache_key)
        if cached is not None and not _system_manager_cache_is_stale(cached, now):
            return JSONResponse(
                core._envelope(  # noqa: SLF001
                    cached["payload"],
                    source_id="system-manager",
                    profile_id=profile_id,
                    freshness="live",
                    request_id=rid,
                    read_only=False,
                    mutations_supported=["upsert", "delete"],
                    degraded_reason=None,
                    fetched_at=float(cached.get("fetched_at", now)),
                    stale_after=float(cached.get("stale_after", now)),
                ),
                status_code=200,
            )
        if cached is not None and _system_manager_cache_is_stale(cached, now):
            asyncio.create_task(
                _refresh_system_manager_cache_entry(client, cache_key, table, payload, rid)
            )
            return JSONResponse(
                core._envelope(  # noqa: SLF001
                    cached["payload"],
                    source_id="system-manager",
                    profile_id=profile_id,
                    freshness="stale",
                    request_id=rid,
                    read_only=False,
                    mutations_supported=["upsert", "delete"],
                    degraded_reason="upstream_refresh_pending",
                    fetched_at=float(cached.get("fetched_at", now)),
                    stale_after=float(cached.get("stale_after", now)),
                ),
                status_code=200,
            )

        try:
            status, body = await client.request("POST", "/v1/db/read", request_id=rid, json_body=payload)
        except UpstreamError as exc:
            if cached is not None:
                return JSONResponse(
                    core._envelope(  # noqa: SLF001
                        cached["payload"],
                        source_id="system-manager",
                        profile_id=profile_id,
                        freshness="stale",
                        request_id=rid,
                        read_only=False,
                        mutations_supported=["upsert", "delete"],
                        degraded_reason=f"upstream_unavailable:{exc.status}",
                        fetched_at=float(cached.get("fetched_at", now)),
                        stale_after=float(cached.get("stale_after", now)),
                    ),
                    status_code=200,
                )
            return _json_error(_upstream_status(exc.status), "system_manager_unavailable", exc.detail or "unavailable", rid)
        if status >= 400:
            if cached is not None:
                return JSONResponse(
                    core._envelope(  # noqa: SLF001
                        cached["payload"],
                        source_id="system-manager",
                        profile_id=profile_id,
                        freshness="stale",
                        request_id=rid,
                        read_only=False,
                        mutations_supported=["upsert", "delete"],
                        degraded_reason=f"upstream_status:{status}",
                        fetched_at=float(cached.get("fetched_at", now)),
                        stale_after=float(cached.get("stale_after", now)),
                    ),
                    status_code=200,
                )
            return _json_error(_upstream_status(status), "system_manager_unavailable", (body.get("error") if isinstance(body, dict) else None) or "unavailable", rid)

        await _system_manager_cache_set(cache_key, table, body)
        freshness = "live" if status < 400 else "unavailable"
        return JSONResponse(
            core._envelope(  # noqa: SLF001
                body,
                source_id="system-manager",
                profile_id=profile_id,
                freshness=freshness,
                request_id=rid,
                read_only=False,
                mutations_supported=["upsert", "delete"],
                degraded_reason=None,
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
            await _system_manager_cache_invalidate(table=table)
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
            await _system_manager_cache_invalidate(table="services")
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
        await _system_manager_cache_invalidate()
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
