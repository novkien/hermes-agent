"""Permit and issue decision writes.

Hermes exposes no REST route for either: both are agent tool-calls that shell
out to CLI scripts under ``~/.hermes/scripts``. Rather than reimplement the
schema (and drift from it), this module drives the same two scripts the agent
itself uses, so the dashboard and the agent can never disagree about what a
decision means.

Safety rules, all load-bearing:
  * argv is always a fixed list and ``shell=False`` — no string interpolation
    ever reaches a shell;
  * every flag name is hardcoded here, so a caller cannot introduce one;
  * calls are time-bounded and their exit status is mapped to real HTTP codes;
  * every invocation is logged independently of the caller's own audit trail.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("agent_mission_control.data_backend.decisions")

CALL_TIMEOUT_SECONDS = 15
MAX_FIELD_CHARS = 8000

# permit field -> CLI flag. The set is closed: anything else is rejected rather
# than forwarded, so widening the write surface is a deliberate edit here.
PERMIT_FIELD_FLAGS: dict[str, str] = {
    "status": "--status",
    "approved": "--approved",
    "executed": "--executed",
    "approval_note": "--approval-note",
    "action_plan": "--action-plan",
    "execution_result": "--execution-result",
    "result_status": "--result-status",
}

# Mirrors permits_db.py's own truthiness check for the approved column.
PERMIT_TRUTHY = ("ok", "true", "yes", "1", "x", "checked")

ISSUE_STATUSES = ("open", "resolved", "dismissed", "merged")
ISSUE_EVENT_TYPES = (
    "observed",
    "recurred",
    "investigation",
    "workaround",
    "recovered",
    "reproduced",
    "not_reproduced",
    "verification_failed",
    "resolved",
    "dismissed",
    "merged",
)
ISSUE_UPDATE_FIELDS = (
    "status",
    "resolution",
    "verification",
    "merge_into_id",
    "event_type",
    "context",
    "severity",
    "delete",
    "reason",
)


class DecisionError(Exception):
    """A decision could not be applied. `status` is the HTTP code to return."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _script_path(scripts_dir: str | Path, filename: str) -> Path:
    root = Path(scripts_dir).expanduser().resolve()
    candidate = root / filename
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise DecisionError(503, f"decision script not installed: {filename}") from None
    if resolved.parent != root:
        raise DecisionError(
            503, f"decision script escapes configured directory: {filename}"
        )
    return resolved


def _text(value: Any, field: str) -> str:
    if not isinstance(value, (str, int, float)):
        raise DecisionError(400, f"{field} must be a string")
    text = str(value)
    if len(text) > MAX_FIELD_CHARS:
        raise DecisionError(413, f"{field} exceeds {MAX_FIELD_CHARS} characters")
    return text


def _run(argv: list[str], *, what: str) -> str:
    """Run a fixed argv with no shell. Returns stdout on success."""
    script = argv[1]
    if not Path(script).is_file():
        raise DecisionError(503, f"{what} script not installed at {script}")
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, shell=False
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=CALL_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("%s timed out after %ss", what, CALL_TIMEOUT_SECONDS)
        raise DecisionError(504, f"{what} timed out") from None
    except OSError as exc:
        logger.warning("%s failed to start: %s", what, type(exc).__name__)
        raise DecisionError(500, f"{what} could not be started") from None

    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()
        # The CLIs answer with {"success": false, "error": ...} on handled
        # failures; unwrap it so the caller sees the reason, not a JSON blob.
        detail = message
        # One CLI reports on stdout, the other on stderr — check both.
        for stream in (completed.stdout, completed.stderr):
            parsed = _parse_json_stdout(stream)
            if isinstance(parsed, dict) and parsed.get("error"):
                detail = str(parsed["error"])
                break
        lowered = detail.lower()
        if "not found" in lowered:
            status = 404
        elif "already deleted" in lowered:
            status = 409
        elif "ValueError" in message or "must be" in lowered or "requires" in lowered:
            # Bad input is the caller's fault, not a server fault.
            status = 400
        else:
            status = 502
        logger.warning("%s exited %s: %s", what, completed.returncode, detail[:400])
        raise DecisionError(status, detail[:400] or f"{what} failed")

    logger.info("%s ok", what)
    return completed.stdout


def _permit_exists(
    permit_id: str,
    *,
    permits_script: Path,
    python_executable: str,
) -> bool:
    """`permits_db.py update` is an UPSERT: an unknown --permit-id creates a
    row. A decision endpoint must never create a permit, so existence is
    checked first and a miss is a 404."""
    argv = [python_executable, str(permits_script), "get", "--permit-id", permit_id]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, shell=False
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=CALL_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        raise DecisionError(504, "permit lookup timed out") from None
    payload = _parse_json_stdout(completed.stdout)
    if isinstance(payload, dict) and payload.get("success") is True:
        return True
    return False


def _parse_json_stdout(stdout: str) -> Any:
    stripped = (stdout or "").strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        # The CLIs print human text on some paths; the caller re-reads the row
        # anyway, so the raw output is more useful than an error here.
        return {"output": stripped[:2000]}


def apply_permit_decision(
    permit_id: str,
    body: dict[str, Any],
    *,
    scripts_dir: str | Path,
    python_executable: str = sys.executable,
) -> dict[str, Any]:
    """Apply a permit decision via `permits_db.py update`."""
    if not isinstance(body, dict):
        raise DecisionError(400, "body must be an object")

    unknown = sorted(set(body) - set(PERMIT_FIELD_FLAGS) - {"delete"})
    if unknown:
        raise DecisionError(400, f"unsupported fields: {', '.join(unknown)}")

    permit_id = _text(permit_id, "permit_id")
    permits_script = _script_path(scripts_dir, "permits_db.py")
    if not _permit_exists(
        permit_id,
        permits_script=permits_script,
        python_executable=python_executable,
    ):
        raise DecisionError(404, f"permit not found: {permit_id}")

    argv = [python_executable, str(permits_script), "update", "--permit-id", permit_id]

    if body.get("delete") is True:
        argv.append("--delete")
    else:
        for field, flag in PERMIT_FIELD_FLAGS.items():
            if field not in body or body[field] is None:
                continue
            value = body[field]
            if field in ("approved", "executed") and isinstance(value, bool):
                # The column is free text with a truthiness check, not a
                # boolean, so a JSON bool has to be spelled the way the CLI
                # reads it back.
                value = "ok" if value else ""
            argv.extend([flag, _text(value, field)])
        if len(argv) == 5:
            raise DecisionError(400, "no permit fields to update")

    return {
        "permit_id": permit_id,
        "applied": [k for k in body if k != "delete"] or ["delete"],
        "result": _parse_json_stdout(_run(argv, what="permit decision")),
    }


def apply_issue_update(
    issue_id: str,
    body: dict[str, Any],
    *,
    scripts_dir: str | Path,
    python_executable: str = sys.executable,
) -> dict[str, Any]:
    """Apply an issue transition via `agent_notes_db.py update --json`."""
    if not isinstance(body, dict):
        raise DecisionError(400, "body must be an object")

    unknown = sorted(set(body) - set(ISSUE_UPDATE_FIELDS))
    if unknown:
        raise DecisionError(400, f"unsupported fields: {', '.join(unknown)}")

    payload: dict[str, Any] = {}
    for field in ISSUE_UPDATE_FIELDS:
        if field not in body or body[field] is None:
            continue
        payload[field] = body[field]
    if not payload:
        raise DecisionError(400, "no issue fields to update")

    # Enum checks happen here for a fast, specific 400; the script re-validates
    # and remains the real boundary.
    status = payload.get("status")
    if status is not None and status not in ISSUE_STATUSES:
        raise DecisionError(400, f"status must be one of: {', '.join(ISSUE_STATUSES)}")
    event_type = payload.get("event_type")
    if event_type is not None and event_type not in ISSUE_EVENT_TYPES:
        raise DecisionError(
            400, f"event_type must be one of: {', '.join(ISSUE_EVENT_TYPES)}"
        )
    if "merge_into_id" in payload:
        try:
            payload["merge_into_id"] = int(payload["merge_into_id"])
        except (TypeError, ValueError):
            raise DecisionError(400, "merge_into_id must be an integer") from None
    if payload.get("delete"):
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DecisionError(400, "reason is required")

    for key in ("resolution", "verification", "context", "reason"):
        if key in payload:
            payload[key] = _text(payload[key], key)

    try:
        numeric_id = int(str(issue_id).strip())
    except ValueError:
        raise DecisionError(400, "issue id must be an integer") from None

    issues_script = _script_path(scripts_dir, "agent_notes_db.py")
    argv = [
        python_executable,
        str(issues_script),
        "update",
        "--id",
        str(numeric_id),
        "--json",
        json.dumps(payload),
    ]
    return {
        "issue_id": numeric_id,
        "applied": sorted(payload),
        "result": _parse_json_stdout(_run(argv, what="issue update")),
    }
