"""Security helpers: constant-time compare, redaction, rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from collections import defaultdict, deque
from typing import Any, Optional

# Header values that must never be logged or stored.
_SECRET_HEADERS = (
    "authorization",
    "x-hermes-session-token",
    "x-csrf-token",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-api-key",
    "idempotency-key",
)

_SECRET_QUERY_PARAMS = ("token", "key", "password", "secret", "code", "auth")


def constant_time_equal(a: str, b: str) -> bool:
    """Constant-time string comparison (length-safe for empty inputs)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _redact_header_value(value: str) -> str:
    """Turn 'Bearer <token>' / '<token>' into a redacted marker."""
    v = value.strip()
    if v.lower().startswith("bearer "):
        return "Bearer <redacted>"
    if v.lower().startswith("basic "):
        return "Basic <redacted>"
    if v and v != "<redacted>":
        return "<redacted>"
    return v


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Copy of headers with secret-bearing values redacted."""
    out = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in _SECRET_HEADERS:
            out[k] = _redact_header_value(v)
        else:
            out[k] = v
    return out


def redact_text(text: str) -> str:
    """Scrub common secret shapes from a free-text blob (logs, summaries)."""
    # Header-with-value (incl. 'Authorization: Bearer <tok>' as one unit)
    text = re.sub(
        r"(?i)(authorization|set-cookie|x-hermes-session-token|x-csrf-token|x-api-key)"
        r"(\s*[:=]\s*)(?:bearer\s+)?[A-Za-z0-9._~+/=-]+",
        r"\1\2<redacted>",
        text,
    )
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer <redacted>", text)
    text = re.sub(r"(?i)\b(password|passwd|api[_-]?key|secret|token)\s*[:=]\s*\S+",
                  r"\1=<redacted>", text)
    return text


def redact_query_params(params: dict[str, str]) -> dict[str, str]:
    out = {}
    for k, v in params.items():
        out[k] = "<redacted>" if k.lower() in _SECRET_QUERY_PARAMS else v
    return out


# Short, closed-vocabulary decision fields whose VALUES are safe to audit — an
# audit line that says "permit approved" is worth far more than one that says
# only "permit touched". Everything not listed stays key-name-only, and even
# these are length-capped so a caller cannot smuggle free text through one.
AUDIT_VALUE_FIELDS = frozenset({
    "status", "approved", "executed", "event_type", "enabled", "state",
    "severity", "confirm", "action",
})
_AUDIT_VALUE_MAX = 32


def _audit_value(value: Any) -> Optional[str]:
    """Render an allowlisted field's value, or None if it is not loggable."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text) > _AUDIT_VALUE_MAX or any(c in text for c in "\r\n"):
            return None
        return text
    return None


def build_request_summary(
    method: str,
    path: str,
    query: dict[str, str] | None,
    body_keys: Optional[list[str]] = None,
    body: Optional[dict] = None,
) -> str:
    """Audit summary built from method/path/whitelisted param NAMES only.

    Never includes header values, tokens, passwords, or message content.
    Body content is limited to key names, plus the values of the short
    closed-vocabulary fields in ``AUDIT_VALUE_FIELDS``; free-text fields such as
    ``resolution`` or ``approval_note`` contribute their name and nothing more.
    """
    q = redact_query_params(query or {})
    qs = "&".join(f"{k}={v}" for k, v in sorted(q.items())) if q else ""
    parts = [method, path]
    if qs:
        parts.append("?" + qs)
    keys = set(body_keys or ())
    if isinstance(body, dict):
        keys |= {str(k) for k in body}
    if keys:
        parts.append("body_keys=" + ",".join(sorted(keys)))
    if isinstance(body, dict):
        decided = [
            f"{k}={rendered}"
            for k in sorted(body)
            if k in AUDIT_VALUE_FIELDS and (rendered := _audit_value(body[k])) is not None
        ]
        if decided:
            parts.append(" ".join(decided))
    return " ".join(parts)


class SlidingWindowRateLimiter:
    """Per-key sliding-window rate limiter (thread-safe, memory only)."""

    def __init__(self, limit: int, window_seconds: float):
        self._limit = limit
        self._window = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self._window:
                q.popleft()
            if len(q) >= self._limit:
                return False
            q.append(now)
            return True

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)
