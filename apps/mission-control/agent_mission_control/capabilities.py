"""Capability registry — probes each upstream source at startup and on refresh.

Per source: {healthy, schema_fingerprint, routes_checked, last_checked_at}.
Probes:
- adapter:  GET /health + GET /capabilities
- dashboard: GET /health (9119)
- gateway:   GET /health (open) + GET /v1/models (expect 401 = auth-required proof)
- cron:      route presence check (gateway route table evidence)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from .clients import AdapterClient, DashboardClient, GatewayClient, UpstreamError
from .store import Store

_SOURCES = ("adapter", "hermes-dashboard", "hermes-gateway", "cron")


class CapabilityRegistry:
    def __init__(
        self,
        adapter: AdapterClient,
        dashboard: DashboardClient,
        gateway: GatewayClient,
        store: Store,
    ):
        self._adapter = adapter
        self._dashboard = dashboard
        self._gateway = gateway
        self._store = store
        self._lock = asyncio.Lock()
        self._capabilities: dict[str, dict[str, Any]] = {}

    async def refresh(self) -> dict[str, dict[str, Any]]:
        """Probe all sources concurrently; merge into the registry."""
        async with self._lock:
            results = await asyncio.gather(
                self._probe_adapter(),
                self._probe_dashboard(),
                self._probe_gateway(),
                self._probe_cron(),
                return_exceptions=True,
            )
            for source_id, result in zip(_SOURCES, results):
                if isinstance(result, Exception):
                    self._capabilities[source_id] = {
                        "healthy": False,
                        "schema_fingerprint": None,
                        "routes_checked": [],
                        "last_checked_at": time.time(),
                        "error": str(result),
                    }
                else:
                    self._capabilities[source_id] = result
            return dict(self._capabilities)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return dict(self._capabilities)

    # -- probes -----------------------------------------------------------
    async def _probe_adapter(self) -> dict[str, Any]:
        routes_checked: list[str] = []
        healthy = False
        fingerprint = None
        try:
            s, body, _ = await self._adapter.health()
            routes_checked.append(f"GET /health -> {s}")
            healthy = healthy or (200 <= s < 300)
            s2, cap, _ = await self._adapter.capabilities()
            routes_checked.append(f"GET /capabilities -> {s2}")
            if 200 <= s2 < 300 and isinstance(cap, dict):
                fingerprint = cap.get("schema_fingerprint") or cap.get("fingerprint")
                if isinstance(cap, dict) and "sources" in cap:
                    fp_sources = cap["sources"]
                    if isinstance(fp_sources, dict) and "adapter" in fp_sources:
                        pass
            healthy = healthy or (200 <= s2 < 300)
        except UpstreamError as e:
            routes_checked.append(f"error: {e.status}")
        except Exception as e:  # noqa: BLE001
            routes_checked.append(f"error: {type(e).__name__}")
        if fingerprint:
            self._store.record_fingerprint("adapter", str(fingerprint))
        else:
            fingerprint = self._store.get_fingerprint("adapter")
        return {
            "healthy": healthy,
            "schema_fingerprint": fingerprint,
            "routes_checked": routes_checked,
            "last_checked_at": time.time(),
        }

    async def _probe_dashboard(self) -> dict[str, Any]:
        routes_checked: list[str] = []
        try:
            s, _, _ = await self._dashboard.get("/api/health")
            routes_checked.append(f"GET /api/health -> {s}")
            healthy = 200 <= s < 300
        except UpstreamError as e:
            routes_checked.append(f"error: {e.status}")
            healthy = False
        return {
            "healthy": healthy,
            "schema_fingerprint": None,
            "routes_checked": routes_checked,
            "last_checked_at": time.time(),
        }

    async def _probe_gateway(self) -> dict[str, Any]:
        routes_checked: list[str] = []
        try:
            s, _, _ = await self._gateway.request("GET", "/health")
            routes_checked.append(f"GET /health -> {s}")
            healthy = 200 <= s < 300
        except UpstreamError as e:
            routes_checked.append(f"error: {e.status}")
            healthy = False
        # Auth-required proof: /v1/models without valid key must 401.
        try:
            s2, _, _ = await self._gateway.request("GET", "/v1/models")
            routes_checked.append(f"GET /v1/models -> {s2} (expect 401 = auth gate active)")
        except UpstreamError as e:
            s2 = e.status
            routes_checked.append(f"GET /v1/models -> error {s2}")
        return {
            "healthy": healthy,
            "schema_fingerprint": None,
            "routes_checked": routes_checked,
            "last_checked_at": time.time(),
        }

    async def _probe_cron(self) -> dict[str, Any]:
        """Cron availability is a route-table fact (u02b row 37), not a live probe."""
        configured = bool(self._gateway._nas_jwt_secret)  # noqa: SLF001
        return {
            "healthy": configured,
            "schema_fingerprint": None,
            "routes_checked": [
                "POST /api/cron/fire (route table u02b row 37)",
                f"NAS_JWT_SECRET configured: {configured}",
            ],
            "last_checked_at": time.time(),
        }
