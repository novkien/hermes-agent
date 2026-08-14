// Route registry: every locked surface from §9.2 + profile scoping.
// Primary tabs (S6) render real modules; S7 surfaces are placeholders with
// route stubs that Stage 7 fills in — but every item stays reachable.

const GROUPS = ['OPERATE', 'GOVERN', 'BUILD & INTEGRATE', 'SYSTEM'];

function route(key, label, { group, path, placeholder = false, profileScoped = true, readOnly = true, module = null } = {}) {
  return { key, label, group, path: path || `/${key}`, placeholder, profileScoped, readOnly, module };
}

export const ROUTES = Object.freeze({
  // OPERATE — primary tabs implemented in Stage 6
  overview: route('overview', 'Overview', { group: 'OPERATE', module: 'tabs/overview.js' }),
  chat: route('chat', 'Chat', { group: 'OPERATE', module: 'tabs/chat.js' }),
  sessions: route('sessions', 'Sessions', { group: 'OPERATE', module: 'tabs/sessions.js' }),
  fleet: route('fleet', 'Fleet / Topology', { group: 'OPERATE', module: 'tabs/fleet.js' }),
  kanban: route('kanban', 'Kanban', { group: 'OPERATE', module: 'tabs/kanban.js' }),
  'run-inspector': route('run-inspector', 'Run Inspector', { group: 'OPERATE', module: 'tabs/run-inspector.js' }),
  cron: route('cron', 'Cron', { group: 'OPERATE', module: 'tabs/cron.js' }),
  activity: route('activity', 'Activity', { group: 'OPERATE', module: 'tabs/activity.js' }),
  alerts: route('alerts', 'Alerts', { group: 'OPERATE', module: 'tabs/alerts.js' }),
  analytics: route('analytics', 'Analytics / Spend', { group: 'OPERATE', module: 'tabs/analytics.js' }),

  // GOVERN — Stage 7 placeholders
  issues: route('issues', 'Issues', { group: 'GOVERN', placeholder: true }),
  permits: route('permits', 'Permits', { group: 'GOVERN', placeholder: true }),
  'room-binding': route('room-binding', 'Room Binding', { group: 'GOVERN', placeholder: true }),
  threads: route('threads', 'Threads', { group: 'GOVERN', module: 'tabs/threads.js', readOnly: false }),
  'action-audit': route('action-audit', 'Action Audit', { group: 'GOVERN', placeholder: true }),

  // BUILD & INTEGRATE — Stage 7 placeholders
  skills: route('skills', 'Skills', { group: 'BUILD & INTEGRATE', placeholder: true }),
  memory: route('memory', 'Memory', { group: 'BUILD & INTEGRATE', placeholder: true }),
  profiles: route('profiles', 'Profiles', { group: 'BUILD & INTEGRATE', placeholder: true }),
  models: route('models', 'Models', { group: 'BUILD & INTEGRATE', placeholder: true }),
  tools: route('tools', 'Tools / Toolsets', { group: 'BUILD & INTEGRATE', placeholder: true }),
  mcp: route('mcp', 'MCP', { group: 'BUILD & INTEGRATE', placeholder: true }),
  plugins: route('plugins', 'Plugins', { group: 'BUILD & INTEGRATE', placeholder: true }),
  webhooks: route('webhooks', 'Webhooks', { group: 'BUILD & INTEGRATE', placeholder: true }),
  channels: route('channels', 'Channels / Messaging', { group: 'BUILD & INTEGRATE', placeholder: true }),
  artifacts: route('artifacts', 'Artifacts', { group: 'BUILD & INTEGRATE', placeholder: true }),
  files: route('files', 'Files', { group: 'BUILD & INTEGRATE', placeholder: true }),

  // SYSTEM — Stage 7 placeholders + native System Manager surface
  'system-manager': route('system-manager', 'System Manager', {
    group: 'SYSTEM', module: 'tabs/system-manager.js', readOnly: false,
  }),
  logs: route('logs', 'Logs', { group: 'SYSTEM', placeholder: true }),
  'command-center': route('command-center', 'Command Center', { group: 'SYSTEM', placeholder: true }),
  settings: route('settings', 'Settings / System', { group: 'SYSTEM', placeholder: true }),
  'llama-proxy': route('llama-proxy', 'llama-proxy', { group: 'SYSTEM', placeholder: true }),
  '9router': route('9router', '9router', { group: 'SYSTEM', placeholder: true }),
});

export function navGroups() {
  const groups = {};
  for (const g of GROUPS) groups[g] = [];
  for (const r of Object.values(ROUTES)) groups[r.group].push(r.key);
  return groups;
}

export function routesForGroup(group) {
  return navGroups()[group] || [];
}

export function allRouteKeys() {
  return Object.keys(ROUTES);
}
