"""Uvicorn entrypoint for agent-mission-control.

Run (dev):   uvicorn main:app --host 127.0.0.1 --port 51763
Run (Pi):    uvicorn main:app --host 0.0.0.0 --port 51763  (requires ALLOWED_CIDRS)
"""

from __future__ import annotations

import logging
import os
import re

from .app import create_app, refuse_start_if_needed
from .config import Settings

app = create_app()
settings: Settings = app.state.settings

# ---------------------------------------------------------------------------
# Access-log redaction: never log Authorization / X-Hermes-Session-Token /
# Set-Cookie / cookie values. Installed as a filter on the uvicorn access
# logger so it applies regardless of how uvicorn was launched.
# ---------------------------------------------------------------------------
_REDACT_PATTERNS = [
    (re.compile(r"(?i)(authorization|x-hermes-session-token|x-csrf-token|x-api-key)"
                r"(\s*[:=]\s*)(?:bearer\s+)?([^\s\"']+)", re.I),
     r"\1\2<redacted>"),
    (re.compile(r"(?i)(set-cookie|cookie)(\s*[:=]\s*)([^\s\"']+)", re.I),
     r"\1\2<redacted>"),
    # Inline Bearer tokens (defense-in-depth: not a normal uvicorn access-log
    # shape, but scrub them wherever they appear).
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
     r"Bearer <redacted>"),
    # Secret query params in logged URLs (?token=, ?key=, ?password=, ...) —
    # added with the S8 merge (SSE ?token= fallback would otherwise leak the
    # full session id into the uvicorn access log).
    (re.compile(r"(?i)([?&](?:token|key|password|secret|code|auth|api[_-]?key)=)"
                r"([^&\s\"']+)", re.I),
     r"\1<redacted>"),
]


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # uvicorn.access records carry a 5-tuple args
            # (client_addr, method, full_path, http_version, status_code).
            # Redact inside full_path (index 2) so uvicorn's access formatter
            # keeps its expected shape; also scrub any inline header shapes.
            args = list(record.args) if isinstance(record.args, tuple) else None
            if args and len(args) >= 3 and isinstance(args[2], str):
                redacted = _redact(args[2])
                if redacted != args[2]:
                    args[2] = redacted
                    record.args = tuple(args)
                    record.msg = record.msg  # keep original format string
            else:
                msg = record.getMessage()
                redacted = _redact(msg)
                if redacted != msg:
                    record.msg = redacted
                    record.args = ()
        except Exception:  # noqa: BLE001 — never break logging
            pass
        return True


def _redact(text: str) -> str:
    out = text
    for pat, repl in _REDACT_PATTERNS:
        out = pat.sub(repl, out)
    return out


def install_redaction() -> None:
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn"):
        logger = logging.getLogger(name)
        logger.addFilter(RedactionFilter())


install_redaction()

# Fail-closed startup guard: refuse to bind 0.0.0.0 without ALLOWED_CIDRS.
if refuse_start_if_needed(settings):
    raise SystemExit(1)
