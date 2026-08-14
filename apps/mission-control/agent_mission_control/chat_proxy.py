"""Chat streaming proxy (Stage 5 item 3) — merged into agent_mission_control (S8).

BFF auth+CSRF POST /api/chat/stream -> gateway 8642
POST /api/sessions/{session_id}/chat/stream (Bearer HERMES_GATEWAY_API_KEY,
server-side only). Forwards SSE frames as-is with request_id correlation.
On upstream error -> `event: error` + data JSON. Session create passthrough.
NO PTY/shell anywhere. Idempotency-Key passthrough on session create.

Request body: {message, system_message?, instructions?, model?, provider?,
model_options?} (+ optional session_id to stream into an existing session).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from typing import Any, AsyncIterator, Optional

import httpx

from .clients import GatewayClient, UpstreamError as ClientUpstreamError

UPSTREAM_CHAT_PATH = "/api/sessions/{session_id}/chat/stream"
UPSTREAM_SESSION_CREATE = "/api/sessions"
UPSTREAM_SESSION_EVENTS = "/api/sessions/{session_id}/events/stream"
UPSTREAM_SESSIONS_RUNNING = "/api/sessions/running"

CHAT_ATTACHMENT_MAX_COUNT = 8
CHAT_ATTACHMENT_MAX_BYTES = 700 * 1024
CHAT_ATTACHMENTS_TOTAL_BYTES = 700 * 1024
_ATTACHMENT_KINDS = frozenset({"image", "pdf", "file"})
_MIME_TYPE_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_DATA_URL_RE = re.compile(
    r"^data:([^;,]*)(?:;[^;,=]+=[^;,]+)*;base64,(.*)$", re.IGNORECASE | re.DOTALL
)


class UpstreamError(Exception):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"upstream error {status}")
        self.status = status
        self.body = body


class AttachmentValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_chat_attachments(value: Any) -> list[dict[str, Any]] | None:
    """Validate the BFF attachment contract and emit Hermes' compatibility shape."""
    if value is None or value == []:
        return None
    if not isinstance(value, list):
        raise AttachmentValidationError("invalid_attachments", "attachments must be an array")
    if len(value) > CHAT_ATTACHMENT_MAX_COUNT:
        raise AttachmentValidationError(
            "too_many_attachments",
            f"at most {CHAT_ATTACHMENT_MAX_COUNT} attachments are allowed",
        )

    normalized: list[dict[str, Any]] = []
    total_bytes = 0
    for index, item in enumerate(value):
        label = f"attachment {index + 1}"
        if not isinstance(item, dict):
            raise AttachmentValidationError("invalid_attachment", f"{label} must be an object")

        name = item.get("name")
        mime_type = item.get("mime_type")
        kind = item.get("kind")
        declared_size = item.get("size")
        data = item.get("data")
        if not isinstance(name, str) or not name.strip() or len(name) > 255:
            raise AttachmentValidationError("invalid_attachment_name", f"{label} has an invalid name")
        name = name.strip()
        if any(ord(char) < 32 for char in name) or "/" in name or "\\" in name:
            raise AttachmentValidationError("invalid_attachment_name", f"{label} name must be a filename")
        if not isinstance(mime_type, str) or not _MIME_TYPE_RE.fullmatch(mime_type.strip()):
            raise AttachmentValidationError("invalid_attachment_mime", f"{label} has an invalid mime_type")
        mime_type = mime_type.strip().lower()
        if kind not in _ATTACHMENT_KINDS:
            raise AttachmentValidationError("invalid_attachment_kind", f"{label} has an invalid kind")
        if kind == "image" and not mime_type.startswith("image/"):
            raise AttachmentValidationError("invalid_attachment_kind", f"{label} image kind requires image MIME")
        if kind == "pdf" and mime_type != "application/pdf":
            raise AttachmentValidationError("invalid_attachment_kind", f"{label} pdf kind requires application/pdf")
        if isinstance(declared_size, bool) or not isinstance(declared_size, int) or declared_size <= 0:
            raise AttachmentValidationError("invalid_attachment_size", f"{label} has an invalid size")
        if not isinstance(data, str) or not data.strip():
            raise AttachmentValidationError("attachment_data_required", f"{label} data is required")

        encoded = data.strip()
        match = _DATA_URL_RE.fullmatch(encoded)
        if match:
            data_mime = match.group(1).strip().lower()
            if data_mime and data_mime != mime_type:
                raise AttachmentValidationError(
                    "attachment_mime_mismatch", f"{label} data MIME does not match mime_type"
                )
            encoded = match.group(2)
        encoded = re.sub(r"\s+", "", encoded)
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise AttachmentValidationError(
                "invalid_attachment_data", f"{label} data must be valid base64"
            ) from exc
        size = len(decoded)
        if size == 0 or size != declared_size:
            raise AttachmentValidationError(
                "attachment_size_mismatch", f"{label} size does not match decoded data"
            )
        if size > CHAT_ATTACHMENT_MAX_BYTES:
            raise AttachmentValidationError("attachment_too_large", f"{label} exceeds 700 KB")
        total_bytes += size
        if total_bytes > CHAT_ATTACHMENTS_TOTAL_BYTES:
            raise AttachmentValidationError("attachments_too_large", "combined attachments exceed 700 KB")

        data_url = f"data:{mime_type};base64,{encoded}"
        normalized.append({
            "name": name,
            "fileName": name,
            "type": kind,
            "contentType": mime_type,
            "mimeType": mime_type,
            "mediaType": mime_type,
            "content": encoded,
            "data": encoded,
            "base64": encoded,
            "dataUrl": data_url,
            "size": size,
        })
    return normalized


async def create_session(gateway: GatewayClient, body: dict, idempotency_key: Optional[str] = None) -> dict:
    """Passthrough POST /api/sessions; returns the upstream JSON.

    S4 GatewayClient.request returns (status, body, headers).
    """
    extra_headers = {}
    if idempotency_key:
        extra_headers["Idempotency-Key"] = idempotency_key
    try:
        status, data, _ = await gateway.request(
            "POST", UPSTREAM_SESSION_CREATE, json_body=body,
            extra_headers=extra_headers or None,
        )
    except ClientUpstreamError as exc:
        raise UpstreamError(exc.status, exc.body)
    if status >= 400:
        raise UpstreamError(status, data)
    return {"status": status, "body": data}


async def _open_stream(
    gateway: GatewayClient,
    method: str,
    path: str,
    *,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> tuple[httpx.Response, str]:
    """Open an upstream SSE request. Returns (response, request_id).

    Shared by every streaming path so the error contract is identical whichever
    one a caller uses: a non-2xx never reaches the relay as a "stream" of an
    error page — the small body is drained here and re-raised as UpstreamError,
    which the route turns into a proper `event: error` frame.
    """
    request_id = uuid.uuid4().hex[:16]
    try:
        resp = await gateway.stream(
            method, path, json_body=json_body, params=params,
            extra_headers={"X-Request-Id": request_id},
        )
    except ClientUpstreamError as exc:
        raise UpstreamError(exc.status, {"detail": exc.detail})
    if resp.status_code >= 400:
        err_body = (await resp.aread()).decode("utf-8", "replace")[:2000]
        await resp.aclose()
        try:
            parsed = json.loads(err_body)
        except Exception:
            parsed = {"detail": err_body}
        raise UpstreamError(resp.status_code, parsed)
    return resp, request_id


async def stream_chat(
    gateway: GatewayClient,
    session_id: str,
    body: dict,
) -> tuple[httpx.Response, str]:
    """Start a turn and stream it back. Returns (response, request_id)."""
    return await _open_stream(
        gateway, "POST", UPSTREAM_CHAT_PATH.format(session_id=session_id),
        json_body=body,
    )


async def open_session_events(
    gateway: GatewayClient,
    session_id: str,
    after_seq: int = 0,
) -> tuple[httpx.Response, str]:
    """Watch a session's live turn without starting one.

    The read-only counterpart to `stream_chat`, and the reason a turn driven
    from the Hermes CLI, Telegram or cron shows up here as it happens instead of
    arriving in one lump when the gateway finally persists it. Frames are the
    same names and shapes `stream_chat` produces, so everything downstream —
    the relay, the SPA's parser, the turn reducer — is reused unchanged.

    `after_seq` resumes where a dropped connection left off, so a reconnect does
    not replay text the reader already has.
    """
    return await _open_stream(
        gateway, "GET", UPSTREAM_SESSION_EVENTS.format(session_id=session_id),
        params={"after_seq": after_seq} if after_seq else None,
    )


async def read_running_sessions(gateway: GatewayClient) -> list:
    """Sessions the gateway currently has a turn in flight for.

    Nothing in the Hermes session list says whether a turn is running — that is
    why the sider's old "active" chip was on for every session that had ever
    existed. This is the only honest source for it.
    """
    try:
        status, data, _ = await gateway.request("GET", UPSTREAM_SESSIONS_RUNNING)
    except ClientUpstreamError as exc:
        raise UpstreamError(exc.status, {"detail": exc.detail})
    if status >= 400:
        raise UpstreamError(status, data)
    rows = data.get("running") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else []


async def iter_forwarded_frames(
    resp: httpx.Response, request_id: str
) -> AsyncIterator[bytes]:
    """Relay the upstream SSE body byte-for-byte.

    Two properties matter here and both were missing from the earlier
    line-reassembling version:

    * **No frame-boundary buffering.** The old loop accumulated lines and only
      emitted once a blank line arrived, so any frame whose terminator was
      delayed held the whole turn in memory — the transcript then landed in one
      lump when the stream closed, which is exactly the "nothing appears until I
      refresh" symptom. Bytes go out the moment httpx hands them over; the
      gateway's own ``: keepalive`` comments (every 30s) pass straight through.
    * **Close on the way out, always.** The ``finally`` is what makes a client
      disconnect propagate: Starlette cancels this generator, we close the
      upstream response, aiohttp raises ConnectionResetError inside
      ``_handle_session_chat_stream``, and the gateway cancels the agent task.
      That is the entire mechanism behind the composer's Stop button — without
      the ``finally`` the upstream run kept going after the browser walked away.
    """
    try:
        async for chunk in resp.aiter_bytes():
            yield chunk
    finally:
        await resp.aclose()


def open_frame(request_id: str, upstream_request_id: Optional[str] = None) -> str:
    """Synthetic first frame, emitted before anything upstream has arrived.

    The gateway sends nothing at all between accepting the POST and the agent
    producing ``run.started`` — which can be seconds while history loads. This
    frame lets the SPA distinguish "the relay is live and we are waiting on the
    agent" from "the request is still in flight", so the composer can show a
    real state instead of freezing.
    """
    data = json.dumps({"request_id": request_id,
                       "upstream_request_id": upstream_request_id})
    return f"event: bff.open\ndata: {data}\n\n"


def error_frame(message: str, request_id: str, upstream_status: Optional[int] = None) -> str:
    data = json.dumps(
        {"error": message, "request_id": request_id,
         "upstream_status": upstream_status}
    )
    return f"event: error\ndata: {data}\n\n"
