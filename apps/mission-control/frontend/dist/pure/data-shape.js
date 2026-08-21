// Runtime contract normalization for AgentOS Mission Control.
//
// The BFF is the canonical envelope owner, but this module deliberately accepts
// the legacy double-envelope shapes observed in Wave 2 so a browser refresh
// remains safe during deployment and stale assets cannot crash a tab.

const STANDARD_LIST_KEYS = Object.freeze([
  'items', 'rows', 'list', 'results', 'records', 'entries',
  'tasks', 'sessions', 'messages', 'events', 'runs', 'attachments',
  'alerts', 'skills', 'plugins', 'permits', 'issues', 'profiles', 'lines',
]);

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function unwrapPayload(value, maxDepth = 4) {
  let current = value;
  let depth = 0;
  while (depth < maxDepth && isObject(current)
    && Object.prototype.hasOwnProperty.call(current, 'data')
    && (Object.prototype.hasOwnProperty.call(current, 'meta') || Object.keys(current).length === 1)) {
    current = current.data;
    depth += 1;
  }
  return current;
}

export function listFrom(value, preferredKeys = []) {
  const raw = unwrapPayload(value);
  if (Array.isArray(raw)) return raw;
  if (!isObject(raw)) return [];

  const keys = [...preferredKeys, ...STANDARD_LIST_KEYS];
  const seen = new Set();
  for (const key of keys) {
    if (seen.has(key) || !Object.prototype.hasOwnProperty.call(raw, key)) continue;
    seen.add(key);
    const candidate = unwrapPayload(raw[key]);
    if (Array.isArray(candidate)) return candidate;
    if (isObject(candidate)) {
      for (const nestedKey of keys) {
        const nested = unwrapPayload(candidate[nestedKey]);
        if (Array.isArray(nested)) return nested;
      }
    }
  }
  return [];
}

export function recordFrom(value, preferredKeys = []) {
  const raw = unwrapPayload(value);
  if (!isObject(raw)) return null;
  for (const key of preferredKeys) {
    const candidate = unwrapPayload(raw[key]);
    if (isObject(candidate)) return candidate;
  }
  return raw;
}

export function sessionRows(value) {
  return listFrom(value, ['sessions']);
}

export function taskRows(value) {
  return listFrom(value, ['tasks']);
}

export function alertRows(value) {
  return listFrom(value, ['alerts']);
}

export function profileRows(value) {
  return listFrom(value, ['profiles']);
}

export function capabilityRegistry(value) {
  const raw = unwrapPayload(value);
  if (!isObject(raw)) return {};
  const sources = unwrapPayload(raw.sources);
  return isObject(sources) ? sources : raw;
}

export function summarizeSourceHealth(value, {
  required = ['adapter', 'hermes-dashboard'],
  optional = ['hermes-gateway', 'cron'],
} = {}) {
  const sources = capabilityRegistry(value);
  const missingRequired = [];
  const unhealthyRequired = [];
  const unhealthyOptional = [];

  for (const id of required) {
    const source = sources[id];
    if (!isObject(source)) missingRequired.push(id);
    else if (source.healthy !== true) unhealthyRequired.push(id);
  }
  for (const id of optional) {
    const source = sources[id];
    if (isObject(source) && source.healthy !== true) unhealthyOptional.push(id);
  }

  let state = 'healthy';
  if (missingRequired.length || unhealthyRequired.length) state = 'down';
  else if (unhealthyOptional.length) state = 'degraded';

  return {
    state,
    sources,
    missingRequired,
    unhealthyRequired,
    unhealthyOptional,
    unhealthyCount: missingRequired.length + unhealthyRequired.length + unhealthyOptional.length,
  };
}

export function pulseRecord(value) {
  return recordFrom(value) || {};
}

export function logLines(value) {
  return listFrom(value, ['lines', 'logs']).map((line) => {
    if (typeof line === 'string') return line;
    if (line === null || line === undefined) return '';
    if (isObject(line)) {
      const ts = line.timestamp ?? line.ts ?? line.time ?? '';
      const level = line.level ?? line.severity ?? '';
      const message = line.message ?? line.msg ?? JSON.stringify(line);
      return [ts, level, message].filter(Boolean).join(' ');
    }
    return String(line);
  });
}

export function errorMessage(payload, fallback = 'Request failed') {
  if (!payload) return fallback;
  const error = payload.error;
  if (typeof error === 'string' && error) return error;
  if (isObject(error)) return error.message || error.detail || error.code || fallback;
  if (typeof payload.detail === 'string' && payload.detail) return payload.detail;
  if (isObject(payload.detail)) return payload.detail.message || payload.detail.detail || fallback;
  return fallback;
}
