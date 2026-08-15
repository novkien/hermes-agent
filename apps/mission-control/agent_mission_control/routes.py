"""HTTP route layer: auth/session/CSRF, read proxy, mutation allowlist, audit,
static serving, SSE stub, capability/audit endpoints.

The app factory in ``app.py`` wires everything together; this module keeps the
route/security logic separate for testability.

Upstream auth contract (stage1 evidence + operator correction 2026-08-07):
- Dashboard 9119:  X-Hermes-Session-Token (REST read surface; NO chat/stream)
- Gateway   8642:  Authorization: Bearer <API_SERVER_KEY>; chat/stream lives
                    HERE (POST /api/sessions/{id}/chat/stream, api_server.py)
- Adapter   8643:  Authorization: Bearer <ADAPTER_TOKEN>
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
import httpx

from .cache import Cache
from .capabilities import CapabilityRegistry
from . import alerts as alerts_mod
from . import chat_proxy
from . import redact as redact_mod
from . import search as search_mod
from .correlation import CorrelationEngine
from .event_bus import EventBus, sse_frame, sse_frame_named, sse_heartbeat
from .pulse import Pulse
from .run_inspector import RunInspector
from .runner_manager import RunnerManager, RunnerSpawnError
from .clients import (
    AdapterClient,
    DashboardClient,
    GatewayClient,
    UpstreamError,
)
from .config import Settings
from .ip_utils import CidrList, resolve_client_ip
from .security import (
    SlidingWindowRateLimiter,
    build_request_summary,
    constant_time_equal,
)
from .session_persona_store import SessionPersonaStore
from .store import Store

logger = logging.getLogger("agent_mission_control.routes")


@dataclass(frozen=True)
class SessionExecutionTarget:
    execution_mode: str
    profile_name: str | None
    client: GatewayClient


_RUNNER_ERROR_STATUS = {
    "runner_profile_missing": 400,
    "runner_executable_missing": 503,
    "runner_start_timeout": 504,
    "runner_auth_failed": 502,
    "runner_unhealthy": 502,
    "runner_exited": 502,
}

# --------------------------------------------------------------------------
# Read allowlist — derived from stage1-evidence u02-dashboard-routes.json
# (GET routes of the 9119 dashboard, excluding 9119's own auth/UI routes).
# Source: hermes-api/dashboard/u02-dashboard-routes.json
READ_ALLOWLIST = [
    "/api/actions", "/api/analytics", "/api/audio", "/api/config",
    "/api/credentials", "/api/cron", "/api/curator", "/api/dashboard",
    "/api/egress", "/api/env", "/api/files", "/api/fs", "/api/git",
    "/api/health", "/api/hermes", "/api/learning", "/api/logs", "/api/mcp",
    "/api/media", "/api/memory", "/api/messaging", "/api/model", "/api/ops",
    "/api/pairing", "/api/portal", "/api/profiles", "/api/providers",
    "/api/sessions", "/api/skills", "/api/ssh", "/api/status", "/api/system",
    "/api/tools", "/api/webhooks",
]
# Excluded from the allowlist (9119-internal auth/UI/PTY, never proxied):
_READ_EXCLUDE = {
    "/api/auth", "/api/chat", "/api/pty", "/api/ws", "/api/events",
    # Sensitive surfaces never proxied (freeze §10/§14.3 — no secrets, no
    # arbitrary filesystem access, no credential/SSH/oauth/pairing material):
    "/api/env", "/api/fs", "/api/ssh", "/api/credentials",
    "/api/providers/oauth", "/api/pairing",
}

# Mutation allowlist — each maps 1:1 to a verified Hermes gateway (8642) route
# from stage1-evidence u02b-gateway-routes.md. permit decision and issue_update
# are deliberately NOT exposed in v1 (task §9).
MUTATION_ALLOWLIST = {
    "chat_send": {
        "route": "/api/sessions/{id}/chat/stream",
        "method": "POST",
        "summary": "chat.send",
        "stream": True,
    },
    "session_create": {
        "route": "/api/sessions",
        "method": "POST",
        "summary": "session.create",
    },
    "session_patch": {
        "route": "/api/sessions/{id}",
        "method": "PATCH",
        "body_keys_allow": ("title", "end_reason"),
        "summary": "session.patch",
    },
    "session_delete": {
        "route": "/api/sessions/{id}",
        "method": "DELETE",
        "require_confirm": "1",
        "summary": "session.delete",
    },
    # Branch a conversation instead of editing it. The gateway's HTTP surface
    # has no edit/rewind route (Hermes Desktop does that over its WebSocket RPC
    # with `truncate_before_user_ordinal`), so a fork is how the SPA offers
    # "take this somewhere else" without losing the original thread.
    "session_fork": {
        "route": "/api/sessions/{id}/fork",
        "method": "POST",
        "body_keys_allow": ("id", "session_id", "title"),
        "summary": "session.fork",
    },
    # Distinct from the per-turn model override the composer already sends:
    # this pins the session itself, so runs started from Telegram or cron use
    # the same model.
    "session_model_lock": {
        "route": "/api/sessions/{id}/model",
        "method": "POST",
        "body_keys_allow": ("model", "provider", "base_url", "model_options"),
        "summary": "session.model_lock",
    },
    # Explicit cancellation, alongside the implicit disconnect path. Only
    # resolves once the gateway registers session-chat-stream runs in its run
    # registry; until then it answers 404 and the SPA falls back to aborting
    # the stream, which interrupts the agent on its own.
    "run_stop": {
        "route": "/v1/runs/{id}/stop",
        "method": "POST",
        "summary": "run.stop",
    },
    "cron_fire": {
        "route": "/api/cron/fire",
        "method": "POST",
        "cron": True,
        "summary": "cron.fire",
    },
}

# Gateway (8642) GET routes the SPA may read through /api/gateway/<path>.
# The dashboard read proxy cannot serve these: they describe what the *gateway's
# own agent* can do (its toolsets, its skills, its feature flags), which is
# exactly what the chat surface needs in order to stop guessing. Verified
# against `_http_route_table()` in gateway/platforms/api_server.py.
GATEWAY_READ_PATHS = frozenset({
    "/v1/capabilities",
    "/v1/skills",
    "/v1/toolsets",
    "/v1/models",
    "/api/model/options",
})

ADAPTER_ROUTE_PATTERNS = (
    re.compile(r"^/health$"),
    re.compile(r"^/capabilities$"),
    re.compile(r"^/kanban/boards$"),
    re.compile(r"^/kanban/tasks$"),
    re.compile(r"^/kanban/tasks/[^/]+$"),
    re.compile(r"^/kanban/tasks/[^/]+/(?:events|runs|attachments)$"),
    re.compile(r"^/kanban/tasks/[^/]+/worker-session$"),
    re.compile(r"^/kanban/board/summary$"),
    re.compile(r"^/permits(?:/[^/]+)?$"),
    re.compile(r"^/issues(?:/[^/]+)?$"),
    re.compile(r"^/sessions/search$"),
    re.compile(r"^/sessions/[^/]+/timeline$"),
    re.compile(r"^/sources/[^/]+/fingerprint$"),
    re.compile(r"^/room-binding$"),
    re.compile(r"^/room-sessions$"),
    # Cards attributed to each of a room's threads. The join it performs spans
    # the kanban board databases and state.db, so only the adapter can answer
    # it; the Fleet chart would otherwise have to page every board and every
    # session on each load.
    re.compile(r"^/room-cards$"),
    re.compile(r"^/session-tips$"),
    re.compile(r"^/thread-sessions$"),
)

# Upstream (9119 dashboard) MUTATION proxy — the verified dashboard write
# routes the SPA actually calls through /api/upstream/<dashboard-path>.
# Every entry maps to a u02 evidence write route; nothing outside this set is
# forwarded. Method translation: the SPA uses PATCH /cron/jobs/{id} and
# POST /cron/jobs/{id}/fire while the dashboard registers PUT /cron/jobs/{id}
# and POST /cron/jobs/{id}/trigger — the proxy adapts (canonical SPA surface,
# BFF translates to the upstream verb/path).
UPSTREAM_MUTATION_SPECS = {
    "/api/sessions/{session_id}": {
        "methods": {"PATCH", "DELETE"},
        "upstream_path": "/api/sessions/{session_id}",
        "summary": "upstream.session.update",
        "body_keys_allow": ("name", "archived"),
    },
    # One path, two verbs with different upstream shapes: the SPA's PATCH
    # becomes the dashboard's PUT, while DELETE passes through untranslated.
    #
    # The dashboard's CronJobUpdate is {"updates": {...}} — a nested envelope,
    # not flat fields. Sending flat fields (as the SPA used to) produces a 422
    # and is why cron edits never took effect.
    "/api/cron/jobs/{job_id}": {
        "methods": {"PATCH", "DELETE"},
        "upstream_path": "/api/cron/jobs/{job_id}",
        "upstream_method_map": {"PATCH": "PUT"},
        "summary": "upstream.cron.update",
        "body_keys_allow": ("updates",),
        "body_nested_keys_allow": {
            "updates": (
                "prompt", "schedule", "name", "deliver", "skills", "model",
                "provider", "base_url", "script", "context_from",
                "enabled_toolsets", "workdir", "no_agent", "enabled",
            ),
        },
        "require_confirm": ("DELETE",),
        "forward_query": ("profile",),
    },
    "/api/cron/jobs/{job_id}/fire": {
        "methods": {"POST"},
        "upstream_path": "/api/cron/jobs/{job_id}/trigger",
        "summary": "upstream.cron.trigger",
        "forward_query": ("profile",),
    },
    # Pause/resume are their own upstream routes. The SPA previously faked them
    # with a {"state": …} PATCH, which upstream never accepted.
    "/api/cron/jobs/{job_id}/pause": {
        "methods": {"POST"},
        "upstream_path": "/api/cron/jobs/{job_id}/pause",
        "summary": "upstream.cron.pause",
        "forward_query": ("profile",),
    },
    "/api/cron/jobs/{job_id}/resume": {
        "methods": {"POST"},
        "upstream_path": "/api/cron/jobs/{job_id}/resume",
        "summary": "upstream.cron.resume",
        "forward_query": ("profile",),
    },
    "/api/ops/doctor": {
        "methods": {"POST"},
        "upstream_path": "/api/ops/doctor",
        "summary": "upstream.ops.doctor",
    },
    "/api/ops/security-audit": {
        "methods": {"POST"},
        "upstream_path": "/api/ops/security-audit",
        "summary": "upstream.ops.security_audit",
    },
    "/api/ops/backup": {
        "methods": {"POST"},
        "upstream_path": "/api/ops/backup",
        "summary": "upstream.ops.backup",
    },
    # Skill writes. Taken from the dashboard's own OpenAPI document (9119
    # /openapi.json, read 2026-08-09) rather than guessed — every verb and body
    # below is the one the route actually declares.
    #
    # PUT /api/skills/content {name, content} — 404 when the skill does not
    # exist, 400 unless `content` opens with YAML frontmatter.
    "/api/skills/content": {
        "methods": {"PUT"},
        "upstream_path": "/api/skills/content",
        "summary": "upstream.skill.save",
        "body_keys_allow": ("name", "content"),
        "forward_query": ("profile",),
    },
    # PUT (not POST) /api/skills/toggle {name, enabled} — one route serves both
    # enable and disable; the SPA picks the flag.
    "/api/skills/toggle": {
        "methods": {"PUT"},
        "upstream_path": "/api/skills/toggle",
        "summary": "upstream.skill.toggle",
        "body_keys_allow": ("name", "enabled"),
        "forward_query": ("profile",),
    },
    # Removal lives under the skill hub. It answers 200 with a background job
    # id, so the SPA must re-read the inventory rather than trust the response.
    "/api/skills/hub/uninstall": {
        "methods": {"POST"},
        "upstream_path": "/api/skills/hub/uninstall",
        "summary": "upstream.skill.uninstall",
        "body_keys_allow": ("name",),
        "forward_query": ("profile",),
    },
    # No archive route exists on 9119 — the OpenAPI document declares no path
    # matching /archiv/ at all — so archive stays unallowlisted.

    # ---- cron: full lifecycle -------------------------------------------
    # Schedules are not validated here. Upstream owns the cron grammar, so its
    # 4xx passes through unchanged rather than being second-guessed by the BFF.
    # Body mirrors the dashboard's CronJobCreate model field-for-field.
    "/api/cron/jobs": {
        "methods": {"POST"},
        "upstream_path": "/api/cron/jobs",
        "summary": "upstream.cron.create",
        "body_keys_allow": (
            "prompt", "schedule", "name", "deliver", "skills", "model", "provider",
            "base_url", "script", "context_from", "enabled_toolsets", "workdir",
            "no_agent",
        ),
        "forward_query": ("profile",),
    },
    # ---- plugins ---------------------------------------------------------
    # These rewrite config.yaml immediately but do not hot-reload the running
    # gateway, so the envelope tells the UI to surface a restart prompt.
    "/api/dashboard/agent-plugins/{name}/enable": {
        "methods": {"POST"},
        "upstream_path": "/api/dashboard/agent-plugins/{name}/enable",
        "summary": "upstream.plugin.enable",
        "response_meta": {"restart_required": True},
    },
    "/api/dashboard/agent-plugins/{name}/disable": {
        "methods": {"POST"},
        "upstream_path": "/api/dashboard/agent-plugins/{name}/disable",
        "summary": "upstream.plugin.disable",
        "response_meta": {"restart_required": True},
    },

    # ---- profiles --------------------------------------------------------
    "/api/profiles": {
        "methods": {"POST"},
        "upstream_path": "/api/profiles",
        "summary": "upstream.profile.create",
        "body_keys_allow": (
            "name", "clone_from", "clone_from_default", "clone_all", "no_skills",
            "description", "provider", "model",
        ),
    },
    # Literal paths are declared before the {name} pattern: first match wins,
    # so "active" must never be captured as a profile name.
    "/api/profiles/active": {
        "methods": {"POST"},
        "upstream_path": "/api/profiles/active",
        "summary": "upstream.profile.activate",
        "body_keys_allow": ("name",),
    },
    "/api/profiles/{name}": {
        "methods": {"PATCH", "DELETE"},
        "upstream_path": "/api/profiles/{name}",
        "summary": "upstream.profile.update",
        "body_keys_allow": ("name", "description", "model"),
        "require_confirm": ("DELETE",),
    },
    # The per-profile system prompt. Free text, so no key filtering beyond the
    # single field the upstream route reads.
    "/api/profiles/{name}/soul": {
        "methods": {"PUT"},
        "upstream_path": "/api/profiles/{name}/soul",
        "summary": "upstream.profile.soul_save",
        # Upstream's ProfileSoulUpdate field is `content`; a body spelled `soul`
        # passes the allowlist and then 422s, so it is deliberately not allowed.
        "body_keys_allow": ("content",),
    },
    "/api/profiles/{name}/description": {
        "methods": {"PUT"},
        "upstream_path": "/api/profiles/{name}/description",
        "summary": "upstream.profile.description",
        "body_keys_allow": ("description",),
    },
    "/api/profiles/{name}/model": {
        "methods": {"PUT"},
        "upstream_path": "/api/profiles/{name}/model",
        "summary": "upstream.profile.model",
        "body_keys_allow": ("model", "provider"),
    },
    "/api/profiles/{name}/describe-auto": {
        "methods": {"POST"},
        "upstream_path": "/api/profiles/{name}/describe-auto",
        "summary": "upstream.profile.describe_auto",
    },

    # ---- mcp servers -----------------------------------------------------
    # POST only. Upstream's PUT on this path is MCPServersReplace — a whole-map
    # replace of mcp.json — so driving it from a single-server form would let a
    # stale client silently delete every server it did not know about.
    "/api/mcp/servers": {
        "methods": {"POST"},
        "upstream_path": "/api/mcp/servers",
        "summary": "upstream.mcp.create",
        "body_keys_allow": ("name", "command", "args", "env", "url", "auth"),
        "reject_sentinel": True,
    },
    "/api/mcp/servers/{name}": {
        "methods": {"DELETE"},
        "upstream_path": "/api/mcp/servers/{name}",
        "summary": "upstream.mcp.delete",
        "require_confirm": True,
    },
    "/api/mcp/servers/{name}/test": {
        "methods": {"POST"},
        "upstream_path": "/api/mcp/servers/{name}/test",
        "summary": "upstream.mcp.test",
    },
    "/api/mcp/servers/{name}/enabled": {
        "methods": {"PUT"},
        "upstream_path": "/api/mcp/servers/{name}/enabled",
        "summary": "upstream.mcp.toggle",
        "body_keys_allow": ("enabled",),
    },
    "/api/mcp/catalog/install": {
        "methods": {"POST"},
        "upstream_path": "/api/mcp/catalog/install",
        "summary": "upstream.mcp.catalog_install",
        "body_keys_allow": ("name", "env", "enable"),
        "reject_sentinel": True,
    },

    # ---- toolsets --------------------------------------------------------
    "/api/tools/toolsets/{name}": {
        "methods": {"PUT"},
        "upstream_path": "/api/tools/toolsets/{name}",
        "summary": "upstream.toolset.toggle",
        "body_keys_allow": ("enabled",),
    },
    "/api/tools/toolsets/{name}/model": {
        "methods": {"PUT"},
        "upstream_path": "/api/tools/toolsets/{name}/model",
        "summary": "upstream.toolset.model",
        "body_keys_allow": ("model",),
    },
    "/api/tools/toolsets/{name}/provider": {
        "methods": {"PUT"},
        "upstream_path": "/api/tools/toolsets/{name}/provider",
        "summary": "upstream.toolset.provider",
        # `capability` scopes a web-toolset selection to search or extract;
        # upstream's ToolsetProviderSelect has no base_url field.
        "body_keys_allow": ("provider", "capability"),
    },
    # Carries credential-shaped values. Upstream never returns them (the config
    # read reports `is_set` only), and the SPA sends only fields the operator
    # retyped, so an untouched masked value never round-trips.
    "/api/tools/toolsets/{name}/env": {
        "methods": {"PUT"},
        "upstream_path": "/api/tools/toolsets/{name}/env",
        "summary": "upstream.toolset.env",
        "body_keys_allow": ("env",),
        "reject_sentinel": True,
    },
    "/api/tools/toolsets/{name}/post-setup": {
        "methods": {"POST"},
        "upstream_path": "/api/tools/toolsets/{name}/post-setup",
        "summary": "upstream.toolset.post_setup",
        "body_keys_allow": ("key",),
    },
    "/api/tools/terminal/backend": {
        "methods": {"PUT"},
        "upstream_path": "/api/tools/terminal/backend",
        "summary": "upstream.toolset.terminal_backend",
        "body_keys_allow": ("backend",),
    },

    # ---- memory provider -------------------------------------------------
    "/api/memory/provider": {
        "methods": {"PUT"},
        "upstream_path": "/api/memory/provider",
        "summary": "upstream.memory.provider",
        "body_keys_allow": ("provider",),
    },
    # target: "all" | "memory" | "user"
    "/api/memory/reset": {
        "methods": {"POST"},
        "upstream_path": "/api/memory/reset",
        "summary": "upstream.memory.reset",
        "body_keys_allow": ("target",),
        "require_confirm": True,
    },
    "/api/memory/providers/{name}/config": {
        "methods": {"PUT"},
        "upstream_path": "/api/memory/providers/{name}/config",
        "summary": "upstream.memory.provider_config",
        "body_keys_allow": ("values",),
        "reject_sentinel": True,
    },
    "/api/memory/providers/{name}/setup": {
        "methods": {"POST"},
        "upstream_path": "/api/memory/providers/{name}/setup",
        "summary": "upstream.memory.provider_setup",
        "body_keys_allow": ("values",),
        "reject_sentinel": True,
    },

    # ---- webhooks --------------------------------------------------------
    "/api/webhooks": {
        "methods": {"POST"},
        "upstream_path": "/api/webhooks",
        "summary": "upstream.webhook.create",
        "body_keys_allow": (
            "name", "description", "events", "prompt", "script", "skills",
            "deliver", "deliver_only", "deliver_chat_id", "secret",
        ),
    },
    "/api/webhooks/enable": {
        "methods": {"POST"},
        "upstream_path": "/api/webhooks/enable",
        "summary": "upstream.webhook.subsystem_toggle",
        "body_keys_allow": ("enabled",),
    },
    "/api/webhooks/{name}/enabled": {
        "methods": {"PUT"},
        "upstream_path": "/api/webhooks/{name}/enabled",
        "summary": "upstream.webhook.toggle",
        "body_keys_allow": ("enabled",),
    },
    "/api/webhooks/{name}": {
        "methods": {"DELETE"},
        "upstream_path": "/api/webhooks/{name}",
        "summary": "upstream.webhook.delete",
        "require_confirm": True,
    },

    # ---- messaging platforms --------------------------------------------
    "/api/messaging/platforms/{name}": {
        "methods": {"PUT"},
        "upstream_path": "/api/messaging/platforms/{name}",
        "summary": "upstream.messaging.configure",
        # MessagingPlatformUpdate: enabled / env / clear_env. There is no
        # `config` field upstream.
        "body_keys_allow": ("enabled", "env", "clear_env"),
        "reject_sentinel": True,
    },
    "/api/messaging/platforms/{name}/test": {
        "methods": {"POST"},
        "upstream_path": "/api/messaging/platforms/{name}/test",
        "summary": "upstream.messaging.test",
    },

    # ---- gateway lifecycle ----------------------------------------------
    # Every one of these interrupts live agent work, so all four are confirm-
    # gated. Drain is the graceful option; the UI should present it first.
    "/api/gateway/drain": {
        "methods": {"POST"},
        "upstream_path": "/api/gateway/drain",
        "summary": "upstream.gateway.drain",
        "require_confirm": True,
    },
    "/api/gateway/restart": {
        "methods": {"POST"},
        "upstream_path": "/api/gateway/restart",
        "summary": "upstream.gateway.restart",
        "require_confirm": True,
    },
    "/api/gateway/start": {
        "methods": {"POST"},
        "upstream_path": "/api/gateway/start",
        "summary": "upstream.gateway.start",
        "require_confirm": True,
    },
    "/api/gateway/stop": {
        "methods": {"POST"},
        "upstream_path": "/api/gateway/stop",
        "summary": "upstream.gateway.stop",
        "require_confirm": True,
    },

    # ---- ops -------------------------------------------------------------
    # Hooks are identified by (event, command), not by a path id — delete takes
    # a body, so one spec serves both verbs.
    "/api/ops/hooks": {
        "methods": {"POST", "DELETE"},
        "upstream_path": "/api/ops/hooks",
        "summary": "upstream.ops.hook_write",
        "body_keys_allow": ("event", "command", "matcher", "timeout", "approve"),
        "require_confirm": ("DELETE",),
    },
    "/api/ops/checkpoints/prune": {
        "methods": {"POST"},
        "upstream_path": "/api/ops/checkpoints/prune",
        "summary": "upstream.ops.checkpoint_prune",
        "body_keys_allow": ("keep", "older_than_days"),
        "require_confirm": True,
    },
    "/api/ops/prompt-size": {
        "methods": {"POST"},
        "upstream_path": "/api/ops/prompt-size",
        "summary": "upstream.ops.prompt_size",
    },

    # ---- models ----------------------------------------------------------
    "/api/model/set": {
        "methods": {"POST"},
        "upstream_path": "/api/model/set",
        "summary": "upstream.model.set",
        # `scope` is required upstream ("main" | "auxiliary"); `task` names the
        # auxiliary slot. `api_key` exists upstream but stays unexposed —
        # credential writes are out of scope for this surface.
        "body_keys_allow": ("scope", "provider", "model", "task", "base_url"),
        "forward_query": ("profile",),
    },
    # Upstream's MoA document is deeply nested (presets → slots) and it warns
    # that a partial body erases hand-set values. The SPA therefore re-sends the
    # whole GET payload with one field changed, so the allowlist mirrors that
    # document rather than a flat subset.
    "/api/model/moa": {
        "methods": {"PUT"},
        "upstream_path": "/api/model/moa",
        "summary": "upstream.model.moa",
        "body_keys_allow": (
            "default_preset", "active_preset", "presets",
            "reference_models", "aggregator", "reference_temperature",
            "aggregator_temperature", "max_tokens", "reference_max_tokens",
            "fanout", "reference_timeout",
        ),
    },
}

# The verbs the mutation proxy has to accept. Derived from the specs so adding
# a spec with a new verb cannot leave the route registered without it — that
# mismatch produces a bare FastAPI 405 that never reaches the allowlist check.
UPSTREAM_MUTATION_METHODS = sorted(
    {method for spec in UPSTREAM_MUTATION_SPECS.values() for method in spec["methods"]}
)

# Writes the SPA may perform on a given read surface. Advertised in that read's
# envelope so the UI enables exactly the controls the BFF will actually
# forward, instead of guessing from the route table.
READ_PATH_MUTATIONS = {
    # Sessions were missing here while UPSTREAM_MUTATION_SPECS and
    # MUTATION_ALLOWLIST both carried real session writes, so every session read
    # advertised `read_only: true, mutations_supported: []` and the SPA had no
    # honest signal to gate rename/fork/model-lock/delete on.
    "/api/sessions": ("chat", "create", "rename", "archive", "delete", "fork",
                      "model_lock", "stop"),
    "/api/skills": ("save", "enable", "disable", "delete"),
    "/api/skills/content": ("save", "enable", "disable", "delete"),
    "/api/cron/jobs": ("create", "update", "delete", "fire", "pause", "resume"),
    "/api/dashboard/agent-plugins": ("enable", "disable"),
    # The Plugins tab reads the hub (the only list that carries every installed
    # agent plugin), so the toggle capability has to be advertised there too.
    "/api/dashboard/plugins/hub": ("enable", "disable"),
    "/api/profiles": ("create", "update", "delete", "activate", "describe_auto"),
    "/api/mcp/servers": ("create", "delete", "test", "enable", "disable"),
    "/api/mcp/catalog": ("install",),
    "/api/tools/toolsets": ("enable", "disable", "set_model", "set_provider",
                            "set_env", "post_setup"),
    "/api/tools/terminal/backends": ("set_backend",),
    "/api/memory": ("set_provider", "reset", "configure_provider", "setup_provider"),
    "/api/webhooks": ("create", "delete", "enable", "disable"),
    "/api/messaging/platforms": ("configure", "test"),
    # The dashboard exposes no GET /api/gateway/*; lifecycle controls hang off
    # the status read the Command Center already performs.
    "/api/status": ("gateway_start", "gateway_stop", "gateway_restart", "gateway_drain"),
    "/api/ops/hooks": ("create", "delete"),
    "/api/ops/checkpoints": ("prune",),
    "/api/model": ("set", "set_moa"),
    "/api/config": ("save_section",),
}

# Writable branches of Hermes' config.yaml. A leaf (`True`) means "this whole
# subtree may be written"; a dict means "recurse, and drop anything else".
#
# v1 scope is deliberately narrow: both branches were confirmed to apply to a
# running gateway with no restart. `providers.*` and every credential-shaped
# key stay read-redacted-only — they are never writable through this surface.
CONFIG_WRITE_ALLOW_TREE: dict[str, Any] = {
    "agent": {
        "disabled_toolsets": True,
    },
    # Per-thread toolsets / skills / cross-thread allowlist. The toolset policy
    # re-resolves per inbound message, so an edit takes effect on the next
    # message rather than on restart.
    #
    # BOTH locations are writable because the gateway reads both:
    # `gateway/toolset_policy.py::_topic_extra` prefers
    # platforms.telegram.extra when *that* dict contains `group_topics`, and
    # otherwise falls back to the legacy top-level telegram.extra. Allowing
    # only the typed path would let a write create a second, shadowing copy
    # while the gateway kept reading the legacy one. The UI resolves which
    # location is live and writes back to that same one.
    "platforms": {
        "telegram": {
            "extra": {
                "group_topics": True,
            },
            # Per-thread system prompt / model override, keyed by thread id.
            # Only ever read from the typed path, and deep-merged per key, so a
            # single-thread edit cannot disturb its neighbours. Unlike
            # group_topics this is bound into the running GatewayConfig, so it
            # applies after a gateway restart.
            "channel_overrides": True,
        },
    },
    "telegram": {
        "extra": {
            "group_topics": True,
        },
    },
}


def _prune_to_allow_tree(body: Any, tree: Any) -> Any:
    """Keep only the branches of `body` that `tree` marks writable."""
    if tree is True:
        return body
    if not isinstance(tree, dict) or not isinstance(body, dict):
        return None
    out: dict[str, Any] = {}
    for key, value in body.items():
        if key not in tree:
            continue
        kept = _prune_to_allow_tree(value, tree[key])
        # An empty dict here means the caller named a branch but nothing inside
        # it was writable — dropping it keeps the upstream merge a no-op.
        if kept is None or (isinstance(kept, dict) and not kept):
            continue
        out[key] = kept
    return out


# Which SSE topic a successful write announces. Longest prefix wins, so
# /api/cron/jobs/{id}/pause and /api/cron/jobs both land on cron.changed.
_MUTATION_CHANGE_TOPICS: tuple[tuple[str, str, str], ...] = (
    ("/api/cron/jobs", "cron.changed", "cron_job"),
    ("/api/dashboard/agent-plugins", "plugins.changed", "plugin"),
    ("/api/profiles", "profiles.changed", "profile"),
    ("/api/mcp/servers", "mcp.changed", "mcp_server"),
    ("/api/tools/toolsets", "toolsets.changed", "toolset"),
    ("/api/webhooks", "webhooks.changed", "webhook"),
    ("/api/messaging/platforms", "channels.changed", "platform"),
    ("/api/memory", "memory.changed", "memory_provider"),
    ("/api/gateway", "gateway.changed", "gateway"),
)


def _mutation_change_topic(path: str) -> Optional[tuple[str, str]]:
    for prefix, event_name, entity_type in _MUTATION_CHANGE_TOPICS:
        if path == prefix or path.startswith(prefix + "/"):
            return event_name, entity_type
    return None


def _describe_allow_tree(tree: Any, prefix: str = "") -> list[str]:
    """Dotted paths of a tree, for error messages and audit metadata."""
    if not isinstance(tree, dict):
        return [prefix] if prefix else []
    paths: list[str] = []
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            paths.extend(_describe_allow_tree(value, path))
        else:
            paths.append(path)
    return paths


# A path token never spans a slash, so one token may not smuggle extra path
# segments past the allowlist (".../{name}" cannot become ".../a/b").
_SPEC_TOKEN_RE = re.compile(r"\\\{([a-z_]+)\\\}")


def match_upstream_mutation(path: str, method: str) -> Optional[tuple[dict, dict[str, str]]]:
    """Return (spec, path_tokens) when the SPA mutation path is allowlisted."""
    for pattern, spec in UPSTREAM_MUTATION_SPECS.items():
        if method not in spec["methods"]:
            continue
        regex = _SPEC_TOKEN_RE.sub(
            lambda m: f"(?P<{m.group(1)}>[^/]+)", re.escape(pattern)
        )
        m = re.fullmatch(regex, path)
        if m:
            return spec, m.groupdict()
    return None


def resolve_upstream_method(spec: dict, method: str) -> str:
    """The verb to send upstream. `upstream_method_map` translates per-verb so a
    single path can rewrite PATCH→PUT while passing DELETE through unchanged."""
    mapped = spec.get("upstream_method_map")
    if mapped and method in mapped:
        return mapped[method]
    return spec.get("upstream_method") or method


# Routes that do NOT require a session. There is no login anymore (S8-ALT:
# IP allowlist + auto-session); this set is empty and kept as a constant so
# the middleware contract stays explicit.
_PUBLIC_PATHS: set[str] = set()

_MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_FRESHNESS = ("live", "fresh", "stale", "unavailable", "unsupported", "partial")

_EXTERNAL_DASHBOARD_TARGETS = {
    "llama-proxy": {
        "base_url": "http://192.168.1.140:8082",
        "index_path": "/dashboard",
    },
    "9router": {
        "base_url": "http://192.168.1.140:20128",
        "index_path": "/dashboard",
    },
}

_MEMORY_FILE_ALIASES = {
    "memory": "MEMORY.md",
    "memory.md": "MEMORY.md",
    "user": "USER.md",
    "user.md": "USER.md",
}

_UPSTREAM_PROXY_HOP_HEADERS = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "via",
    "server",
}


def is_allowed_read_path(path: str) -> bool:
    if not path.startswith("/api/"):
        return False
    for excl in _READ_EXCLUDE:
        if path == excl or path.startswith(excl + "/"):
            return False
    for prefix in READ_ALLOWLIST:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def is_allowed_adapter_path(path: str) -> bool:
    """Match concrete adapter routes; template text is never compared literally."""
    return any(pattern.fullmatch(path) for pattern in ADAPTER_ROUTE_PATTERNS)


def split_upstream_envelope(body: Any) -> tuple[Any, Optional[dict[str, Any]]]:
    """Flatten one upstream {data, meta} envelope into the BFF envelope."""
    if isinstance(body, dict) and "data" in body and isinstance(body.get("meta"), dict):
        return body.get("data"), dict(body["meta"])
    return body, None


def upstream_error_status(status: int) -> int:
    return status if 400 <= int(status or 0) < 600 else 502


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def _parse_sse_block(raw: str) -> Optional[tuple[str, Any]]:
    """Split one upstream SSE block into `(event_name, parsed_data)`.

    Returns None for a block with no `data:` field at all — that is a comment,
    which is how both ends keep an idle stream alive, and treating one as an
    event hands the client a frame that means nothing.
    """
    name = "message"
    data_lines: list[str] = []
    seen_data = False
    for line in raw.replace("\r\n", "\n").split("\n"):
        if not line or line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            name = value or "message"
        elif field == "data":
            data_lines.append(value)
            seen_data = True
    if not seen_data:
        return None
    body = "\n".join(data_lines)
    try:
        return name, json.loads(body)
    except Exception:
        return name, body


def _json_error(status: int, code: str, message: str, request_id: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": {"message": message, "code": code}}
    if request_id:
        body["request_id"] = request_id
    return JSONResponse(body, status_code=status)


class Router:
    """Owns all route handlers; created once per app instance."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        dashboard: DashboardClient,
        gateway: GatewayClient,
        adapter: AdapterClient,
        cache: Cache,
        registry: CapabilityRegistry,
        event_bus: EventBus | None = None,
        correlation_engine: CorrelationEngine | None = None,
        run_inspector: RunInspector | None = None,
        alert_engine: alerts_mod.AlertEngine | None = None,
        pulse: Pulse | None = None,
        dashboard_store: SessionPersonaStore | None = None,
        runner_manager: RunnerManager | None = None,
    ):
        self.s = settings
        self.store = store
        self.dashboard_store = dashboard_store
        self.runner_manager = runner_manager
        self.dashboard = dashboard
        self.gateway = gateway
        self.adapter = adapter
        self.cache = cache
        self.registry = registry
        self.event_bus = event_bus or EventBus(store)
        self.correlation_engine = correlation_engine or CorrelationEngine(providers={})
        self.run_inspector = run_inspector or RunInspector(
            self.correlation_engine, providers={}
        )
        self.alert_engine = alert_engine or alerts_mod.AlertEngine(store, settings)
        self.pulse = pulse or Pulse(store)
        self.router = APIRouter()
        self.allowlist: CidrList = settings.allowed_cidrs
        self.session_issue_limiter = SlidingWindowRateLimiter(
            settings.session_issue_rate_limit_per_min, 60.0
        )
        self.mutation_limiter = SlidingWindowRateLimiter(
            settings.mutation_rate_limit_per_min, 60.0
        )

    # ------------------------------------------------------------------ auth
    def _request_profile(self, request: Request) -> Optional[str]:
        return request.query_params.get("profile")

    def _session_from_request(self, request: Request) -> Optional[dict[str, Any]]:
        sid = request.cookies.get(self.s.cookie_name)
        if not sid:
            # ?token= fallback (SSE/EventSource cannot set cookies) — same
            # server-side session system, random id, never the Hermes key.
            sid = request.query_params.get("token") or ""
        if not sid:
            # First-contact fallback: the allowlist gate auto-issued a
            # session for THIS request before the cookie reached the client.
            # Handlers see it as an authenticated session; the response
            # carries the Set-Cookie so the next request uses the cookie.
            auto = getattr(request.state, "auto_session", None)
            if auto is not None:
                return auto
            return None
        row = self.store.get_session(sid)
        if not row:
            return None
        try:
            expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            expires_ts = expires.timestamp()
        except (TypeError, ValueError):
            return None
        if time.time() >= expires_ts:
            self.store.delete_session(sid)
            return None
        return row

    def _require_session(self, request: Request) -> dict[str, Any]:
        session = self._session_from_request(request)
        if session is None:
            raise ApiError(401, "unauthenticated", "valid session required")
        return session

    def _require_csrf(self, request: Request, session: dict[str, Any]) -> None:
        header = request.headers.get("X-CSRF-Token", "")
        if not header or not constant_time_equal(header, session["csrf_token"]):
            raise ApiError(403, "csrf_failed", "missing or invalid X-CSRF-Token")

    def _origin_allowed(self, request: Request) -> bool:
        origin = request.headers.get("Origin")
        if not origin:
            return True  # non-browser clients
        return origin == self.s.allowed_origin

    def _host_allowed(self, request: Request) -> bool:
        host = request.headers.get("Host", "")
        if not host:
            return False
        # Allow both the configured host:port and host-without-port (proxies often
        # strip the port on forwarded Host headers).
        return host == self.s.resolved_allowed_host or host == self.s.resolved_allowed_host.split(":", 1)[0]

    def _guard_mutation(self, request: Request) -> dict[str, Any]:
        """Full pre-upstream chain every mutation owes: session, CSRF, Origin,
        Host, per-session rate limit. Raises ApiError; returns the session."""
        session = self._require_session(request)
        self._require_csrf(request, session)
        if not self._origin_allowed(request):
            raise ApiError(403, "origin_forbidden", "Origin not allowed")
        if not self._host_allowed(request):
            raise ApiError(403, "host_forbidden", "Host not allowed")
        if not self.mutation_limiter.allow(session["id"]):
            raise ApiError(429, "rate_limited", "mutation rate limit exceeded")
        return session

    async def _execution_target_for_session(
        self, session_id: str
    ) -> SessionExecutionTarget:
        """Resolve the right GatewayClient for an existing session.

        Runner-backed sessions (execution_mode='runner') talk to their own
        profile-scoped `hermes serve --isolated` process via runner_manager;
        every other session (the default, and the entire pre-existing
        install base) is unaffected and gets the one shared gateway client
        exactly as before. Falls back to the shared gateway whenever the
        local store or runner manager isn't wired (tests, or the mode is
        unrecorded) — 'gateway' is the correct default, not an error.
        """
        if self.dashboard_store is None or self.runner_manager is None:
            return SessionExecutionTarget("gateway", None, self.gateway)
        mode = self.dashboard_store.get_execution_mode(session_id)
        if mode != "runner":
            return SessionExecutionTarget("gateway", None, self.gateway)
        profile_name = self.dashboard_store.get_persona(session_id)
        if not profile_name:
            # Recorded as runner-backed but the profile pointer is missing —
            # fail back to the shared gateway rather than erroring the turn;
            # this should not happen (set_persona always writes both), but
            # a stale/corrupt row must not break an existing chat.
            return SessionExecutionTarget("gateway", None, self.gateway)
        client = await self.runner_manager.ensure_profile_gateway(profile_name)
        self.runner_manager.touch(profile_name)
        return SessionExecutionTarget("runner", profile_name, client)

    async def _gateway_client_for_session(self, session_id: str) -> GatewayClient:
        return (await self._execution_target_for_session(session_id)).client

    async def _profile_exists(self, profile_name: str) -> bool:
        """True when profile_name is in the dashboard's current profile
        inventory. Fails closed (False) on any upstream trouble — an
        unreachable dashboard must not be treated as "any name is fine"."""
        try:
            status, body, _ = await self.dashboard.get("/api/profiles")
        except UpstreamError:
            return False
        if status >= 400:
            return False
        rows = body
        if isinstance(rows, dict):
            rows = rows.get("profiles") or rows.get("data") or rows.get("items") or []
        if not isinstance(rows, list):
            return False
        for row in rows:
            if isinstance(row, dict) and (row.get("name") or row.get("id")) == profile_name:
                return True
            if isinstance(row, str) and row == profile_name:
                return True
        return False

    def _stream_touch_callback(self, session_id: str):
        """A cheap, throttled per-frame callback for a chat SSE generator to
        call as bytes arrive, so a single turn running longer than the
        runner pool's idle timeout doesn't get its profile gateway reaped
        out from under it mid-stream (the pool only sees activity at the
        start of a turn otherwise — a long-running one goes quiet from the
        pool's point of view even while very much alive)."""
        if self.dashboard_store is None or self.runner_manager is None:
            return lambda: None
        state = {"last": 0.0}

        def _touch() -> None:
            now = time.monotonic()
            if now - state["last"] < 30.0:
                return
            state["last"] = now
            if self.dashboard_store.get_execution_mode(session_id) != "runner":
                return
            profile_name = self.dashboard_store.get_persona(session_id)
            if profile_name:
                self.runner_manager.touch(profile_name)

        return _touch

    # ------------------------------------------------------------- envelope
    def _envelope(
        self,
        data: Any,
        *,
        source_id: str,
        profile_id: Optional[str],
        freshness: str,
        request_id: str,
        fetched_at: Optional[float] = None,
        stale_after: Optional[float] = None,
        schema_fingerprint: Optional[str] = None,
        source_version: Optional[str] = None,
        degraded_reason: Optional[str] = None,
        read_only: bool = True,
        mutations_supported: Optional[list[str]] = None,
        upstream_meta: Optional[dict[str, Any]] = None,
        extra_meta: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        now = time.time()
        meta = {
            "source_id": source_id,
            "source_version": source_version,
            "schema_fingerprint": schema_fingerprint,
            "profile_id": profile_id,
            "fetched_at": fetched_at if fetched_at is not None else now,
            "stale_after": stale_after if stale_after is not None else now,
            "freshness": freshness,
            "read_only": read_only,
            "mutations_supported": mutations_supported or [],
            "degraded_reason": degraded_reason,
            "request_id": request_id,
        }
        if upstream_meta:
            meta["upstream_meta"] = upstream_meta
            if not meta["schema_fingerprint"]:
                meta["schema_fingerprint"] = upstream_meta.get("schema_fingerprint")
            if not meta["source_version"]:
                meta["source_version"] = upstream_meta.get("source_version")
        # Spec-supplied hints (restart_required, confirm_required, …). Reserved
        # keys stay owned by the envelope contract and cannot be overridden.
        for key, value in (extra_meta or {}).items():
            if key not in meta:
                meta[key] = value
        return {"data": data, "meta": meta}

    # ------------------------------------------------------------ memory files
    @staticmethod
    def _memory_filename(file_key: str) -> str | None:
        if not file_key:
            return None
        return _MEMORY_FILE_ALIASES.get(file_key.strip().lower())

    async def memory_file_read(self, request: Request, file_key: str) -> Response:
        filename = self._memory_filename(file_key)
        if not filename:
            return _json_error(404, "not_found", "unknown memory file",
                               request.state.request_id)

        profile_id = self._request_profile(request)
        rid = request.state.request_id

        try:
            status, body, _headers = await self.adapter.memory_file(filename, request_id=rid)
        except UpstreamError as e:
            return _json_error(upstream_error_status(e.status), "memory_read_failed",
                               str(e.detail or "adapter unavailable"), rid)
        if status >= 400:
            return _json_error(upstream_error_status(status), "memory_read_failed",
                               "adapter rejected memory read", rid)
        data, upstream_meta = split_upstream_envelope(body)

        return JSONResponse(
            self._envelope(
                data, source_id="adapter", profile_id=profile_id, freshness="live",
                request_id=rid, read_only=False, mutations_supported=("save",),
                upstream_meta=upstream_meta,
            )
        )

    async def memory_file_write(self, request: Request, file_key: str) -> Response:
        filename = self._memory_filename(file_key)
        if not filename:
            return _json_error(404, "not_found", "unknown memory file",
                               request.state.request_id)

        session = self._require_session(request)
        self._require_csrf(request, session)
        if not self._origin_allowed(request):
            return _json_error(403, "origin_forbidden", "Origin not allowed",
                               request.state.request_id)
        if not self._host_allowed(request):
            return _json_error(403, "host_forbidden", "Host not allowed",
                               request.state.request_id)
        if not self.mutation_limiter.allow(session["id"]):
            return _json_error(429, "rate_limited", "mutation rate limit exceeded",
                               request.state.request_id)

        rid = request.state.request_id
        profile_id = self._request_profile(request)
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("content"), str):
            return _json_error(400, "bad_request", "request body must include content",
                               rid)

        content = body["content"]
        target = f"/api/memory/{filename}"
        summary = build_request_summary(request.method, target, dict(request.query_params))

        try:
            self.store.append_audit(
                request_id=rid, actor="owner", action="hermes.memory.save", target=target,
                profile_id=profile_id, request_summary=summary,
                upstream_status=None, result="pending",
            )
        except Exception as e:  # noqa: BLE001
            return _json_error(503, "audit_failed",
                               f"audit write failed: {type(e).__name__}",
                               request_id=rid)

        try:
            status, response_body, _headers = await self.adapter.memory_file_write(
                filename, content, request_id=rid
            )
        except UpstreamError as e:
            self._record_audit_result(rid, 500, f"error:{type(e).__name__}")
            return _json_error(upstream_error_status(e.status), "memory_write_failed",
                               str(e.detail or "adapter unavailable"), request_id=rid)
        if status >= 400:
            self._record_audit_result(rid, status, "adapter_rejected")
            return _json_error(upstream_error_status(status), "memory_write_failed",
                               "adapter rejected memory write", request_id=rid)

        self._record_audit_result(rid, 200, "ok")
        data, upstream_meta = split_upstream_envelope(response_body)
        return JSONResponse(
            self._envelope(
                data, source_id="adapter", profile_id=profile_id, freshness="live",
                request_id=rid, read_only=False, mutations_supported=("save",),
                upstream_meta=upstream_meta,
            )
        )

    # ---------------------------------------------------------------- reads
    async def proxy_dashboard_read(self, request: Request, path: str) -> Response:
        normalized = "/" + path.lstrip("/")
        if not is_allowed_read_path(normalized):
            return _json_error(404, "not_found", "path not in read allowlist",
                               request.state.request_id)
        params = dict(request.query_params)
        profile = params.get("profile")
        rid = request.state.request_id
        try:
            status, body, headers = await self.dashboard.get(
                normalized, params=params or None, inbound_request_id=rid
            )
        except UpstreamError as e:
            detail = str(e.detail or "upstream error")
            return JSONResponse(
                self._envelope(
                    {"error": detail}, source_id="hermes-dashboard",
                    profile_id=profile, freshness="unavailable", request_id=rid,
                    degraded_reason=f"upstream_error:{e.status}",
                ),
                status_code=upstream_error_status(e.status),
            )
        data, upstream_meta = split_upstream_envelope(body)
        # The dashboard hands back provider credentials in the clear. Strip them
        # here, not in the browser: a client-side mask still ships the secret.
        if normalized == "/api/config" or normalized.startswith("/api/config/"):
            data = redact_mod.redact_config(data)
        if status >= 400:
            return JSONResponse(
                self._envelope(
                    data, source_id="hermes-dashboard", profile_id=profile,
                    freshness="unavailable", request_id=rid,
                    degraded_reason=f"upstream_status:{status}",
                    upstream_meta=upstream_meta,
                ),
                status_code=status,
            )
        mutations = list(READ_PATH_MUTATIONS.get(normalized, ()))
        return JSONResponse(
            self._envelope(
                data, source_id="hermes-dashboard", profile_id=profile,
                freshness="live", request_id=rid,
                schema_fingerprint=headers.get("x-schema-fingerprint"),
                source_version=headers.get("x-hermes-version") or headers.get("x-source-version"),
                read_only=not mutations,
                mutations_supported=mutations,
                upstream_meta=upstream_meta,
            )
        )

    # ------------------------------------------------- upstream proxy (SPA)
    # /api/upstream/<dashboard-path> — the S4 read-proxy surface under the
    # path the frontend actually calls (F-01). Same allowlist + sensitive
    # exclusions as the legacy /api/proxy/dashboard prefix; only the route is
    # added, never the excluded surfaces.
    async def proxy_upstream_read(self, request: Request, path: str) -> Response:
        return await self.proxy_dashboard_read(request, path)

    @staticmethod
    def _normalize_adapter_list_params(
        normalized_path: str, params: dict[str, str]
    ) -> dict[str, str]:
        # Some adapter list endpoints are expensive when unbounded. Keep Issue-tab
        # reads responsive by forcing a bounded page size when caller omitted one.
        if normalized_path == "/issues":
            raw = params.get("limit")
            if raw is None:
                params["limit"] = "25"
            else:
                try:
                    limit = max(1, min(int(raw), 100))
                    params["limit"] = str(limit)
                except ValueError:
                    params["limit"] = "25"
        return params

    # Direct /api/<dashboard-path> reads (skills, model/info, mcp/servers,
    # dashboard/plugins, messaging/platforms, files, logs, webhooks, memory,
    # profiles, config..., ops/doctor|security-audit|backup, actions/{name}/
    # status, issues, fs/list, health, status) — the same allowlisted,
    # envelope-wrapped 9119 proxy with no extra prefix. Sensitive surfaces
    # stay excluded; anything not in the allowlist is a 404.
    async def proxy_dashboard_direct(self, request: Request, path: str) -> Response:
        relative = "/" + path.lstrip("/")
        # An asset requested by an embedded external dashboard resolves against
        # that service, and its path is already relative to the service root.
        referer_service = self._proxy_service_from_referer(request.headers.get("referer"))
        if referer_service:
            return await self.proxy_external_dashboard(
                request, referer_service, relative
            )
        # Otherwise this is a dashboard read. The route strips /api/, so
        # re-attach it: the allowlist matches the real dashboard path
        # (/api/skills, /api/model/info, ...), never the bare suffix.
        return await self.proxy_dashboard_read(request, "/api" + relative)

    @staticmethod
    def _proxy_service_from_referer(referer: str | None) -> str | None:
        if not referer:
            return None
        try:
            parts = urlparse(referer).path.strip("/").split("/")
            if (
                len(parts) >= 4
                and parts[0] == "api"
                and parts[1] == "proxy"
                and parts[2] == "external"
            ):
                service = parts[3]
                return service if service in _EXTERNAL_DASHBOARD_TARGETS else None
        except Exception:  # noqa: BLE001
            return None
        return None

    # ------------------------------------------------- permit / issue decisions
    # The only two adapter writes besides memory files. They are separate
    # handlers rather than ADAPTER_ROUTE_PATTERNS entries on purpose: that
    # allowlist is GET-only and must stay that way.
    #
    # The BFF pre-validates enums so an operator gets an instant, specific
    # error; the adapter re-validates and remains the real boundary.
    PERMIT_DECISION_FIELDS = frozenset({
        "status", "approved", "executed", "approval_note", "action_plan",
        "execution_result", "result_status", "delete",
    })
    # `delete`/`reason` ride the same update path rather than a separate
    # decision route: deletion is soft (the ledger sets deleted_at/
    # deleted_reason and keeps the row — agent_notes_db.py folds it into
    # `update`, matching the one `issue_update` tool Hermes agents call), so
    # it is one more transition, not a second capability.
    ISSUE_UPDATE_FIELDS = frozenset({
        "status", "resolution", "verification", "merge_into_id", "event_type",
        "context", "severity", "delete", "reason",
    })
    ISSUE_STATUSES = ("open", "resolved", "dismissed", "merged")
    ISSUE_EVENT_TYPES = (
        "observed", "recurred", "investigation", "workaround", "recovered",
        "reproduced", "not_reproduced", "verification_failed", "resolved",
        "dismissed", "merged",
    )

    async def _adapter_decision(
        self, request: Request, *, call, entity_id: str, target: str,
        audit_action: str, allowed_fields: frozenset[str],
        validate=None, event: tuple[str, str] | None = None,
    ) -> Response:
        self._guard_mutation(request)
        rid = request.state.request_id
        profile_id = self._request_profile(request)

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json_error(400, "invalid_body", "JSON body required", rid)
        if not isinstance(body, dict) or not body:
            return _json_error(400, "invalid_body", "body must be a non-empty object", rid)
        unknown = sorted(set(body) - allowed_fields)
        if unknown:
            return _json_error(400, "unsupported_field",
                               f"unsupported fields: {', '.join(unknown)}", rid)
        if validate is not None:
            problem = validate(body)
            if problem:
                return _json_error(400, "invalid_field", problem, rid)

        # The body is passed so the audit line records the decision itself
        # ("status=approved"), not just that a decision route was hit. Only the
        # short enum fields in AUDIT_VALUE_FIELDS render their values; the
        # free-text ones (resolution, approval_note, ...) stay name-only.
        summary = build_request_summary(
            request.method, target, dict(request.query_params), body=body)
        try:
            self.store.append_audit(
                request_id=rid, actor="owner", action=audit_action, target=target,
                profile_id=profile_id, request_summary=summary,
                upstream_status=None, result="pending",
            )
        except Exception as e:  # noqa: BLE001
            return _json_error(503, "audit_failed",
                               f"audit write failed: {type(e).__name__}", rid)

        try:
            status, response_body, _ = await call(entity_id, body, request_id=rid)
        except UpstreamError as e:
            self._record_audit_result(rid, e.status, f"error:{type(e).__name__}")
            return _json_error(upstream_error_status(e.status), "decision_failed",
                               str(e.detail or "adapter unavailable"), rid)
        if status >= 400:
            self._record_audit_result(rid, status, f"adapter_rejected:{status}")
            detail = ""
            if isinstance(response_body, dict):
                detail = str(response_body.get("detail")
                             or (response_body.get("error") or {}).get("message") or "")
            return _json_error(upstream_error_status(status), "decision_rejected",
                               detail or "adapter rejected the decision", rid)

        self._record_audit_result(rid, status, "ok")
        data, upstream_meta = split_upstream_envelope(response_body)
        bus = getattr(self, "event_bus", None)
        if bus is not None and event:
            event_name, entity_type = event
            await bus.safe_publish(
                event_name, entity_type + "s", entity_type, str(entity_id),
                {"status": body.get("status")}, coverage="native",
                profile_id=profile_id,
            )
        return JSONResponse(
            self._envelope(
                data, source_id="adapter", profile_id=profile_id, freshness="live",
                request_id=rid, read_only=False, mutations_supported=["decide"],
                upstream_meta=upstream_meta,
            )
        )

    async def permit_decision(self, request: Request, permit_id: str) -> Response:
        return await self._adapter_decision(
            request, call=self.adapter.permit_decision, entity_id=permit_id,
            target=f"/permits/{permit_id}/decision",
            audit_action="adapter.permit.decide",
            allowed_fields=self.PERMIT_DECISION_FIELDS,
            event=("permit.changed", "permit"),
        )

    def _validate_issue_update(self, body: dict) -> str | None:
        # A delete short-circuits every other rule below: it does not carry a
        # status transition, it replaces the whole request.
        if body.get("delete"):
            reason = body.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                return "reason is required"
            return None
        status = body.get("status")
        if status is not None and status not in self.ISSUE_STATUSES:
            return f"status must be one of: {', '.join(self.ISSUE_STATUSES)}"
        event_type = body.get("event_type")
        if event_type is not None and event_type not in self.ISSUE_EVENT_TYPES:
            return f"event_type must be one of: {', '.join(self.ISSUE_EVENT_TYPES)}"
        # Mirrors the upstream script's own rules so the form can say why
        # before a round trip, not after.
        if status == "resolved" and not (body.get("resolution") and body.get("verification")):
            return "resolved requires both resolution and verification"
        if status == "dismissed" and not body.get("resolution"):
            return "dismissed requires a resolution explaining why it is not a defect"
        if status == "merged" and not body.get("merge_into_id"):
            return "merged requires merge_into_id"
        return None

    async def issue_update(self, request: Request, issue_id: str) -> Response:
        return await self._adapter_decision(
            request, call=self.adapter.issue_update, entity_id=issue_id,
            target=f"/issues/{issue_id}/update",
            audit_action="adapter.issue.update",
            allowed_fields=self.ISSUE_UPDATE_FIELDS,
            validate=self._validate_issue_update,
            event=("issue.changed", "issue"),
        )

    # ------------------------------------------------------------ config write
    async def config_write(self, request: Request) -> Response:
        """Sectioned write to Hermes' config.yaml.

        Deliberately not a UPSTREAM_MUTATION_SPECS entry: the writable scope is
        sub-path-specific (``agent.disabled_toolsets``), not top-level-key
        specific, so the generic body-key allowlist cannot express it. Upstream
        deep-merges ``PUT /api/config``, so sending only the touched branch is
        safe and leaves every other key alone.

        ``PUT /api/config/raw`` stays unexposed on purpose — a whole-document
        round-trip would carry redaction sentinels back over real secrets.
        """
        self._guard_mutation(request)
        rid = request.state.request_id
        profile_id = self._request_profile(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json_error(400, "invalid_body", "JSON body required", rid)
        if not isinstance(body, dict):
            return _json_error(400, "invalid_body", "body must be object", rid)

        # Defense in depth: the read path masks secrets, so a body carrying the
        # mask is a stale client echoing a value it was never shown.
        if redact_mod.contains_redacted_sentinel(body):
            return _json_error(
                400, "redacted_value_submitted",
                "body contains a redacted placeholder; re-enter the real value "
                "or omit the field", rid,
            )

        pruned = _prune_to_allow_tree(body, CONFIG_WRITE_ALLOW_TREE)
        if not pruned:
            return _json_error(
                400, "config_path_not_writable",
                "no writable config path in body; writable: "
                + ", ".join(sorted(_describe_allow_tree(CONFIG_WRITE_ALLOW_TREE))),
                rid,
            )

        # Record which writable section was touched, not its contents.
        summary = build_request_summary(
            request.method, "/api/config", dict(request.query_params),
            body_keys=sorted(_describe_allow_tree(pruned)))
        try:
            self.store.append_audit(
                request_id=rid, actor="owner", action="upstream.config.save",
                target="/api/config", profile_id=profile_id,
                request_summary=summary, upstream_status=None, result="pending",
            )
        except Exception as e:  # noqa: BLE001
            return _json_error(503, "audit_failed",
                               f"audit write failed: {type(e).__name__}", rid)

        try:
            status, resp_body, _ = await self.dashboard.request(
                "PUT", "/api/config", json_body=pruned, inbound_request_id=rid,
            )
        except UpstreamError as e:
            self._record_audit_result(rid, e.status, f"error:{e.detail}")
            return JSONResponse(
                self._envelope(
                    {"error": str(e.detail) or "upstream error"},
                    source_id="hermes-dashboard", profile_id=profile_id,
                    freshness="unavailable", request_id=rid, read_only=False,
                    degraded_reason=f"upstream_error:{e.status}",
                ),
                status_code=502,
            )

        self._record_audit_result(rid, status, "ok" if status < 400 else f"upstream:{status}")
        return JSONResponse(
            self._envelope(
                redact_mod.redact_config(resp_body), source_id="hermes-dashboard",
                profile_id=profile_id,
                freshness="live" if status < 400 else "unavailable",
                request_id=rid, read_only=False,
                degraded_reason=None if status < 400 else f"upstream_status:{status}",
                extra_meta={"written_paths": sorted(_describe_allow_tree(pruned))},
            ),
            status_code=status,
        )

    async def upstream_mutation(self, request: Request, path: str) -> Response:
        """Bounded upstream (9119) mutation proxy for the SPA's write calls.

        Same security posture as the gateway mutation path: session required,
        CSRF token verified, origin/host allowed, per-session rate limit,
        audit-before-mutation (append-only row), body-key allowlist, envelope
        response with read_only=False. No path outside UPSTREAM_MUTATION_SPECS
        is forwarded.
        """
        path = "/" + path.lstrip("/")
        matched = match_upstream_mutation(path, request.method)
        if matched is None:
            return _json_error(404, "mutation_unknown", "unknown upstream mutation",
                               request.state.request_id)
        spec, tokens = matched
        self._guard_mutation(request)
        # Destructive specs need the client to say so explicitly, so a replayed
        # or mistyped URL cannot delete a job or stop the gateway on its own.
        # `require_confirm` is either True (every verb) or the verbs it covers,
        # so an edit-and-delete path can gate only the delete.
        confirm_spec = spec.get("require_confirm")
        needs_confirm = (
            confirm_spec is True
            or (isinstance(confirm_spec, (tuple, list, set, frozenset))
                and request.method in confirm_spec)
        )
        if needs_confirm and request.query_params.get("confirm") != "true":
            return _json_error(428, "confirm_required",
                               "destructive mutation requires confirm=true",
                               request.state.request_id)

        rid = request.state.request_id
        profile_id = self._request_profile(request)
        summary = build_request_summary(request.method, path, dict(request.query_params))
        try:
            self.store.append_audit(
                request_id=rid, actor="owner", action=spec["summary"], target=path,
                profile_id=profile_id, request_summary=summary,
                upstream_status=None, result="pending",
            )
        except Exception as e:  # noqa: BLE001
            return _json_error(503, "audit_failed",
                               f"audit write failed: {type(e).__name__}",
                               request_id=rid)

        body: Any = None
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = None
        if spec.get("body_keys_allow") and isinstance(body, dict):
            allowed = set(spec["body_keys_allow"])
            body = {k: v for k, v in body.items() if k in allowed}
        # Some upstream models wrap the real fields one level down (cron's
        # {"updates": {...}}), so the allowlist has to reach inside too.
        for outer, inner_keys in (spec.get("body_nested_keys_allow") or {}).items():
            nested = body.get(outer) if isinstance(body, dict) else None
            if isinstance(nested, dict):
                inner_allowed = set(inner_keys)
                body[outer] = {k: v for k, v in nested.items() if k in inner_allowed}
        # Credential-carrying specs: a body still holding the mask we handed the
        # browser means a stale client is echoing back a value it never saw.
        # Writing it upstream would overwrite the real secret with "[redacted]".
        if spec.get("reject_sentinel") and redact_mod.contains_redacted_sentinel(body):
            self._record_audit_result(rid, 400, "rejected:redaction_sentinel")
            return _json_error(
                400, "redacted_value_submitted",
                "body contains a redacted placeholder; re-enter the real value "
                "or omit the field", rid,
            )

        upstream_path = spec["upstream_path"].format(**tokens)
        upstream_method = resolve_upstream_method(spec, request.method)
        # Profile-scoped writes need the scope on the wire; only the keys a
        # spec names are forwarded, never the caller's whole query string.
        forwarded = {
            key: request.query_params[key]
            for key in spec.get("forward_query", ())
            if key in request.query_params
        }
        try:
            status, resp_body, _ = await self.dashboard.request(
                upstream_method, upstream_path, params=forwarded or None,
                json_body=body, inbound_request_id=rid,
            )
        except UpstreamError as e:
            self._record_audit_result(rid, e.status, f"error:{e.detail}")
            return JSONResponse(
                self._envelope(
                    {"error": str(e.detail) or "upstream error"},
                    source_id="hermes-dashboard", profile_id=profile_id,
                    freshness="unavailable", request_id=rid, read_only=False,
                    degraded_reason=f"upstream_error:{e.status}",
                ),
                status_code=502,
            )

        self._record_audit_result(rid, status, "ok" if status < 400 else f"upstream:{status}")
        if status < 400:
            # The poll workers carry the real fields; these events only say
            # "changed", so an open tab refreshes now instead of at next poll.
            bus = getattr(self, "event_bus", None)
            topic = _mutation_change_topic(path)
            if bus is not None and topic:
                event_name, entity_type = topic
                await bus.safe_publish(
                    event_name, "dashboard", entity_type,
                    str(tokens.get("job_id") or tokens.get("name") or ""),
                    {"state": None}, coverage="native", profile_id=profile_id,
                )
        freshness = "live" if status < 400 else "unavailable"
        return JSONResponse(
            self._envelope(
                resp_body, source_id="hermes-dashboard", profile_id=profile_id,
                freshness=freshness, request_id=rid, read_only=False,
                degraded_reason=None if status < 400 else f"upstream_status:{status}",
                extra_meta=spec.get("response_meta") if status < 400 else None,
            ),
            status_code=status,
        )

    async def proxy_adapter_read(self, request: Request, path: str) -> Response:
        normalized = "/" + path.lstrip("/")
        if not is_allowed_adapter_path(normalized):
            return _json_error(404, "not_found", "path not in adapter allowlist",
                               request.state.request_id)
        profile = request.query_params.get("profile")
        # Management profile is provenance for adapter reads, not an assignee
        # alias. Only adapter-native filters are forwarded.
        params = self._normalize_adapter_list_params(
            normalized,
            {k: v for k, v in request.query_params.items() if k != "profile"},
        )
        rid = request.state.request_id
        try:
            status, body, _ = await self.adapter.request(
                "GET", normalized, params=params or None, inbound_request_id=rid
            )
        except UpstreamError as e:
            detail = str(e.detail or "upstream error")
            return JSONResponse(
                self._envelope(
                    {"error": detail}, source_id="adapter", profile_id=profile,
                    freshness="unavailable", request_id=rid,
                    degraded_reason=f"upstream_error:{e.status}",
                ),
                status_code=upstream_error_status(e.status),
            )
        data, upstream_meta = split_upstream_envelope(body)
        if status >= 400:
            return JSONResponse(
                self._envelope(
                    data, source_id="adapter", profile_id=profile,
                    freshness="unavailable", request_id=rid,
                    degraded_reason=f"upstream_status:{status}",
                    upstream_meta=upstream_meta,
                ),
                status_code=status,
            )
        return JSONResponse(
            self._envelope(
                data, source_id="adapter", profile_id=profile,
                freshness="live", request_id=rid, upstream_meta=upstream_meta,
            )
        )

    async def proxy_gateway_read(self, request: Request, path: str) -> Response:
        """GET /api/gateway/<path> — the gateway's own capability surface.

        Exact-match allowlist rather than a prefix one: the gateway also serves
        run dispatch and session mutation on /v1 and /api, and none of that
        belongs behind a read proxy.
        """
        normalized = "/" + path.lstrip("/")
        if normalized not in GATEWAY_READ_PATHS:
            return _json_error(404, "not_found", "path not in gateway read allowlist",
                               request.state.request_id)
        profile = request.query_params.get("profile")
        rid = request.state.request_id
        try:
            status, body, _ = await self.gateway.request(
                "GET", normalized, inbound_request_id=rid
            )
        except UpstreamError as e:
            detail = str(e.detail or "upstream error")
            return JSONResponse(
                self._envelope(
                    {"error": detail}, source_id="hermes-gateway", profile_id=profile,
                    freshness="unavailable", request_id=rid,
                    degraded_reason=f"upstream_error:{e.status}",
                ),
                status_code=upstream_error_status(e.status),
            )
        data, upstream_meta = split_upstream_envelope(body)
        return JSONResponse(
            self._envelope(
                data, source_id="hermes-gateway", profile_id=profile,
                freshness="live" if status < 400 else "unavailable",
                request_id=rid, upstream_meta=upstream_meta,
                degraded_reason=None if status < 400 else f"upstream_status:{status}",
            ),
            status_code=status,
        )

    def _strip_hop_headers(self, headers: dict[str, str]) -> dict[str, str]:
        return {
            key: value
            for key, value in headers.items()
            if key.lower() not in _UPSTREAM_PROXY_HOP_HEADERS
        }

    def _rewrite_external_dashboard_text(self, html: str, service: str) -> str:
        prefix = f"/api/proxy/external/{service}"
        escaped_prefix = re.escape(prefix.lstrip("/"))

        # Rewrite every quoted root-relative URL in one pass.  The old sequence
        # rewrote /_next first, then matched its own /api/proxy replacement in
        # the generic and /api passes, producing the proxy prefix three times.
        # Excluding both protocol-relative URLs and our own prefix makes this
        # transformation safe for HTML, inline JSON and JavaScript bundles.
        quoted_root = re.compile(
            rf"(?P<quote>[\"'`])/(?!/|{escaped_prefix}(?:/|(?=[\"'`])))"
        )
        rewritten = quoted_root.sub(
            lambda match: f"{match.group('quote')}{prefix}/", html
        )

        # CSS may use an unquoted root-relative url(/asset).  Keep the same
        # idempotency rule for that form as well.
        css_root = re.compile(
            rf"(?P<start>url\(\s*)/(?!/|{escaped_prefix}(?:/|\)))",
            flags=re.IGNORECASE,
        )
        return css_root.sub(
            lambda match: f"{match.group('start')}{prefix}/", rewritten
        )

    def _should_rewrite_external_body(self, content_type: str, path: str) -> bool:
        if not content_type:
            path_lower = path.lower()
            return any(path_lower.endswith(suf) for suf in (".js", ".mjs", ".css", ".html"))
        return any(
            token in content_type
            for token in ("text/html", "text/javascript", "application/javascript", "text/css")
        )

    def _normalize_external_path(
        self, path: str, service: str, target_spec: dict[str, str]
    ) -> str:
        normalized = path.lstrip("/")
        repeated_prefix = f"api/proxy/external/{service}/"
        while normalized.startswith(repeated_prefix):
            normalized = normalized[len(repeated_prefix):]
        if not normalized:
            normalized = target_spec["index_path"].lstrip("/")
        return f"/{normalized}"

    async def proxy_external_dashboard(self, request: Request, service: str, path: str = "") -> Response:
        target_spec = _EXTERNAL_DASHBOARD_TARGETS.get(service)
        if target_spec is None:
            return _json_error(
                404, "proxy_target_not_found",
                f"proxy target not found: {service}",
                request.state.request_id,
            )

        upstream_path = self._normalize_external_path(path, service, target_spec)
        target_url = target_spec["base_url"].rstrip("/") + upstream_path
        params = dict(request.query_params)
        rid = request.state.request_id

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.s.upstream_timeout_seconds),
                follow_redirects=True,
            ) as client:
                upstream = await client.get(
                    target_url, params=params or None, headers={"X-Request-Id": rid},
                )
        except httpx.TimeoutException:
            return _json_error(504, "upstream_timeout", "upstream timeout",
                               request.state.request_id)
        except httpx.HTTPError as exc:
            return _json_error(502, "upstream_unavailable", str(exc),
                               request.state.request_id)

        content = upstream.content
        content_type = (upstream.headers.get("content-type") or "").lower()
        headers = self._strip_hop_headers(dict(upstream.headers))
        headers.pop("content-length", None)
        headers.pop("content-encoding", None)

        if self._should_rewrite_external_body(content_type, upstream_path) and content:
            try:
                text = content.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                text = ""
            content = self._rewrite_external_dashboard_text(text, service).encode("utf-8")
            if "text/html" in content_type:
                headers["content-type"] = "text/html; charset=utf-8"
            headers["x-proxy-target"] = service

        return Response(
            content=content,
            status_code=upstream.status_code,
            headers=headers,
            media_type=content_type or None,
        )

    async def capabilities_endpoint(self, request: Request) -> Response:
        snap = self.registry.snapshot()
        return JSONResponse(
            self._envelope(
                snap, source_id="capability-registry", profile_id=None,
                freshness="live", request_id=request.state.request_id,
            )
        )

    async def capabilities_refresh(self, request: Request) -> Response:
        # Not read-adjacent: refresh fires outbound probes and writes
        # schema_fingerprints, so it carries the same guard as every mutation.
        self._guard_mutation(request)
        rid = request.state.request_id
        try:
            self.store.append_audit(
                request_id=rid, actor="owner", action="capabilities.refresh",
                target="/api/capabilities/refresh", profile_id=None,
                request_summary=build_request_summary(
                    request.method, "/api/capabilities/refresh",
                    dict(request.query_params)),
                upstream_status=None, result="pending",
            )
        except Exception as e:  # noqa: BLE001
            return _json_error(503, "audit_failed",
                               f"audit write failed: {type(e).__name__}", rid)
        await self.registry.refresh()
        self._record_audit_result(rid, 200, "ok")
        return JSONResponse(
            self._envelope(
                self.registry.snapshot(), source_id="capability-registry",
                profile_id=None, freshness="live",
                request_id=request.state.request_id,
            )
        )

    async def audit_endpoint(self, request: Request) -> Response:
        try:
            limit = int(request.query_params.get("limit", 50))
        except ValueError:
            limit = 50
        try:
            offset = int(request.query_params.get("offset", 0))
        except ValueError:
            offset = 0
        rows = self.store.list_audit(limit=limit, offset=offset)
        total = self.store.count_audit()
        return JSONResponse(
            self._envelope(
                {"items": rows, "total": total},
                source_id="control-store", profile_id=None, freshness="live",
                request_id=request.state.request_id,
            )
        )

    async def events_recent_endpoint(self, request: Request) -> Response:
        """Bounded read of the SSE replay buffer.

        The Activity tab is the operational event feed, which is a different
        thing from the Action Audit ledger: audit records what an operator asked
        this BFF to do, while this records what the upstream sources did. Only
        the live SSE stream exposed the latter, so a fresh page load had nothing
        to show until the next event fired. This serves the same bounded buffer
        the stream replays from, so history and live updates agree.
        """
        try:
            limit = int(request.query_params.get("limit", 200))
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 500))
        rows = self.store.replay_latest(limit=limit)
        return JSONResponse(
            self._envelope(
                {"events": rows, "total": self.store.event_replay_count()},
                source_id="event-bus", profile_id=None, freshness="live",
                request_id=request.state.request_id,
            )
        )

    # ------------------------------------------------------------ mutations
    async def _read_body_keys(self, request: Request) -> list[str]:
        """Extract only top-level KEY NAMES from a JSON body (never values)."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return []
        if isinstance(body, dict):
            return list(body.keys())
        return []

    async def mutation(self, request: Request, action: str, path: str,
                       upstream_path: str | None = None,
                       gateway: GatewayClient | None = None,
                       on_success: Callable[[int, Any], None] | None = None) -> Response:
        gateway = gateway or self.gateway
        spec = MUTATION_ALLOWLIST.get(action)
        if spec is None:
            return _json_error(404, "mutation_unknown", "unknown mutation",
                               request.state.request_id)
        session = self._require_session(request)
        self._require_csrf(request, session)

        # Origin/host checks on state-changing requests.
        if not self._origin_allowed(request):
            return _json_error(403, "origin_forbidden", "Origin not allowed",
                               request.state.request_id)
        if not self._host_allowed(request):
            return _json_error(403, "host_forbidden", "Host not allowed",
                               request.state.request_id)

        # Per-session mutation rate limit.
        if not self.mutation_limiter.allow(session["id"]):
            return _json_error(429, "rate_limited", "mutation rate limit exceeded",
                               request.state.request_id)

        # confirm=1 requirement (session delete).
        if spec.get("require_confirm"):
            if request.query_params.get("confirm") != spec["require_confirm"]:
                return _json_error(400, "confirm_required",
                                   "confirm=1 query param required",
                                   request.state.request_id)

        rid = request.state.request_id
        actor = "owner"
        target = path
        summary = build_request_summary(request.method, path, dict(request.query_params))
        profile_id = self._request_profile(request)

        # AUDIT FIRST — append-only row before any upstream call; on failure
        # abort with 503 and never touch upstream.
        try:
            self.store.append_audit(
                request_id=rid, actor=actor, action=spec["summary"], target=target,
                profile_id=profile_id, request_summary=summary,
                upstream_status=None, result="pending",
            )
        except Exception as e:  # noqa: BLE001
            return _json_error(503, "audit_failed",
                               f"audit write failed: {type(e).__name__}",
                               request_id=rid)

        # Read body now (body-limit middleware already enforced the cap).
        body: Any = None
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = None

        # Whitelist body keys for PATCH (only title/end_reason).
        if spec.get("body_keys_allow"):
            if isinstance(body, dict):
                allowed = set(spec["body_keys_allow"])
                body = {k: v for k, v in body.items() if k in allowed}

        idem = request.headers.get("Idempotency-Key")
        extra_headers = {"Idempotency-Key": idem} if idem else None

        # `{id}` is the LAST segment only for the original flat routes
        # (/api/sessions/{id}). Routes with a trailing verb — .../fork,
        # .../model, /v1/runs/{id}/stop — would resolve `{id}` to the verb, so
        # their wrappers hand the resolved upstream path in directly.
        if upstream_path is None:
            upstream_path = spec["route"].format(id=path.rsplit("/", 1)[-1])

        # Streaming mutation (chat) — passthrough SSE without buffering.
        if spec.get("stream"):
            return await self._stream_chat(request, spec, upstream_path, body,
                                           extra_headers, rid, profile_id, idem,
                                           gateway=gateway)

        try:
            if spec.get("cron"):
                status, resp_body, _ = await gateway.cron_fire(
                    body or {}, inbound_request_id=rid, idempotency_key=idem
                )
            else:
                status, resp_body, _ = await gateway.request(
                    spec["method"], upstream_path,
                    json_body=body, inbound_request_id=rid, extra_headers=extra_headers,
                )
        except UpstreamError as e:
            self._record_audit_result(rid, e.status, f"error:{e.detail}")
            return JSONResponse(
                self._envelope(
                    {"error": str(e.detail) or "upstream error"},
                    source_id="hermes-gateway", profile_id=profile_id,
                    freshness="unavailable", request_id=rid,
                    read_only=False, mutations_supported=sorted(MUTATION_ALLOWLIST),
                    degraded_reason=f"upstream_error:{e.status}",
                ),
                status_code=502,
            )

        self._record_audit_result(rid, status, "ok" if status < 400 else f"upstream:{status}")
        # Post-success bookkeeping the generic path can't know about (fork
        # carrying its parent's execution_mode onto the new session id).
        # Best-effort: local bookkeeping must never turn a succeeded upstream
        # mutation into an error response.
        if on_success is not None and status < 400:
            try:
                on_success(status, resp_body)
            except Exception:  # noqa: BLE001
                logger.warning("mutation on_success hook failed for %s", action,
                               exc_info=True)
        # Exact upstream status/body passthrough — never rewrite success.
        freshness = "live" if status < 400 else "unavailable"
        return JSONResponse(
            self._envelope(
                resp_body, source_id="hermes-gateway", profile_id=profile_id,
                freshness=freshness, request_id=rid, read_only=False,
                mutations_supported=sorted(MUTATION_ALLOWLIST),
                degraded_reason=None if status < 400 else f"upstream_status:{status}",
            ),
            status_code=status,
        )

    async def _stream_chat(
        self, request: Request, spec: dict, upstream_path: str, body: Any,
        extra_headers: dict | None, rid: str, profile_id: str | None, idem: str | None,
        *, gateway: GatewayClient | None = None,
    ) -> Response:
        """Proxy the gateway SSE stream chunk-by-chunk (never buffered)."""
        gateway = gateway or self.gateway
        upstream: Any = None
        try:
            upstream = await gateway.stream(
                "POST", upstream_path, json_body=body,
                inbound_request_id=rid, extra_headers=extra_headers,
            )
        except UpstreamError as e:
            self._record_audit_result(rid, e.status, f"error:{e.detail}")
            return JSONResponse(
                self._envelope(
                    {"error": str(e.detail) or "upstream error"},
                    source_id="hermes-gateway", profile_id=profile_id,
                    freshness="unavailable", request_id=rid, read_only=False,
                    mutations_supported=sorted(MUTATION_ALLOWLIST),
                    degraded_reason=f"upstream_error:{e.status}",
                ),
                status_code=502,
            )

        status = upstream.status_code
        if status >= 400:
            try:
                err_body = upstream.json()
            except Exception:  # noqa: BLE001
                err_body = {"error": "upstream error"}
            await upstream.aclose()
            self._record_audit_result(rid, status, f"upstream:{status}")
            return JSONResponse(
                self._envelope(
                    err_body, source_id="hermes-gateway", profile_id=profile_id,
                    freshness="unavailable", request_id=rid, read_only=False,
                    mutations_supported=sorted(MUTATION_ALLOWLIST),
                    degraded_reason=f"upstream_status:{status}",
                ),
                status_code=status,
            )

        self._record_audit_result(rid, status, "stream-started")
        session_id = str((body or {}).get("session_id") or "")
        bus = getattr(self, "event_bus", None)
        if bus is not None:
            # One event per turn (never per frame): marks activity on the
            # session so subscribers can react without flooding the ring.
            await bus.safe_publish(
                "session.changed", "chat", "session", session_id,
                {"event": "message_received"}, coverage="native",
                profile_id=profile_id,
            )

        async def gen():
            touch = self._stream_touch_callback(session_id) if session_id else (lambda: None)
            try:
                async for chunk in upstream.aiter_bytes():
                    touch()
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            gen(),
            status_code=status,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Request-Id": rid,
            },
        )

    def _record_audit_result(self, rid: str, status: int | None, result: str) -> None:
        """Update the pending audit row's upstream_status/result in place.

        ``status`` is None when the request failed before any upstream was
        reached (e.g. an unknown profile, or a runner that would not spawn) —
        the row still has to leave 'pending'.
        """
        try:
            self.store.complete_audit(rid, status, result)
        except Exception:  # noqa: BLE001 — non-fatal after the fact
            pass

    # --------------------------------------------------------------- auth api
    def _effective_client_ip(self, request: Request) -> str:
        """Effective client IP: direct peer unless TRUST_PROXY_HEADERS=1."""
        return resolve_client_ip(
            request.client.host if request.client else None,
            request.headers.get("X-Forwarded-For"),
            self.s.trust_proxy_headers,
        ) or "unknown"

    def auto_issue_session(self, request: Request) -> Optional[dict[str, Any]]:
        """Issue a server-side session for an allowed peer (no login step).

        Called by the allowlist gate on first contact. Per-IP rate limited,
        random 32+ hex session id, CSRF token minted alongside. Returns None
        when the per-IP session-issue rate limit is exceeded (the caller
        turns that into a 429).
        """
        client_ip = self._effective_client_ip(request)
        if not self.session_issue_limiter.allow(client_ip):
            return None
        sid = secrets.token_hex(32)  # 64 hex chars (> 32 required)
        csrf = secrets.token_hex(32)
        try:
            self.store.create_session(sid, csrf, self.s.session_ttl_seconds)
        except Exception as e:  # noqa: BLE001
            raise ApiError(500, "session_store_failed", str(e)) from e
        return {"id": sid, "csrf_token": csrf}

    def _attach_session_cookie(
        self, response: Response, session: dict[str, Any], request: Request
    ) -> None:
        """Set the HttpOnly + SameSite=Strict session cookie on a response."""
        secure = self.s.cookie_secure_requested or (
            self.s.trust_proxy_headers
            and request.headers.get("x-forwarded-proto", "").lower() == "https"
        )
        response.set_cookie(
            key=self.s.cookie_name,
            value=session["id"],
            httponly=True,
            samesite="strict",
            secure=secure,
            max_age=self.s.session_ttl_seconds,
            path="/",
        )

    async def logout(self, request: Request) -> Response:
        session = self._require_session(request)
        self.store.delete_session(session["id"])
        response = JSONResponse({"ok": True})
        response.delete_cookie(self.s.cookie_name, path="/")
        return response

    async def me(self, request: Request) -> Response:
        session = self._require_session(request)
        return JSONResponse(
            self._envelope(
                {"authenticated": True, "session_id": session["id"][:8]},
                source_id="control-store", profile_id=None, freshness="live",
                request_id=request.state.request_id,
            )
        )

    async def csrf_endpoint(self, request: Request) -> Response:
        """GET /api/csrf -> the session-bound CSRF token (F-02 fix).

        The frontend ApiClient fetches this before every mutation and sends
        it back in X-CSRF-Token; the server compares it with the token stored
        on the session row. The same token is also returned at login for
        backwards compatibility — both delivery channels validate against the
        SAME session-bound value, so a token minted on this route works for
        mutations and vice versa.
        """
        session = self._require_session(request)
        return JSONResponse(
            {"token": session["csrf_token"], "session_id": session["id"][:8]},
            headers={"Cache-Control": "no-store"},
        )

    # -------------------------------------------------------------- SSE stream
    async def sse_stream(self, request: Request) -> Response:
        session = self._session_from_request(request)
        if session is None:
            # First-contact case: the allowlist gate auto-issued a session
            # for this request but cannot set a cookie on an SSE response
            # (streaming). Use the auto-issued session so the very first
            # EventSource connection works; the client's next request (and
            # the SPA's /api/csrf fetch) carries the cookie.
            session = getattr(request.state, "auto_session", None)
        if session is None:
            return _json_error(401, "unauthenticated", "valid session required",
                               request.state.request_id)

        last_event_id = request.query_params.get("last_event_id") or (
            request.headers.get("Last-Event-ID") or None
        )
        # The session whose live turn this client wants to watch, folded into
        # THIS stream rather than given a connection of its own.
        #
        # A browser allows six HTTP/1.1 connections per host. This SPA already
        # spends one on this stream permanently; giving the chat tab a second
        # long-lived one meant two open tabs held four of the six forever, and
        # every ordinary request then queued behind them until the whole app
        # appeared frozen. One stream carries everything instead.
        watch_id = (request.query_params.get("watch") or "").strip()
        queue: "asyncio.Queue[dict]" = self.event_bus.make_queue()

        async def subscriber(ev: dict) -> None:
            await queue.put(ev)

        self.event_bus.subscribe("*", subscriber)

        # Started here rather than inside `gen()`: an async generator body does
        # not run until the response is being consumed, and a turn already in
        # flight should be picked up the moment the request arrives — not after
        # the replay backlog has been walked.
        watcher = (
            asyncio.create_task(self._pump_session_frames(watch_id, queue))
            if watch_id else None
        )

        async def gen():
            try:
                replay = await self.event_bus.replay_after(last_event_id)
                for ev in replay:
                    yield sse_frame(ev)
                yield f"retry: {self.event_bus.retry_ms}\n\n"
                from contextlib import suppress

                with suppress(asyncio.CancelledError):
                    while True:
                        try:
                            ev = await asyncio.wait_for(
                                queue.get(), timeout=self.event_bus.heartbeat_seconds
                            )
                            # Watch frames are already SSE-shaped; bus
                            # events need the envelope this channel is defined by.
                            yield ev["_frame"] if "_frame" in ev else sse_frame(ev)
                        except asyncio.TimeoutError:
                            yield sse_heartbeat()
            finally:
                self.event_bus.unsubscribe("*", subscriber)
                if watcher is not None:
                    watcher.cancel()

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                "X-Request-Id": request.state.request_id,
            },
        )

    async def _pump_session_frames(self, session_id: str, queue: "asyncio.Queue[dict]") -> None:
        """Relay one session's live turn frames into an open `/api/events/stream`.

        Re-wrapped rather than passed through raw: the browser reads this
        channel with `EventSource`, whose listeners are keyed by event name, so
        a turn frame has to arrive under a name that channel already knows.
        `chat.frame` carries the original name and payload inside, and the chat
        tab unwraps it into the same reducer a locally streamed turn uses.
        """
        while True:
            resp = None
            try:
                resp, _rid = await chat_proxy.open_session_events(self.gateway, session_id)
                buffer = ""
                async for chunk in chat_proxy.iter_forwarded_frames(resp, _rid):
                    buffer += chunk.decode("utf-8", "replace")
                    while "\n\n" in buffer:
                        raw, buffer = buffer.split("\n\n", 1)
                        frame = _parse_sse_block(raw)
                        if frame is None:
                            continue
                        name, payload = frame
                        await queue.put({"_frame": sse_frame_named("chat.frame", {
                            "session_id": session_id, "event": name, "data": payload,
                        })})
            except asyncio.CancelledError:
                raise
            except Exception:
                # The upstream watch is a best-effort overlay on this channel.
                # It must never take the channel down with it — the rest of the
                # fleet's events keep flowing while it retries.
                pass
            # `iter_forwarded_frames` closes the upstream response on its way
            # out, including when this task is cancelled, so there is nothing
            # to clean up here — only a pause before trying again.
            await asyncio.sleep(3)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                "X-Request-Id": request.state.request_id,
            },
        )

    # ------------------------------------------------------------ search / inspector
    async def search_endpoint(self, request: Request) -> Response:
        q = request.query_params.get("q", "")
        if not q:
            return _json_error(400, "query_required", "q query param required",
                               request.state.request_id)
        try:
            limit = int(request.query_params.get("limit", 20))
        except ValueError:
            limit = 20
        limit = max(1, min(limit, 100))
        result = await search_mod.federated_search(self.adapter, q, limit)
        return JSONResponse(result)

    async def inspect_task(self, request: Request, task_id: str) -> Response:
        return JSONResponse(await self.run_inspector.inspect_task(task_id))

    async def inspect_session(self, request: Request, session_id: str) -> Response:
        return JSONResponse(await self.run_inspector.inspect_session(session_id))

    # ------------------------------------------------------------ alerts / pulse
    async def alerts_endpoint(self, request: Request) -> Response:
        return JSONResponse(
            self._envelope(
                self.alert_engine.list_active(), source_id="alert-engine",
                profile_id=self._request_profile(request), freshness="live",
                request_id=request.state.request_id,
            )
        )

    async def _alert_acknowledge(
        self, request: Request, alert_id: str, action: str,
        audit_action: str, snooze_seconds: int | None = None,
    ) -> Response:
        self._guard_mutation(request)
        rid = request.state.request_id
        target = f"/api/alerts/{alert_id}"
        try:
            self.store.append_audit(
                request_id=rid, actor="owner", action=audit_action, target=target,
                profile_id=self._request_profile(request),
                request_summary=build_request_summary(
                    request.method, target, dict(request.query_params)),
                upstream_status=None, result="pending",
            )
        except Exception as e:  # noqa: BLE001
            return _json_error(503, "audit_failed",
                               f"audit write failed: {type(e).__name__}", rid)
        try:
            result = await self.alert_engine.acknowledge(
                alert_id, action, snooze_seconds=snooze_seconds
            )
        except KeyError:
            self._record_audit_result(rid, 404, "alert_not_found")
            return _json_error(404, "alert_not_found", "alert not found", rid)
        self._record_audit_result(rid, 200, "ok")
        return JSONResponse(result)

    async def alert_ack(self, request: Request, alert_id: str) -> Response:
        return await self._alert_acknowledge(
            request, alert_id, alerts_mod.ACK, "alert.ack"
        )

    async def alert_snooze(self, request: Request, alert_id: str) -> Response:
        try:
            hours = int(request.query_params.get("hours", 1))
        except ValueError:
            hours = 1
        hours = max(1, min(hours, 72))
        return await self._alert_acknowledge(
            request, alert_id, alerts_mod.SNOOZE, "alert.snooze",
            snooze_seconds=hours * 3600,
        )

    # ------------------------------------------------------------ preferences (local)
    # Shell UI state (density, nav collapse, default filters). Never leaves the
    # BFF, so there is no upstream call and no audit row — the mutation chain
    # here exists to stop a cross-origin page from rewriting the operator's UI.
    PREFERENCE_KEYS = frozenset({
        "density", "nav_collapsed", "inspector_width", "default_route",
        "table_sort", "theme",
    })

    async def preferences_read(self, request: Request) -> Response:
        return JSONResponse(
            self._envelope(
                self.store.list_preferences(self._request_profile(request)),
                source_id="local-store",
                profile_id=self._request_profile(request), freshness="live",
                request_id=request.state.request_id,
            )
        )

    async def preferences_write(self, request: Request) -> Response:
        self._guard_mutation(request)
        rid = request.state.request_id
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "invalid_body", "JSON body required", rid)
        if not isinstance(body, dict):
            return _json_error(400, "invalid_body", "body must be object", rid)
        unknown = sorted(set(body) - self.PREFERENCE_KEYS)
        if unknown:
            return _json_error(400, "unknown_preference",
                               f"unsupported keys: {', '.join(unknown)}", rid)
        profile_id = self._request_profile(request)
        for key, value in body.items():
            self.store.set_preference(key, value, profile_id)
        return JSONResponse(
            self._envelope(
                self.store.list_preferences(profile_id), source_id="local-store",
                profile_id=profile_id, freshness="live", request_id=rid,
            )
        )

    # ------------------------------------------------- session persona (local)
    # Which profile's SOUL.md a chat session was created with. The gateway keeps
    # the resolved system_prompt on the session row but not the profile name it
    # came from, so this is the one fact no upstream read can recover — and the
    # only one stored locally. Profile details, model and soul text are always
    # re-read from the dashboard. Local-only like preferences: same mutation
    # chain, no upstream call, no audit row.
    async def session_persona_read(self, request: Request, session_id: str) -> Response:
        self._require_session(request)
        name = (
            self.dashboard_store.get_persona(session_id)
            if self.dashboard_store is not None else None
        )
        return JSONResponse(
            self._envelope(
                {"profile_name": name}, source_id="local-store",
                profile_id=self._request_profile(request), freshness="live",
                request_id=request.state.request_id,
            )
        )

    async def session_persona_write(self, request: Request, session_id: str) -> Response:
        self._guard_mutation(request)
        rid = request.state.request_id
        if self.dashboard_store is None:
            return _json_error(503, "store_unavailable",
                               "dashboard store not configured", rid)
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "invalid_body", "JSON body required", rid)
        if not isinstance(body, dict):
            return _json_error(400, "invalid_body", "body must be object", rid)
        name = body.get("profile_name")
        if not isinstance(name, str) or not name.strip():
            return _json_error(400, "invalid_body",
                               "profile_name must be a non-empty string", rid)
        self.dashboard_store.set_persona(session_id, name.strip())
        return JSONResponse(
            self._envelope(
                {"profile_name": name.strip()}, source_id="local-store",
                profile_id=self._request_profile(request), freshness="live",
                request_id=rid,
            )
        )

    async def pulse_endpoint(self, request: Request) -> Response:
        window = request.query_params.get("window", "24h")
        return JSONResponse(
            self._envelope(
                self.pulse.derive(window), source_id="pulse",
                profile_id=self._request_profile(request), freshness="live",
                request_id=request.state.request_id,
            )
        )

    # ------------------------------------------------------------ chat proxy (S5)
    @staticmethod
    def _created_session_id(body: Any) -> str:
        """Session id out of the gateway's create response.

        `POST /api/sessions` answers `{"object": "hermes.session", "session":
        {"id": ...}}` — the id is NESTED. Reading only the top level (as this
        did) yielded an empty id, which is why a freshly created session
        published a bus event for "" and left the SPA opening a thread it could
        not name. The flat keys stay in the chain for older gateways.
        """
        if not isinstance(body, dict):
            return ""
        nested = body.get("session")
        if isinstance(nested, dict):
            found = nested.get("id") or nested.get("session_id") or nested.get("session_key")
            if found:
                return str(found)
        return str(body.get("id") or body.get("session_id") or body.get("session_key") or "")

    async def chat_create_session(self, request: Request) -> Response:
        session = self._require_session(request)
        self._require_csrf(request, session)
        if not self._origin_allowed(request):
            return _json_error(403, "origin_forbidden", "Origin not allowed",
                               request.state.request_id)
        if not self._host_allowed(request):
            return _json_error(403, "host_forbidden", "Host not allowed",
                               request.state.request_id)
        if not self.mutation_limiter.allow(session["id"]):
            return _json_error(429, "rate_limited", "mutation rate limit exceeded",
                               request.state.request_id)
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "invalid_body", "JSON body required",
                               request.state.request_id)
        if not isinstance(body, dict):
            return _json_error(400, "invalid_body", "body must be object",
                               request.state.request_id)
        idem = request.headers.get("Idempotency-Key")
        rid = request.state.request_id

        # A runtime `profile_name` routes this session to its own isolated,
        # profile-scoped gateway (runner_manager) instead of the shared
        # default gateway — true profile scoping (SOUL/model/credentials/
        # memory/state.db all genuinely from that profile), not the old
        # persona-copy (SOUL text borrowed into a session still running on
        # the default profile's gateway). Absent profile_name, behavior is
        # unchanged: the shared default gateway, execution_mode='gateway'.
        profile_name = body.get("profile_name")
        execution_target = SessionExecutionTarget("gateway", None, self.gateway)
        wants_runner = isinstance(profile_name, str) and bool(profile_name.strip())

        # AUDIT FIRST — before ANY upstream effect. The profile-inventory
        # lookup and the runner spawn below are both real side effects (an
        # HTTP call to the dashboard, and a `hermes serve --isolated`
        # subprocess), so the pending row has to exist before them, not just
        # before the gateway create. Same rule the generic mutation() path
        # follows; auditing after the spawn would leave a crashed request
        # with real effects and no trail.
        #
        # Defensive (S8-FIX t_7fcdab02): resolve the optional profile instead
        # of hardcoding None — the nullable schema (migration 003) is primary,
        # but no audit call site should force a NOT NULL failure.
        try:
            self.store.append_audit(
                request_id=rid, actor="owner", action="chat_session_create",
                target="/api/sessions", profile_id=self._request_profile(request),
                request_summary="POST /api/sessions", upstream_status=None,
                result="pending",
            )
        except Exception as e:  # noqa: BLE001
            return _json_error(503, "audit_failed",
                               f"audit write failed: {type(e).__name__}", rid)

        if wants_runner:
            profile_name = profile_name.strip()
            if self.runner_manager is None:
                self._record_audit_result(rid, None, "error:runner_unhealthy")
                return _json_error(503, "runner_unhealthy",
                                   "profile-scoped chat runner not configured", rid)
            # Fail closed on an unknown profile before ever touching a
            # process spawn — a garbage/injected name still cannot reach
            # argv (create_subprocess_exec never shells out), but there is
            # no reason to pay for a spawn attempt Hermes would only reject.
            if not await self._profile_exists(profile_name):
                self._record_audit_result(rid, None, "error:runner_profile_missing")
                return _json_error(400, "runner_profile_missing",
                                   f"no such profile: {profile_name!r}", rid)
            try:
                client = await self.runner_manager.ensure_profile_gateway(profile_name)
            except RunnerSpawnError as exc:
                self._record_audit_result(rid, None, f"error:{exc.code}")
                return _json_error(
                    _RUNNER_ERROR_STATUS.get(exc.code, 502), exc.code, str(exc), rid
                )
            execution_target = SessionExecutionTarget("runner", profile_name, client)

        try:
            upstream_body = dict(body)
            upstream_body.pop("profile", None)
            upstream_body.pop("profile_name", None)
            result = await chat_proxy.create_session(execution_target.client, upstream_body, idem)
        except chat_proxy.UpstreamError as exc:
            self._record_audit_result(rid, exc.status, f"error:{exc.status}")
            detail = "upstream error"
            if isinstance(exc.body, dict):
                error_field = exc.body.get("error")
                if isinstance(error_field, dict):
                    detail = (
                        error_field.get("message")
                        or error_field.get("detail")
                        or error_field.get("code")
                        or detail
                    )
                else:
                    detail = (
                        exc.body.get("detail")
                        or exc.body.get("message")
                        or exc.body.get("error")
                        or detail
                    )
            else:
                detail = exc.body if exc.body is not None else detail
            return _json_error(
                upstream_error_status(exc.status),
                "upstream_error",
                str(detail),
                rid,
            )
        self._record_audit_result(rid, result["status"], "ok")
        if result["status"] < 400:
            body_data = result["body"] if isinstance(result["body"], dict) else {}
            sid = self._created_session_id(body_data)
            if profile_name and sid and self.dashboard_store is not None:
                self.dashboard_store.set_persona(
                    sid, profile_name, execution_mode=execution_target.execution_mode
                )
                body_data = dict(body_data)
                body_data["profile_name"] = profile_name
                body_data["execution_mode"] = execution_target.execution_mode
                result = {**result, "body": body_data}
            bus = getattr(self, "event_bus", None)
            if bus is not None:
                await bus.safe_publish(
                    "session.changed", "chat", "session", str(sid),
                    {"event": "created"}, coverage="native",
                    profile_id=self._request_profile(request),
                )
        return JSONResponse(content=result["body"], status_code=result["status"])

    async def chat_sessions_alias(self, request: Request) -> Response:
        """POST /api/chat/sessions — the SPA's canonical session-create path.

        Alias of chat_create_session (the S5 /api/chat/session route): same
        auth+CSRF+rate-limit+audit-before-mutation passthrough to gateway
        POST /api/sessions. Both route names resolve; neither duplicates a
        conflicting gateway path.
        """
        return await self.chat_create_session(request)

    async def chat_stream(self, request: Request) -> Response:
        session = self._require_session(request)
        self._require_csrf(request, session)
        if not self._origin_allowed(request):
            return _json_error(403, "origin_forbidden", "Origin not allowed",
                               request.state.request_id)
        if not self._host_allowed(request):
            return _json_error(403, "host_forbidden", "Host not allowed",
                               request.state.request_id)
        if not self.mutation_limiter.allow(session["id"]):
            return _json_error(429, "rate_limited", "mutation rate limit exceeded",
                               request.state.request_id)
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "invalid_body", "JSON body required",
                               request.state.request_id)
        if not isinstance(body, dict):
            return _json_error(400, "invalid_body", "body must be object",
                               request.state.request_id)
        session_id = str(body.get("session_id") or "")
        if not session_id:
            return _json_error(400, "session_id_required", "session_id required",
                               request.state.request_id)
        rid = request.state.request_id
        try:
            attachments = chat_proxy.normalize_chat_attachments(body.get("attachments"))
        except chat_proxy.AttachmentValidationError as exc:
            return _json_error(400, exc.code, str(exc), rid)
        upstream_body = {
            k: body.get(k) for k in
            # `require_model_lock` is what makes the composer's model pick
            # actually run: without it the gateway ranks a per-request model
            # BELOW the model persisted on the session row and silently uses
            # the latter. See pure/chat-model.js#chatStreamBody.
            ("message", "system_message", "instructions", "model", "provider",
             "model_options", "require_model_lock")
            if body.get(k) is not None
        }
        if attachments:
            upstream_body["attachments"] = attachments
        self.store.append_audit(
            request_id=rid, actor="owner", action="chat_stream",
            target=f"/api/sessions/{session_id}/chat/stream",
            profile_id=self._request_profile(request),
            request_summary="POST /api/chat/stream", upstream_status=None, result="streaming",
        )

        gateway = await self._gateway_client_for_session(session_id)
        try:
            resp, request_id = await chat_proxy.stream_chat(
                gateway, session_id, upstream_body
            )
        except chat_proxy.UpstreamError as exc:
            self._record_audit_result(rid, exc.status, f"error:{exc.status}")
            async def err_gen():
                yield chat_proxy.error_frame(
                    f"upstream {exc.status}: {exc.body}", rid, exc.status
                )
            return StreamingResponse(
                err_gen(), media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        except Exception as exc:
            self._record_audit_result(rid, 502, f"error:{type(exc).__name__}")
            err_msg = f"upstream unreachable: {type(exc).__name__}"
            async def err_gen2():
                yield chat_proxy.error_frame(err_msg, rid, None)
            return StreamingResponse(
                err_gen2(), media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        async def forward():
            # Emitted before the first upstream byte so the composer can show
            # "connected, waiting on the agent" instead of an inert screen.
            yield chat_proxy.open_frame(rid, request_id)
            touch = self._stream_touch_callback(session_id)
            try:
                async for frame in chat_proxy.iter_forwarded_frames(resp, request_id):
                    touch()
                    yield frame
            except asyncio.CancelledError:
                # The client hung up (Stop button, tab closed). Let the
                # cancellation through: chat_proxy's `finally` closes the
                # upstream response, which is what interrupts the agent.
                raise
            except Exception:
                yield chat_proxy.error_frame("stream interrupted", rid, None)

        return StreamingResponse(
            forward(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Request-Id": rid,
                "X-Upstream-Request-Id": request_id,
            },
        )

    def _stream_session(self, request: Request) -> Optional[dict]:
        """Session for a streaming GET, falling back to the auto-issued one.

        Same reasoning as `sse_stream`: the allowlist gate can mint a session for
        a first-contact request but cannot set its cookie on a streaming
        response, so insisting on the cookie would make the very first watcher
        connection of a fresh browser fail for no security benefit.
        """
        session = self._session_from_request(request)
        if session is None:
            session = getattr(request.state, "auto_session", None)
        return session

    async def chat_running(self, request: Request) -> Response:
        """GET /api/chat/running — sessions with a turn in flight right now."""
        session = self._stream_session(request)
        if session is None:
            return _json_error(401, "unauthenticated", "valid session required",
                               request.state.request_id)
        rid = request.state.request_id
        try:
            rows = await chat_proxy.read_running_sessions(self.gateway)
        except chat_proxy.UpstreamError as exc:
            # A gateway that cannot answer this is not an error worth blocking
            # the UI over — "nothing is running" is the honest default, and the
            # degraded reason says why the answer is thin.
            return JSONResponse(self._envelope(
                {"running": []}, source_id="hermes-gateway",
                profile_id=self._request_profile(request), freshness="degraded",
                request_id=rid, degraded_reason=f"upstream {exc.status}",
            ))
        return JSONResponse(self._envelope(
            {"running": rows}, source_id="hermes-gateway",
            profile_id=self._request_profile(request), freshness="live",
            request_id=rid,
        ))

    # ------------------------------------------------------------ static
    def _static_index(self, request: Request, full_path: str = "") -> Response:
        # Direct static assets at root (for example /app.js, /styles.css) were
        # being treated as SPA routes and incorrectly served as index.html.
        # Resolve and serve existing files before falling back to the SPA shell.
        if full_path:
            root = self.s.resolved_frontend_dir.resolve()
            target = (root / full_path).resolve()
            if str(target).startswith(str(root)) and target.is_file():
                return FileResponse(target, headers={"Cache-Control": "no-store, max-age=0"})
        index = self.s.resolved_frontend_dir / "index.html"
        if not index.is_file():
            return _json_error(404, "frontend_missing", "frontend build not found",
                               request.state.request_id)
        return FileResponse(index, headers={"Cache-Control": "no-store, max-age=0"})

    def _static_asset(self, request: Request, file_path: str) -> Response:
        root = self.s.resolved_frontend_dir.resolve()
        target = (root / file_path).resolve()
        if not str(target).startswith(str(root)) or not target.is_file():
            return _json_error(404, "not_found", "asset not found",
                               request.state.request_id)
        return FileResponse(target, headers={"Cache-Control": "no-store, max-age=0"})

    # ------------------------------------------------------------- routing
    def build(self) -> APIRouter:
        r = self.router

        # ---- no login route (S8-ALT: IP allowlist + auto-session). The
        # SPA loads directly for allowed peers; /login is just the SPA index.

        # ---- auth-gated API ----
        r.add_api_route("/api/auth/logout", self.logout, methods=["POST"])
        r.add_api_route("/api/auth/me", self.me, methods=["GET"])
        r.add_api_route("/api/csrf", self.csrf_endpoint, methods=["GET"])
        r.add_api_route("/api/capabilities", self.capabilities_endpoint, methods=["GET"])
        r.add_api_route("/api/capabilities/refresh", self.capabilities_refresh, methods=["POST"])
        r.add_api_route("/api/audit", self.audit_endpoint, methods=["GET"])
        # Registered before the dashboard read catch-all, and ahead of
        # /api/events/stream only for readability — the paths do not overlap.
        r.add_api_route("/api/events/recent", self.events_recent_endpoint, methods=["GET"])

        # ---- mutations (allowlist, 1:1 mirror of gateway 8642 routes) ----
        r.add_api_route("/api/sessions/{session_id}/chat/stream",
                        self._mutation_chat, methods=["POST"])
        r.add_api_route("/api/sessions", self._mutation_session_create, methods=["POST"])
        r.add_api_route("/api/sessions/{session_id}",
                        self._mutation_session_patch, methods=["PATCH"])
        r.add_api_route("/api/sessions/{session_id}",
                        self._mutation_session_delete, methods=["DELETE"])
        r.add_api_route("/api/sessions/{session_id}/fork",
                        self._mutation_session_fork, methods=["POST"])
        r.add_api_route("/api/sessions/{session_id}/model",
                        self._mutation_session_model, methods=["POST"])
        r.add_api_route("/api/runs/{run_id}/stop",
                        self._mutation_run_stop, methods=["POST"])
        r.add_api_route("/api/cron/fire", self._mutation_cron_fire, methods=["POST"])

        # ---- gateway (8642) reads — agent capability surface ----
        r.add_api_route("/api/gateway/{path:path}", self.proxy_gateway_read,
                        methods=["GET"])

        # ---- read proxy (catch-all) ----
        r.add_api_route("/api/proxy/dashboard/{path:path}", self.proxy_dashboard_read,
                        methods=["GET"])
        r.add_api_route("/api/proxy/external/{service}", self.proxy_external_dashboard,
                        methods=["GET"])
        r.add_api_route("/api/proxy/external/{service}/{path:path}", self.proxy_external_dashboard,
                        methods=["GET"])
        r.add_api_route("/api/upstream/{path:path}", self.proxy_upstream_read,
                        methods=["GET"])
        r.add_api_route("/api/adapter/{path:path}", self.proxy_adapter_read, methods=["GET"])

        # ---- upstream (9119) mutation proxy — bounded SPA write surface ----
        r.add_api_route("/api/upstream/{path:path}", self.upstream_mutation,
                        methods=UPSTREAM_MUTATION_METHODS)

        # ---- local editable memory sources (local file-backed tabs) ----
        r.add_api_route("/api/memory/{file_key}", self.memory_file_read,
                        methods=["GET"])
        r.add_api_route("/api/memory/{file_key}", self.memory_file_write,
                        methods=["PUT"])

        # ---- SSE stream (real fabric, Stage 5) ----
        r.add_api_route("/api/events/stream", self.sse_stream, methods=["GET"])

        # ---- Stage 5: federated search / run inspector / alerts / pulse ----
        r.add_api_route("/api/search", self.search_endpoint, methods=["GET"])
        r.add_api_route("/api/run-inspector/task/{task_id}", self.inspect_task, methods=["GET"])
        r.add_api_route("/api/run-inspector/session/{session_id}", self.inspect_session, methods=["GET"])
        r.add_api_route("/api/alerts", self.alerts_endpoint, methods=["GET"])
        r.add_api_route("/api/alerts/{alert_id}/ack", self.alert_ack, methods=["POST"])
        r.add_api_route("/api/alerts/{alert_id}/snooze", self.alert_snooze, methods=["POST"])
        r.add_api_route("/api/pulse", self.pulse_endpoint, methods=["GET"])
        r.add_api_route("/api/preferences", self.preferences_read, methods=["GET"])
        r.add_api_route("/api/preferences", self.preferences_write, methods=["PUT"])
        r.add_api_route("/api/config", self.config_write, methods=["PUT"])
        # Before the dashboard catch-all: /api/sessions/... otherwise proxies
        # to 9119, which has no persona route and would 404.
        r.add_api_route("/api/sessions/{session_id}/persona",
                        self.session_persona_read, methods=["GET"])
        r.add_api_route("/api/sessions/{session_id}/persona",
                        self.session_persona_write, methods=["POST"])
        r.add_api_route("/api/permits/{permit_id}/decision", self.permit_decision,
                        methods=["POST"])
        r.add_api_route("/api/issues/{issue_id}/update", self.issue_update,
                        methods=["POST"])

        # ---- Stage 5: chat proxy (session create + stream) ----
        r.add_api_route("/api/chat/session", self.chat_create_session, methods=["POST"])
        # SPA canonical path (F-01): /api/chat/sessions == session-create.
        r.add_api_route("/api/chat/sessions", self.chat_sessions_alias, methods=["POST"])
        r.add_api_route("/api/chat/stream", self.chat_stream, methods=["POST"])
        # Read-only live view of a turn someone else started. Before the
        # dashboard catch-all below, which would otherwise treat these as 9119
        # paths and 404 them.
        r.add_api_route("/api/chat/running", self.chat_running, methods=["GET"])

        # ---- direct /api/<dashboard-path> reads (SPA per-tab surface) ----
        # Registered after every explicit BFF route so the specific handlers
        # (search/inspector/alerts/pulse/chat/events/...) always win; this
        # catch-all only serves allowlisted 9119 dashboard reads.
        r.add_api_route("/api/{path:path}", self.proxy_dashboard_direct, methods=["GET"])

        # ---- static (catch-all after API) ----
        r.add_api_route("/assets/{file_path:path}", self._static_asset, methods=["GET"])
        r.add_api_route("/{full_path:path}", self._static_index, methods=["GET"])
        return r

    # Mutation route wrappers (thin: fix the path, delegate to generic handler)
    async def _mutation_chat(self, request: Request, session_id: str) -> Response:
        # Explicit upstream path: the generic `{id}` substitution would have
        # resolved to the trailing "stream" segment.
        path = f"/api/sessions/{session_id}/chat/stream"
        gateway = await self._gateway_client_for_session(session_id)
        return await self.mutation(request, "chat_send", path, upstream_path=path,
                                   gateway=gateway)

    async def _mutation_session_fork(self, request: Request, session_id: str) -> Response:
        path = f"/api/sessions/{session_id}/fork"
        gateway = await self._gateway_client_for_session(session_id)
        return await self.mutation(request, "session_fork", path, upstream_path=path,
                                   gateway=gateway,
                                   on_success=self._fork_persona_writer(session_id))

    def _fork_persona_writer(self, source_session_id: str):
        """Carry a session's profile + execution mode onto its fork.

        A fork of a runner-backed session is itself runner-backed: it lives
        in the same profile's isolated `hermes serve` process, since that is
        the gateway the fork request was proxied to. Without recording it,
        get_execution_mode() would default the new id to 'gateway' and the
        fork's very first turn would be routed to the shared default gateway
        — wrong process, wrong profile, no error.
        """
        if self.dashboard_store is None:
            return None
        mode = self.dashboard_store.get_execution_mode(source_session_id)
        profile_name = self.dashboard_store.get_persona(source_session_id)
        if mode != "runner" or not profile_name:
            # Nothing profile-scoped to inherit: leave the fork on the
            # default 'gateway' path, exactly as before this feature.
            return None

        def _write(_status: int, resp_body: Any) -> None:
            new_sid = self._created_session_id(resp_body)
            if new_sid:
                self.dashboard_store.set_persona(
                    new_sid, profile_name, execution_mode="runner"
                )

        return _write

    async def _mutation_session_model(self, request: Request, session_id: str) -> Response:
        path = f"/api/sessions/{session_id}/model"
        gateway = await self._gateway_client_for_session(session_id)
        return await self.mutation(request, "session_model_lock", path, upstream_path=path,
                                   gateway=gateway)

    async def _mutation_run_stop(self, request: Request, run_id: str) -> Response:
        # Not resolved to a runner-backed client: run_id is a gateway run id,
        # not a session id, and there is no existing lookup from one to the
        # other anywhere in this codebase. This route already 404s until the
        # gateway registers the run (see MUTATION_ALLOWLIST comment) and the
        # SPA falls back to aborting the stream either way, so it stays on
        # the shared default gateway rather than guessing a mapping.
        path = f"/v1/runs/{run_id}/stop"
        return await self.mutation(request, "run_stop", path, upstream_path=path)

    async def _mutation_session_create(self, request: Request) -> Response:
        return await self.mutation(request, "session_create", "/api/sessions")

    async def _mutation_session_patch(self, request: Request, session_id: str) -> Response:
        gateway = await self._gateway_client_for_session(session_id)
        return await self.mutation(request, "session_patch", f"/api/sessions/{session_id}",
                                   gateway=gateway)

    async def _mutation_session_delete(self, request: Request, session_id: str) -> Response:
        gateway = await self._gateway_client_for_session(session_id)
        return await self.mutation(request, "session_delete", f"/api/sessions/{session_id}",
                                   gateway=gateway)

    async def _mutation_cron_fire(self, request: Request) -> Response:
        return await self.mutation(request, "cron_fire", "/api/cron/fire")
