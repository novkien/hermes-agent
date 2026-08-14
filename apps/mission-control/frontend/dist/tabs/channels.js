// Channels / Messaging tab — upstream /api/messaging/platforms.
//
// 33 platforms, each with its own env vars and connection state. The old tab
// rendered a flat chip list and computed a health merge it never displayed.
// This one groups by state, because the operational question is always "what
// is connected, and what is misconfigured", not "what exists".

import { listRows } from '../pure/envelope-list.js';
import { el, clear, statusChip, iconButton, segmented } from '../ui.js';
import { createTable } from '../components/table.js';
import { createForm } from '../components/form.js';
import { createDetail } from '../components/detail.js';
import { applyStateToTable, boolChip, filterInput, loadEnvelope, paint, runMutation, sideHint, tabToolbar } from './_kit.js';
import { filterRows, filterSummary } from '../pure/text-filter.js';

export const ROUTE = 'channels';
export const LABEL = 'Channels / Messaging';
export const GROUP = 'BUILD & INTEGRATE';

export const SOURCE_ENDPOINTS = Object.freeze([
  '/api/messaging/platforms',
  '/api/health',
]);

const BASE = '/api/upstream/api/messaging/platforms';

export const KNOWN_PLATFORMS = Object.freeze(['telegram', 'discord', 'api_server', 'homeassistant']);

const STATE_TONE = {
  connected: 'ok',
  disconnected: 'idle',
  error: 'danger',
  degraded: 'warn',
};

export function renderChannels(envelope, healthEnvelope = null) {
  const res = listRows(envelope, {
    pick: (raw) => raw.platforms || raw.items || raw.list || null,
    map: (p) => {
      const id = p.platform_id ?? p.id ?? p.platform ?? null;
      return {
        id,
        platform: p.platform ?? id ?? null,
        connected: p.state === 'connected' || p.connected === true,
        state: p.state ?? (p.connected === true ? 'connected' : 'disconnected'),
        label: p.name ?? p.label ?? null,
        enabled: p.enabled === true,
        configured: p.configured === true,
      };
    },
  });

  // Gateway health, when supplied, tells us whether a platform the config
  // calls "connected" is actually running right now.
  const health = (healthEnvelope && healthEnvelope.data) || null;
  if (health && Array.isArray(res.rows)) {
    for (const row of res.rows) {
      if (health.gateway_state && row.platform) {
        const gs = health.gateway_state[row.platform] ?? health.gateway_state[row.id];
        if (gs != null) row.gateway_state = gs;
      }
    }
  }
  return res;
}

const FILTERS = [
  { value: 'active', label: 'Active' },
  { value: 'problem', label: 'Needs attention' },
  { value: 'all', label: 'All' },
];

function isProblem(row) {
  return Boolean(row.error_code) || (row.enabled && !row.configured);
}

function matchesFilter(row, filter) {
  if (filter === 'active') return row.enabled === true || row.state === 'connected';
  if (filter === 'problem') return isProblem(row);
  return true;
}

export function createChannels({ api, profile, toolbar }) {
  const root = el('div', { class: 'tab tab-channels' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(main);

  let rows = [];
  let meta = null;
  let selected = null;
  let filter = 'active';

  const table = createTable({
    rowId: (row) => row.id,
    emptyTitle: 'No platforms in this view',
    emptyNote: 'Switch to “All” to see every platform Hermes supports.',
    sort: { key: 'name', dir: 'asc' },
    columns: [
      {
        key: 'name',
        label: 'Platform',
        sortable: true,
        render: (row) => el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: row.name || row.id }),
          el('span', { class: 'cell-dim mono', text: row.id }),
        ]),
      },
      {
        key: 'state',
        label: 'State',
        width: '118px',
        sortable: true,
        render: (row) => statusChip(STATE_TONE[row.state] || 'idle', row.state || 'unknown'),
      },
      { key: 'enabled', label: 'Enabled', width: '92px', sortable: true, render: (row) => boolChip(row.enabled) },
      {
        key: 'configured',
        label: 'Credentials',
        width: '116px',
        sortable: true,
        render: (row) => (row.configured ? statusChip('ok', 'configured') : statusChip('warn', 'missing')),
      },
      {
        key: 'error_message',
        label: 'Last error',
        render: (row) => (row.error_message
          ? el('span', { class: 'cell-danger', text: row.error_message, title: row.error_message })
          : null),
      },
    ],
    rowActions: (row) => [
      iconButton({
        icon: row.enabled ? 'ban' : 'power',
        label: row.enabled ? 'Disable platform' : 'Enable platform',
        tone: row.enabled ? '' : 'accent',
        disabled: !row.configured && !row.enabled,
        disabledReason: 'Set the required credentials first',
        onClick: () => runMutation(
          () => api.put(`${BASE}/${encodeURIComponent(row.id)}`, { enabled: !row.enabled }, { profile }),
          { pending: 'Toggle platform', ok: `${row.name || row.id} ${row.enabled ? 'disabled' : 'enabled'}`, onDone: load },
        ),
      }),
    ],
    onSelect: (row) => { selected = row; renderSide(); },
  });
  main.append(table.node);

  // The segmented control narrows by state; this narrows by name. A list
  // this long is unusable without both.
  let query = '';
  const SEARCH_FIELDS = ['platform', 'name', 'label', 'state', 'description'];

  function visibleRows() {
    return filterRows(rows.filter((row) => matchesFilter(row, filter)), query, SEARCH_FIELDS);
  }

  async function load() {
    table.setLoading();
    const result = await loadEnvelope(api, BASE, {
      profile,
      pick: (raw) => (Array.isArray(raw) ? raw : raw?.platforms || null),
    });
    meta = result.meta;
    rows = Array.isArray(result.data) ? result.data : [];
    applyStateToTable(table, { ...result, data: visibleRows() });
    if (selected) {
      selected = rows.find((r) => r.id === selected.id) || null;
      table.setSelected(selected?.id ?? null);
    }
    renderToolbar(toolbar);
    renderSide();
  }

  function renderToolbar(host) {
    if (!host) return;
    const counts = {
      all: rows.length,
      active: rows.filter((r) => matchesFilter(r, 'active')).length,
      problem: rows.filter(isProblem).length,
    };
    paint(host, tabToolbar({
      title: 'Channels',
      subtitle: query
        ? filterSummary(visibleRows().length, counts.all, 'platform')
        : `${counts.active} active of ${counts.all} supported`,
      meta,
      onRefresh: () => load(),
      filters: [
        filterInput({
          value: query,
          placeholder: 'Find a platform…',
          ariaLabel: 'Filter platforms',
          onChange: (next) => {
            if (next === query) return;
            query = next;
            table.setRows(visibleRows());
            renderToolbar(host);
          },
        }),
        segmented(FILTERS.map((f) => ({ ...f, count: counts[f.value] })), {
          value: filter,
          ariaLabel: 'Filter platforms',
          onChange: (next) => { filter = next; table.setRows(visibleRows()); renderToolbar(host); },
        }),
      ],
    }));
  }

  function credentialsSection(row) {
    const envVars = Array.isArray(row.env_vars) ? row.env_vars : [];
    if (!envVars.length) {
      return el('div', { class: 'field-hint', text: 'This platform needs no credentials.' });
    }
    const form = createForm({
      submitLabel: 'Save credentials',
      submitIcon: 'lock',
      note: 'Blank fields are left unchanged.',
      values: Object.fromEntries(envVars.map((v) => [v.key, ''])),
      fields: envVars.map((v) => ({
        key: v.key,
        label: v.key,
        span: v.is_password ? 2 : 1,
        // The masked value is shown as a placeholder only: it is never the
        // field's value, so it can never be submitted back as a real secret.
        placeholder: v.is_set ? (v.redacted_value || '•••••• (set)') : '',
        hint: [v.prompt, v.required ? 'required' : ''].filter(Boolean).join(' · '),
      })),
      onSubmit: async (diff) => {
        const env = Object.fromEntries(
          Object.entries(diff).filter(([, value]) => String(value ?? '').trim() !== ''),
        );
        if (!Object.keys(env).length) return;
        const res = await runMutation(
          () => api.put(`${BASE}/${encodeURIComponent(row.id)}`, { env }, { profile }),
          { pending: 'Save credentials', ok: `${row.name || row.id} credentials saved` },
        );
        if (res) await load();
      },
    });
    return form.node;
  }

  function detailPanel(row) {
    const sections = [{ title: 'Credentials', node: credentialsSection(row) }];

    if (row.error_message) {
      sections.unshift({
        title: 'Last error',
        node: el('div', { class: 'notice notice-danger' }, [
          el('div', { text: row.error_message }),
          row.error_code ? el('code', { class: 'mono', text: row.error_code }) : null,
        ].filter(Boolean)),
      });
    }

    if (row.home_channel) {
      sections.push({
        title: 'Home channel',
        node: el('dl', { class: 'detail-dl' }, [
          el('dt', { class: 'detail-dt', text: 'Name' }),
          el('dd', { class: 'detail-dd', text: row.home_channel.name || '—' }),
          el('dt', { class: 'detail-dt', text: 'Chat id' }),
          el('dd', { class: 'detail-dd mono', text: row.home_channel.chat_id || '—' }),
          el('dt', { class: 'detail-dt', text: 'Thread id' }),
          el('dd', { class: 'detail-dd mono', text: row.home_channel.thread_id || '—' }),
        ]),
      });
    }

    return createDetail({
      title: row.name || row.id,
      meta,
      chips: [
        statusChip(STATE_TONE[row.state] || 'idle', row.state || 'unknown'),
        boolChip(row.enabled),
        row.gateway_running ? el('span', { class: 'chip chip-ok', text: 'gateway running' }) : null,
      ].filter(Boolean),
      fields: [
        { label: 'Id', value: row.id, mono: true },
        { label: 'Description', value: row.description },
        { label: 'Docs', value: row.docs_url, mono: true },
        { label: 'Updated', value: row.updated_at, mono: true },
      ],
      sections,
      actions: [
        iconButton({
          icon: row.enabled ? 'ban' : 'power',
          label: row.enabled ? 'Disable' : 'Enable',
          tone: row.enabled ? '' : 'accent',
          disabled: !row.configured && !row.enabled,
          disabledReason: 'Set the required credentials first',
          onClick: () => runMutation(
            () => api.put(`${BASE}/${encodeURIComponent(row.id)}`, { enabled: !row.enabled }, { profile }),
            { pending: 'Toggle platform', ok: `${row.name || row.id} ${row.enabled ? 'disabled' : 'enabled'}`, onDone: load },
          ),
        }),
        iconButton({
          icon: 'plug',
          label: 'Test connection',
          onClick: () => runMutation(
            () => api.post(`${BASE}/${encodeURIComponent(row.id)}/test`, {}, { profile }),
            { pending: 'Test platform', ok: `${row.name || row.id} test finished`, onDone: load },
          ),
        }),
      ],
      raw: row,
    });
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (!selected) {
      paint(inspectorHost, sideHint('Select a platform', [
        'Hermes can speak on 30+ messaging platforms; only the ones with credentials can be enabled.',
        'Credential values are never sent back to the browser — a set value shows as a mask, and a blank field means "leave unchanged".',
        'Interactive onboarding (pairing codes, QR flows) still has to be done on the Hermes host.',
      ]));
      return;
    }
    paint(inspectorHost, detailPanel(selected));
  }

  renderSide();

  return {
    mount(container) { clear(container); container.append(root); },
    renderInspector,
    activate() { return load(); },
    deactivate() { return {}; },
    refresh: load,
    renderToolbar,
    get data() { return rows; },
  };
}
