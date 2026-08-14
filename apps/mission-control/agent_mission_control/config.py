"""Configuration for agent-mission-control.

Every value is read from the environment (or an env-backed file for the
9119 dashboard password). No secrets live in source. Values are resolved
once per ``Settings`` instance; tests construct ``Settings(overrides=...)``
directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .ip_utils import CidrList

# Placeholder guard values rejected as "not configured" (mirrors the
# has_usable_secret philosophy from the Hermes gateway startup guard).
_PLACEHOLDERS = {
    "changeme", "your_api_key", "your_api_key_here", "your-api-key",
    "placeholder", "example", "dummy", "null", "none", "***", "**", "*",
}


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip()
    return v if v else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _read_password_file(path: str | None) -> str | None:
    if not path:
        return None
    try:
        content = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return content if content else None


def usable_secret(value: str | None, min_length: int = 1) -> bool:
    """True when the value is non-empty and not a known placeholder."""
    if not value:
        return False
    value = value.strip()
    if len(value) < min_length:
        return False
    return value.lower() not in _PLACEHOLDERS


@dataclass
class Settings:
    # --- auth / sessions (IP allowlist model, D-004) ---
    allowed_cidrs_csv: str | None = None  # "192.168.0.0/24,100.64.0.0/10"
    trust_proxy_headers: bool = False
    session_ttl_seconds: int = 12 * 3600
    cookie_secure: bool = False
    cookie_name: str = "amc_session"

    # --- upstream clients ---
    dashboard_url: str = "http://127.0.0.1:9119"
    dashboard_basic_auth_password: str | None = None
    adapter_url: str = "http://192.168.1.128:8643"
    adapter_token: str | None = None
    gateway_url: str = "http://127.0.0.1:8642"
    gateway_token: str | None = None
    nas_jwt_secret: str | None = None  # required for POST /api/cron/fire

    # --- profile-scoped chat runner pool (runner_manager.py) ---
    # Executable spawned as `<runner_hermes_executable> --profile X serve
    # --isolated --host 127.0.0.1 --port 0` per profile with an active
    # runner-backed chat session. Must resolve on PATH or be an absolute path.
    runner_hermes_executable: str = "hermes"
    runner_pool_max: int = 3
    runner_pool_idle_seconds: float = 900.0
    runner_pool_keepalive_fresh_seconds: float = 90.0
    runner_port_announce_timeout_seconds: float = 90.0

    # --- request security ---
    allowed_origin: str = "http://192.168.1.9:51763"
    allowed_host: str | None = None  # derived from allowed_origin when unset
    body_limit_bytes: int = 1024 * 1024
    session_issue_rate_limit_per_min: int = 5
    mutation_rate_limit_per_min: int = 30

    # --- cache / registry ---
    cache_ttl_seconds: int = 60
    cache_max_concurrency: int = 4
    upstream_timeout_seconds: float = 10.0
    # Gap allowed between SSE frames on a chat stream. A turn that reasons or
    # runs tools can be silent for minutes, so this is far larger than the
    # request timeout used for ordinary reads.
    chat_stream_read_timeout_seconds: float = 300.0
    sse_heartbeat_seconds: float = 15.0

    # --- event fabric (Stage 5) ---
    event_ring_buffer_size: int = 500
    event_db_replay_limit: int = 2000
    sse_retry_ms: int = 5000

    # --- source workers (Stage 5) polling cadences ---
    poll_kanban_seconds: int = 10
    poll_permits_seconds: int = 20
    poll_issues_seconds: int = 20
    poll_cron_seconds: int = 45
    poll_health_seconds: int = 15
    poll_sessions_seconds: int = 15
    # Drives the "a turn is running now" indicator, so it is deliberately the
    # fastest poller here: the state it reports lasts seconds, not minutes.
    poll_running_seconds: int = 5
    poll_adapter_health_seconds: int = 30
    poll_analytics_seconds: int = 120
    poll_backoff_max_seconds: int = 300
    alerts_tick_seconds: int = 30

    # --- alert rule thresholds (deterministic, configurable per rule) ---
    alert_stale_seconds: int = 300
    alert_heartbeat_stale_seconds: int = 300
    alert_permit_expiry_hours: int = 24
    alert_permit_pending_days: int = 7
    alert_mutation_fail_window_seconds: int = 600
    alert_mutation_fail_min: int = 3
    # R10 fires only when a threshold is configured; 0 disables the rule
    # rather than guessing what "a lot of tokens" means for this deployment.
    alert_token_threshold: int = 0

    # --- run inspector (Stage 5) ---
    inspector_timeline_limit: int = 200

    # --- runtime ---
    bind_host: str = "127.0.0.1"
    frontend_dir: str = "../frontend/dist"
    store_path: str = "agentos-dashboard.db"
    # Separate file from store_path: the control store is frozen at its 8
    # permitted tables, so dashboard state Hermes cannot hold lives here.
    dashboard_store_path: str = "store.db"

    # --- internal knobs (tests) ---
    overrides: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_env(cls, overrides: dict | None = None) -> "Settings":
        bind_host = _env("BIND_HOST", "127.0.0.1") or "127.0.0.1"
        allowed_cidrs_csv = _env("ALLOWED_CIDRS")
        if bind_host in {"127.0.0.1", "::1", "localhost"} and not allowed_cidrs_csv:
            # Local-loopback bind without explicit allowlist should still work
            # for dev/test usage on localhost.
            allowed_cidrs_csv = "127.0.0.1/32,::1/128"
        s = cls(
            allowed_cidrs_csv=allowed_cidrs_csv,
            trust_proxy_headers=_env_bool("TRUST_PROXY_HEADERS", False),
            session_ttl_seconds=_env_int("SESSION_TTL_HOURS", 12) * 3600,
            cookie_secure=_env_bool("COOKIE_SECURE", False),
            dashboard_url=_env("HERMES_DASHBOARD_URL", "http://127.0.0.1:9119") or "http://127.0.0.1:9119",
            dashboard_basic_auth_password=(
                _env("DASHBOARD_BASIC_AUTH_PASSWORD")
                or _read_password_file(_env("DASHBOARD_BASIC_AUTH_PASSWORD_FILE"))
            ),
            adapter_url=_env("ADAPTER_URL", "http://192.168.1.128:8643") or "http://192.168.1.128:8643",
            adapter_token=_env("ADAPTER_TOKEN"),
            gateway_url=_env("HERMES_GATEWAY_URL", "http://127.0.0.1:8642") or "http://127.0.0.1:8642",
            gateway_token=(
                _env("HERMES_GATEWAY_API_KEY")
                or _env("API_SERVER_KEY")
            ),
            nas_jwt_secret=_env("NAS_JWT_SECRET"),
            runner_hermes_executable=_env("RUNNER_HERMES_EXECUTABLE", "hermes") or "hermes",
            runner_pool_max=_env_int("RUNNER_POOL_MAX", 3),
            runner_pool_idle_seconds=float(_env("RUNNER_POOL_IDLE_SECONDS", "900") or "900"),
            runner_pool_keepalive_fresh_seconds=float(
                _env("RUNNER_POOL_KEEPALIVE_FRESH_SECONDS", "90") or "90"
            ),
            runner_port_announce_timeout_seconds=float(
                _env("RUNNER_PORT_ANNOUNCE_TIMEOUT_SECONDS", "90") or "90"
            ),
            allowed_origin=_env("ALLOWED_ORIGIN", "http://192.168.1.9:51763") or "http://192.168.1.9:51763",
            allowed_host=_env("ALLOWED_HOST"),
            body_limit_bytes=_env_int("BODY_LIMIT_BYTES", 1024 * 1024),
            session_issue_rate_limit_per_min=_env_int("SESSION_ISSUE_RATE_LIMIT_PER_MIN", 5),
            mutation_rate_limit_per_min=_env_int("MUTATION_RATE_LIMIT_PER_MIN", 30),
            cache_ttl_seconds=_env_int("CACHE_TTL_SECONDS", 60),
            cache_max_concurrency=_env_int("CACHE_MAX_CONCURRENCY", 4),
            upstream_timeout_seconds=float(_env("UPSTREAM_TIMEOUT_SECONDS", "10") or "10"),
            chat_stream_read_timeout_seconds=float(
                _env("CHAT_STREAM_READ_TIMEOUT_SECONDS", "300") or "300"
            ),
            sse_heartbeat_seconds=float(_env("SSE_HEARTBEAT_SECONDS", "15") or "15"),
            event_ring_buffer_size=_env_int("EVENT_RING_BUFFER_SIZE", 500),
            event_db_replay_limit=_env_int("EVENT_DB_REPLAY_LIMIT", 2000),
            sse_retry_ms=_env_int("SSE_RETRY_MS", 5000),
            poll_kanban_seconds=_env_int("POLL_KANBAN_SECONDS", 10),
            poll_analytics_seconds=_env_int("POLL_ANALYTICS_SECONDS", 120),
            poll_permits_seconds=_env_int("POLL_PERMITS_SECONDS", 20),
            poll_issues_seconds=_env_int("POLL_ISSUES_SECONDS", 20),
            poll_cron_seconds=_env_int("POLL_CRON_SECONDS", 45),
            poll_health_seconds=_env_int("POLL_HEALTH_SECONDS", 15),
            poll_sessions_seconds=_env_int("POLL_SESSIONS_SECONDS", 15),
            poll_running_seconds=_env_int("POLL_RUNNING_SECONDS", 5),
            poll_adapter_health_seconds=_env_int("POLL_ADAPTER_HEALTH_SECONDS", 30),
            poll_backoff_max_seconds=_env_int("POLL_BACKOFF_MAX_SECONDS", 300),
            alerts_tick_seconds=_env_int("ALERTS_TICK_SECONDS", 30),
            alert_stale_seconds=_env_int("ALERT_STALE_SECONDS", 300),
            alert_heartbeat_stale_seconds=_env_int("ALERT_HEARTBEAT_STALE_SECONDS", 300),
            alert_permit_expiry_hours=_env_int("ALERT_PERMIT_EXPIRY_HOURS", 24),
            alert_permit_pending_days=_env_int("ALERT_PERMIT_PENDING_DAYS", 7),
            alert_mutation_fail_window_seconds=_env_int("ALERT_MUTATION_FAIL_WINDOW_SECONDS", 600),
            alert_mutation_fail_min=_env_int("ALERT_MUTATION_FAIL_MIN", 3),
            alert_token_threshold=_env_int("ALERT_TOKEN_THRESHOLD", 0),
            inspector_timeline_limit=_env_int("INSPECTOR_TIMELINE_LIMIT", 200),
            bind_host=bind_host,
            frontend_dir=_env("FRONTEND_DIR", "../frontend/dist") or "../frontend/dist",
            store_path=_env("STORE_PATH", "agentos-dashboard.db") or "agentos-dashboard.db",
            dashboard_store_path=_env("DASHBOARD_STORE_PATH", "store.db") or "store.db",
        )
        if overrides:
            for k, v in overrides.items():
                setattr(s, k, v)
        return s

    @property
    def allowed_cidrs(self) -> "CidrList":
        return CidrList.parse(self.allowed_cidrs_csv)

    @property
    def resolved_allowed_host(self) -> str:
        if self.allowed_host:
            return self.allowed_host
        origin = self.allowed_origin
        # strip scheme; keep host[:port]
        if "://" in origin:
            origin = origin.split("://", 1)[1]
        return origin.rstrip("/")

    @property
    def resolved_frontend_dir(self) -> Path:
        return Path(self.frontend_dir)

    @property
    def cookie_secure_requested(self) -> bool:
        return self.cookie_secure


def should_refuse_start(bind_host: str, allowlist_configured: bool) -> bool:
    """Fail-closed startup guard.

    Refuse to start when binding 0.0.0.0 without an IP allowlist
    (ALLOWED_CIDRS). Loopback-only development instances may run without
    one. Mirrors the pre-S8-ALT OWNER_PASSWORD guard.
    """
    if bind_host in {"0.0.0.0", "::"}:
        return not allowlist_configured
    return False
