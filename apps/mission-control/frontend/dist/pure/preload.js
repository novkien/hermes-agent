// Staged preload primitives — pure logic (unit-testable in node).
// (1) staged load, (2) idle code prefetch, (3) summary prefetch <=4 concurrent,
// (4) heavy-on-demand, (5) cache per profile/route/filter/source-fingerprint
// with stale-while-revalidate, (6) cancel obsolete in-flight, (7) single-flight.

export function createPreloadQueue({ maxConcurrent = 4 } = {}) {
  let active = 0;
  const waiting = [];
  const inFlight = new Map();

  function pump() {
    while (active < maxConcurrent && waiting.length > 0) {
      const { key, fn, resolve, reject } = waiting.shift();
      active += 1;
      fn().then(resolve, reject).finally(() => {
        active -= 1;
        inFlight.delete(key);
        pump();
      });
    }
  }

  return {
    push(key, fn) {
      if (inFlight.has(key)) {
        return Promise.reject(new Error(`already in flight: ${key}`));
      }
      inFlight.set(key, true);
      return new Promise((resolve, reject) => {
        waiting.push({ key, fn, resolve, reject });
        pump();
      });
    },
    get active() {
      return active;
    },
    get pending() {
      return waiting.length;
    },
  };
}

export function createCache({ maxEntries = 16 } = {}) {
  const map = new Map();

  function touch(key) {
    const v = map.get(key);
    if (v !== undefined) {
      map.delete(key);
      map.set(key, v);
    }
  }

  function evict() {
    while (map.size > maxEntries) {
      const oldest = map.keys().next().value;
      if (oldest === undefined) break;
      map.delete(oldest);
    }
  }

  return {
    set(key, value) {
      map.set(key, value);
      evict();
    },
    get(key) {
      touch(key);
      return map.get(key);
    },
    has(key) {
      return map.has(key);
    },
    async swr(key, fetchFn, { revalidate = true } = {}) {
      return swr(this, key, fetchFn, { revalidate });
    },
    delete(key) {
      map.delete(key);
    },
    clear() {
      map.clear();
    },
    get size() {
      return map.size;
    },
  };
}

// stale-while-revalidate: serve cached immediately, refresh in background.
export async function swr(cache, key, fetchFn, { revalidate = true } = {}) {
  const cached = cache.get(key);
  const isFreshCache =
    cached && cached.meta && (cached.meta.freshness === 'live' || cached.meta.freshness === 'fresh');
  if (cached && !revalidate) {
    return { value: cached, refreshed: null };
  }
  if (cached && isFreshCache) {
    return { value: cached, refreshed: null };
  }
  const fresh = await fetchFn();
  cache.set(key, fresh);
  return { value: cached || fresh, refreshed: fresh };
}

export function singleFlight() {
  const flights = new Map();
  return {
    run(key, fn) {
      if (flights.has(key)) return flights.get(key);
      const p = Promise.resolve().then(fn).finally(() => {
        flights.delete(key);
      });
      flights.set(key, p);
      return p;
    },
  };
}

// Only allow prefetch for known non-sensitive sources.
const PREFETCHABLE_SOURCES = new Set([
  'hermes-api', 'gateway-api', 'kanban-db', 'permits-db', 'issues-store',
  'room-binding', 'dashboard-local',
]);

export function isPrefetchable(payload) {
  if (!payload || typeof payload !== 'object') return false;
  const sourceId = payload.source_id || (payload.meta && payload.meta.source_id);
  return PREFETCHABLE_SOURCES.has(sourceId);
}
