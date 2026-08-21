// Adapter between the shared in-RAM resource store and legacy tab renderers.
// Tabs still keep their view/filter state; operational rows come from here.

export function liveMeta(state, sourceId = 'mission-control-read-model') {
  const freshness = state?.provenance === 'stale' ? 'stale'
    : state?.provenance === 'missing' ? 'unavailable' : 'fresh';
  return {
    freshness,
    source_id: sourceId,
    revision: Number(state?.revision || 0),
    fetched_at: state?.fetchedAt || null,
    last_error: state?.error || null,
    syncing: state?.syncing === true,
  };
}

export function liveRows(liveStore, resourceKey, profile, normalize = (row) => row) {
  if (!liveStore) return null;
  const state = liveStore.select(resourceKey, (value) => value, profile);
  if (!state || (state.provenance === 'missing' && state.revision === 0)) return null;
  return {
    state,
    rows: [...state.entities.values()].map((entry) => normalize(entry.payload)).filter(Boolean),
    meta: liveMeta(state),
  };
}

export function liveSummary(liveStore, resourceKey, profile) {
  if (!liveStore) return null;
  const state = liveStore.select(resourceKey, (value) => value, profile);
  if (!state || (state.provenance === 'missing' && state.revision === 0)) return null;
  return { state, data: state.snapshot || {}, meta: liveMeta(state) };
}

export function bindLiveResources(liveStore, resourceKeys, profile, callback) {
  if (!liveStore) return null;
  const keys = [...new Set(resourceKeys)];
  return liveStore.subscribe(
    (view) => keys.map((key) => view.resource(key, profile).version).join(':'),
    () => callback(keys),
  );
}

export function mergeProjectedRows(current, projected, idOf) {
  const previous = new Map((current || []).map((row) => [String(idOf(row)), row]));
  return projected.map((row) => ({ ...(previous.get(String(idOf(row))) || {}), ...row }));
}
