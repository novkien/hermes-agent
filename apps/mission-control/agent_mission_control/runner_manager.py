"""Per-profile pool of isolated Hermes API gateway processes.

Mirrors Hermes' own desktop app (``apps/desktop/electron/main.ts``'s
``backendPool``/``ensureBackend``/``spawnPoolBackend``/``evictLruPoolBackends``/
``startPoolIdleReaper``): lazily spawn one
``hermes --profile <X> gateway run --force --external-supervisor`` subprocess
per profile the first time a chat session needs it, reuse that process across
every session of the same profile, LRU-evict past a soft cap (never evicting
anything touched within the keepalive-fresh window — an active chat is never
killed just to make room), and reap anything idle past a hard timeout.

The Desktop ``serve --isolated`` backend is JSON-RPC over ``/api/ws``. It has
read-only session REST routes and therefore cannot satisfy Mission Control's
existing create/chat/SSE contract. ``gateway run`` owns the REST API server
used by the shared default gateway on :8642, so each managed profile receives
an ephemeral loopback port and process-scoped API key. This module still owns
process lifecycle only and hands back a normal ``GatewayClient``.

``--force`` is load-bearing because Mission Control is the external process
manager for this dedicated child. ``--external-supervisor`` keeps restart and
update requests from detaching an unmanaged replacement process.
"""

from __future__ import annotations

import asyncio
from collections import deque
from enum import Enum
import logging
import os
import re
import shutil
import secrets
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from .clients import GatewayClient

logger = logging.getLogger("agent_mission_control.runner_manager")


def _resolve_runner_executable(raw: str) -> str:
    """Resolve a hermes executable safely for non-interactive service env.

    Real deployments often start mission-control with a minimal PATH, so plain
    ``hermes`` may be unavailable even when the active virtual environment has a
    local binary. Fall back to the current interpreter's ``bin/hermes`` when PATH
    resolution fails. This preserves compatibility with the simple env var while
    making subprocess launch robust on services.
    """
    candidate = (raw or "").strip() or "hermes"
    if os.path.isabs(candidate):
        return candidate

    found = shutil.which(candidate)
    if found:
        return found

    local_in_venv = os.path.join(os.path.dirname(sys.executable), "hermes")
    if os.path.isfile(local_in_venv) and os.access(local_in_venv, os.X_OK):
        return local_in_venv

    user_local = os.path.expanduser("~/") or ""
    local_bin_candidate = os.path.join(user_local, ".local", "bin", "hermes")
    if os.path.isfile(local_bin_candidate) and os.access(local_bin_candidate, os.X_OK):
        return local_bin_candidate

    return candidate

class RunnerSpawnError(RuntimeError):
    """A profile gateway process failed to start or become healthy."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RunnerState(str, Enum):
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass
class _PoolEntry:
    profile: str
    process: Optional["asyncio.subprocess.Process"] = None
    port: Optional[int] = None
    client: Optional[GatewayClient] = None
    last_active_at: float = field(default_factory=time.monotonic)
    ready: Optional["asyncio.Future"] = None
    state: RunnerState = RunnerState.STARTING
    output_tail: deque[str] = field(default_factory=lambda: deque(maxlen=80))
    drain_tasks: list["asyncio.Task"] = field(default_factory=list)


def _scrub_line(value: str) -> str:
    value = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+",
        r"\1<redacted>",
        value,
    )
    return re.sub(
        r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,]+",
        r"\1<redacted>",
        value,
    )


def _remember_output(entry: _PoolEntry, label: str, raw: bytes) -> str:
    text = _scrub_line(raw.decode("utf-8", errors="replace").rstrip())
    if text:
        entry.output_tail.append(f"{label}: {text}")
        logger.debug("runner[%s].%s: %s", entry.profile, label, text)
    return text


def _allocate_loopback_port(host: str) -> int:
    """Ask the kernel for an unused loopback port for the child API server.

    aiohttp accepts port 0 but Hermes does not announce the kernel-selected
    value. Close the probe socket immediately before spawn; the tiny local
    bind race is covered by the bounded startup probe and a fail-closed error.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


async def _wait_until_ready(
    client: "GatewayClient",
    process: "asyncio.subprocess.Process",
    entry: _PoolEntry,
    timeout_seconds: float,
) -> None:
    """Poll the served backend until both readiness and chat auth work.

    Hermes Desktop treats a port announcement as "socket bound", not "ready".
    Mirror that contract here: use a bounded health ladder, then exercise the
    credentialed session-read leg Mission Control will actually use.
    """
    deadline = time.monotonic() + timeout_seconds
    last_issue = "no readiness response"
    health_seen = False
    while time.monotonic() < deadline:
        if process.returncode is not None:
            raise _failure_from_output(
                entry,
                f"Hermes for {entry.profile!r} exited during startup "
                f"({process.returncode})",
            )

        remaining = max(0.05, deadline - time.monotonic())
        probe_timeout = min(2.0, remaining)
        healthy = False
        for path in ("/api/health", "/api/status", "/health"):
            try:
                status, _, _ = await asyncio.wait_for(
                    client.get(path), timeout=probe_timeout
                )
            except Exception as exc:  # noqa: BLE001
                last_issue = f"{path}: {type(exc).__name__}"
                break
            if status in {404, 405}:
                last_issue = f"{path} returned {status}"
                continue
            if status in {401, 403}:
                raise RunnerSpawnError(
                    "runner_auth_failed",
                    f"runner readiness credential was rejected ({status})",
                )
            if status < 400:
                healthy = True
                health_seen = True
            else:
                last_issue = f"{path} returned {status}"
            break

        if healthy:
            try:
                status, _, _ = await asyncio.wait_for(
                    client.get("/api/sessions", params={"limit": 1}),
                    timeout=probe_timeout,
                )
            except Exception as exc:  # noqa: BLE001
                last_issue = f"authenticated session probe: {type(exc).__name__}"
            else:
                if status < 400:
                    return
                if status in {401, 403}:
                    raise RunnerSpawnError(
                        "runner_auth_failed",
                        f"runner session credential was rejected ({status})",
                    )
                last_issue = f"authenticated /api/sessions returned {status}"

        await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    code = "runner_unhealthy" if health_seen else "runner_start_timeout"
    raise RunnerSpawnError(
        code,
        f"Hermes runner did not become ready within {timeout_seconds}s: {last_issue}",
    )


def _drain_stream(
    stream: "asyncio.StreamReader", entry: _PoolEntry, label: str
) -> "asyncio.Task":
    """Keep reading a pipe to log lines and stop it filling up and blocking
    the child once the caller has stopped reading it for its own reasons."""

    async def _drain() -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            _remember_output(entry, label, line)

    return asyncio.ensure_future(_drain())


def _terminate(process: "asyncio.subprocess.Process") -> None:
    """SIGTERM the whole process group (mirrors the desktop's
    ``stopBackendChild``: spawned with ``start_new_session=True`` so
    ``pgid == pid``, and killing the group takes any MCP grandchildren with
    it); fall back to signaling the direct child if the group signal fails."""
    if process.returncode is not None:
        return
    if not isinstance(process, asyncio.subprocess.Process):
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except ProcessLookupError:
            pass


def _kill(process: "asyncio.subprocess.Process") -> None:
    if process.returncode is not None:
        return
    if isinstance(process, asyncio.subprocess.Process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


async def _close_entry_resources(entry: _PoolEntry, grace_seconds: float) -> None:
    if entry.client is not None:
        await entry.client.aclose()
        entry.client = None
    process = entry.process
    if process is not None:
        _terminate(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            _kill(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("runner[%s] did not exit after SIGKILL", entry.profile)
    for task in entry.drain_tasks:
        if not task.done():
            task.cancel()
    if entry.drain_tasks:
        await asyncio.gather(*entry.drain_tasks, return_exceptions=True)
    entry.drain_tasks.clear()


def _failure_from_output(entry: _PoolEntry, fallback: str) -> RunnerSpawnError:
    tail = "\n".join(entry.output_tail)
    lowered = tail.lower()
    if "profile" in lowered and "does not exist" in lowered:
        return RunnerSpawnError(
            "runner_profile_missing",
            f"profile {entry.profile!r} is not available to the Hermes runtime",
        )
    detail = entry.output_tail[-1] if entry.output_tail else fallback
    return RunnerSpawnError("runner_exited", f"{fallback}: {detail}")


class RunnerManager:
    """Per-profile pool of managed ``hermes gateway run`` processes."""

    def __init__(
        self,
        *,
        hermes_executable: str,
        host: str = "127.0.0.1",
        pool_max: int = 3,
        idle_seconds: float = 900.0,
        keepalive_fresh_seconds: float = 90.0,
        port_announce_timeout_seconds: float = 90.0,
        health_probe_timeout_seconds: float = 10.0,
        reap_interval_seconds: float = 60.0,
        stop_grace_seconds: float = 5.0,
    ):
        self._hermes_executable = _resolve_runner_executable(hermes_executable)
        self._host = host
        self._pool_max = max(1, pool_max)
        self._idle_seconds = idle_seconds
        self._keepalive_fresh_seconds = keepalive_fresh_seconds
        self._port_announce_timeout_seconds = port_announce_timeout_seconds
        self._health_probe_timeout_seconds = health_probe_timeout_seconds
        self._reap_interval_seconds = reap_interval_seconds
        self._stop_grace_seconds = stop_grace_seconds
        self._pool: dict[str, _PoolEntry] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: Optional["asyncio.Task"] = None
        logger.info(
            "runner manager configured executable=%r HERMES_HOME=%r host=%s",
            self._hermes_executable,
            os.environ.get("HERMES_HOME"),
            self._host,
        )

    async def ensure_profile_gateway(self, profile_name: str) -> GatewayClient:
        """Return a GatewayClient for profile_name's own isolated gateway,
        spawning it if this is the first session for that profile. Two
        concurrent callers for the same not-yet-running profile share the
        same in-flight spawn instead of racing two subprocesses."""
        # Every pool lookup/insert happens under the lock, including the hit
        # path: an unlocked `self._pool.get()` could hand back an entry that
        # a concurrent _evict_lru/_reap_once is already tearing down, and the
        # caller would then await a GatewayClient being aclose()d underneath
        # it. Touching last_active_at *before* releasing the lock is what
        # makes the subsequent await safe — an entry inside the
        # keepalive-fresh window is never evicted, and idle_seconds is far
        # larger still, so neither reclaim path can take it while we wait.
        async with self._lock:
            entry = self._pool.get(profile_name)
            if entry is None:
                await self._evict_lru(keep=self._pool_max - 1)
                entry = _PoolEntry(profile=profile_name)
                self._pool[profile_name] = entry
                entry.ready = asyncio.ensure_future(self._spawn(entry))
                self._start_reaper()
            entry.last_active_at = time.monotonic()
            ready = entry.ready

        try:
            return await ready
        except Exception:
            async with self._lock:
                # Only drop the entry if it is still the failed one — a retry
                # may already have replaced it with a healthy spawn.
                if self._pool.get(profile_name) is entry:
                    self._pool.pop(profile_name, None)
            raise

    def touch(self, profile_name: str) -> None:
        """Mark a profile's gateway as recently used, sparing it from LRU
        eviction and idle reaping while a session is actively streaming."""
        entry = self._pool.get(profile_name)
        if entry is not None:
            entry.last_active_at = time.monotonic()

    async def _spawn(self, entry: _PoolEntry) -> GatewayClient:
        token = secrets.token_urlsafe(32)
        try:
            port = _allocate_loopback_port(self._host)
        except OSError as exc:
            raise RunnerSpawnError(
                "runner_start_timeout",
                f"could not allocate a loopback port for {entry.profile!r}: {exc}",
            ) from exc
        argv = [
            self._hermes_executable,
            "--profile", entry.profile,
            "gateway", "run",
            "--force",
            "--external-supervisor",
        ]
        env = dict(os.environ)
        env["API_SERVER_HOST"] = self._host
        env["API_SERVER_PORT"] = str(port)
        env["API_SERVER_KEY"] = token
        env["HERMES_PARENT_PID"] = str(os.getpid())

        entry.state = RunnerState.STARTING
        logger.info("runner[%s] starting via %r", entry.profile, self._hermes_executable)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            code = "runner_executable_missing" if isinstance(exc, FileNotFoundError) else "runner_exited"
            raise RunnerSpawnError(
                code,
                f"failed to start Hermes for {entry.profile!r} using "
                f"{self._hermes_executable!r}: {exc}",
            ) from exc

        entry.process = process
        entry.port = port
        entry.client = GatewayClient(f"http://{self._host}:{port}", token)
        entry.drain_tasks.append(_drain_stream(process.stdout, entry, "stdout"))
        entry.drain_tasks.append(_drain_stream(process.stderr, entry, "stderr"))

        try:
            await _wait_until_ready(
                entry.client,
                process,
                entry,
                min(
                    self._port_announce_timeout_seconds,
                    self._health_probe_timeout_seconds,
                ),
            )
            entry.state = RunnerState.READY
            entry.last_active_at = time.monotonic()
            logger.info(
                "runner[%s] ready pid=%s port=%s",
                entry.profile,
                process.pid,
                port,
            )
            return entry.client
        except RunnerSpawnError as exc:
            entry.state = RunnerState.FAILED
            logger.warning("runner[%s] failed code=%s: %s", entry.profile, exc.code, exc)
            await _close_entry_resources(entry, self._stop_grace_seconds)
            raise
        except Exception as exc:  # noqa: BLE001
            entry.state = RunnerState.FAILED
            wrapped = RunnerSpawnError(
                "runner_unhealthy",
                f"Hermes for {entry.profile!r} failed during startup: {type(exc).__name__}",
            )
            logger.warning("runner[%s] failed: %s", entry.profile, exc)
            await _close_entry_resources(entry, self._stop_grace_seconds)
            raise wrapped from exc

    async def _evict_lru(self, *, keep: int) -> None:
        """Evict least-recently-used pool entries until at most ``keep``
        remain — but only ones idle past the keepalive-fresh window. An
        actively chatting profile is never killed to honor the soft cap; the
        pool is allowed to exceed it instead."""
        if len(self._pool) <= keep:
            return
        now = time.monotonic()
        evictable = sorted(
            (
                e for e in self._pool.values()
                if now - e.last_active_at > self._keepalive_fresh_seconds
            ),
            key=lambda e: e.last_active_at,
        )
        removable = len(self._pool) - max(0, keep)
        for entry in evictable:
            if removable <= 0:
                break
            logger.info(
                "evicting idle profile gateway %r (LRU cap %s)",
                entry.profile, self._pool_max,
            )
            await self._stop(entry.profile)
            removable -= 1

    def _start_reaper(self) -> None:
        if self._reaper_task is not None and not self._reaper_task.done():
            return
        self._reaper_task = asyncio.ensure_future(self._reap_loop())

    async def _reap_once(self) -> None:
        """One idle-reap pass — split out from _reap_loop so it's callable
        directly in tests without waiting on a real sleep interval.

        Holds the pool lock for the whole pass so it cannot tear an entry
        down while ensure_profile_gateway is handing that same entry out.
        """
        async with self._lock:
            now = time.monotonic()
            for profile in list(self._pool.keys()):
                entry = self._pool.get(profile)
                if entry is None:
                    continue
                if now - entry.last_active_at > self._idle_seconds:
                    logger.info(
                        "reaping idle profile gateway %r (idle > %ss)",
                        profile, self._idle_seconds,
                    )
                    await self._stop(profile)

    async def _reap_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._reap_interval_seconds)
                await self._reap_once()
                if not self._pool:
                    return
        except asyncio.CancelledError:
            return

    async def _stop(self, profile_name: str) -> None:
        """Tear one entry down. Callers must already hold ``self._lock``
        (asyncio.Lock is not reentrant, so this must never take it itself):
        _evict_lru runs inside ensure_profile_gateway's critical section,
        and _reap_once/stop_all take the lock around their whole pass."""
        entry = self._pool.pop(profile_name, None)
        if entry is None:
            return
        entry.state = RunnerState.STOPPING
        await _close_entry_resources(entry, self._stop_grace_seconds)

    async def stop_all(self) -> None:
        """Tear down every pool entry — called from app shutdown so the BFF
        process never leaves orphaned profile gateways behind on a clean
        exit (a crash still relies on HERMES_PARENT_PID's watchdog)."""
        async with self._lock:
            for profile in list(self._pool.keys()):
                await self._stop(profile)
            if self._reaper_task is not None:
                self._reaper_task.cancel()
                self._reaper_task = None
