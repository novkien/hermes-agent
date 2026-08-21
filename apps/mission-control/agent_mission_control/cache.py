"""In-memory TTL cache with stale-while-revalidate.

Keyed by profile/route/source fingerprint. Serves stale data while a
background refresh runs; a semaphore caps concurrent upstream preloads so a
burst of misses cannot hammer the upstreams.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

Fetcher = Callable[[], Awaitable[tuple[Any, dict[str, Any]]]]
"""Fetcher returns (payload, meta_extras); payload must be JSON-serializable."""


@dataclass
class CacheEntry:
    key: str
    payload: Any
    meta: dict[str, Any]
    fetched_at: float
    stale_after: float
    fingerprint: Optional[str] = None


class Cache:
    def __init__(self, ttl_seconds: float = 60.0, max_concurrency: int = 4):
        self._ttl = ttl_seconds
        self._sem = asyncio.Semaphore(max(1, max_concurrency))
        self._entries: dict[str, CacheEntry] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._source_touched: set[str] = set()

    # -- public API -------------------------------------------------------
    async def get(
        self,
        key: str,
        source_id: str,
        fingerprint: str | None,
        fetch: Fetcher,
    ) -> CacheEntry:
        """Return a usable entry, revalidating in the background when stale."""
        now = time.time()
        entry = self._entries.get(key)
        if entry is not None and now < entry.stale_after:
            self._source_touched.add(source_id)
            return entry

        # No fresh entry: kick a refresh (await it if nothing cached yet).
        task = self._start_refresh(key, source_id, fingerprint, fetch)
        if entry is not None:
            # Serve stale now; refresh continues in background.
            self._source_touched.add(source_id)
            return entry
        if task is not None:
            try:
                return await asyncio.wait_for(task, timeout=self._ttl * 2 + 30)
            except (asyncio.TimeoutError, Exception):
                # Fall through to a synthetic unavailable entry rather than
                # surfacing a cache-internal failure to the client.
                pass
        return CacheEntry(
            key=key,
            payload=None,
            meta={"error": "upstream_unavailable"},
            fetched_at=now,
            stale_after=now,
        )

    def invalidate(self, source_id: str | None = None, key: str | None = None) -> int:
        """Drop entries. Stage 5 SSE calls invalidate(source_id=...) on events."""
        removed = 0
        if key is not None:
            self._entries.pop(key, None)
            removed = 1
        elif source_id is not None:
            for k in [k for k, e in self._entries.items() if e.meta.get("source_id") == source_id]:
                self._entries.pop(k, None)
                removed += 1
        else:
            removed = len(self._entries)
            self._entries.clear()
        return removed

    def snapshot(self) -> dict[str, Any]:
        return {
            "entries": len(self._entries),
            "inflight": len(self._inflight),
            "sources_touched": sorted(self._source_touched),
        }

    # -- internals --------------------------------------------------------
    def _start_refresh(
        self, key: str, source_id: str, fingerprint: str | None, fetch: Fetcher
    ) -> Optional[asyncio.Task]:
        existing = self._inflight.get(key)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(self._refresh(key, source_id, fingerprint, fetch))
        self._inflight[key] = task
        task.add_done_callback(lambda t: self._inflight.pop(key, None))
        return task

    async def _refresh(
        self, key: str, source_id: str, fingerprint: str | None, fetch: Fetcher
    ) -> CacheEntry:
        async with self._sem:
            payload, meta = await fetch()
        now = time.time()
        entry = CacheEntry(
            key=key,
            payload=payload,
            meta=meta,
            fetched_at=now,
            stale_after=now + self._ttl,
            fingerprint=fingerprint,
        )
        entry.meta.setdefault("source_id", source_id)
        async with self._lock:
            self._entries[key] = entry
            self._source_touched.add(source_id)
        return entry
