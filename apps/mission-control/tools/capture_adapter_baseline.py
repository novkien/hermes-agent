#!/usr/bin/env python3
"""Capture a value-free behavioral baseline from the deployed Adapter.

The output is safe to check in: response values are replaced by type/shape
descriptors, dynamic mapping keys are collapsed, and memory bodies and write
routes are never requested. The bearer token is read only to authenticate the
requests and is never included in output or errors.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "http://127.0.0.1:8643"
DEFAULT_TOKEN_FILE = "/etc/agentos-data-adapter/token"
MAX_RESPONSE_BYTES = 8_000_000
SAMPLE_ITEMS = 3

REDACTED_FIELD = re.compile(
    r"(?:authorization|cookie|password|passwd|secret|token|api[_-]?key)", re.I
)
UUIDISH = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f-]{27,}|[0-9a-f]{24,}|[A-Za-z0-9_-]{48,})$",
    re.I,
)
SESSIONISH = re.compile(r"^\d{8}[_-][A-Za-z0-9_-]{6,}$")


@dataclass(frozen=True)
class Probe:
    name: str
    method: str
    path_template: str
    classification: str = "read"


ROUTE_INVENTORY = (
    Probe("health", "GET", "/health", "public-read"),
    Probe("capabilities", "GET", "/capabilities"),
    Probe("memory-read", "GET", "/memory/files/{file_key}", "sensitive-read"),
    Probe("memory-write", "PUT", "/memory/files/{file_key}", "mutation"),
    Probe("kanban-boards", "GET", "/kanban/boards"),
    Probe("kanban-tasks", "GET", "/kanban/tasks"),
    Probe("kanban-task-detail", "GET", "/kanban/tasks/{task_id}"),
    Probe("kanban-task-events", "GET", "/kanban/tasks/{task_id}/events"),
    Probe("kanban-task-runs", "GET", "/kanban/tasks/{task_id}/runs"),
    Probe("kanban-task-attachments", "GET", "/kanban/tasks/{task_id}/attachments"),
    Probe(
        "kanban-task-worker-session",
        "GET",
        "/kanban/tasks/{task_id}/worker-session",
    ),
    Probe("kanban-board-summary", "GET", "/kanban/board/summary"),
    Probe("permits", "GET", "/permits"),
    Probe("permit-detail", "GET", "/permits/{permit_id}"),
    Probe("issues", "GET", "/issues"),
    Probe("issue-detail", "GET", "/issues/{issue_id}"),
    Probe("sessions-search", "GET", "/sessions/search"),
    Probe("room-sessions", "GET", "/room-sessions"),
    Probe("session-tips", "GET", "/session-tips"),
    Probe("room-cards", "GET", "/room-cards"),
    Probe("thread-sessions", "GET", "/thread-sessions"),
    Probe("session-timeline", "GET", "/sessions/{session_id}/timeline"),
    Probe("source-fingerprint", "GET", "/sources/{source_id}/fingerprint"),
    Probe("room-binding", "GET", "/room-binding"),
    Probe("permit-decision", "POST", "/permits/{permit_id}/decision", "mutation"),
    Probe("issue-update", "POST", "/issues/{issue_id}/update", "mutation"),
)


def _safe_mapping_key(key: Any) -> str:
    text = str(key)
    if REDACTED_FIELD.search(text):
        return "<redacted-field>"
    if text.isdigit() or UUIDISH.fullmatch(text) or SESSIONISH.fullmatch(text) or len(text) > 80:
        return "<dynamic-key>"
    return text


def _shape(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return "depth-limit"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        samples = [_shape(item, depth=depth + 1) for item in value[:SAMPLE_ITEMS]]
        unique: list[Any] = []
        for sample in samples:
            if sample not in unique:
                unique.append(sample)
        return {"type": "array", "sample_shapes": unique}
    if isinstance(value, dict):
        fields: dict[str, Any] = {}
        dynamic_shapes: list[Any] = []
        for raw_key in sorted(value, key=lambda item: str(item)):
            key = _safe_mapping_key(raw_key)
            child = _shape(value[raw_key], depth=depth + 1)
            if key == "<dynamic-key>":
                if child not in dynamic_shapes:
                    dynamic_shapes.append(child)
                continue
            if key == "<redacted-field>":
                fields[key] = "redacted"
                continue
            fields[key] = child
        if dynamic_shapes:
            fields["<dynamic-key>"] = {"sample_shapes": dynamic_shapes}
        return {"type": "object", "fields": fields}
    return type(value).__name__


def _decode_json(raw: bytes) -> Any:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("response exceeds baseline probe limit")
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def _request(
    base_url: str,
    token: str,
    path: str,
    *,
    timeout: float,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
) -> tuple[int, Any, float]:
    data = None if json_body is None else json.dumps(json_body).encode("utf-8")
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        data=data,
        method=method,
    )
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            body = _decode_json(response.read(MAX_RESPONSE_BYTES + 1))
            status = response.status
    except HTTPError as exc:
        status = exc.code
        body = _decode_json(exc.read(MAX_RESPONSE_BYTES + 1))
    except URLError as exc:
        raise RuntimeError(f"adapter transport failed: {type(exc.reason).__name__}") from None
    elapsed_ms = (time.perf_counter() - started) * 1000
    return status, body, elapsed_ms


def _data_rows(body: Any, *keys: str) -> list[dict[str, Any]]:
    value = body.get("data") if isinstance(body, dict) else None
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _first_identifier(rows: list[dict[str, Any]], *names: str) -> str | None:
    for row in rows:
        for name in names:
            value = row.get(name)
            if value is not None and str(value).strip():
                return str(value)
    return None


def _first_recursive_value(value: Any, candidate_keys: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in candidate_keys and child is not None and str(child).strip():
                return str(child)
            found = _first_recursive_value(child, candidate_keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_recursive_value(child, candidate_keys)
            if found:
                return found
    return None


def _record(status: int, body: Any, elapsed_ms: float) -> dict[str, Any]:
    return {
        "status": status,
        "elapsed_ms": round(elapsed_ms, 3),
        "response_shape": _shape(body),
    }


def capture(base_url: str, token: str, timeout: float) -> dict[str, Any]:
    records: dict[str, Any] = {}
    raw: dict[str, Any] = {}

    def get(name: str, path: str) -> Any:
        status, body, elapsed = _request(base_url, token, path, timeout=timeout)
        records[name] = _record(status, body, elapsed)
        raw[name] = body
        return body

    get("health", "/health")
    get("capabilities", "/capabilities")
    status, memory_body, elapsed = _request(
        base_url, token, "/memory/files/memory", timeout=timeout
    )
    if isinstance(memory_body, dict) and isinstance(memory_body.get("data"), dict):
        memory_body = {**memory_body, "data": {**memory_body["data"]}}
        memory_body["data"].pop("content", None)
        memory_body["data"].pop("path", None)
    records["memory-read"] = _record(status, memory_body, elapsed)
    status, body, elapsed = _request(
        base_url,
        token,
        "/memory/files/__phase0_missing__",
        timeout=timeout,
        method="PUT",
        json_body={},
    )
    records["memory-write"] = _record(status, body, elapsed)

    get("kanban-boards", "/kanban/boards")
    tasks = get("kanban-tasks", "/kanban/tasks?board=all&limit=1")
    task_id = _first_identifier(_data_rows(tasks), "id", "task_id")
    if task_id:
        encoded_task = urlencode({"id": task_id})[3:]
        get("kanban-task-detail", f"/kanban/tasks/{encoded_task}")
        get("kanban-task-events", f"/kanban/tasks/{encoded_task}/events?limit=1")
        get("kanban-task-runs", f"/kanban/tasks/{encoded_task}/runs?limit=1")
        get(
            "kanban-task-attachments",
            f"/kanban/tasks/{encoded_task}/attachments?limit=1",
        )
        get("kanban-task-worker-session", f"/kanban/tasks/{encoded_task}/worker-session")
    else:
        for name in (
            "kanban-task-detail",
            "kanban-task-events",
            "kanban-task-runs",
            "kanban-task-attachments",
            "kanban-task-worker-session",
        ):
            records[name] = {"status": "not-probed", "reason": "no-safe-identifier"}
    get("kanban-board-summary", "/kanban/board/summary?board=all")

    permits = get("permits", "/permits?limit=1")
    permit_id = _first_identifier(_data_rows(permits), "permit_id", "id")
    if permit_id:
        encoded = urlencode({"id": permit_id})[3:]
        get("permit-detail", f"/permits/{encoded}")
    else:
        records["permit-detail"] = {"status": "not-probed", "reason": "empty-source"}
    status, body, elapsed = _request(
        base_url,
        token,
        "/permits/__phase0_missing__/decision",
        timeout=timeout,
        method="POST",
        json_body={"__phase0_unsupported__": True},
    )
    records["permit-decision"] = _record(status, body, elapsed)

    issues = get("issues", "/issues?limit=1")
    issue_id = _first_identifier(_data_rows(issues), "issue_id", "id")
    if issue_id:
        encoded = urlencode({"id": issue_id})[3:]
        get("issue-detail", f"/issues/{encoded}?occurrence_limit=1")
    else:
        records["issue-detail"] = {"status": "not-probed", "reason": "empty-source"}
    status, body, elapsed = _request(
        base_url,
        token,
        "/issues/not-an-integer/update",
        timeout=timeout,
        method="POST",
        json_body={"status": "open"},
    )
    records["issue-update"] = _record(status, body, elapsed)

    sessions = get("sessions-search", "/sessions/search?q=the&limit=1")
    session_id = _first_identifier(_data_rows(sessions), "id", "session_id")
    binding = get("room-binding", "/room-binding")
    chat_id = _first_recursive_value(binding, {"chat_id", "room_chat_id"})
    thread_id = _first_recursive_value(binding, {"thread_id", "topic_id"})

    if chat_id:
        chat_query = urlencode({"chat_id": chat_id, "limit": 1, "history": 1})
        room_sessions = get("room-sessions", f"/room-sessions?{chat_query}")
        room_session_id = _first_identifier(
            _data_rows(room_sessions, "sessions"), "id", "session_id"
        )
        if room_session_id is not None:
            session_id = room_session_id
        get("room-cards", f"/room-cards?{urlencode({'chat_id': chat_id, 'per_thread': 1})}")
        if thread_id:
            get(
                "thread-sessions",
                f"/thread-sessions?{urlencode({'chat_id': chat_id, 'thread_ids': thread_id})}",
            )
        else:
            records["thread-sessions"] = {
                "status": "not-probed",
                "reason": "no-safe-thread-context",
            }
    else:
        for name in ("room-sessions", "room-cards", "thread-sessions"):
            records[name] = {"status": "not-probed", "reason": "no-safe-room-context"}

    if session_id:
        encoded = urlencode({"id": session_id})[3:]
        get("session-tips", f"/session-tips?{urlencode({'ids': session_id})}")
        get("session-timeline", f"/sessions/{encoded}/timeline?limit=1")
    else:
        records["session-tips"] = {"status": "not-probed", "reason": "empty-source"}
        records["session-timeline"] = {"status": "not-probed", "reason": "empty-source"}

    for source_id in ("kanban", "permits", "issues", "state"):
        status, body, elapsed = _request(
            base_url,
            token,
            f"/sources/{source_id}/fingerprint",
            timeout=timeout,
        )
        records[f"source-fingerprint:{source_id}"] = _record(status, body, elapsed)

    del raw
    return {
        "format_version": 1,
        "capture_policy": {
            "response_values": "types-only",
            "dynamic_keys": "collapsed",
            "memory_content": "discarded-before-shaping",
            "mutations": "negative-validation-only; no write target resolved",
        },
        "route_inventory": [probe.__dict__ for probe in ROUTE_INVENTORY],
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    token = open(args.token_file, encoding="utf-8").read().strip()
    if not token:
        raise SystemExit("adapter token file is empty")
    print(json.dumps(capture(args.base_url, token, args.timeout), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
