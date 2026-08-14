// S7 route spec — the governance/integration/system tab modules.
// Route ids match the S6 locked registry (route-registry.test.js locked list);
// groups match §9.2. Endpoints/sources frozen to the u02/u02b/u01 evidence.
// proxy: 'adapter' | 'upstream' (BFF /api/upstream/<path>) | 'bff' | 'direct-iframe'

export const S7_ROUTE_SPEC = Object.freeze([
  // ---- GOVERN ----
  {
    id: 'issues', group: 'GOVERN', source: 'adapter',
    endpoints: ['/api/adapter/issues', '/api/adapter/issues/{id}'],
    proxy: 'adapter', readOnly: false,
    capabilityNote: 'transitions via POST /api/issues/{id}/update (adapter → agent_notes_db.py)',
  },
  {
    id: 'permits', group: 'GOVERN', source: 'adapter',
    endpoints: ['/api/adapter/permits', '/api/adapter/permits/{id}'],
    proxy: 'adapter', readOnly: false,
    capabilityNote: 'decisions via POST /api/permits/{id}/decision (adapter → permits_db.py)',
  },
  {
    id: 'room-binding', group: 'GOVERN', source: 'adapter',
    endpoints: ['/api/adapter/room-binding'],
    proxy: 'adapter', readOnly: true,
    capabilityNote: 'read-only in dashboard v1',
  },
  {
    id: 'threads', group: 'GOVERN', source: 'upstream',
    endpoints: ['/api/config'],
    proxy: 'upstream', readOnly: false,
    capabilityNote: 'per-thread toolset/skill/cross-thread policy via the config allow-tree',
  },
  {
    id: 'action-audit', group: 'GOVERN', source: 'bff',
    endpoints: ['/api/audit'],
    proxy: 'bff', readOnly: true,
    capabilityNote: 'append-only by BFF; UI read/export',
  },
  // ---- BUILD & INTEGRATE ----
  {
    id: 'skills', group: 'BUILD & INTEGRATE', source: 'upstream',
    endpoints: ['/api/skills', '/api/skills/content'], proxy: 'upstream', readOnly: true,
    capabilityNote: 'inventory + SKILL.md read; writes gated on meta.mutations_supported',
  },
  {
    id: 'memory', group: 'BUILD & INTEGRATE', source: 'bff',
    endpoints: ['/api/memory/MEMORY.md', '/api/memory/USER.md'], proxy: 'bff',
    readOnly: false,
    capabilityNote: 'editable local .hermes/memories sources: MEMORY.md and USER.md',
  },
  {
    id: 'profiles', group: 'BUILD & INTEGRATE', source: 'upstream',
    endpoints: ['/api/profiles'], proxy: 'upstream', readOnly: true,
    capabilityNote: 'read-only; profile switching is the shell switcher',
  },
  {
    id: 'models', group: 'BUILD & INTEGRATE', source: 'upstream',
    endpoints: ['/api/model/info', '/api/model/options'],
    proxy: 'upstream', readOnly: true,
    capabilityNote: 'read-only in dashboard v1',
  },
  {
    id: 'tools', group: 'BUILD & INTEGRATE', source: 'upstream',
    endpoints: ['/api/tools/toolsets'], proxy: 'upstream', readOnly: true,
    capabilityNote: 'read-only in dashboard v1; enabled-state badges only',
  },
  {
    id: 'mcp', group: 'BUILD & INTEGRATE', source: 'upstream',
    endpoints: ['/api/mcp/servers'], proxy: 'upstream', readOnly: true,
    capabilityNote: 'read-only in dashboard v1; capability badges per server',
  },
  {
    id: 'plugins', group: 'BUILD & INTEGRATE', source: 'upstream',
    endpoints: ['/api/dashboard/plugins'], proxy: 'upstream', readOnly: true,
    capabilityNote: 'read-only in dashboard v1; enabled/disabled state badges',
  },
  {
    id: 'webhooks', group: 'BUILD & INTEGRATE', source: 'upstream',
    endpoints: ['/api/webhooks'], proxy: 'upstream', readOnly: true,
    capabilityNote: 'read-only in dashboard v1',
  },
  {
    id: 'channels', group: 'BUILD & INTEGRATE', source: 'upstream',
    endpoints: ['/api/messaging/platforms'],
    proxy: 'upstream', readOnly: true,
    capabilityNote: 'read-only in dashboard v1; gateway_state via health/status',
  },
  {
    id: 'artifacts', group: 'BUILD & INTEGRATE', source: 'upstream+adapter',
    endpoints: ['/api/files', '/api/adapter/kanban/tasks/{id}/attachments'],
    proxy: 'upstream+adapter', readOnly: true,
    capabilityNote: 'read-only in dashboard v1; task deep links via adapter',
  },
  {
    id: 'files', group: 'BUILD & INTEGRATE', source: 'upstream',
    endpoints: ['/api/files'], proxy: 'upstream', readOnly: true,
    capabilityNote: 'read-only in dashboard v1; path guards mandatory',
  },
  // ---- SYSTEM ----
  {
    id: 'logs', group: 'SYSTEM', source: 'upstream',
    endpoints: ['/api/logs'], proxy: 'upstream', readOnly: true,
    capabilityNote: 'read/export only; bounded line counts',
  },
  {
    id: 'command-center', group: 'SYSTEM', source: 'upstream',
    endpoints: ['/api/ops/doctor', '/api/ops/security-audit', '/api/ops/backup', '/api/ops/import', '/api/actions/{name}/status'],
    proxy: 'upstream', readOnly: false,
    capabilityNote: 'run buttons only for verified GET/POST actions with confirm',
  },
  {
    id: 'settings', group: 'SYSTEM', source: 'upstream',
    endpoints: ['/api/config', '/api/config/defaults', '/api/config/schema'],
    proxy: 'upstream', readOnly: true,
    capabilityNote: 'read-only display; secrets redacted server-side',
  },
  {
    id: 'llama-proxy', group: 'SYSTEM', source: 'direct-iframe',
    endpoints: ['http://192.168.1.140:8082/dashboard'],
    proxy: 'direct-iframe', readOnly: true,
    capabilityNote: 'direct browser iframe to Llama Proxy',
  },
  {
    id: '9router', group: 'SYSTEM', source: 'direct-iframe',
    endpoints: ['http://192.168.1.140:20128/dashboard'],
    proxy: 'direct-iframe', readOnly: true,
    capabilityNote: 'direct browser iframe to 9router',
  },
]);

export function s7ById(id) {
  return S7_ROUTE_SPEC.find((r) => r.id === id) || null;
}
