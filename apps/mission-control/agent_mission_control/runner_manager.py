"""Per-profile pool of isolated Hermes gateway processes.

Mirrors Hermes' own desktop app (``apps/desktop/electron/main.ts``'s
``backendPool``/``ensureBackend``/``spawnPoolBackend``/``evictLruPoolBackends``/
``startPoolIdleReaper``): lazily spawn one
``hermes --profile <X> serve --isolated --host 127.0.0.1 --port 0`` subprocess
per profile the first time a chat session needs it, reuse that process across
every session of the same profile, LRU-evict past a soft cap (never evicting
anything touched within the keepalive-fresh window — an active chat is never
killed just to make room), and reap anything idle past a hard timeout.

``serve --isolated`` speaks the exact same gateway HTTP wire protocol that
``chat_proxy.py``/``GatewayClient`` already implement for the shared default
gateway on :8642 — this module owns process lifecycle only and hands back a
plain ``GatewayClient`` pointed at the spawned process's port/token. Nothing
about the chat streaming/SSE path changes.

``--isolated`` is load-bearing: without it (and without ``HERMES_DESKTOP=1``,
which this module deliberately does not set — see module docstring in the
project plan), ``hermes --profile X serve`` silently redirects into the
single shared machine dashboard instead of running standalone scoped to that
profile. Never spawn without it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import signal
import time
from dataclasses import dataclass, field
from typing import Optional

from .clients import GatewayClient

logger = logging.getLogger("agent_mission_control.runner_manager")

# `serve` prints this line to stdout once uvicorn has bound its socket. The
# legacy `dashboard` alternative spelling is accepted too (older runtimes);
# see hermes_cli's own port-announcement consumer for the same regex shape.
_READY_RE = re.compile(r"^HERMES_(?:BACKEND|DASHBOARD)_READY port=(\d+)")


class RunnerSpawnError(RuntimeError):
    """A profile gateway process failed to start or become healthy."""


class _ProcessExited(RuntimeError):
    """The child closed stdout (exited, or the fd went away) before
    announcing a port."""


@dataclass
class _PoolEntry:
    profile: str
    process: Optional["asyncio.subprocess.Process"] = None
    port: Optional[int] = None
    client: Optional[GatewayClient] = None
    last_active_at: float = field(default_factory=time.monotonic)
    ready: Optional["asyncio.Future"] = None


async def _read_port_announcement(stdout: "asyncio.StreamReader") -> int:
    while True:
        line = await stdout.readline()
        if not line:
            raise _ProcessExited()
        text = line.decode("utf-8", errors="replace").rstrip("\n")
        match = _READY_RE.match(text)
        if match:
            return int(match.group(1))


def _drain_stream(stream: "asyncio.StreamReader", label: str) -> "asyncio.Task":
    """Keep reading a pipe to log lines and stop it filling up and blocking
    the child once the caller has stopped reading it for its own reasons."""

    async def _drain() -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            logger.debug("%s: %s", label, line.decode("utf-8", errors="replace").rstrip())

    return asyncio.ensure_future(_drain())


def _terminate(process: "asyncio.subprocess.Process") -> None:
    """SIGTERM the whole process group (mirrors the desktop's
    ``stopBackendChild``: spawned with ``start_new_session=True`` so
    ``pgid == pid``, and killing the group takes any MCP grandchildren with
    it); fall back to signaling the direct child if the group signal fails."""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except ProcessLookupError:
            pass


class RunnerManager:
    """Per-profile pool of isolated ``hermes serve`` gateway processes."""

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
        self._hermes_executable = hermes_executable
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
        argv = [
            self._hermes_executable,
            "--profile", entry.profile,
            "serve", "--isolated",
            "--host", self._host,
            "--port", "0",
        ]
        env = dict(os.environ)
        env["HERMES_DASHBOARD_SESSION_TOKEN"] = token
        env["HERMES_PARENT_PID"] = str(os.getpid())

        logger.info("spawning profile gateway for %r", entry.profile)
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
            raise RunnerSpawnError(
                f"failed to spawn hermes serve for {entry.profile!r}: {exc}"
            ) from exc

        entry.process = process
        _drain_stream(process.stderr, f"runner[{entry.profile}].stderr")

        try:
            port = await asyncio.wait_for(
                _read_port_announcement(process.stdout),
                timeout=self._port_announce_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            _terminate(process)
            raise RunnerSpawnError(
                f"hermes serve for {entry.profile!r} did not announce a port "
                f"within {self._port_announce_timeout_seconds}s"
            ) from exc
        except _ProcessExited as exc:
            raise RunnerSpawnError(
                f"hermes serve for {entry.profile!r} exited before "
                "announcing a port"
            ) from exc

        _drain_stream(process.stdout, f"runner[{entry.profile}].stdout")

        entry.port = port
        base_url = f"http://{self._host}:{port}"
        client = GatewayClient(base_url, token)

        try:
            status, _, _ = await asyncio.wait_for(
                client.get("/health"), timeout=self._health_probe_timeout_seconds
            )
            if status >= 400:
                raise RunnerSpawnError(
                    f"hermes serve for {entry.profile!r} health probe returned {status}"
                )
        except RunnerSpawnError:
            await client.aclose()
            _terminate(process)
            raise
        except Exception as exc:
            await client.aclose()
            _terminate(process)
            raise RunnerSpawnError(
                f"hermes serve for {entry.profile!r} failed its health probe: {exc}"
            ) from exc

        entry.client = client
        entry.last_active_at = time.monotonic()
        logger.info("profile gateway ready for %r on port %s", entry.profile, port)
        return client

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
        while True:
            await asyncio.sleep(self._reap_interval_seconds)
            await self._reap_once()
            if not self._pool:
                return

    async def _stop(self, profile_name: str) -> None:
        """Tear one entry down. Callers must already hold ``self._lock``
        (asyncio.Lock is not reentrant, so this must never take it itself):
        _evict_lru runs inside ensure_profile_gateway's critical section,
        and _reap_once/stop_all take the lock around their whole pass."""
        entry = self._pool.pop(profile_name, None)
        if entry is None:
            return
        if entry.client is not None:
            await entry.client.aclose()
        process = entry.process
        if process is None:
            return
        _terminate(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=self._stop_grace_seconds)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass

    async def stop_all(self) -> None:
        """Tear down every pool entry — called from app shutdown so the BFF
        process never leaves orphaned profile gateways behind on a clean
        exit (a crash still relies on HERMES_PARENT_PID's watchdog)."""
        async with self._lock:
            for profile in list(self._pool.keys()):
                await self._stop(profile)
            if self._reaper_task is not None:
                self._reaper_task.cancel()
