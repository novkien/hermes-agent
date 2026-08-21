"""HTTP client for the Hermes System Manager daemon.

Kept separate from the dashboard/gateway/adapter clients because System Manager
is an optional companion service and owns a distinct SQLite database. The
client does not log request/response bodies because current development rows may
contain native credentials.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .clients import UpstreamError

DEFAULT_URL = "http://127.0.0.1:8787"


class SystemManagerClient:
    source_id = "system-manager"

    def __init__(self) -> None:
        self.base_url = (os.getenv("SYSTEM_MANAGER_URL", DEFAULT_URL) or DEFAULT_URL).strip().rstrip("/")
        self.token = (os.getenv("SYSTEM_MANAGER_TOKEN", "") or "").strip()
        try:
            timeout = float(os.getenv("SYSTEM_MANAGER_TIMEOUT_SECONDS", "15") or "15")
        except ValueError:
            timeout = 15.0
        self.timeout = max(1.0, timeout)

    async def request(
        self,
        method: str,
        path: str,
        *,
        request_id: str,
        json_body: Any = None,
    ) -> tuple[int, Any]:
        headers = {"X-Request-Id": request_id}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            # A short-lived client keeps this optional integration free of new
            # AppDeps/shutdown ownership. The daemon is LAN/local and requests
            # are low-frequency operator/inventory calls.
            async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
                response = await client.request(method, path, json=json_body, headers=headers)
        except httpx.TimeoutException as exc:
            raise UpstreamError(504, {"error": "system_manager_timeout"}, "timeout") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(
                502,
                {"error": "system_manager_unavailable"},
                type(exc).__name__,
            ) from exc
        try:
            body = response.json()
        except ValueError:
            body = {"success": False, "error": response.text[:1000] or "invalid upstream response"}
        return response.status_code, body
