"""Mirror an in-process turn's events to the gateway, for other clients to watch.

The gateway broadcasts the turns it runs itself (``SessionEventHub``), but the
CLI does not go through the gateway at all: ``hermes`` builds its own ``AIAgent``
in its own process and talks to the model directly. So a conversation driven
from a terminal was invisible to every other client — the dashboard's chat view
showed the user's message and then nothing at all until the turn ended and its
messages were persisted, at which point the whole thing (thinking, tool calls,
answer) appeared at once.

The only channel the two processes already share is the gateway's own HTTP API,
so that is what this uses: one long-lived chunked POST per turn, carrying the
same event names and payload shapes the gateway's chat/stream endpoint emits, so
that everything downstream — the BFF relay, the browser's parser, its turn
reducer — treats a CLI turn exactly like any other.

Three properties are non-negotiable, because this is a courtesy feature sitting
next to the user's actual work:

* **It never blocks the agent.** Callbacks hand off to a bounded queue and
  return; a background thread does the talking. If the queue fills, events are
  dropped, not awaited.
* **It never fails the turn.** Every error path is swallowed. A gateway that is
  down, unreachable, or unauthenticated simply means nobody is watching.
* **It never outlives the turn.** ``close()`` ends the request, and the gateway
  closes the turn out for watchers if this process dies without doing so.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# The real gate on what gets relayed is the CALL SITE: this is attached
# explicitly around the turns that need it (the interactive CLI, the gateway's
# platform adapters), never globally. This set is the backstop for the one case
# that must never be relayed — the gateway's own chat/stream endpoint already
# publishes to the hub directly, and relaying as well would double every frame
# for everyone watching.
EXCLUDED_PLATFORMS = {"api", "api_server"}

_SENTINEL = object()
_MAX_QUEUE = 4000
_FLUSH_SECONDS = 0.015
_CONNECT_TIMEOUT = 2.0


class SessionEventRelay:
    """Ship one turn's events to the gateway's session-event ingest endpoint."""

    def __init__(
        self,
        session_id: str,
        base_url: str,
        api_key: str,
        *,
        platform: str = "cli",
        run_id: Optional[str] = None,
    ) -> None:
        self.session_id = session_id
        self.platform = platform
        self.run_id = run_id or f"run_{uuid.uuid4().hex}"
        self.message_id = f"msg_{uuid.uuid4().hex}"
        self._url = f"{base_url.rstrip('/')}/api/sessions/{session_id}/events/ingest"
        self._api_key = api_key
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=_MAX_QUEUE)
        self._thread: Optional[threading.Thread] = None
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------- lifecycle

    def start(self) -> "SessionEventRelay":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._pump, name="session-event-relay", daemon=True
        )
        self._thread.start()
        return self

    def restore(self) -> None:
        """Put the agent's original callbacks back. Replaced by the wiring below."""

    def close(self, timeout: float = 1.0) -> None:
        """End the turn for watchers, then let the request finish.

        Best-effort by design: the caller is on the path of the user's own
        prompt returning, and waiting on a courtesy stream to drain would make
        the relay visible in exactly the way it must never be.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(_SENTINEL)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # -------------------------------------------------------------- emitting

    def emit(self, name: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Queue one event. Never raises, never blocks."""
        if self._closed:
            return
        data = dict(payload or {})
        with self._seq_lock:
            self._seq += 1
            data.setdefault("seq", self._seq)
        data.setdefault("session_id", self.session_id)
        data.setdefault("run_id", self.run_id)
        data.setdefault("ts", time.time())
        try:
            self._queue.put_nowait({"event": name, "data": data})
        except queue.Full:
            # Dropping text is the right failure here: the transcript the user
            # is actually reading is in their terminal, and the persisted
            # history still has every word.
            pass

    # --------------------------------------------------------------- pumping

    def _lines(self):
        """Yield NDJSON for the request body until the turn ends.

        Coalescing matters: a fast model emits a token callback every few
        milliseconds, and one HTTP write per token would put more load on the
        loopback interface than the turn itself. Everything already queued goes
        out in a single write.
        """
        while True:
            try:
                item = self._queue.get(timeout=_FLUSH_SECONDS)
            except queue.Empty:
                continue
            if item is _SENTINEL:
                return
            batch = [item]
            while len(batch) < 256:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is _SENTINEL:
                    yield _encode(batch)
                    return
                batch.append(nxt)
            yield _encode(batch)

    def _pump(self) -> None:
        try:
            import requests
        except Exception:
            return
        try:
            requests.post(
                self._url,
                data=self._lines(),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/x-ndjson",
                },
                timeout=(_CONNECT_TIMEOUT, None),
            )
        except Exception as exc:
            # Nothing the user asked for has failed. Debug level on purpose:
            # a gateway that is simply not running must not print a warning on
            # every turn.
            logger.debug("[session-event-relay] %s: %s", type(exc).__name__, exc)
            # Unblock the generator's consumer if the request died early.
            try:
                while True:
                    self._queue.get_nowait()
            except queue.Empty:
                pass


def _encode(batch) -> bytes:
    out = []
    for item in batch:
        try:
            out.append(json.dumps(item, ensure_ascii=False))
        except Exception:
            continue
    return ("\n".join(out) + "\n").encode("utf-8")


# ------------------------------------------------------------------ wiring


def gateway_ingest_target() -> Optional[tuple]:
    """(base_url, api_key) for this profile's gateway, or None if unusable."""
    try:
        from agent.secret_scope import get_secret
    except Exception:
        get_secret = None

    def _read(name: str, default: str = "") -> str:
        if get_secret is not None:
            try:
                return str(get_secret(name, default) or default)
            except Exception:
                pass
        return os.environ.get(name, default)

    key = _read("API_SERVER_KEY", "")
    if not key:
        return None
    port = _read("API_SERVER_PORT", "8642")
    # Deliberately not API_SERVER_HOST: that is the BIND address, and a gateway
    # bound to 0.0.0.0 is still reached from this machine over loopback.
    return f"http://127.0.0.1:{port}", key


def start_turn_relay(
    agent, session_id: str, user_message: Any, platform: Optional[str] = None
):
    """Begin mirroring one turn. Returns a handle for `finish_turn_relay`, or None.

    Anchored to the CALL SITE of a turn rather than to agent construction,
    because that is the only place the turn's own boundaries are known: when it
    started, what was asked, and what it produced. Watchers need all three —
    without `run.started` a remote client has nothing to open a view on, and
    without a terminal frame it would spin forever.
    """
    # The agent knows what it is; asking it beats making every call site repeat
    # a string it already carries.
    if not platform:
        platform = str(getattr(agent, "platform", "") or "")
    try:
        relay = attach_session_event_relay(agent, session_id, platform)
    except Exception:
        return None
    if relay is None:
        return None
    try:
        text = user_message
        if isinstance(user_message, dict):
            text = user_message.get("content") or user_message.get("text") or ""
        relay.emit("run.started", {
            "user_message": {"role": "user", "content": _as_text(text)},
            "platform": platform,
            "runtime": {"model": getattr(agent, "model", "") or ""},
        })
        relay.emit("message.started", {
            "message": {"id": relay.message_id, "role": "assistant"},
        })
    except Exception:
        pass
    return relay


def finish_turn_relay(relay, result: Any = None) -> None:
    """Close a turn out for watchers. Safe to call with None, twice, or after an error."""
    if relay is None:
        return
    try:
        final = ""
        usage = None
        if isinstance(result, dict):
            final = result.get("final_response") or ""
            usage = result.get("usage")
        relay.emit("assistant.completed", {
            "message_id": relay.message_id,
            "content": _as_text(final),
            "completed": True,
            "partial": False,
            "interrupted": False,
        })
        relay.emit("run.completed", {
            "message_id": relay.message_id,
            "completed": True,
            "messages": [],
            "usage": usage,
        })
        relay.emit("done", {})
    except Exception:
        pass
    finally:
        try:
            relay.restore()
        except Exception:
            pass
        try:
            relay.close()
        except Exception:
            pass


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def attach_session_event_relay(agent, session_id: str, platform: str) -> Optional[SessionEventRelay]:
    """Mirror `agent`'s streaming callbacks onto a session event relay.

    Wraps rather than replaces: every existing callback still fires, with the
    same arguments, before anything is queued. The terminal UI is the user's
    primary view of their own turn and is not negotiable for the sake of a
    remote watcher.
    """
    if not session_id or not platform or platform in EXCLUDED_PLATFORMS:
        return None
    target = gateway_ingest_target()
    if not target:
        return None
    base_url, api_key = target

    relay = SessionEventRelay(session_id, base_url, api_key, platform=platform).start()
    message_id = relay.message_id

    original_delta = getattr(agent, "stream_delta_callback", None)
    original_reasoning = getattr(agent, "reasoning_callback", None)
    original_progress = getattr(agent, "tool_progress_callback", None)

    def _delta(text, *args, **kwargs):
        if text:
            relay.emit("assistant.delta", {"message_id": message_id, "delta": text})
        if original_delta:
            return original_delta(text, *args, **kwargs)
        return None

    def _reasoning(text, *args, **kwargs):
        if text:
            relay.emit("reasoning.delta", {"message_id": message_id, "delta": text})
        if original_reasoning:
            return original_reasoning(text, *args, **kwargs)
        return None

    def _progress(event_type, tool_name=None, preview=None, args=None, **kwargs):
        # Same mapping the gateway's chat/stream endpoint applies, so a CLI tool
        # row and a gateway tool row are indistinguishable to a watcher.
        try:
            if event_type == "reasoning.available":
                relay.emit("tool.progress", {
                    "message_id": message_id,
                    "tool_name": tool_name or "_thinking",
                    "delta": preview or "",
                })
            elif event_type in {"tool.started", "tool.completed", "tool.failed"}:
                payload = {
                    "message_id": message_id, "tool_name": tool_name,
                    "preview": preview, "args": args,
                }
                result = kwargs.get("result")
                if result is not None:
                    payload["result"] = _truncate_result(result)
                for field in ("duration", "is_error", "tool_call_id"):
                    value = kwargs.get(field)
                    if value is not None:
                        payload[field] = value
                relay.emit(event_type, payload)
        except Exception:
            pass
        if original_progress:
            return original_progress(
                event_type, tool_name=tool_name, preview=preview, args=args, **kwargs
            )
        return None

    agent.stream_delta_callback = _delta
    agent.reasoning_callback = _reasoning
    agent.tool_progress_callback = _progress

    def _restore() -> None:
        # The agent outlives the turn in the interactive CLI, and the next turn
        # gets its own relay. Leaving these wrappers in place would chain one
        # per turn onto the same agent and keep a dead relay's queue reachable.
        agent.stream_delta_callback = original_delta
        agent.reasoning_callback = original_reasoning
        agent.tool_progress_callback = original_progress

    relay.restore = _restore
    return relay


_TOOL_RESULT_LIMIT = 16000


def _truncate_result(result) -> str:
    if isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            text = str(result)
    if len(text) > _TOOL_RESULT_LIMIT:
        return text[:_TOOL_RESULT_LIMIT] + "\n… (truncated)"
    return text
