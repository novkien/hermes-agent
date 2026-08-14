#!/usr/bin/env python3
"""Non-destructive discovery of the upstream dashboard's skill write routes.

Why this exists: the 9119 dashboard answers an unknown path AND a method
mismatch with the same catch-all 404 (`No such API endpoint: <path>`), so a
GET probe cannot tell "route absent" from "route exists but is POST-only".
The only way to tell them apart is to send the real method.

Safety: every probe targets a skill name that cannot exist. A route that is
present rejects it ("skill not found" / 400 / 422); a route that is absent
returns the catch-all. Nothing is created, changed or deleted either way.

Run:  sudo .venv/bin/python tools/probe_skill_writes.py
(needs root only to read /etc/agent-mission-control/env for the 9119 password)
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mission_control.clients import DashboardClient, UpstreamError  # noqa: E402

ENV_PATH = Path("/etc/agent-mission-control/env")
NONEXISTENT = "__amc_probe_skill_that_does_not_exist__"

# The dashboard answers a method mismatch with 405, which means the earlier
# "/api/skills/<verb>" probes were matching a "/api/skills/{name}" path
# parameter, not a verb route. So probe the REST shape directly.
#
# Every body is deliberately INVALID (no YAML frontmatter). The upstream
# rejects it during validation, before touching the filesystem, so even a
# route that would write cannot write here.
BAD_BODY = {"content": "probe: no frontmatter, must be rejected\n"}

PROBES = [
    ("GET", f"/api/skills/{NONEXISTENT}", None),
    ("PUT", f"/api/skills/{NONEXISTENT}", BAD_BODY),
    ("PATCH", f"/api/skills/{NONEXISTENT}", {"enabled": False}),
    ("POST", f"/api/skills/{NONEXISTENT}", BAD_BODY),
    ("DELETE", f"/api/skills/{NONEXISTENT}", None),
    ("POST", f"/api/skills/{NONEXISTENT}/enable", None),
    ("POST", f"/api/skills/{NONEXISTENT}/disable", None),
    ("POST", f"/api/skills/{NONEXISTENT}/toggle", {"enabled": False}),
    ("POST", f"/api/skills/{NONEXISTENT}/archive", None),
    ("POST", f"/api/skills/{NONEXISTENT}/unarchive", None),
    ("PUT", f"/api/skills/{NONEXISTENT}/content", BAD_BODY),
    ("POST", "/api/skills", {"name": NONEXISTENT, **BAD_BODY}),
    ("PUT", "/api/skills/content", {"name": NONEXISTENT, **BAD_BODY}),
    ("POST", "/api/skills/toggle", {"name": NONEXISTENT, "enabled": False}),
]


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def classify(status: int, body) -> str:
    """Absent routes answer with the dashboard's catch-all; anything else exists."""
    text = json.dumps(body) if not isinstance(body, str) else body
    if status == 404 and "No such API endpoint" in text:
        return "ABSENT"
    if status == 405:
        return "WRONG-METHOD"
    return "EXISTS"


async def main() -> int:
    env = read_env(ENV_PATH)
    client = DashboardClient(
        env.get("HERMES_DASHBOARD_URL", "http://127.0.0.1:9119"),
        env.get("DASHBOARD_BASIC_AUTH_PASSWORD"),
        timeout=15.0,
    )

    found = []
    try:
        for method, path, body in PROBES:
            params = {"name": NONEXISTENT} if body is None else None
            try:
                status, payload, _headers = await client.request(
                    method, path, params=params, json_body=body,
                    inbound_request_id="skill-write-probe",
                )
            except UpstreamError as exc:
                status, payload = exc.status, exc.detail
            verdict = classify(status, payload)
            snippet = json.dumps(payload)[:150] if payload is not None else ""
            print(f"{verdict:<13} {method:<7} {path:<28} {status}  {snippet}")
            if verdict != "ABSENT":
                found.append((method, path, status, payload))
    finally:
        await client.aclose()

    print("\n--- routes that exist ---")
    if not found:
        print("none: the dashboard exposes no skill write route on any probed shape")
    for method, path, status, payload in found:
        print(f"{method} {path} -> {status} {json.dumps(payload)[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
