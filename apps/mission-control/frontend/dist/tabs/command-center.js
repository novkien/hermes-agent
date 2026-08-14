// Command Center — Hermes system state plus the operational actions that need
// a human on the trigger.
//
// The old version printed /api/health and /api/status as raw JSON. Both
// payloads are highly structured — gateway state, per-platform connection
// state, host resources — so they become status cards, and the gateway
// lifecycle controls that make a "restart required" badge actionable live here.

import { el, clear, skeleton, statusChip, unavailableState, errorPanel, fmtAge } from '../ui.js';
import { icon } from '../icons.js';
import { provenanceBadge } from '../provenance.js';
import { mutationCalls } from '../pure/read-only-audit.js';
import { recordFrom } from '../pure/data-shape.js';
import { createStatRow } from '../components/stat.js';
import { createTable } from '../components/table.js';
import { createForm } from '../components/form.js';
import { loadEnvelope, tabToolbar, runMutation, paint, primaryButton, confirmAction, sideHint } from './_kit.js';

export const ROUTE = 'command-center';
export const LABEL = 'Command Center';
export const GROUP = 'SYSTEM';
export const READ_ONLY_NOTE = 'operational reads plus confirmed allowlisted actions';

export const SOURCE_ENDPOINTS = Object.freeze(['/api/health', '/api/status']);

export const VERIFIED_ACTIONS = Object.freeze([
  {
    id: 'doctor',
    label: 'Run doctor',
    method: 'POST',
    endpoint: '/api/upstream/api/ops/doctor',
    upstreamAction: 'POST /api/ops/doctor',
    confirm: true,
    csrf: true,
  },
  {
    id: 'security-audit',
    label: 'Run security audit',
    method: 'POST',
    endpoint: '/api/upstream/api/ops/security-audit',
    upstreamAction: 'POST /api/ops/security-audit',
    confirm: true,
    csrf: true,
  },
  {
    id: 'backup',
    label: 'Run backup',
    method: 'POST',
    endpoint: '/api/upstream/api/ops/backup',
    upstreamAction: 'POST /api/ops/backup',
    confirm: true,
    csrf: true,
  },
  {
    id: 'prompt-size',
    label: 'Measure prompt size',
    method: 'POST',
    endpoint: '/api/upstream/api/ops/prompt-size',
    upstreamAction: 'POST /api/ops/prompt-size',
    confirm: false,
    csrf: true,
  },
]);

// Every one of these stops or interrupts the running gateway, so each is
// confirm-gated at the BFF and armed twice in the UI.
export const GATEWAY_ACTIONS = Object.freeze([
  { id: 'restart', label: 'Restart', iconName: 'refresh', tone: 'warn' },
  { id: 'drain', label: 'Drain', iconName: 'pause', tone: 'warn' },
  { id: 'start', label: 'Start', iconName: 'play', tone: 'accent' },
  { id: 'stop', label: 'Stop', iconName: 'power', tone: 'danger' },
]);

export const READ_ONLY_ENDPOINTS = Object.freeze([
  { id: 'health', label: 'Health', method: 'GET', endpoint: '/api/health' },
  { id: 'status', label: 'Status', method: 'GET', endpoint: '/api/status' },
]);

export function commandCenterCapabilities() {
  return {
    verifiedActions: VERIFIED_ACTIONS.map((action) => ({ ...action })),
    readOnly: READ_ONLY_ENDPOINTS.map((item) => ({ ...item })),
    mutationCalls: mutationCalls('command-center'),
  };
}

export function actionBadges() {
  const operations = new Map(mutationCalls('command-center').map((item) => [item.action, item]));
  return VERIFIED_ACTIONS.map((action) => {
    const operation = operations.get(action.upstreamAction);
    return {
      // CSRF is the gate that decides whether the button can exist at all.
      // `confirm` decides whether it arms first, which is a separate axis.
      id: action.id,
      enabled: Boolean(operation?.csrf),
      csrf: Boolean(operation?.csrf),
      confirm: Boolean(operation?.confirm),
      reason: operation ? null : 'not allowlisted in dashboard v1',
    };
  });
}

const GATEWAY_TONE = { running: 'ok', draining: 'warn', stopped: 'idle', error: 'danger' };
const PLATFORM_TONE = { connected: 'ok', disconnected: 'idle', error: 'danger' };

function pct(value) {
  const n = Number(value);
  return Number.isFinite(n) ? `${Math.round(n)}%` : '—';
}

function gib(bytes) {
  const n = Number(bytes);
  return Number.isFinite(n) ? `${(n / 1024 ** 3).toFixed(1)} GB` : '—';
}

export function createCommandCenter({ api, profile, toolbar }) {
  const root = el('div', { class: 'tab tab-command-center' });
  const stats = el('div', { class: 'stat-row-host' });
  const cards = el('div', { class: 'grid-cards' });
  const output = el('section', { class: 'panel command-output-panel' });
  root.append(stats, cards, output);

  let health = null;
  let status = null;
  let system = null;
  let checkpoints = null;
  let hooks = null;
  let update = null;
  let runningAction = null;
  let inspectorHost = null;

  async function load() {
    clear(cards);
    cards.append(skeleton({ lines: 6 }));

    const [healthRes, statusRes, systemRes, checkpointRes, hooksRes, updateRes] = await Promise.all([
      loadEnvelope(api, '/api/health', { profile, allowEmpty: false }),
      loadEnvelope(api, '/api/status', { profile, allowEmpty: false }),
      loadEnvelope(api, '/api/upstream/api/system/stats', { profile, allowEmpty: false }),
      loadEnvelope(api, '/api/upstream/api/ops/checkpoints', { profile, allowEmpty: false }),
      loadEnvelope(api, '/api/upstream/api/ops/hooks', { profile, allowEmpty: false }),
      loadEnvelope(api, '/api/upstream/api/hermes/update/check', { profile, allowEmpty: false }),
    ]);

    health = healthRes;
    status = statusRes;
    system = systemRes.state === 'ready' ? systemRes.data : null;
    checkpoints = checkpointRes.state === 'ready' ? checkpointRes.data : null;
    hooks = hooksRes.state === 'ready' ? hooksRes.data : null;
    update = updateRes.state === 'ready' ? updateRes.data : null;

    renderStats();
    renderCards();
    renderToolbar(toolbar);
    if (health.state !== 'ready' && status.state !== 'ready') {
      paint(output, unavailableState({ reason: 'Hermes operational endpoints unavailable' }));
    }
  }

  function renderStats() {
    clear(stats);
    const s = status.state === 'ready' ? status.data : {};
    stats.append(createStatRow([
      {
        label: 'Gateway',
        value: s.gateway_state || 'unknown',
        iconName: 'power',
        seriesIndex: 1,
        foot: s.gateway_updated_at ? fmtAge(s.gateway_updated_at) : '',
      },
      { label: 'Active sessions', value: String(s.active_sessions ?? '—'), iconName: 'sessions', seriesIndex: 2 },
      { label: 'Active agents', value: String(s.active_agents ?? '—'), iconName: 'fleet', seriesIndex: 3 },
      {
        label: 'CPU',
        value: system ? pct(system.cpu_percent) : '—',
        iconName: 'analytics',
        seriesIndex: 4,
        foot: system?.cpu_count ? `${system.cpu_count} cores` : '',
      },
      {
        label: 'Memory',
        value: system?.memory ? pct(system.memory.percent) : '—',
        iconName: 'overview',
        seriesIndex: 5,
        foot: system?.memory ? `${gib(system.memory.used)} of ${gib(system.memory.total)}` : '',
      },
    ]));
  }

  function card(title, meta, body, actions = []) {
    const section = el('section', { class: 'panel' });
    const head = el('div', { class: 'panel-head' }, [el('div', { class: 'panel-title', text: title })]);
    if (meta) head.append(provenanceBadge(meta));
    for (const action of actions) if (action) head.append(action);
    section.append(head, el('div', { class: 'panel-body' }, [body]));
    return section;
  }

  function gatewayCard() {
    const s = status.state === 'ready' ? status.data : {};
    const body = el('div', { class: 'stack-sm' });

    body.append(el('div', { class: 'inline-chips' }, [
      statusChip(GATEWAY_TONE[s.gateway_state] || 'idle', s.gateway_state || 'unknown'),
      s.gateway_busy ? statusChip('warn', 'busy') : null,
      s.gateway_drainable ? el('span', { class: 'chip', text: 'drainable' }) : null,
      s.gateway_mode ? el('span', { class: 'chip', text: s.gateway_mode }) : null,
    ].filter(Boolean)));

    if (s.gateway_exit_reason) {
      body.append(el('div', { class: 'field-hint', text: `last exit: ${s.gateway_exit_reason}` }));
    }

    const controls = el('div', { class: 'inline-chips' });
    for (const action of GATEWAY_ACTIONS) {
      controls.append(confirmAction({
        label: action.label,
        iconName: action.iconName,
        confirmLabel: `${action.label} — confirm?`,
        tone: action.tone,
        onConfirm: () => runMutation(
          () => api.post(`/api/upstream/api/gateway/${action.id}?confirm=true`, {}, { profile }),
          { pending: action.label, ok: `Gateway ${action.id} accepted`, onDone: load },
        ),
      }));
    }
    body.append(controls);
    body.append(el('div', { class: 'field-hint', text: 'Each control is confirm-gated at the server as well as here.' }));
    return card('Gateway', status.meta, body);
  }

  function platformsCard() {
    const s = status.state === 'ready' ? status.data : {};
    const platforms = s.gateway_platforms && typeof s.gateway_platforms === 'object' ? s.gateway_platforms : {};
    const entries = Object.entries(platforms);
    if (!entries.length) return null;
    const body = el('div', { class: 'stack-sm' });
    for (const [name, info] of entries) {
      body.append(el('div', { class: 'choice-row' }, [
        el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: name }),
          info.error_message ? el('span', { class: 'cell-danger', text: info.error_message }) : null,
        ].filter(Boolean)),
        statusChip(PLATFORM_TONE[info.state] || 'idle', info.state || 'unknown'),
      ]));
    }
    return card(`Platforms (${entries.length})`, null, body);
  }

  function versionCard() {
    const s = status.state === 'ready' ? status.data : {};
    const body = el('div', { class: 'stack-sm' });
    body.append(el('dl', { class: 'detail-dl' }, [
      el('dt', { class: 'detail-dt', text: 'Version' }),
      el('dd', { class: 'detail-dd mono', text: s.version || '—' }),
      el('dt', { class: 'detail-dt', text: 'Released' }),
      el('dd', { class: 'detail-dd mono', text: s.release_date || '—' }),
      el('dt', { class: 'detail-dt', text: 'Config version' }),
      el('dd', {
        class: 'detail-dd mono',
        text: s.config_version === s.latest_config_version
          ? String(s.config_version ?? '—')
          : `${s.config_version} (latest ${s.latest_config_version})`,
      }),
      el('dt', { class: 'detail-dt', text: 'Host' }),
      el('dd', { class: 'detail-dd mono', text: system ? `${system.hostname} · ${system.os} ${system.arch}` : '—' }),
    ]));

    if (update?.update_available) {
      body.append(el('div', { class: 'notice notice-warn' }, [
        el('div', { text: `${update.behind} commits behind on ${update.install_method}.` }),
        el('div', { class: 'field-hint', text: `Apply with \`${update.update_command}\` on the Hermes host — self-update is not proxied.` }),
      ]));
    } else if (update) {
      body.append(el('div', { class: 'field-hint', text: 'Hermes is up to date.' }));
    }
    return card('Version', health.meta, body);
  }

  function checkpointsCard() {
    if (!checkpoints) return null;
    const sessions = Array.isArray(checkpoints.sessions) ? checkpoints.sessions : [];
    const body = el('div', { class: 'stack-sm' });
    body.append(el('div', { class: 'field-hint', text: `${sessions.length} checkpointed session${sessions.length === 1 ? '' : 's'} · ${gib(checkpoints.total_bytes)} on disk` }));
    if (sessions.length) {
      const table = createTable({
        rowId: (row) => row.session_id || row.id,
        columns: [
          { key: 'session_id', label: 'Session', mono: true, sortable: true },
          { key: 'bytes', label: 'Size', align: 'right', width: '90px', sortable: true, render: (row) => gib(row.bytes) },
        ],
      });
      table.setRows(sessions);
      body.append(table.node);
    }
    body.append(el('div', { class: 'inline-chips' }, [
      confirmAction({
        iconName: 'trash',
        label: 'Prune checkpoints',
        confirmLabel: 'Prune — confirm?',
        onConfirm: () => runMutation(
          () => api.post('/api/upstream/api/ops/checkpoints/prune?confirm=true', {}, { profile }),
          { pending: 'Prune', ok: 'Checkpoints pruned', onDone: load },
        ),
      }),
    ]));
    return card('Checkpoints', null, body);
  }

  function hooksCard() {
    if (!hooks) return null;
    const list = Array.isArray(hooks.hooks) ? hooks.hooks : [];
    const validEvents = Array.isArray(hooks.valid_events) ? hooks.valid_events : [];
    const body = el('div', { class: 'stack-sm' });

    if (list.length) {
      for (const hook of list) {
        body.append(el('div', { class: 'choice-row' }, [
          el('div', { class: 'cell-stack' }, [
            el('span', { class: 'cell-strong', text: hook.event }),
            el('span', { class: 'cell-dim mono', text: hook.command, title: hook.command }),
          ]),
          confirmAction({
            iconName: 'trash',
            label: 'Delete',
            confirmLabel: 'Delete — confirm?',
            onConfirm: () => runMutation(
              () => api.request('/api/upstream/api/ops/hooks?confirm=true', {
                method: 'DELETE',
                body: { event: hook.event, command: hook.command },
                profile,
              }),
              { pending: 'Delete hook', ok: 'Hook deleted', onDone: load },
            ),
          }),
        ]));
      }
    } else {
      body.append(el('div', { class: 'field-hint', text: 'No hooks configured.' }));
    }

    const form = createForm({
      submitLabel: 'Add hook',
      submitIcon: 'plus',
      note: 'A hook runs a shell command on the Hermes host when the event fires.',
      values: { event: validEvents[0] || '', command: '', matcher: '', approve: true },
      fields: [
        { key: 'event', label: 'Event', type: 'select', options: validEvents, required: true },
        { key: 'matcher', label: 'Matcher', hint: 'Optional filter on the event payload.' },
        { key: 'command', label: 'Command', required: true, span: 2, hint: 'Runs on the Hermes host with the gateway user’s privileges.' },
        { key: 'approve', label: 'Approve now', type: 'toggle', hint: 'Without this the hook is configured but never fires.' },
      ],
      onSubmit: async (diff, all) => {
        const res = await runMutation(
          () => api.post('/api/upstream/api/ops/hooks', {
            event: all.event,
            command: all.command,
            matcher: all.matcher || null,
            approve: Boolean(all.approve),
          }, { profile }),
          { pending: 'Add hook', ok: 'Hook added' },
        );
        if (res) await load();
      },
    });
    body.append(form.node);
    return card(`Hooks (${list.length})`, null, body);
  }

  function renderCards() {
    clear(cards);
    for (const node of [gatewayCard(), versionCard(), platformsCard(), checkpointsCard(), hooksCard()]) {
      if (node) cards.append(node);
    }
    if (!output.firstChild) {
      output.append(el('div', { class: 'panel-head' }, [
        el('div', { class: 'panel-title', text: 'Command output' }),
      ]));
      output.append(el('div', { class: 'panel-body mono command-output', text: 'No command has been run.' }));
    }
  }

  function renderToolbar(host) {
    if (!host) return;
    const badges = new Map(actionBadges().map((badge) => [badge.id, badge]));
    const buttons = VERIFIED_ACTIONS.map((action) => {
      const badge = badges.get(action.id);
      const iconName = { doctor: 'spark', backup: 'archive', 'security-audit': 'lock' }[action.id] || 'analytics';
      const label = runningAction === action.id ? `${action.label}…` : action.label;
      const button = badge?.confirm
        ? confirmAction({
          label,
          iconName,
          tone: 'warn',
          confirmLabel: `${action.label} — confirm?`,
          onConfirm: () => runAction(action),
        })
        : primaryButton(label, iconName, () => runAction(action));
      if (runningAction || !badge?.enabled) button.disabled = true;
      if (!badge?.enabled) button.title = badge?.reason || 'not available';
      return button;
    });
    paint(host, tabToolbar({
      title: 'Command Center',
      subtitle: status?.state === 'ready' ? `Hermes ${status.data.version} · gateway ${status.data.gateway_state}` : '',
      meta: status?.meta,
      onRefresh: () => load(),
      actions: buttons,
    }));
  }

  async function runAction(action) {
    if (runningAction) return;
    runningAction = action.id;
    renderToolbar(toolbar);
    clear(output);
    output.append(skeleton({ lines: 8 }));
    try {
      const response = await api.post(action.endpoint, {}, { profile });
      clear(output);
      output.append(el('div', { class: 'panel-head' }, [
        el('div', { class: 'panel-title' }, [icon('check', { size: 13 }), ` ${action.label} result`]),
        provenanceBadge(response.meta),
      ]));
      output.append(el('pre', {
        class: 'panel-body mono pre-wrap command-output',
        text: JSON.stringify(response.data, null, 2).slice(0, 40000),
      }));
      await load();
    } catch (err) {
      clear(output);
      output.append(errorPanel({ message: err.message, requestId: err.request_id }));
    } finally {
      runningAction = null;
      renderToolbar(toolbar);
    }
  }

  function renderInspector(container) {
    inspectorHost = container;
    if (!inspectorHost) return;
    const gateway = recordFrom(status?.data) || {};
    paint(inspectorHost, sideHint('Control plane', [
      'Every lifecycle action here is destructive and audited: it is written to the action log before the upstream call, with the request id kept on both sides.',
      `Gateway reports ${gateway.state || gateway.status || 'unknown'}.`,
      'Restart and drain apply to the running gateway process. A plugin toggle only edits config.yaml — it needs a restart to take effect.',
      'Backup download and import move real state; confirm the target before running either.',
    ]));
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
      return { scroll: root.scrollTop || 0 };
    },
    renderInspector,
    refresh: load,
    renderToolbar,
  };
}
