// AgentOS Mission Control SPA shell.
//
// Canonical contracts enforced here:
// - one profile parameter in the document query, never duplicated in hash;
// - one first-contact auth request before parallel bootstrap reads;
// - profile-bound tab instances are never reused across profiles;
// - operational status is derived from BFF data, never hard-coded;
// - every registered route resolves to a mountable tab implementation.

import { ROUTES, navGroups } from './pure/route-registry.js';
import { registerS7Routes } from './pure/s7-registration.js';
import { parseRouteWithProfile, buildHash, FALLBACK_PATH } from './pure/hash-router.js';
import { createStateStore, restoreOrEmpty } from './pure/state-store.js';
import { createPrefetch } from './preload.js';
import { createPalette } from './palette.js';
import { SseClient } from './events.js';
import { api } from './api.js';
import { readProfileFromLocation, setProfileInLocation, profileCachePrefix } from './profile.js';
import { el, clear, skeleton, emptyState, unavailableState } from './ui.js';
import { provenanceBadge } from './provenance.js';
import { icon, ROUTE_ICONS } from './icons.js';
import {
  alertRows,
  listFrom,
  pulseRecord,
  summarizeSourceHealth,
  taskRows,
} from './pure/data-shape.js';

const PRIMARY_MODULES = {
  overview: () => import('./tabs/overview.js').then((m) => m.createOverview),
  chat: () => import('./tabs/chat.js').then((m) => m.createChat),
  sessions: () => import('./tabs/sessions.js').then((m) => m.createSessions),
  fleet: () => import('./tabs/fleet.js').then((m) => m.createFleet),
  kanban: () => import('./tabs/kanban.js').then((m) => m.createKanban),
  'run-inspector': () => import('./tabs/run-inspector.js').then((m) => m.createRunInspector),
  cron: () => import('./tabs/cron.js').then((m) => m.createCron),
  activity: () => import('./tabs/activity.js').then((m) => m.createActivity),
  alerts: () => import('./tabs/alerts.js').then((m) => m.createAlerts),
  analytics: () => import('./tabs/analytics.js').then((m) => m.createAnalytics),
  'system-manager': () => import('./tabs/system-manager.js').then((m) => m.createSystemManager),
};

const REGISTRY = registerS7Routes(ROUTES).registry;

// Routes that are a full-height application pane rather than a document: they
// own an inner scroller and must never make the workspace column scroll too.
// A second, outer scrollbar is not just cosmetic — a wheel that reaches the
// end of the inner scroller chains into it and drags the whole pane out of
// view, which is what used to knock the chat transcript off its bottom.
const FIXED_PANE_ROUTES = new Set(['memory', 'chat']);
const S7_LOADERS = {
  issues: () => import('./tabs/issues.js').then((m) => m.createIssues || (() => s7TabAdapter(m, m.renderIssuesList, 'issues'))),
  permits: () => import('./tabs/permits.js').then((m) => m.createPermits || (() => s7TabAdapter(m, m.renderPermitsList, 'permits'))),
  'room-binding': () => import('./tabs/room-binding.js').then((m) => m.createRoomBinding || (() => s7TabAdapter(m, m.renderRoomBinding, 'room-binding'))),
  threads: () => import('./tabs/threads.js').then((m) => m.createThreads),
  'action-audit': () => import('./tabs/action-audit.js').then((m) => m.createActionAudit || (() => s7TabAdapter(m, m.renderAudit, 'action-audit'))),
  skills: () => import('./tabs/skills.js').then((m) => m.createSkills || (() => s7TabAdapter(m, m.renderSkills, 'skills'))),
  memory: () => import('./tabs/memory.js').then((m) => m.createMemory || (() => s7TabAdapter(m, m.renderMemory, 'memory'))),
  profiles: () => import('./tabs/profiles.js').then((m) => m.createProfiles || (() => s7TabAdapter(m, m.renderProfiles, 'profiles'))),
  models: () => import('./tabs/models.js').then((m) => m.createModels || (() => s7TabAdapter(m, m.renderModelInfo, 'models'))),
  tools: () => import('./tabs/tools.js').then((m) => m.createTools || (() => s7TabAdapter(m, m.renderToolsets, 'tools'))),
  mcp: () => import('./tabs/mcp.js').then((m) => m.createMcp || (() => s7TabAdapter(m, m.renderMcpServers, 'mcp'))),
  plugins: () => import('./tabs/plugins.js').then((m) => m.createPlugins || (() => s7TabAdapter(m, m.renderPlugins, 'plugins'))),
  webhooks: () => import('./tabs/webhooks.js').then((m) => m.createWebhooks || (() => s7TabAdapter(m, m.renderWebhooks, 'webhooks'))),
  channels: () => import('./tabs/channels.js').then((m) => m.createChannels || (() => s7TabAdapter(m, m.renderChannels, 'channels'))),
  artifacts: () => import('./tabs/artifacts.js').then((m) => m.createArtifacts || (() => s7TabAdapter(m, m.renderArtifacts, 'artifacts'))),
  files: () => import('./tabs/files.js').then((m) => m.createFiles || (() => s7TabAdapter(m, m.renderFiles, 'files'))),
  logs: () => import('./tabs/logs.js').then((m) => m.createLogs || (() => s7TabAdapter(m, m.renderLogs, 'logs'))),
  'command-center': () => import('./tabs/command-center.js').then((m) => m.createCommandCenter || (() => s7TabAdapter(m, m.commandCenterCapabilities, 'command-center'))),
  settings: () => import('./tabs/settings.js').then((m) => m.createSettings || (() => s7TabAdapter(m, m.renderConfig, 'settings'))),
  'llama-proxy': () => import('./tabs/llama-proxy.js').then((m) => m.createLlamaProxy || (() => s7TabAdapter(m, m.diagnosticsStrip, 'llama-proxy'))),
  '9router': () => import('./tabs/9router.js').then((m) => m.create9router || (() => s7TabAdapter(m, m.diagnosticsStrip, '9router'))),
};

async function s7TabAdapter(module, renderFn, key) {
  const route = REGISTRY[key] || ROUTES[key];
  return function createS7Tab({ api: client, profile }) {
    const root = el('div', { class: 'tab tab-s7' });
    const body = el('div', { class: 's7-body' });
    const toolbar = el('div', { class: 'tab-toolbar' }, [
      el('span', { class: 'tab-title', text: route?.label || key }),
    ]);
    root.append(toolbar, body);
    let envelope = null;

    async function load() {
      clear(body);
      body.append(skeleton({ lines: 5 }));
      const endpoints = (module.SOURCE_ENDPOINTS || []).filter((path) => !path.includes('{'));
      if (endpoints.length === 0) {
        envelope = { data: null, meta: { freshness: 'unsupported', source_id: 'route-registry' } };
        render();
        return;
      }
      try {
        envelope = await client.get(endpoints[0], { profile });
      } catch (err) {
        envelope = {
          data: null,
          meta: {
            freshness: 'unavailable',
            source_id: 'unknown',
            degraded_reason: err.message,
            request_id: err.request_id || null,
          },
        };
      }
      render();
    }

    function render() {
      clear(body);
      const output = typeof renderFn === 'function' ? renderFn(envelope) : null;
      if (output && (output.state === 'unavailable' || output.state === 'unsupported')) {
        body.append(unavailableState({
          reason: envelope?.meta?.degraded_reason || `${key} source ${output.state}`,
          requestId: envelope?.meta?.request_id,
        }));
        return;
      }
      if (output && output.state === 'empty') {
        body.append(provenanceBadge(envelope?.meta, { empty: true }));
        body.append(emptyState({ title: `No ${route?.label || key} data` }));
        return;
      }

      body.append(provenanceBadge(envelope?.meta));
      if (output && Array.isArray(output.rows)) {
        const ul = el('ul', { class: 'list' });
        for (const row of output.rows.slice(0, 100)) {
          const identifier = row.id || row.key || row.name || 'item';
          const detail = row.title || row.description || row.summary || row.issue
            || row.context || row.category || row.version || row.state || row.status || '';
          const children = [
            el('span', { class: 'mono', text: String(identifier) }),
            el('span', { text: String(detail) }),
          ];
          const state = row.state || row.status;
          if (state) children.push(el('span', { class: `chip chip-${state}`, text: state }));
          ul.append(el('li', { class: 'list-item' }, children));
        }
        body.append(ul);
      } else {
        body.append(el('pre', {
          class: 'mono pre-wrap',
          text: JSON.stringify(output?.rows ?? envelope?.data ?? {}, null, 2).slice(0, 12000),
        }));
      }
    }

    return {
      mount(container) {
        clear(container);
        container.append(root);
      },
      activate() {
        return load();
      },
      deactivate() {
        return {};
      },
      refresh: load,
      get data() {
        return envelope?.data ?? null;
      },
    };
  };
}

export async function boot({ root } = {}) {
  const app = root || document.getElementById('app');
  if (!app) throw new Error('no #app root');

  let profile = readProfileFromLocation();
  let me = null;
  let profiles = [];
  let capabilitiesEnvelope = null;
  let shellPulse = null;
  let shellAlerts = null;
  let inspectorState = null;
  let currentRouteKey = null;
  let currentRouteParams = {};
  let currentInstanceKey = null;

  let prefs = { density: 'comfortable', nav_collapsed: [] };

  const tabInstances = new Map();
  const stateStore = createStateStore({ maxRoutes: 16 });
  const prefetch = createPrefetch({ maxConcurrent: 4 });
  const sse = new SseClient({ url: '/api/events/stream' });

  const statusStrip = el('div', { class: 'status-strip' });
  const leftNav = el('nav', { class: 'left-nav', 'aria-label': 'Main navigation' });
  const workspace = el('main', { class: 'workspace' });
  const workspaceTopbar = el('div', { class: 'workspace-topbar' });
  const workspaceContent = el('div', { class: 'workspace-content' });
  workspace.append(workspaceTopbar, workspaceContent);
  const inspector = el('aside', { class: 'inspector', 'aria-label': 'Contextual inspector' });
  const header = el('header', { class: 'left-header' });
  const profileSelect = el('select', {
    class: 'select profile-select',
    'aria-label': 'Active profile',
    onchange: (event) => switchProfile(event.target.value),
  });
  const healthDot = el('span', {
    class: 'health-dot health-down',
    title: 'source health unavailable',
    'aria-label': 'source health',
  });
  const searchTrigger = el('button', {
    class: 'btn btn-sm',
    title: 'Search (Ctrl+K)',
    onclick: () => palette.open(),
  }, [icon('search', { size: 12 }), ' Search']);

  header.append(
    el('div', { class: 'brand', text: 'AgentOS' }),
    el('div', { class: 'brand-tagline', text: 'Hermes Mission Control' }),
    profileSelect,
    el('div', { class: 'header-row' }, [healthDot, searchTrigger]),
  );
  leftNav.append(header);

  function routeKey(path) {
    return String(path || FALLBACK_PATH).replace(/^\/+/, '') || 'overview';
  }

  // Shell preferences live in the BFF control store, not Web Storage, so an
  // operator's density/nav layout follows them across browsers.
  function applyPrefs() {
    document.documentElement.dataset.density = prefs.density === 'compact' ? 'compact' : 'comfortable';
  }

  async function loadPrefs() {
    try {
      const stored = (await api.get('/api/preferences', { profile })).data;
      if (stored && typeof stored === 'object') prefs = { ...prefs, ...stored };
    } catch (_err) { /* defaults are fine; prefs are never load-bearing */ }
    applyPrefs();
  }

  function savePrefs(patch) {
    prefs = { ...prefs, ...patch };
    applyPrefs();
    api.put('/api/preferences', patch, { profile }).catch(() => null);
  }

  function navCollapsed(group) {
    return Array.isArray(prefs.nav_collapsed) && prefs.nav_collapsed.includes(group);
  }

  function toggleNavGroup(group) {
    const set = new Set(Array.isArray(prefs.nav_collapsed) ? prefs.nav_collapsed : []);
    if (set.has(group)) set.delete(group); else set.add(group);
    savePrefs({ nav_collapsed: [...set] });
    renderNav();
  }

  // Per-item counts come from data the shell already holds — no extra fetches.
  function navBadges() {
    const pulse = pulseRecord(shellPulse?.data);
    const alerts = alertRows(shellAlerts?.data).filter(
      (item) => !['resolved', 'closed'].includes(String(item.state || '').toLowerCase()));
    const health = summarizeSourceHealth(capabilitiesEnvelope?.data);
    const out = {};
    const add = (key, count, tone) => {
      if (Number.isFinite(count) && count > 0) out[key] = { count, tone };
    };
    add('permits', Number(pulse.pending_permits), 'warn');
    add('issues', Number(pulse.open_issues), 'warn');
    add('kanban', Number(pulse.running_tasks), 'accent');
    add('alerts', alerts.length, alerts.some((a) => String(a.severity || '').toLowerCase() === 'critical') ? 'danger' : 'warn');
    add('cron', Number(pulse.failing_cron), 'danger');
    add('command-center', health.unhealthyCount, 'danger');
    return out;
  }

  function instanceId(key, scope = profile) {
    return `${scope}::${key}`;
  }

  function renderStatusStrip() {
    const health = summarizeSourceHealth(capabilitiesEnvelope?.data);
    const pulse = pulseRecord(shellPulse?.data);
    const activeAlerts = alertRows(shellAlerts?.data);
    const critical = activeAlerts.filter((item) => String(item.severity || '').toLowerCase() === 'critical'
      && !['resolved', 'closed'].includes(String(item.state || '').toLowerCase())).length;

    clear(statusStrip);
    statusStrip.append(
      statusStripItem('profiles', `profile: ${profile}`),
      statusStripItem('spark', `sse: ${sse.state}`),
      statusStripItem('plug', 'source issues:', health.unhealthyCount, { route: 'command-center' }),
      statusStripItem('permits', 'pending permits:', pulse.pending_permits ?? '—', { route: 'permits' }),
      statusStripItem('alerts', 'critical alerts:', critical, { danger: critical > 0, route: 'alerts', params: { severity: 'critical' } }),
      statusStripItem('activity', 'running tasks:', pulse.running_tasks ?? '—', { route: 'kanban', params: { status: 'running' } }),
    );
  }

  function statusStripItem(iconName, label, value, { danger = false, route = null, params = {} } = {}) {
    const classes = `status-item${danger ? ' status-item-danger' : ''}${route ? ' status-item-link' : ''}`;
    const children = [icon(iconName, { size: 11, className: 'status-item-icon' }), label];
    if (value !== undefined) children.push(el('span', { class: 'status-item-value', text: String(value) }));
    if (!route) return el('span', { class: classes }, children);
    return el('button', {
      class: classes, type: 'button', title: `Go to ${route}`,
      onclick: () => navigate(route, params),
    }, children);
  }

  function renderNav() {
    clear(leftNav);
    leftNav.append(header);
    const badges = navBadges();
    for (const [groupName, keys] of Object.entries(navGroups())) {
      const collapsed = navCollapsed(groupName);
      const section = el('div', { class: `nav-group${collapsed ? ' is-collapsed' : ''}` });
      const groupCount = keys.reduce((sum, key) => sum + (badges[key]?.count || 0), 0);
      section.append(el('button', {
        class: 'nav-group-title',
        type: 'button',
        'aria-expanded': collapsed ? 'false' : 'true',
        onclick: () => toggleNavGroup(groupName),
      }, [
        el('span', { class: 'nav-group-caret', text: '▾' }),
        el('span', { text: groupName }),
        collapsed && groupCount
          ? el('span', { class: 'nav-badge nav-badge-warn', text: String(groupCount) })
          : null,
      ].filter(Boolean)));
      const list = el('ul', { class: 'nav-list' });
      for (const key of keys) {
        const route = REGISTRY[key] || ROUTES[key];
        const badge = badges[key];
        list.append(el('button', {
          class: `nav-item${key === currentRouteKey ? ' active' : ''}`,
          'data-route': key,
          onclick: () => navigate(key),
        }, [
          icon(ROUTE_ICONS[key] || 'overview', { size: 15, className: 'nav-icon' }),
          el('span', { class: 'nav-item-label', text: route.label }),
          badge ? el('span', { class: `nav-badge nav-badge-${badge.tone}`, text: String(badge.count) }) : null,
        ].filter(Boolean)));
      }
      section.append(list);
      leftNav.append(section);
    }
  }

  // Every tab gets its chrome in the same place: breadcrumb left, the tab's own
  // actions in the middle slot, shell-level controls pinned right.
  const topbarSlot = el('div', { class: 'topbar-slot' });

  function renderBreadcrumb(route) {
    clear(workspaceTopbar);
    if (!route) return;
    clear(topbarSlot);
    workspaceTopbar.append(
      el('span', { class: 'breadcrumb-group', text: route.group || '' }),
      el('span', { class: 'breadcrumb-sep', text: '/' }),
      el('span', { class: 'breadcrumb-current' }, [
        icon(ROUTE_ICONS[currentRouteKey] || 'overview', { size: 13 }),
        route.label,
      ]),
      topbarSlot,
      el('div', { class: 'topbar-right' }, [
        el('button', {
          class: 'btn btn-sm',
          type: 'button',
          title: 'Refresh this tab',
          onclick: () => tabInstances.get(currentInstanceKey)?.refresh?.(),
        }, [icon('retry', { size: 12 }), ' Refresh']),
      ]),
    );
  }

  function saveCurrentInstance() {
    if (!currentInstanceKey || !tabInstances.has(currentInstanceKey)) return;
    const instance = tabInstances.get(currentInstanceKey);
    const state = instance.deactivate ? instance.deactivate() : {};
    stateStore.save(currentInstanceKey, {
      data: instance.data ?? null,
      filters: instance.filters ?? null,
      scroll: state?.scroll || 0,
      selection: state?.selection ?? null,
      inspector: inspectorState,
    });
  }

  function writeRouteToHistory(route, params, mode) {
    if (mode === 'none') return;
    const url = new URL(window.location.href);
    url.searchParams.set('profile', profile);
    url.hash = buildHash(route.path, params);
    const next = `${url.pathname}${url.search}${url.hash}`;
    if (mode === 'replace') history.replaceState(null, '', next);
    else history.pushState(null, '', next);
  }

  function navigate(key, params = {}, { historyMode = 'push' } = {}) {
    const normalizedKey = routeKey(key);
    const route = REGISTRY[normalizedKey] || ROUTES[normalizedKey];
    if (!route) return;

    saveCurrentInstance();
    currentRouteKey = normalizedKey;
    currentRouteParams = { ...(params || {}) };
    renderNav();
    renderBreadcrumb(route);
    renderInspectorPlaceholder();
    writeRouteToHistory(route, currentRouteParams, historyMode);

    activateRoute(normalizedKey, currentRouteParams).catch((err) => {
      console.error('route activation failed', err);
      clear(workspaceContent);
      workspaceContent.append(el('div', { class: 'panel-error' }, [
        el('div', { class: 'panel-error-title', text: 'Route failed' }),
        el('div', { class: 'panel-error-msg', text: String(err?.message || err) }),
      ]));
    });
  }

  async function activateRoute(key, params = {}) {
    const route = REGISTRY[key] || ROUTES[key];
    if (!route) return;
    workspace.classList.toggle('workspace-fixed-pane', FIXED_PANE_ROUTES.has(key));
    const id = instanceId(key);
    const restored = restoreOrEmpty(stateStore, id);
    inspectorState = restored.inspector || null;
    let instance = tabInstances.get(id);

    if (!instance) {
      if (route.placeholder) {
        const { createPlaceholder } = await import('./tabs/placeholder.js');
        instance = createPlaceholder({ route, onNavigate: navigate });
      } else {
        const loader = PRIMARY_MODULES[key] || S7_LOADERS[key];
        if (!loader) throw new Error(`no module for route ${key}`);
        const create = await loader();
        const factoryArgs = {
          api, profile, events: null, sse, onNavigate: navigate,
          refreshInspector: renderInspectorPlaceholder,
          toolbar: topbarSlot,
        };
        let next = create(factoryArgs);
        while (next && typeof next.then === 'function') next = await next;
        while (typeof next === 'function') {
          next = next(factoryArgs);
          while (next && typeof next.then === 'function') next = await next;
        }
        instance = next;
        if (!instance || typeof instance.mount !== 'function') {
          throw new Error(`route factory for ${key} returned non-mountable instance (${typeof instance})`);
        }
      }
      tabInstances.set(id, instance);
    }

    currentInstanceKey = id;
    clear(workspaceContent);
    instance.mount(workspaceContent);
    // Re-painted per activation so a cached instance still owns the topbar slot
    // after the breadcrumb render cleared it.
    if (typeof instance.renderToolbar === 'function') {
      clear(topbarSlot);
      instance.renderToolbar(topbarSlot);
    }
    if (typeof instance.setSourceHealth === 'function') instance.setSourceHealth(capabilitiesEnvelope);
    // currentInstanceKey is only known now, so a tab-owned inspector renders here.
    renderInspectorPlaceholder();
    await instance.activate?.(params);
    if (restored.scroll && workspace.scrollTo) workspace.scrollTo(0, restored.scroll);
  }

  function renderInspectorPlaceholder() {
    // A tab may own the inspector column (e.g. Chat renders its session list
    // there); otherwise fall back to the generic provenance placeholder.
    // Resolve against the *target* route so a pending navigation never paints
    // the previous tab's inspector.
    const expectedId = currentRouteKey ? instanceId(currentRouteKey) : null;
    const owner = expectedId ? tabInstances.get(expectedId) : null;
    if (owner && typeof owner.renderInspector === 'function') {
      clear(inspector);
      owner.renderInspector(inspector);
      return;
    }
    clear(inspector);
    inspector.append(el('div', { class: 'inspector-title', text: 'Inspector' }));
    if (inspectorState) {
      inspector.append(el('div', { class: 'kv' }, Object.entries(inspectorState).map(([key, value]) =>
        el('div', { class: 'kv-row' }, [
          el('span', { class: 'kv-k', text: key }),
          el('span', { class: 'kv-v', text: JSON.stringify(value) }),
        ]))));
    } else {
      inspector.append(el('div', {
        class: 'inspector-empty',
        text: 'Select an entity to inspect provenance, relations and actions.',
      }));
    }
  }

  const palette = createPalette({
    routes: REGISTRY,
    onNavigate: (key) => navigate(key),
    onProfileSwitch: (next) => switchProfile(next),
    searchSources: {
      sessions: (query) => api.get(`/api/adapter/sessions/search?q=${encodeURIComponent(query)}`, { profile })
        .then((response) => listFrom(response.data, ['sessions']).map((session) => ({
          id: session.id || session.session_id,
          title: session.title || session.id || session.session_id,
          route: '/sessions',
          params: { s: session.id || session.session_id },
          type: 'session',
        }))).catch(() => []),
      tasks: (query) => api.get('/api/adapter/kanban/tasks', { profile })
        .then((response) => taskRows(response.data)
          .filter((task) => String(task.id || '').includes(query)
            || String(task.title || '').toLowerCase().includes(query.toLowerCase()))
          .slice(0, 8)
          .map((task) => ({
            id: task.id,
            title: task.title || task.id,
            route: '/kanban',
            params: { task: task.id },
            type: 'task',
          }))).catch(() => []),
    },
  });

  // Keys that work everywhere.
  //
  // ⌘K already opened the palette, and that was the whole keyboard surface of a
  // dashboard whose every tab is a list you have to find something in. These
  // three cover what an operator does over and over — search this list, reload
  // it, and remind me what the keys are — and they deliberately do nothing
  // while a field has focus, so typing a session title never triggers them.
  const SHORTCUTS = Object.freeze([
    ['⌘K / Ctrl+K', 'Command palette — jump to any tab, session or task'],
    ['/', "Focus the current tab's filter box"],
    ['r', 'Refresh the current tab'],
    ['Esc', 'Clear the filter, or close the palette'],
    ['?', 'This list'],
  ]);

  function typingInField(target) {
    if (!target || !target.tagName) return false;
    if (target.isContentEditable) return true;
    return ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName);
  }

  function showShortcuts() {
    const existing = document.querySelector('.shortcut-sheet-layer');
    if (existing) { existing.remove(); return; }
    const layer = el('div', { class: 'shortcut-sheet-layer' });
    const sheet = el('div', { class: 'shortcut-sheet', role: 'dialog', 'aria-label': 'Keyboard shortcuts' }, [
      el('div', { class: 'shortcut-sheet-title', text: 'Keyboard shortcuts' }),
      ...SHORTCUTS.map(([keys, description]) => el('div', { class: 'shortcut-row' }, [
        el('kbd', { text: keys }),
        el('span', { text: description }),
      ])),
    ]);
    layer.append(sheet);
    layer.addEventListener('mousedown', (event) => { if (event.target === layer) layer.remove(); });
    document.body.append(layer);
  }

  window.addEventListener('keydown', (event) => {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === 'Escape') {
      document.querySelector('.shortcut-sheet-layer')?.remove();
      return;
    }
    if (typingInField(event.target)) return;
    if (event.key === '/') {
      const field = workspace.querySelector('[data-tab-filter]')
        || topbarSlot.querySelector('[data-tab-filter]');
      if (!field) return;
      event.preventDefault();
      field.focus();
      field.select?.();
      return;
    }
    if (event.key === 'r') {
      const instance = currentInstanceKey ? tabInstances.get(currentInstanceKey) : null;
      if (typeof instance?.refresh !== 'function') return;
      event.preventDefault();
      Promise.resolve(instance.refresh()).catch(() => null);
      return;
    }
    if (event.key === '?') {
      event.preventDefault();
      showShortcuts();
    }
  });

  async function switchProfile(next, { navigateAfter = true } = {}) {
    if (!next || next === profile) return;
    const previous = profile;
    const route = currentRouteKey || 'overview';
    const params = { ...currentRouteParams };
    saveCurrentInstance();
    currentInstanceKey = null;
    profile = next;
    setProfileInLocation(next);
    clearCacheForProfile(previous);
    prefetch.invalidateObsolete();
    renderNav();
    renderStatusStrip();
    await loadShell();
    if (navigateAfter) navigate(route, params, { historyMode: 'replace' });
  }

  function clearCacheForProfile(scope) {
    const prefix = profileCachePrefix(scope);
    for (const key of [...prefetch.cache.keys()]) {
      if (key.startsWith(prefix)) prefetch.cache.delete(key);
    }
  }

  async function loadShell() {
    const normalizeProfile = (raw) => {
      if (!raw || typeof raw !== 'object') return { id: 'default', name: 'default' };
      const id = raw.id || raw.name || raw.path || raw.profile_id || 'default';
      return raw.id === id ? raw : { ...raw, id };
    };

    // Establish the browser session cookie before parallel bootstrap reads.
    try {
      me = (await api.get('/api/auth/me', { profile })).data;
    } catch (_err) {
      me = null;
    }

    const [profilesResult, capabilitiesResult, pulseResult, alertsResult] = await Promise.allSettled([
      api.get('/api/upstream/api/profiles', { profile }),
      api.get('/api/capabilities', { profile }),
      api.get('/api/pulse?window=1h', { profile }),
      api.get('/api/alerts', { profile }),
    ]);

    if (profilesResult.status === 'fulfilled') {
      const payload = profilesResult.value?.data;
      profiles = listFrom(payload, ['profiles']).map(normalizeProfile);
    } else {
      profiles = [];
    }
    capabilitiesEnvelope = capabilitiesResult.status === 'fulfilled' ? capabilitiesResult.value : null;
    shellPulse = pulseResult.status === 'fulfilled' ? pulseResult.value : null;
    shellAlerts = alertsResult.status === 'fulfilled' ? alertsResult.value : null;

    renderHeader();
    renderStatusStrip();
    palette.setCommands(profiles);
    if (currentInstanceKey) {
      const instance = tabInstances.get(currentInstanceKey);
      instance?.setSourceHealth?.(capabilitiesEnvelope);
    }
    sse.connect();
  }

  function renderHeader() {
    clear(profileSelect);
    const values = profiles.length ? [...profiles] : [{ id: 'default', name: 'default' }];
    if (!values.some((item) => (item.id || item.name || item.path || item.profile_id) === profile)) {
      values.unshift({ id: profile, name: profile });
    }
    for (const item of values) {
      const value = item.id || item.name || item.path || item.profile_id || 'default';
      const text = item.name || item.id || item.path || item.profile_id || 'default';
      const option = el('option', { value, text });
      option.selected = value === profile;
      profileSelect.append(option);
    }

    const health = summarizeSourceHealth(capabilitiesEnvelope?.data);
    healthDot.className = `health-dot${health.state === 'down' ? ' health-down' : health.state === 'degraded' ? ' health-degraded' : ''}`;
    const details = [
      ...health.missingRequired.map((id) => `${id}: missing`),
      ...health.unhealthyRequired.map((id) => `${id}: down`),
      ...health.unhealthyOptional.map((id) => `${id}: degraded`),
    ];
    healthDot.title = details.length ? details.join(' · ') : 'required sources healthy';
    healthDot.setAttribute('aria-label', healthDot.title);
  }

  sse.onStateChange = () => renderStatusStrip();
  sse.on('cache.invalidated', (event) => {
    const key = event.entity_id || event.payload?.key;
    if (key) prefetch.cache.delete(key);
  });
  sse.on('alert.changed', () => loadShell().catch(() => null));
  sse.on('source.health', () => loadShell().catch(() => null));

  app.append(leftNav, workspace, inspector, statusStrip);
  setProfileInLocation(profile);
  await loadShell();
  await loadPrefs();

  for (const [key, loader] of Object.entries(PRIMARY_MODULES)) {
    prefetch.registerRouteCode(key, () => loader().catch(() => null));
  }
  prefetch.prefetch(`${profileCachePrefix(profile)}overview-summary`, () =>
    api.get('/api/pulse?window=1h', { profile }).catch(() => null)).catch(() => null);
  prefetch.prefetch(`${profileCachePrefix(profile)}kanban-summary`, () =>
    api.get('/api/adapter/kanban/board/summary', { profile }).catch(() => null)).catch(() => null);
  prefetch.expose();

  async function onHashChange() {
    const route = parseRouteWithProfile(window.location.hash, window.location.search);
    if (route.profile !== profile) await switchProfile(route.profile, { navigateAfter: false });
    navigate(routeKey(route.path), route.params, { historyMode: 'none' });
  }
  window.addEventListener('hashchange', () => onHashChange().catch(console.error));

  const initial = parseRouteWithProfile(window.location.hash, window.location.search);
  if (initial.profile !== profile) profile = initial.profile;
  navigate(routeKey(initial.path), initial.params, { historyMode: 'replace' });

  return {
    navigate,
    switchProfile,
    get profile() {
      return profile;
    },
    getStateStore() {
      return stateStore;
    },
    getPrefetch() {
      return prefetch;
    },
    getSse() {
      return sse;
    },
    get me() {
      return me;
    },
  };
}

export { ROUTES, FALLBACK_PATH };
