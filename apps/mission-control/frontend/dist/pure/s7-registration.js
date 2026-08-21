// S7 tab registration — fills the placeholder routes S6 registered with real
// modules. Pure (no DOM): consumes the S6 route-registry contract
// { ROUTES, route() } and returns a registry map with the S7 modules wired in.
// Registration keeps every locked surface reachable (AC-02).

import { S7_ROUTE_SPEC } from './s7-route-spec.js';

// module path for each S7 route (matches src/tabs/*.js)
const MODULE_BY_ID = Object.freeze({
  issues: 'tabs/issues.js',
  permits: 'tabs/permits.js',
  'room-binding': 'tabs/room-binding.js',
  threads: 'tabs/threads.js',
  'action-audit': 'tabs/action-audit.js',
  skills: 'tabs/skills.js',
  memory: 'tabs/memory.js',
  profiles: 'tabs/profiles.js',
  models: 'tabs/models.js',
  tools: 'tabs/tools.js',
  mcp: 'tabs/mcp.js',
  plugins: 'tabs/plugins.js',
  webhooks: 'tabs/webhooks.js',
  channels: 'tabs/channels.js',
  artifacts: 'tabs/artifacts.js',
  files: 'tabs/files.js',
  logs: 'tabs/logs.js',
  'command-center': 'tabs/command-center.js',
  settings: 'tabs/settings.js',
  'llama-proxy': 'tabs/llama-proxy.js',
  '9router': 'tabs/9router.js',
});

/**
 * registerS7Routes(registry) — mutate a copy of the S6 registry object,
 * replacing placeholder entries with real module-backed routes.
 * Returns { registry, s7Keys, remainingPlaceholders }.
 */
export function registerS7Routes(registry) {
  const out = { ...registry };
  const s7Keys = [];
  for (const spec of S7_ROUTE_SPEC) {
    const key = spec.id;
    if (!out[key]) {
      throw new Error(`S7 route ${key} missing from S6 registry`);
    }
    out[key] = {
      ...out[key],
      placeholder: false,
      module: MODULE_BY_ID[key],
      readOnly: spec.readOnly,
      group: spec.group,
      source: spec.source,
      endpoints: [...spec.endpoints],
      capabilityNote: spec.capabilityNote,
    };
    s7Keys.push(key);
  }
  const remainingPlaceholders = Object.values(out).filter((r) => r.placeholder).map((r) => r.key);
  return { registry: out, s7Keys, remainingPlaceholders };
}

export { MODULE_BY_ID };
