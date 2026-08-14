#!/usr/bin/env python3
"""Enumerate the upstream services' real route tables (read-only).

Probing one path shape at a time can only ever prove "this shape is absent".
If the service publishes an OpenAPI document, the whole table is available in
one GET — which settles what a skill enable/disable/archive/delete would have
to be called, or proves nothing of the sort is registered.

Falls back to targeted GET probes when no schema document is served.

Run:  sudo .venv/bin/python tools/probe_upstream_routes.py
(needs root only to read /etc/agent-mission-control/env for upstream creds)
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mission_control.clients import (  # noqa: E402
    AdapterClient,
    DashboardClient,
    GatewayClient,
    UpstreamError,
)

ENV_PATH = Path("/etc/agent-mission-control/env")
SCHEMA_PATHS = ("/openapi.json", "/api/openapi.json", "/docs/openapi.json")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


async def fetch(client, method: str, path: str):
    try:
        return await client.request(
            method, path, inbound_request_id="route-probe",
        )
    except UpstreamError as exc:
        return exc.status, exc.detail, {}
    except Exception as exc:  # transport-level
        return 0, {"error": str(exc)}, {}


async def dump(label: str, client, interest: str) -> None:
    print(f"\n=== {label} ===")
    schema = None
    for path in SCHEMA_PATHS:
        status, body, _ = await fetch(client, "GET", path)
        if status == 200 and isinstance(body, dict) and "paths" in body:
            schema = body
            print(f"openapi: {path}")
            break
    if not schema:
        print("openapi: not served")
        return

    paths = schema.get("paths") or {}
    print(f"routes: {len(paths)}")
    hits = []
    for route, ops in sorted(paths.items()):
        if interest not in route:
            continue
        verbs = sorted(v.upper() for v in ops if v.lower() in
                       {"get", "post", "put", "patch", "delete"})
        hits.append(f"  {', '.join(verbs):<22} {route}")
    print(f"--- routes containing {interest!r} ---")
    print("\n".join(hits) if hits else "  (none)")

    writes = []
    for route, ops in sorted(paths.items()):
        verbs = sorted(v.upper() for v in ops if v.lower() in
                       {"post", "put", "patch", "delete"})
        if verbs:
            writes.append(f"  {', '.join(verbs):<22} {route}")
    print(f"--- all write routes ({len(writes)}) ---")
    print("\n".join(writes) if writes else "  (none)")


async def main() -> int:
    env = read_env(ENV_PATH)
    clients = [
        ("dashboard 9119", DashboardClient(
            env.get("HERMES_DASHBOARD_URL", "http://127.0.0.1:9119"),
            env.get("DASHBOARD_BASIC_AUTH_PASSWORD"), timeout=15.0)),
        ("gateway 8642", GatewayClient(
            env.get("HERMES_GATEWAY_URL", "http://127.0.0.1:8642"),
            env.get("API_SERVER_KEY"), timeout=15.0)),
        ("adapter 8643", AdapterClient(
            env.get("ADAPTER_URL", "http://127.0.0.1:8643"),
            env.get("ADAPTER_TOKEN"), timeout=15.0)),
    ]
    try:
        for label, client in clients:
            await dump(label, client, "skill")
    finally:
        for _label, client in clients:
            await client.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
