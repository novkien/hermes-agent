"""Upstream HTTP clients (httpx.AsyncClient wrappers).

Three sources:
- DashboardClient  — Hermes dashboard API (9119), cookie-session auth
                    (password-login once, then replay the session cookie)
- GatewayClient    — Hermes gateway API (8642), Authorization: Bearer
- AdapterClient    — AgentOS data adapter (8643), Authorization: Bearer
All clients inject an outbound X-Request-Id and accept an inbound request_id
for correlation. No secret values are ever logged here.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Optional

import httpx

from .security import redact_headers


def new_request_id() -> str:
    return uuid.uuid4().hex


class UpstreamError(RuntimeError):
    def __init__(self, status: int, body: Any, detail: str = ""):
        self.status = status
        self.body = body
        self.detail = detail
        super().__init__(f"upstream {status}: {detail}")


class _BaseClient:
    source_id: str = "unknown"

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        route_timeouts: Optional[dict[str, float]] = None,
        stream_read_timeout: float = 300.0,
        headers: Optional[dict[str, str]] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._default_timeout = timeout
        self._route_timeouts = route_timeouts or {}
        self._stream_read_timeout = stream_read_timeout
        self._static_headers = headers or {}
        self._client: Optional[httpx.AsyncClient] = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._default_timeout),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
            )
        return self._client

    def _timeout_for(self, path: str) -> float:
        for prefix, t in self._route_timeouts.items():
            if path.startswith(prefix):
                return t
        return self._default_timeout

    def _build_headers(
        self,
        inbound_request_id: str | None,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers = dict(self._static_headers)
        headers["X-Request-Id"] = inbound_request_id or new_request_id()
        if extra:
            headers.update(extra)
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        content: Optional[bytes] = None,
        inbound_request_id: str | None = None,
        extra_headers: Optional[dict[str, str]] = None,
        follow_redirects: bool = False,
        raw: bool = False,
    ) -> tuple[int, Any, dict[str, str]]:
        """Return (status, body, response_headers).

        body is parsed JSON when possible, else raw text; ``raw=True`` returns
        raw bytes for streaming passthrough.
        """
        client = self._ensure_client()
        headers = self._build_headers(inbound_request_id, extra_headers)
        try:
            resp = await client.request(
                method,
                path,
                params=params,
                json=json_body,
                content=content,
                headers=headers,
                follow_redirects=follow_redirects,
                timeout=self._timeout_for(path),
            )
        except httpx.TimeoutException:
            raise UpstreamError(504, {"error": "upstream timeout"}, "timeout")
        except httpx.HTTPError as e:
            raise UpstreamError(502, {"error": "upstream unavailable"}, str(e))

        if raw:
            body: Any = resp.content
        else:
            try:
                body = resp.json()
            except json.JSONDecodeError:
                body = resp.text
        return resp.status_code, body, dict(resp.headers)

    async def stream(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        inbound_request_id: str | None = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> httpx.Response:
        """Start an upstream request and return the live httpx.Response for
        streaming passthrough (SSE). Caller MUST consume or aclose it."""
        client = self._ensure_client()
        headers = self._build_headers(inbound_request_id, extra_headers)
        # `client.request()` reads the whole body before returning, which turns
        # an SSE stream into one buffered blob delivered after the turn ends.
        # `send(..., stream=True)` returns as soon as the headers land so frames
        # reach the browser as the agent produces them. The read timeout is the
        # gap allowed *between* frames, not a budget for the whole turn — a turn
        # that thinks or runs tools for minutes is normal here.
        request = client.build_request(
            method,
            path,
            params=params,
            json=json_body,
            headers=headers,
            timeout=httpx.Timeout(
                self._timeout_for(path), read=self._stream_read_timeout
            ),
        )
        try:
            return await client.send(request, stream=True)
        except httpx.TimeoutException:
            raise UpstreamError(504, {"error": "upstream timeout"}, "timeout")
        except httpx.HTTPError as e:
            raise UpstreamError(502, {"error": "upstream unavailable"}, str(e))

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def describe_headers(self, headers: dict[str, str]) -> str:
        """Redacted one-line header description for logs."""
        return json.dumps(redact_headers(headers), sort_keys=True)


class DashboardClient(_BaseClient):
    """Hermes dashboard API on 9119 — cookie-session auth.

    The dashboard (gated basic_auth) does NOT accept X-Hermes-Session-Token
    from non-loopback peers (System Manager live-verified 2026-08-08). This
    client authenticates once via ``POST /auth/password-login``
    ({"username": "admin", "password": ...}), captures the HttpOnly session
    cookie, and replays it on every 9119 request. On 401 the cookie is
    considered expired: re-login (with a lock so only one login runs) and
    retry once.
    """

    source_id = "hermes-dashboard"

    def __init__(
        self,
        base_url: str,
        basic_auth_password: str | None,
        *,
        timeout: float = 10.0,
        route_timeouts: Optional[dict[str, float]] = None,
    ):
        super().__init__(base_url, timeout=timeout, route_timeouts=route_timeouts)
        self._basic_auth_password = basic_auth_password
        self._cookie: Optional[str] = None
        # asyncio.Lock (not threading.Lock): these guards are used inside
        # async coroutines (_ensure_auth). A threading.Lock acquire() blocks
        # the event-loop thread and deadlocks concurrent dashboard callers
        # (registry probe + poll workers) during startup.
        self._auth_lock = asyncio.Lock()
        self._relogin_lock = asyncio.Lock()

    def _set_cookie_from_response(self, resp: httpx.Response) -> None:
        set_cookie = resp.headers.get("set-cookie", "")
        if not set_cookie:
            return
        # Keep the raw Set-Cookie value(s) so the dashboard receives the
        # exact cookie it issued. A session cookie pair may span multiple
        # Set-Cookie headers; httpx exposes them newline-joined.
        cookie = set_cookie.split(";", 1)[0]
        self._cookie = cookie

    async def _ensure_auth(self) -> None:
        if not self._basic_auth_password:
            raise UpstreamError(
                503, {"error": "dashboard_auth_unconfigured"},
                "DASHBOARD_BASIC_AUTH_PASSWORD not configured",
            )
        if self._cookie:
            return
        # Login exactly once per cookie; other callers wait on the lock.
        async with self._relogin_lock:
            if self._cookie:
                return
            client = self._ensure_client()
            try:
                resp = await client.post(
                    "/auth/password-login",
                    json={"provider": "basic", "username": "admin", "password": self._basic_auth_password},
                )
            except httpx.HTTPError as e:
                raise UpstreamError(
                    502, {"error": "dashboard login unavailable"}, str(e)
                ) from e
            if resp.status_code != 200:
                raise UpstreamError(
                    resp.status_code, {"error": "dashboard login failed"},
                    "dashboard login rejected",
                )
            self._set_cookie_from_response(resp)

    def _auth_headers(self) -> dict[str, str]:
        if self._cookie:
            return {"Cookie": self._cookie}
        return {}

    async def _authed_request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        content: Optional[bytes] = None,
        inbound_request_id: str | None = None,
        extra_headers: Optional[dict[str, str]] = None,
        follow_redirects: bool = False,
        raw: bool = False,
        _retried: bool = False,
    ) -> tuple[int, Any, dict[str, str]]:
        await self._ensure_auth()
        headers = dict(extra_headers or {})
        headers.update(self._auth_headers())
        # Call the BASE client request directly so we never recurse back
        # into _authed_request.
        status, body, resp_headers = await super().request(
            method, path, params=params, json_body=json_body, content=content,
            inbound_request_id=inbound_request_id, extra_headers=headers,
            follow_redirects=follow_redirects, raw=raw,
        )
        if status == 401 and not _retried:
            # Cookie expired: drop it, re-login, retry exactly once.
            self._cookie = None
            await self._ensure_auth()
            headers = dict(extra_headers or {})
            headers.update(self._auth_headers())
            status, body, resp_headers = await super().request(
                method, path, params=params, json_body=json_body, content=content,
                inbound_request_id=inbound_request_id, extra_headers=headers,
                follow_redirects=follow_redirects, raw=raw,
            )
        return status, body, resp_headers

    async def get(
        self,
        path: str,
        *,
        params: Optional[dict] = None,
        inbound_request_id: str | None = None,
        raw: bool = False,
    ) -> tuple[int, Any, dict[str, str]]:
        return await self._authed_request(
            "GET", path, params=params, inbound_request_id=inbound_request_id, raw=raw
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        content: Optional[bytes] = None,
        inbound_request_id: str | None = None,
        extra_headers: Optional[dict[str, str]] = None,
        follow_redirects: bool = False,
        raw: bool = False,
    ) -> tuple[int, Any, dict[str, str]]:
        return await self._authed_request(
            method, path, params=params, json_body=json_body, content=content,
            inbound_request_id=inbound_request_id, extra_headers=extra_headers,
            follow_redirects=follow_redirects, raw=raw,
        )


class GatewayClient(_BaseClient):
    """Hermes gateway API on 8642 — Authorization: Bearer auth.

    POST /api/cron/fire uses a separate NAS-minted JWT (Bearer, from
    NAS_JWT_SECRET) instead of the gateway API key.
    """

    source_id = "hermes-gateway"

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        nas_jwt_secret: str | None = None,
        timeout: float = 10.0,
        route_timeouts: Optional[dict[str, float]] = None,
        stream_read_timeout: float = 300.0,
    ):
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        super().__init__(
            base_url, timeout=timeout, route_timeouts=route_timeouts,
            stream_read_timeout=stream_read_timeout, headers=headers,
        )
        self._nas_jwt_secret = nas_jwt_secret

    async def cron_fire(
        self,
        body: dict[str, Any],
        *,
        inbound_request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, Any, dict[str, str]]:
        """POST /api/cron/fire — NAS-minted JWT credential, not the API key."""
        if not self._nas_jwt_secret:
            raise UpstreamError(503, {"error": "cron_fire_unconfigured"},
                                "NAS_JWT_SECRET not configured")
        extra = {"Authorization": f"Bearer {self._nas_jwt_secret}"}
        if idempotency_key:
            extra["Idempotency-Key"] = idempotency_key
        return await self.request(
            "POST", "/api/cron/fire", json_body=body,
            inbound_request_id=inbound_request_id, extra_headers=extra,
        )

    async def get(
        self,
        path: str,
        *,
        params: Optional[dict] = None,
        inbound_request_id: str | None = None,
        raw: bool = False,
    ) -> tuple[int, Any, dict[str, str]]:
        """GET helper (source workers poll /health, /v1/models, ...)."""
        return await self.request("GET", path, params=params,
                                  inbound_request_id=inbound_request_id, raw=raw)


class AdapterClient(_BaseClient):
    """AgentOS data adapter on 8643 — Bearer token auth."""

    source_id = "adapter"

    def __init__(
        self,
        base_url: str,
        token: str | None,
        *,
        timeout: float = 10.0,
        route_timeouts: Optional[dict[str, float]] = None,
    ):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        super().__init__(base_url, timeout=timeout, route_timeouts=route_timeouts, headers=headers)

    async def _get(self, path: str, params: Optional[dict] = None,
                   request_id: str | None = None) -> tuple[int, Any, dict[str, str]]:
        # Named `request_id` to match every typed method below; several of them
        # pass it by keyword, so a mismatch here raises TypeError at call time.
        return await self.request("GET", path, params=params,
                                  inbound_request_id=request_id)

    # -- typed methods (adapter endpoint contract, architecture-freeze §3) --
    async def health(self, request_id: str | None = None):
        return await self._get("/health", request_id=request_id)

    async def capabilities(self, request_id: str | None = None):
        return await self._get("/capabilities", request_id=request_id)

    async def kanban_tasks(self, params: dict | None = None, request_id: str | None = None):
        return await self._get("/kanban/tasks", params, request_id)

    async def kanban_task_detail(self, task_id: str, request_id: str | None = None):
        return await self._get(f"/kanban/tasks/{task_id}", request_id=request_id)

    async def kanban_task_events(self, task_id: str, params: dict | None = None,
                                 request_id: str | None = None):
        return await self._get(f"/kanban/tasks/{task_id}/events", params, request_id)

    async def kanban_task_runs(self, task_id: str, request_id: str | None = None):
        return await self._get(f"/kanban/tasks/{task_id}/runs", request_id=request_id)

    async def kanban_task_attachments(self, task_id: str, request_id: str | None = None):
        return await self._get(f"/kanban/tasks/{task_id}/attachments", request_id=request_id)

    async def kanban_board_summary(self, request_id: str | None = None):
        return await self._get("/kanban/board/summary", request_id=request_id)

    async def permits(self, params: dict | None = None, request_id: str | None = None):
        return await self._get("/permits", params, request_id)

    async def permit_detail(self, permit_id: str, request_id: str | None = None):
        return await self._get(f"/permits/{permit_id}", request_id=request_id)

    async def issues(self, params: dict | None = None, request_id: str | None = None):
        return await self._get("/issues", params, request_id)

    async def issue_detail(self, issue_id: str, params: dict | None = None,
                           request_id: str | None = None):
        return await self._get(f"/issues/{issue_id}", params, request_id)

    async def sessions_search(self, q: str, params: dict | None = None,
                              request_id: str | None = None):
        p = dict(params or {})
        p["q"] = q
        return await self._get("/sessions/search", p, request_id)

    async def session_timeline(self, session_id: str, params: dict | None = None,
                               request_id: str | None = None):
        return await self._get(f"/sessions/{session_id}/timeline", params, request_id)

    async def room_binding(self, params: dict | None = None, request_id: str | None = None):
        return await self._get("/room-binding", params, request_id)

    async def fingerprint(self, source: str, request_id: str | None = None):
        return await self._get(f"/sources/{source}/fingerprint", None, request_id)

    # -- Stage 5 aliases (S5 adapter client naming) ------------------------
    async def board_summary(self, request_id: str | None = None):
        return await self.kanban_board_summary(request_id=request_id)

    async def tasks(self, limit: int = 100, **params):
        p = dict(params)
        p["limit"] = min(int(limit), 100)
        return await self.request("GET", "/kanban/tasks", params=p)

    async def permits_list(self, limit: int = 100, **params):
        p = dict(params)
        p["limit"] = min(int(limit), 100)
        return await self.request("GET", "/permits", params=p)

    async def issues_list(self, limit: int = 100, **params):
        p = dict(params)
        p["limit"] = min(int(limit), 100)
        return await self.request("GET", "/issues", params=p)

    async def memory_file(self, filename: str, request_id: str | None = None):
        return await self._get(f"/memory/files/{filename}", request_id=request_id)

    async def memory_file_write(
        self, filename: str, content: str, request_id: str | None = None
    ):
        return await self.request(
            "PUT",
            f"/memory/files/{filename}",
            json_body={"content": content},
            inbound_request_id=request_id,
        )

    async def session_search(self, q: str, limit: int = 200):
        return await self.sessions_search(q, params={"limit": min(int(limit), 200)})

    # -- decision writes ---------------------------------------------------
    # Hermes exposes no REST route for either; the adapter drives the same CLI
    # scripts the agent's own tool-calls use. Deliberately not reachable through
    # the GET-only ADAPTER_ROUTE_PATTERNS proxy.
    async def permit_decision(
        self, permit_id: str, body: dict, request_id: str | None = None
    ):
        return await self.request(
            "POST", f"/permits/{permit_id}/decision",
            json_body=body, inbound_request_id=request_id,
        )

    async def issue_update(
        self, issue_id: str, body: dict, request_id: str | None = None
    ):
        return await self.request(
            "POST", f"/issues/{issue_id}/update",
            json_body=body, inbound_request_id=request_id,
        )
