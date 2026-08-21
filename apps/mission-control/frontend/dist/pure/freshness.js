// Freshness derivation from the normalized source envelope meta.
// The UI must render freshness/degraded/unsupported states from meta — never
// infer from a 200 alone.

export const CACHE_FRESHNESS = Object.freeze({
  LIVE: 'live',
  FRESH: 'fresh',
  STALE: 'stale',
  UNAVAILABLE: 'unavailable',
  UNSUPPORTED: 'unsupported',
  PARTIAL: 'partial',
});

const KNOWN = new Set(Object.values(CACHE_FRESHNESS));

export function freshnessFromMeta(meta) {
  if (!meta || typeof meta !== 'object') return CACHE_FRESHNESS.UNAVAILABLE;
  const f = meta.freshness;
  return KNOWN.has(f) ? f : CACHE_FRESHNESS.PARTIAL;
}

// stale_after = fetched_at + ttlSeconds; null ttl means never stale.
export function staleBy(fetchedAt, ttlSeconds) {
  if (!fetchedAt || ttlSeconds === null || ttlSeconds === undefined) return null;
  const t = new Date(fetchedAt).getTime() + ttlSeconds * 1000;
  return new Date(t).toISOString().replace(/\.000Z$/, 'Z');
}

export function isStale(meta, now = Date.now()) {
  if (!meta) return true;
  if (meta.freshness === 'live' || meta.freshness === 'fresh') return false;
  if (meta.freshness === 'stale') return true;
  if (meta.stale_after) return new Date(meta.stale_after).getTime() < now;
  return false;
}
