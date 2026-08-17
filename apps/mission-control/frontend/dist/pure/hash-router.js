// Pure hash-router helpers.
// Canonical URL contract:
//   /?profile=<id>#/<route>?<entity-or-filter-params>
// `profile` belongs only to the document query. A legacy hash profile is read
// once for compatibility and removed when the URL is canonicalized.

const DEFAULT_PROFILE = 'default';
const FALLBACK_PATH = '/overview';

function normalizePath(path) {
  const raw = String(path || FALLBACK_PATH).trim();
  if (!raw || raw === '/') return FALLBACK_PATH;
  return raw.startsWith('/') ? raw : `/${raw}`;
}

export function parseHash(hash = '') {
  let value = String(hash || '');
  if (value.startsWith('#')) value = value.slice(1);
  if (!value || value === '/') return { path: FALLBACK_PATH, params: {} };

  const question = value.indexOf('?');
  const pathPart = question === -1 ? value : value.slice(0, question);
  const queryPart = question === -1 ? '' : value.slice(question + 1);
  const params = {};
  const query = new URLSearchParams(queryPart);
  for (const [key, item] of query.entries()) params[key] = item;
  return { path: normalizePath(pathPart), params };
}

export function buildHash(path, params = {}) {
  const query = new URLSearchParams();
  for (const key of Object.keys(params || {}).sort()) {
    if (key === 'profile') continue;
    const value = params[key];
    if (value === undefined || value === null || value === '') continue;
    query.set(key, String(value));
  }
  const suffix = query.toString();
  return `#${normalizePath(path)}${suffix ? `?${suffix}` : ''}`;
}

export function parseRouteWithProfile(hash, search = '') {
  const route = parseHash(hash);
  const urlParams = new URLSearchParams(String(search || ''));
  const legacyHashProfile = route.params.profile || null;
  delete route.params.profile;
  const profile = urlParams.get('profile') || legacyHashProfile || DEFAULT_PROFILE;
  return { ...route, profile };
}

/**
 * Normalize the retired Run Inspector route into Kanban's analysis subview.
 *
 * The route is intentionally handled at the hash boundary rather than kept in
 * the route registry: bookmarks continue to work, but the retired surface
 * cannot reappear in navigation, the palette, or a cached tab instance.
 */
export function redirectLegacyRunInspector(route) {
  if (!route || route.path !== '/run-inspector') return route;
  const params = { view: 'inspect' };
  // Match the old inspector's precedence: task wins if a malformed link has
  // both entity types. Do not carry arbitrary legacy query state into the
  // Kanban view.
  if (route.params?.task) params.task = route.params.task;
  else if (route.params?.session) params.session = route.params.session;
  return { ...route, path: '/kanban', params };
}

export function buildDeepLink(path, params = {}, inspectorState = {}) {
  const merged = { ...(params || {}) };
  if (inspectorState && inspectorState.selected) merged.selected = inspectorState.selected;
  return buildHash(path, merged);
}

export { DEFAULT_PROFILE, FALLBACK_PATH, normalizePath };
