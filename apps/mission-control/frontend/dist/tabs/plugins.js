// Plugins tab — upstream /api/dashboard/plugins/hub.
//
// The old version read /api/dashboard/plugins, which lists only the two plugins
// that ship a dashboard tab — so the tab looked empty on a host running 93 of
// them. The hub endpoint is what the Hermes web UI itself renders: every
// installed agent plugin with its runtime status, and it is the only read that
// pairs with the enable/disable routes.

import { listRows } from '../pure/envelope-list.js';
import { el, clear, statusChip, iconButton, segmented } from '../ui.js';
import { createTable } from '../components/table.js';
import { createDetail } from '../components/detail.js';
import { applyStateToTable, filterInput, loadEnvelope, paint, runMutation, sideHint, tabToolbar } from './_kit.js';
import { filterRows, filterSummary } from '../pure/text-filter.js';

export const ROUTE = 'plugins';
export const LABEL = 'Plugins';
export const GROUP = 'BUILD & INTEGRATE';

export const SOURCE_ENDPOINTS = Object.freeze(['/api/dashboard/plugins/hub']);

const HUB_PATH = '/api/upstream/api/dashboard/plugins/hub';

// `runtime_status` is upstream's own vocabulary; "active" is the only value
// that means the gateway has the plugin loaded right now.
const STATUS_TONE = {
  active: 'ok',
  inactive: 'idle',
  error: 'danger',
  disabled: 'idle',
};

export function renderPlugins(envelope) {
  return listRows(envelope, {
    pick: (raw) => (Array.isArray(raw) ? raw : raw.plugins || raw.items || raw.list || null),
    map: (plugin) => {
      const enabled = plugin.runtime_status === 'active'
        || plugin.enabled === true || plugin.active === true;
      return {
        id: plugin.name ?? plugin.id ?? null,
        name: plugin.name ?? plugin.id ?? null,
        enabled,
        state: plugin.runtime_status ?? (enabled ? 'active' : 'inactive'),
        version: plugin.version ?? null,
        description: plugin.description ?? plugin.summary ?? null,
        source: plugin.source ?? null,
      };
    },
  });
}

const FILTERS = [
  { value: 'all', label: 'All' },
  { value: 'active', label: 'Active' },
  { value: 'inactive', label: 'Inactive' },
  { value: 'dashboard', label: 'Has UI' },
];

function matchesFilter(row, filter) {
  if (filter === 'active') return row.runtime_status === 'active';
  if (filter === 'inactive') return row.runtime_status !== 'active';
  if (filter === 'dashboard') return row.has_dashboard_manifest === true;
  return true;
}

export function createPlugins({ api, profile, toolbar }) {
  const root = el('div', { class: 'tab tab-plugins' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(main);

  let rows = [];
  let orphans = [];
  let meta = null;
  let selected = null;
  let filter = 'all';

  function toggle(row) {
    const enabling = row.runtime_status !== 'active';
    const verb = enabling ? 'enable' : 'disable';
    return runMutation(
      () => api.post(
        `/api/upstream/api/dashboard/agent-plugins/${encodeURIComponent(row.name)}/${verb}`,
        {}, { profile },
      ),
      { pending: `${verb} plugin`, ok: `${row.name} ${enabling ? 'enabled' : 'disabled'}`, onDone: load },
    );
  }

  const table = createTable({
    rowId: (row) => row.name,
    emptyTitle: 'No plugins installed',
    emptyNote: 'Hermes reports no agent plugins on this host.',
    sort: { key: 'name', dir: 'asc' },
    columns: [
      {
        key: 'name',
        label: 'Plugin',
        sortable: true,
        render: (row) => el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: row.name }),
          row.description
            ? el('span', { class: 'cell-dim', text: row.description, title: row.description })
            : null,
        ].filter(Boolean)),
      },
      {
        key: 'runtime_status',
        label: 'Runtime',
        width: '104px',
        sortable: true,
        render: (row) => statusChip(STATUS_TONE[row.runtime_status] || 'idle', row.runtime_status || 'unknown'),
      },
      { key: 'version', label: 'Version', width: '90px', sortable: true, mono: true },
      { key: 'source', label: 'Source', width: '96px', sortable: true },
      {
        key: 'has_dashboard_manifest',
        label: 'UI',
        width: '60px',
        align: 'center',
        sortable: true,
        render: (row) => (row.has_dashboard_manifest ? el('span', { class: 'chip chip-accent', text: 'tab' }) : null),
      },
    ],
    rowActions: (row) => [
      iconButton({
        icon: row.runtime_status === 'active' ? 'ban' : 'power',
        label: row.runtime_status === 'active' ? 'Disable plugin' : 'Enable plugin',
        tone: row.runtime_status === 'active' ? '' : 'accent',
        onClick: () => toggle(row),
      }),
    ],
    onSelect: (row) => { selected = row; renderSide(); },
  });
  main.append(table.node);

  // The segmented control narrows by state; this narrows by name. A list
  // this long is unusable without both.
  let query = '';
  const SEARCH_FIELDS = ['name', 'label', 'title', 'description', 'category', 'version', 'source'];

  function visibleRows() {
    return filterRows(rows.filter((row) => matchesFilter(row, filter)), query, SEARCH_FIELDS);
  }

  async function load() {
    table.setLoading();
    const payload = await loadEnvelope(api, HUB_PATH, { profile, allowEmpty: false });
    meta = payload.meta;
    const body = payload.data && typeof payload.data === 'object' ? payload.data : {};
    rows = Array.isArray(body.plugins) ? body.plugins : [];
    // Dashboard plugins with no agent plugin behind them. They still render a
    // tab in the Hermes UI, so listing them separately explains the difference
    // rather than leaving them mysteriously absent.
    orphans = Array.isArray(body.orphan_dashboard_plugins) ? body.orphan_dashboard_plugins : [];
    const result = { ...payload, state: payload.state === 'ready' && !rows.length ? 'empty' : payload.state };
    applyStateToTable(table, { ...result, data: visibleRows() });
    if (selected) {
      selected = rows.find((r) => r.name === selected.name) || null;
      table.setSelected(selected?.name ?? null);
    }
    renderToolbar(toolbar);
    renderSide();
  }

  function renderToolbar(host) {
    if (!host) return;
    const active = rows.filter((r) => r.runtime_status === 'active').length;
    const counts = {
      all: rows.length,
      active,
      inactive: rows.length - active,
      dashboard: rows.filter((r) => r.has_dashboard_manifest).length,
    };
    paint(host, tabToolbar({
      title: 'Plugins',
      subtitle: query
        ? filterSummary(visibleRows().length, rows.length, 'plugin')
        : rows.length ? `${active} active of ${rows.length} installed` : '',
      meta,
      onRefresh: () => load(),
      filters: [
        filterInput({
          value: query,
          placeholder: 'Find a plugin…',
          ariaLabel: 'Filter plugins',
          onChange: (next) => {
            if (next === query) return;
            query = next;
            table.setRows(visibleRows());
            renderToolbar(host);
          },
        }),
        segmented(FILTERS.map((f) => ({ ...f, count: counts[f.value] })), {
          value: filter,
          ariaLabel: 'Filter plugins',
          onChange: (next) => {
            filter = next;
            table.setRows(visibleRows());
            renderToolbar(host);
          },
        }),
      ],
    }));
  }

  function detailPanel(row) {
    const active = row.runtime_status === 'active';
    const manifest = row.dashboard_manifest || null;

    const sections = [];
    if (row.auth_required) {
      sections.push({
        title: 'Authentication',
        node: el('div', { class: 'notice notice-warn' }, [
          el('div', { text: 'This plugin needs authentication before it can run.' }),
          row.auth_command
            ? el('code', { class: 'mono', text: row.auth_command })
            : null,
          el('div', { class: 'field-hint', text: 'Run the command on the Hermes host; the dashboard does not proxy interactive auth.' }),
        ].filter(Boolean)),
      });
    }
    if (manifest) {
      sections.push({
        title: 'Dashboard tab',
        node: el('dl', { class: 'detail-dl' }, [
          el('dt', { class: 'detail-dt', text: 'Label' }),
          el('dd', { class: 'detail-dd', text: manifest.label || '—' }),
          el('dt', { class: 'detail-dt', text: 'Path' }),
          el('dd', { class: 'detail-dd mono', text: manifest.tab?.path || '—' }),
        ]),
      });
    }

    return createDetail({
      title: row.name,
      meta,
      chips: [
        statusChip(STATUS_TONE[row.runtime_status] || 'idle', row.runtime_status || 'unknown'),
        row.source ? el('span', { class: 'chip', text: row.source }) : null,
        row.user_hidden ? el('span', { class: 'chip', text: 'hidden' }) : null,
      ].filter(Boolean),
      fields: [
        { label: 'Version', value: row.version, mono: true },
        { label: 'Description', value: row.description },
        { label: 'Path', value: row.path, mono: true },
        { label: 'Removable', value: row.can_remove ? 'yes' : 'no' },
        { label: 'Git updatable', value: row.can_update_git ? 'yes' : 'no' },
      ],
      sections,
      actions: [
        iconButton({
          icon: active ? 'ban' : 'power',
          label: active ? 'Disable' : 'Enable',
          tone: active ? '' : 'accent',
          onClick: () => toggle(row),
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
      const hint = sideHint('Select a plugin', [
        'Every agent plugin installed on this Hermes host is listed, with the runtime status the gateway reports.',
        'Enabling or disabling writes config.yaml immediately, but the running gateway only picks it up after a restart.',
        'Install, update and remove are deliberately not exposed here — they run arbitrary plugin code.',
      ]);
      if (orphans.length) {
        hint.append(el('div', { class: 'side-hint-title', text: `Dashboard-only plugins (${orphans.length})` }));
        hint.append(el('ul', { class: 'list list-plain' }, orphans.map((o) => el('li', { class: 'list-item' }, [
          el('span', { class: 'cell-strong', text: o.label || o.name }),
          el('span', { class: 'cell-dim', text: o.description || '' }),
        ]))));
      }
      paint(inspectorHost, hint);
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
