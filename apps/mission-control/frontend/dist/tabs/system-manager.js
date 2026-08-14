// System Manager — single DB editor for Hermes services, API credentials,
// accounts and operator notes. Cron intentionally remains in its dedicated tab.

import {
  el, clear, statusChip, segmented, iconButton, confirmButton, fmtAge,
  skeleton, unavailableState,
} from '../ui.js';
import { createTable } from '../components/table.js';
import { createForm } from '../components/form.js';

export const ROUTE = 'system-manager';
export const LABEL = 'System Manager';
export const GROUP = 'SYSTEM';
export const SOURCE_ENDPOINTS = Object.freeze([
  '/api/system-manager/services',
  '/api/system-manager/api',
  '/api/system-manager/accounts',
  '/api/system-manager/notes',
]);

const VIEWS = Object.freeze([
  { value: 'services', label: 'Services' },
  { value: 'api', label: 'API' },
  { value: 'accounts', label: 'Accounts' },
  { value: 'notes', label: 'Notes' },
]);

const SERVICE_TONE = {
  running: 'ok', active: 'ok', stopped: 'idle', inactive: 'idle',
  failed: 'danger', missing: 'danger', unknown: 'idle',
};

function rowsFrom(payload) {
  if (!payload || typeof payload !== 'object') return [];
  return Array.isArray(payload.rows) ? payload.rows : [];
}

function labelFor(row, table) {
  if (table === 'services') return row.display_name || row.name || row.id;
  if (table === 'api') return row.service || row.id;
  if (table === 'accounts') return row.email || row.username || row.provider || row.id;
  return row.title || row.key || row.id;
}

function detailsFor(row, table) {
  if (table === 'services') return `${row.host_id || '—'} · ${row.manager || '—'}`;
  if (table === 'api') return row.base_url || row.description || '';
  if (table === 'accounts') return row.provider || row.description || '';
  return row.category || row.key || '';
}

export function createSystemManager({ api, profile, toolbar }) {
  const root = el('div', { class: 'tab tab-system-manager' });
  const main = el('div', { class: 'split-main' });
  root.append(main);

  let active = 'services';
  let rows = [];
  let selected = null;
  let creating = false;
  let search = '';
  let loading = false;
  let error = null;
  let inspectorHost = null;
  let pollTimer = null;

  const table = createTable({
    rowId: (row) => row.id,
    emptyTitle: `No ${active} records`,
    emptyNote: 'Running systemd/Docker services are discovered by the 30-second reconciler.',
    sort: { key: 'name', dir: 'asc' },
    columns: columnsFor(active),
    onSelect: (row) => {
      selected = row;
      creating = false;
      renderSide();
    },
  });
  main.append(table.node);

  function columnsFor(tableName) {
    if (tableName === 'services') return [
      {
        key: 'name', label: 'Service', sortable: true,
        render: (row) => el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: labelFor(row, tableName) }),
          el('span', { class: 'cell-dim mono', text: `${row.host_id || '—'} · ${row.manager || '—'}` }),
        ]),
      },
      {
        key: 'observed_state', label: 'State', width: '110px', sortable: true,
        render: (row) => statusChip(SERVICE_TONE[row.observed_state] || 'idle', row.observed_state || 'unknown'),
      },
      { key: 'health', label: 'Health', width: '110px', sortable: true },
      { key: 'management_mode', label: 'Mode', width: '100px', sortable: true },
      {
        key: 'last_seen_at', label: 'Seen', width: '100px', sortable: true,
        render: (row) => row.last_seen_at ? fmtAge(row.last_seen_at) : '—',
      },
    ];
    if (tableName === 'api') return [
      { key: 'service', label: 'Service', sortable: true },
      { key: 'api_key', label: 'API key / token', sortable: false, mono: true },
      { key: 'base_url', label: 'Base URL', sortable: true, mono: true },
      { key: 'description', label: 'Description', sortable: true },
    ];
    if (tableName === 'accounts') return [
      { key: 'provider', label: 'Provider', sortable: true },
      { key: 'email', label: 'Email', sortable: true },
      { key: 'username', label: 'Username', sortable: true },
      { key: 'password', label: 'Password', sortable: false, mono: true },
      { key: 'totp_secret', label: 'TOTP', sortable: false, mono: true },
    ];
    return [
      {
        key: 'title', label: 'Note', sortable: true,
        render: (row) => el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: labelFor(row, tableName) }),
          el('span', { class: 'cell-dim mono', text: row.key || row.id }),
        ]),
      },
      { key: 'category', label: 'Category', width: '140px', sortable: true },
      { key: 'scope_type', label: 'Scope', width: '120px', sortable: true },
      {
        key: 'updated_at', label: 'Updated', width: '100px', sortable: true,
        render: (row) => row.updated_at ? fmtAge(row.updated_at) : '—',
      },
    ];
  }

  function visibleRows() {
    const term = search.trim().toLowerCase();
    if (!term) return rows;
    return rows.filter((row) => {
      const hay = `${labelFor(row, active)} ${detailsFor(row, active)} ${Object.values(row).join(' ')}`.toLowerCase();
      return hay.includes(term);
    });
  }

  function rebuildTable() {
    // createTable deliberately keeps its schema immutable, so swap the node
    // with a new table when the sub-surface changes.
    const next = createTable({
      rowId: (row) => row.id,
      emptyTitle: `No ${active} records`,
      emptyNote: active === 'services'
        ? 'Running systemd/Docker services are discovered by the 30-second reconciler.'
        : 'Create a record from the toolbar.',
      sort: { key: active === 'notes' ? 'updated_at' : (active === 'accounts' ? 'provider' : active === 'api' ? 'service' : 'name'), dir: active === 'notes' ? 'desc' : 'asc' },
      columns: columnsFor(active),
      onSelect: (row) => { selected = row; creating = false; renderSide(); },
    });
    table.node.replaceWith(next.node);
    Object.assign(table, next);
  }

  async function load(keepSelection = true) {
    loading = true;
    error = null;
    table.setLoading();
    const wanted = keepSelection ? selected?.id : null;
    try {
      const q = search.trim() ? `?q=${encodeURIComponent(search.trim())}&limit=500` : '?limit=500';
      const result = await api.get(`/api/system-manager/${active}${q}`, { profile });
      rows = rowsFrom(result.data);
      selected = wanted ? rows.find((row) => String(row.id) === String(wanted)) || null : null;
      table.setRows(visibleRows());
      table.setSelected(selected?.id ?? null);
    } catch (err) {
      error = err;
      rows = [];
      table.setUnavailable({ reason: err.message || 'System Manager unavailable', requestId: err.request_id });
    } finally {
      loading = false;
      renderToolbar(toolbar);
      renderSide();
    }
  }

  async function syncNow() {
    try {
      await api.post('/api/system-manager/sync', {}, { profile });
      await load(true);
    } catch (err) {
      error = err;
      renderSide();
    }
  }

  async function saveRow(where, values) {
    const result = await api.put(`/api/system-manager/${active}`, { where, values }, { profile });
    creating = false;
    const row = result.data?.row || result.data?.data?.row || null;
    await load(false);
    if (row?.id) {
      selected = rows.find((item) => item.id === row.id) || row;
      table.setSelected(selected.id);
      renderSide();
    }
    return row;
  }

  async function deleteRow(row) {
    await api.put(`/api/system-manager/${active}`, { where: { id: row.id }, values: { _delete: true } }, { profile });
    selected = null;
    creating = false;
    await load(false);
  }

  async function serviceAction(row, action) {
    await api.post(`/api/system-manager/services/${encodeURIComponent(row.id)}/action`, { action }, { profile });
    await load(true);
  }

  function renderToolbar(host) {
    if (!host) return;
    clear(host);
    const searchInput = el('input', {
      class: 'input input-sm', type: 'search', placeholder: `Search ${active}…`,
      value: search, 'data-tab-filter': 'true',
    });
    searchInput.addEventListener('input', () => {
      search = searchInput.value;
      table.setRows(visibleRows());
    });
    searchInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') load(false).catch(() => null);
    });

    const views = segmented(VIEWS, {
      value: active,
      ariaLabel: 'System Manager table',
      onChange: (next) => {
        active = next;
        search = '';
        selected = null;
        creating = false;
        rebuildTable();
        load(false).catch(() => null);
      },
    });

    const actions = [
      el('button', { class: 'btn btn-sm', type: 'button', onclick: () => syncNow() }, 'Sync now'),
    ];
    if (active !== 'services') {
      actions.push(el('button', {
        class: 'btn btn-accent btn-sm', type: 'button',
        onclick: () => { creating = true; selected = null; table.setSelected(null); renderSide(); },
      }, `New ${active === 'api' ? 'API' : active === 'accounts' ? 'account' : 'note'}`));
    }

    host.append(views, searchInput, ...actions);
  }

  function formSpec(tableName, row = {}) {
    if (tableName === 'api') return {
      fields: [
        { key: 'service', label: 'Service', required: true },
        { key: 'api_key', label: 'API key / token', span: 2 },
        { key: 'base_url', label: 'Base URL', span: 2 },
        { key: 'description', label: 'Description', span: 2 },
        { key: 'notes', label: 'Notes', type: 'textarea', span: 2 },
        { key: 'enabled', label: 'Enabled', type: 'toggle' },
      ],
      values: {
        service: row.service || '', api_key: row.api_key || '', base_url: row.base_url || '',
        description: row.description || '', notes: row.notes || '', enabled: row.enabled !== 0,
      },
    };
    if (tableName === 'accounts') return {
      fields: [
        { key: 'provider', label: 'Provider', required: true },
        { key: 'email', label: 'Email' },
        { key: 'username', label: 'Username' },
        { key: 'password', label: 'Password', span: 2 },
        { key: 'totp_secret', label: 'TOTP secret', span: 2 },
        { key: 'phone', label: 'Phone' },
        { key: 'description', label: 'Description', span: 2 },
        { key: 'notes', label: 'Notes', type: 'textarea', span: 2 },
        { key: 'enabled', label: 'Enabled', type: 'toggle' },
      ],
      values: {
        provider: row.provider || '', email: row.email || '', username: row.username || '',
        password: row.password || '', totp_secret: row.totp_secret || '', phone: row.phone || '',
        description: row.description || '', notes: row.notes || '', enabled: row.enabled !== 0,
      },
    };
    if (tableName === 'notes') return {
      fields: [
        { key: 'key', label: 'Key' },
        { key: 'title', label: 'Title' },
        { key: 'body', label: 'Body', type: 'textarea', rows: 10, span: 2 },
        { key: 'category', label: 'Category' },
        { key: 'tags', label: 'Tags', type: 'tags', span: 2 },
        { key: 'scope_type', label: 'Scope type' },
        { key: 'scope_id', label: 'Scope ID' },
        { key: 'pinned', label: 'Pinned', type: 'toggle' },
        { key: 'created_by', label: 'Created by' },
      ],
      values: {
        key: row.key || '', title: row.title || '', body: row.body || '', category: row.category || '',
        tags: Array.isArray(row.tags) ? row.tags : (typeof row.tags === 'string' && row.tags ? row.tags.split(',').map((v) => v.trim()).filter(Boolean) : []),
        scope_type: row.scope_type || '', scope_id: row.scope_id || '', pinned: row.pinned === 1,
        created_by: row.created_by || 'owner',
      },
    };
    return {
      fields: [
        { key: 'display_name', label: 'Display name' },
        { key: 'management_mode', label: 'Management mode', type: 'select', options: ['observe', 'managed'] },
        { key: 'healthcheck', label: 'Healthcheck / endpoint', span: 2 },
        { key: 'log_source', label: 'Log source', span: 2 },
        { key: 'desired_state', label: 'Desired state' },
        { key: 'notes', label: 'Notes', type: 'textarea', span: 2 },
      ],
      values: {
        display_name: row.display_name || '', management_mode: row.management_mode || 'observe',
        healthcheck: row.healthcheck || '', log_source: row.log_source || '',
        desired_state: row.desired_state || '', notes: row.notes || '',
      },
    };
  }

  function editor(tableName, row, { isNew = false } = {}) {
    const spec = formSpec(tableName, row || {});
    const form = createForm({
      fields: spec.fields,
      values: spec.values,
      submitLabel: isNew ? 'Create' : 'Save changes',
      submitIcon: isNew ? 'plus' : 'save',
      onCancel: isNew ? () => { creating = false; renderSide(); } : null,
      onSubmit: async (diff, all) => {
        const values = isNew ? all : diff;
        if (!Object.keys(values).length) return;
        form.setBusy(true);
        try {
          const where = isNew ? undefined : { id: row.id };
          const saved = await saveRow(where, values);
          if (saved) form.commit(formSpec(tableName, saved).values);
        } finally {
          form.setBusy(false);
        }
      },
    });
    return form.node;
  }

  function serviceInspector(row) {
    const managed = row.management_mode === 'managed';
    const actions = el('div', { class: 'form-actions' }, [
      ...['start', 'stop', 'restart', 'enable', 'disable'].map((action) => iconButton({
        icon: action === 'start' ? 'play' : action === 'stop' ? 'stop' : action === 'restart' ? 'retry' : action === 'enable' ? 'check' : 'ban',
        label: action,
        disabled: !managed,
        disabledReason: 'Set management_mode=managed first',
        onClick: () => serviceAction(row, action).catch((err) => { error = err; renderSide(); }),
      })),
      iconButton({
        icon: 'retry', label: 'Refresh service',
        onClick: () => serviceAction(row, 'refresh').catch((err) => { error = err; renderSide(); }),
      }),
    ]);

    const facts = el('div', { class: 'kv' }, [
      ['Host', row.host_id], ['Manager', row.manager], ['Unit', row.unit],
      ['Observed', row.observed_state], ['Substate', row.observed_substate], ['Health', row.health],
      ['PID', row.pid], ['Restarts', row.restart_count], ['Last seen', row.last_seen_at],
      ['Logs fetched', row.logs_fetched_at],
    ].map(([key, value]) => el('div', { class: 'kv-row' }, [
      el('span', { class: 'kv-k', text: key }),
      el('span', { class: 'kv-v mono', text: value === null || value === undefined || value === '' ? '—' : String(value) }),
    ])));

    const logs = el('pre', {
      class: 'mono pre-wrap',
      text: row.recent_logs || 'No captured logs yet.',
    });
    return el('div', {}, [actions, facts, editor('services', row), el('div', { class: 'inspector-title', text: 'Latest 100 log lines' }), logs]);
  }

  function renderSide() {
    if (!inspectorHost) return;
    clear(inspectorHost);
    inspectorHost.append(el('div', { class: 'inspector-title', text: 'System Manager' }));
    if (error) {
      inspectorHost.append(unavailableState({ reason: error.message || 'System Manager request failed', requestId: error.request_id }));
    }
    if (loading) {
      inspectorHost.append(skeleton({ lines: 5 }));
      return;
    }
    if (creating) {
      inspectorHost.append(editor(active, {}, { isNew: true }));
      return;
    }
    if (!selected) {
      inspectorHost.append(el('div', {
        class: 'inspector-empty',
        text: active === 'services'
          ? 'Select a service to inspect state, edit metadata or view its latest 100 log lines.'
          : `Select a ${active} record or create a new one.`,
      }));
      return;
    }

    inspectorHost.append(el('div', { class: 'cell-stack' }, [
      el('span', { class: 'cell-strong', text: labelFor(selected, active) }),
      el('span', { class: 'cell-dim mono', text: selected.id }),
    ]));

    if (active === 'services') {
      inspectorHost.append(serviceInspector(selected));
      return;
    }
    inspectorHost.append(editor(active, selected));
    inspectorHost.append(confirmButton({
      icon: 'trash',
      label: `Delete ${labelFor(selected, active)}`,
      confirmLabel: 'Click again to delete',
      onConfirm: () => deleteRow(selected).catch((err) => { error = err; renderSide(); }),
    }));
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  return {
    mount(container) {
      clear(container);
      container.append(root);
    },
    async activate() {
      renderToolbar(toolbar);
      await load(false);
      if (!pollTimer) {
        pollTimer = setInterval(() => load(true).catch(() => null), 30000);
      }
    },
    deactivate() {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
      return { selection: selected?.id || null };
    },
    refresh: () => load(true),
    renderToolbar,
    renderInspector,
    get data() { return rows; },
    get filters() { return { table: active, search }; },
  };
}
