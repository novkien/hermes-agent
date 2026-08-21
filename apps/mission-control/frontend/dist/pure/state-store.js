// In-memory per-route state store with LRU keep-alive.
// Contract: no secrets / operational records in Web Storage — this store is
// memory-only by design; localStorage is reserved for pure UI prefs elsewhere.

const REDACT_KEYS = /token|secret|password|key|authorization|credential|cookie/i;

function redact(value) {
  if (value === null || value === undefined) return value;
  if (typeof value === 'string') return REDACT_KEYS.test(value) ? '[redacted]' : value;
  if (Array.isArray(value)) return value.map(redact);
  if (typeof value === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(value)) out[k] = REDACT_KEYS.test(k) ? '[redacted]' : redact(v);
    return out;
  }
  return value;
}

export function createStateStore({ maxRoutes = 8 } = {}) {
  const map = new Map(); // route -> state

  function touch(route) {
    const value = map.get(route);
    if (value !== undefined) {
      map.delete(route);
      map.set(route, value);
    }
  }

  function evictIfNeeded() {
    while (map.size > maxRoutes) {
      const oldest = map.keys().next().value;
      if (oldest === undefined) break;
      map.delete(oldest);
    }
  }

  return {
    save(route, state) {
      // Serialize through JSON to drop live references; redact secret-like values.
      const safe = redact(state);
      map.set(route, safe);
      evictIfNeeded();
    },
    restore(route) {
      touch(route);
      return map.get(route);
    },
    exists(route) {
      return map.has(route);
    },
    clear() {
      map.clear();
    },
    get size() {
      return map.size;
    },
  };
}

// Returns an empty object for routes never saved; evicted routes are undefined.
export function restoreOrEmpty(store, route) {
  if (!store.exists(route)) return {};
  return store.restore(route) || {};
}
