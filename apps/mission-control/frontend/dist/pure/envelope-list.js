// Shared pure renderer for upstream-proxied envelope lists (BUILD & INTEGRATE).
// Normalizes the envelope {data, meta} into render rows with explicit state:
// empty != unavailable != unsupported. Never infers capability from a 200.

export function stateFromMeta(meta, hasData) {
  if (!meta) return 'unavailable';
  if (meta.freshness === 'unavailable') return 'unavailable';
  if (meta.freshness === 'unsupported') return 'unsupported';
  if (!hasData) return meta.freshness === 'partial' ? 'partial' : 'empty';
  return meta.freshness === 'partial' ? 'partial' : 'ready';
}

/**
 * listRows(envelope, { pick }) — pick receives the raw payload and returns the
 * array of items (or undefined if the shape is unknown -> 'empty').
 */
export function listRows(envelope, { pick, map = (x) => x } = {}) {
  const meta = (envelope && envelope.meta) || null;
  const raw = (envelope && envelope.data) || null;

  const state = stateFromMeta(meta, !!raw);
  if (state === 'unavailable' || state === 'unsupported') {
    return { rows: [], meta, state };
  }
  if (!raw) return { rows: [], meta, state };

  const picked = pick ? pick(raw) : Array.isArray(raw) ? raw : raw.items || raw.list || null;
  if (!picked || !Array.isArray(picked) || picked.length === 0) {
    return { rows: [], meta, state: state === 'partial' ? 'partial' : 'empty' };
  }
  return { rows: picked.map(map), meta, state };
}

/** Normalize a single-record envelope (detail views). */
export function recordView(envelope, { map = (x) => x, pick = (raw) => raw } = {}) {
  const meta = (envelope && envelope.meta) || null;
  const raw = (envelope && envelope.data) || null;
  const hasData = raw !== null && raw !== undefined
    && !(typeof raw === 'object' && !Array.isArray(raw) && Object.keys(raw).length === 0);
  const state = stateFromMeta(meta, hasData);
  if (state === 'unavailable' || state === 'unsupported' || !hasData) {
    return { record: null, meta, state };
  }
  return { record: map(pick(raw)), meta, state: 'ready' };
}
