"""Server-side secret redaction for config payloads.

The dashboard's /api/config returns provider credentials in plaintext. The BFF
must strip them before the browser ever sees them; a client-side mask is not a
control, the secret has already crossed the wire by then.

REDACTED_KEY_PATTERN is duplicated in frontend/dist/tabs/settings.js
(REDACTED_KEYS). tests/test_runtime_contracts.py asserts the two stay
byte-identical, so drift fails the suite instead of silently reopening the leak.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED_KEY_PATTERN = r"(api[_-]?key|secret|token|password|credential|private[_-]?key)"
REDACTED_SENTINEL = "[redacted]"

_KEY_RE = re.compile(REDACTED_KEY_PATTERN, re.IGNORECASE)


def redact_config(value: Any) -> Any:
    """Recursively replace secret-shaped values with the sentinel."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _KEY_RE.search(key):
                out[key] = REDACTED_SENTINEL
            else:
                out[key] = redact_config(item)
        return out
    if isinstance(value, list):
        return [redact_config(item) for item in value]
    return value


def contains_redacted_sentinel(value: Any) -> bool:
    """True when a payload carries the sentinel, i.e. a masked read echoed back.

    Writing that back upstream would overwrite a real credential with the
    literal string "[redacted]", so a write carrying it must be refused.
    """
    if isinstance(value, dict):
        return any(contains_redacted_sentinel(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_redacted_sentinel(item) for item in value)
    return value == REDACTED_SENTINEL
