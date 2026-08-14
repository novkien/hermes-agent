// Route inventory builder — every locked surface from §9.2 with reachability.
// Used for evidence (route inventory) and command palette completeness.

export const ALL_LOCKED_KEYS = Object.freeze([
  // OPERATE
  'overview', 'chat', 'sessions', 'fleet', 'kanban', 'run-inspector', 'cron',
  'activity', 'alerts', 'analytics',
  // GOVERN
  'issues', 'permits', 'room-binding', 'threads', 'action-audit',
  // BUILD & INTEGRATE
  'skills', 'memory', 'profiles', 'models', 'tools', 'mcp',
  'plugins', 'webhooks', 'channels', 'artifacts', 'files',
  // SYSTEM
  'logs', 'command-center', 'settings', 'llama-proxy',
  '9router',
]);

// Stage-7 routes (the modules this card implements) mapped to their modules.
const S7_MODULES = Object.freeze({
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

const GROUP_OF = Object.freeze({
  overview: 'OPERATE', chat: 'OPERATE', sessions: 'OPERATE', fleet: 'OPERATE',
  kanban: 'OPERATE', 'run-inspector': 'OPERATE', cron: 'OPERATE',
  activity: 'OPERATE', alerts: 'OPERATE', analytics: 'OPERATE',
  issues: 'GOVERN', permits: 'GOVERN', 'room-binding': 'GOVERN', threads: 'GOVERN',
  'action-audit': 'GOVERN',
  skills: 'BUILD & INTEGRATE', memory: 'BUILD & INTEGRATE', profiles: 'BUILD & INTEGRATE',
  models: 'BUILD & INTEGRATE', tools: 'BUILD & INTEGRATE', mcp: 'BUILD & INTEGRATE',
  plugins: 'BUILD & INTEGRATE', webhooks: 'BUILD & INTEGRATE', channels: 'BUILD & INTEGRATE',
  artifacts: 'BUILD & INTEGRATE', files: 'BUILD & INTEGRATE',
  logs: 'SYSTEM', 'command-center': 'SYSTEM', settings: 'SYSTEM', 'llama-proxy': 'SYSTEM',
  '9router': 'SYSTEM',
});

export function buildRouteInventory() {
  return ALL_LOCKED_KEYS.map((key) => {
    const module = S7_MODULES[key] || null;
    const isS7 = module !== null;
    return {
      key,
      group: GROUP_OF[key],
      path: `/${key}`,
      stage: isS7 ? 7 : 6,
      module,
      placeholder: !isS7,
      reachable: true,
    };
  });
}
