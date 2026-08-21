// Shared live resource state for every Mission Control route.
//
// Operational data stays in RAM only. SQLite on the BFF is the persistent
// last-known-good layer; browser storage is deliberately not involved.

function resourceState() {
  return {
    revision: 0,
    entities: new Map(),
    snapshot: null,
    provenance: 'missing',
    fetchedAt: null,
    error: null,
    syncing: false,
    version: 0,
  };
}

function entityPayload(row) {
  if (!row || typeof row !== 'object') return null;
  return row.payload && typeof row.payload === 'object' ? row.payload : row;
}

export function createLiveResourceStore({
  api,
  schedule = (fn) => requestAnimationFrame(fn),
} = {}) {
  if (!api || typeof api.get !== 'function') throw new Error('live store requires api.get');

  const profiles = new Map();
  const inflight = new Map();
  const controllers = new Map();
  const requestSequence = new Map();
  const routeResources = new Map();
  const subscribers = new Set();
  let activeProfile = 'default';
  let activeRoute = 'overview';
  let notifyScheduled = false;

  function profileResources(profile = activeProfile) {
    if (!profiles.has(profile)) profiles.set(profile, new Map());
    return profiles.get(profile);
  }

  function getResource(key, profile = activeProfile) {
    const resources = profileResources(profile);
    if (!resources.has(key)) resources.set(key, resourceState());
    return resources.get(key);
  }

  function notify() {
    if (notifyScheduled) return;
    notifyScheduled = true;
    schedule(() => {
      notifyScheduled = false;
      for (const subscription of subscribers) {
        let next;
        try { next = subscription.selector(apiView()); } catch (_err) { continue; }
        if (subscription.equals(subscription.value, next)) continue;
        subscription.value = next;
        try { subscription.callback(next); } catch (err) { console.error('live store subscriber error', err); }
      }
    });
  }

  function applyResource(key, incoming, profile = activeProfile) {
    if (!incoming || typeof incoming !== 'object') return false;
    const state = getResource(key, profile);
    const revision = Number(incoming.revision || 0);
    if (revision < state.revision) return false;
    if (incoming.provenance === 'unchanged') {
      const changed = state.syncing;
      state.syncing = false;
      return changed;
    }

    const nextEntities = new Map();
    for (const row of Array.isArray(incoming.entities) ? incoming.entities : []) {
      const id = row?.entity_id;
      const payload = entityPayload(row);
      if (id == null || !payload) continue;
      nextEntities.set(String(id), {
        payload,
        revision: Number(row.revision ?? revision),
      });
    }
    state.entities = nextEntities;
    state.snapshot = incoming.snapshot ?? null;
    state.revision = revision;
    state.provenance = incoming.provenance || 'live';
    state.fetchedAt = incoming.fetched_at || null;
    state.error = incoming.last_error || null;
    state.syncing = false;
    state.version += 1;
    return true;
  }

  function abortObsolete(profile, route) {
    for (const [key, controller] of controllers) {
      if (key !== `${profile}:${route}`) {
        controller.abort();
        controllers.delete(key);
      }
    }
  }

  function setContext(profile, route) {
    activeProfile = profile || 'default';
    activeRoute = route || 'overview';
    abortObsolete(activeProfile, activeRoute);
  }

  async function hydrate(route = activeRoute, profile = activeProfile, { force = false } = {}) {
    setContext(profile, route);
    const key = `${profile}:${route}`;
    if (!force && inflight.has(key)) return inflight.get(key);
    if (force) controllers.get(key)?.abort();
    const controller = new AbortController();
    controllers.set(key, controller);
    const sequence = (requestSequence.get(key) || 0) + 1;
    requestSequence.set(key, sequence);

    const promise = api.get(`/api/live/bootstrap?route=${encodeURIComponent(route)}`, {
      profile, signal: controller.signal,
    }).then((response) => {
      if (requestSequence.get(key) !== sequence || activeProfile !== profile) return null;
      const resources = response?.data?.resources || {};
      routeResources.set(key, new Set(Object.keys(resources)));
      let changed = false;
      for (const [resourceKey, resource] of Object.entries(resources)) {
        changed = applyResource(resourceKey, resource, profile) || changed;
      }
      if (changed) notify();
      return response?.data || null;
    }).catch((error) => {
      if (error?.name === 'AbortError') return null;
      // Never erase last-known-good. Only freshness/error state changes.
      for (const resourceKey of routeResources.get(key) || []) {
        const state = getResource(resourceKey, profile);
        state.syncing = false;
        state.error = String(error?.message || error);
        if (state.entities.size || state.snapshot != null) state.provenance = 'stale';
        state.version += 1;
      }
      notify();
      return null;
    }).finally(() => {
      if (inflight.get(key) === promise) inflight.delete(key);
      if (controllers.get(key) === controller) controllers.delete(key);
    });
    inflight.set(key, promise);
    return promise;
  }

  async function resyncResource(resourceKey, profile = activeProfile, { force = false } = {}) {
    const state = getResource(resourceKey, profile);
    const key = `${profile}:resource:${resourceKey}`;
    if (!force && inflight.has(key)) return inflight.get(key);
    if (force) controllers.get(key)?.abort();
    state.syncing = true;
    notify();
    const controller = new AbortController();
    controllers.set(key, controller);
    const sequence = (requestSequence.get(key) || 0) + 1;
    requestSequence.set(key, sequence);
    const after = force ? 0 : state.revision;
    const promise = api.get(
      `/api/live/resource/${encodeURIComponent(resourceKey)}?after_revision=${after}`,
      { profile, signal: controller.signal },
    ).then((response) => {
      if (requestSequence.get(key) !== sequence) return null;
      if (applyResource(resourceKey, response?.data, profile)) notify();
      else { state.syncing = false; notify(); }
      return response?.data || null;
    }).catch((error) => {
      if (error?.name !== 'AbortError') {
        state.syncing = false;
        state.error = String(error?.message || error);
        if (state.entities.size || state.snapshot != null) state.provenance = 'stale';
        notify();
      }
      return null;
    }).finally(() => {
      if (inflight.get(key) === promise) inflight.delete(key);
      if (controllers.get(key) === controller) controllers.delete(key);
    });
    inflight.set(key, promise);
    return promise;
  }

  function applyEvent(event) {
    if (!event || typeof event !== 'object' || !event.resource_key) return false;
    const profile = event.profile_id || activeProfile;
    if (event.profile_id && event.profile_id !== activeProfile) return false;
    if (event.operation === 'resync-required') {
      if (event.resource_key === '*') hydrate(activeRoute, profile, { force: true });
      else resyncResource(event.resource_key, profile, { force: true });
      return true;
    }
    const state = getResource(event.resource_key, profile);
    const revision = Number(event.revision || 0);
    const entityId = event.entity_id == null ? '' : String(event.entity_id);
    const existing = entityId ? state.entities.get(entityId) : null;
    if (entityId && existing && revision && revision <= existing.revision) return false;
    if (!entityId && revision && revision < state.revision) return false;

    if (event.operation === 'upsert' && entityId) {
      state.entities.set(entityId, { payload: event.payload || {}, revision });
    } else if (event.operation === 'delete' && entityId) {
      state.entities.delete(entityId);
    } else if (event.operation === 'replace-summary') {
      state.snapshot = event.payload || {};
    } else if (event.operation === 'invalidate') {
      state.provenance = state.entities.size || state.snapshot != null ? 'stale' : 'missing';
      resyncResource(event.resource_key, profile).catch(() => null);
    } else {
      return false;
    }
    state.revision = Math.max(state.revision, revision);
    state.provenance = event.operation === 'invalidate' ? state.provenance : 'live';
    state.error = null;
    state.version += 1;
    notify();
    return true;
  }

  function forceResync(route = activeRoute, profile = activeProfile) {
    // Existing state remains visible while the local snapshot is re-read.
    for (const resourceKey of routeResources.get(`${profile}:${route}`) || []) {
      getResource(resourceKey, profile).syncing = true;
    }
    notify();
    return hydrate(route, profile, { force: true });
  }

  function select(resourceKey, selector = (value) => value, profile = activeProfile) {
    return selector(getResource(resourceKey, profile));
  }

  function subscribe(selector, callback, { equals = Object.is } = {}) {
    const subscription = { selector, callback, equals, value: selector(apiView()) };
    subscribers.add(subscription);
    return () => subscribers.delete(subscription);
  }

  function apiView() {
    return {
      profile: activeProfile,
      route: activeRoute,
      resource: (key, profile = activeProfile) => getResource(key, profile),
      entities: (key, profile = activeProfile) => [...getResource(key, profile).entities.values()].map((row) => row.payload),
    };
  }

  function dispose() {
    for (const controller of controllers.values()) controller.abort();
    controllers.clear();
    inflight.clear();
    subscribers.clear();
  }

  return {
    setContext, hydrate, resyncResource, forceResync, applyEvent,
    select, subscribe, view: apiView, dispose,
    get profile() { return activeProfile; },
    get route() { return activeRoute; },
  };
}
