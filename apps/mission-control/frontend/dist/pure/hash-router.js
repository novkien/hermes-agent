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

export function buildDeepLink(path, params = {}, inspectorState = {}) {
  const merged = { ...(params || {}) };
  if (inspectorState && inspectorState.selected) merged.selected = inspectorState.selected;
  return buildHash(path, merged);
}

export { DEFAULT_PROFILE, FALLBACK_PATH, normalizePath };
