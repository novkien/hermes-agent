// MCP tab — upstream /api/mcp/servers + /api/mcp/catalog.
//
// Two surfaces in one tab: the servers this Hermes has configured, and the
// catalog it can install from. Both were read-only bullet lists; add, edit,
// test, toggle and catalog-install are all real upstream routes.

import { listRows } from '../pure/envelope-list.js';
import { el, clear, statusChip, iconButton, confirmButton, segmented } from '../ui.js';
import { createTable } from '../components/table.js';
import { createForm } from '../components/form.js';
import { createDetail } from '../components/detail.js';
import { toast } from '../components/toast.js';
import {
  applyStateToTable, boolChip, filterInput, loadEnvelope, paint, primaryButton, runMutation, sideHint, tabToolbar,
} from './_kit.js';
import { filterRows, filterSummary } from '../pure/text-filter.js';

export const ROUTE = 'mcp';
export const LABEL = 'MCP';
export const GROUP = 'BUILD & INTEGRATE';

export const SOURCE_ENDPOINTS = Object.freeze(['/api/mcp/servers']);

const SERVERS_PATH = '/api/upstream/api/mcp/servers';
const CATALOG_PATH = '/api/upstream/api/mcp/catalog';

export function renderMcpServers(envelope) {
  return listRows(envelope, {
    pick: (raw) => (Array.isArray(raw) ? raw : raw.servers || raw.items || raw.list || null),
    map: (s) => ({
      id: s.name ?? s.id ?? null,
      name: s.name ?? null,
      enabled: s.enabled === true || s.state === 'enabled',
      state: s.state ?? (s.enabled === true ? 'enabled' : s.enabled === false ? 'disabled' : null),
      transport: s.transport ?? s.type ?? null,
      description: s.description ?? null,
    }),
  });
}

const VIEWS = [
  { value: 'servers', label: 'Servers' },
  { value: 'catalog', label: 'Catalog' },
];

export function createMcp({ api, profile, toolbar }) {
  const root = el('div', { class: 'tab tab-mcp' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(main);

  let view = 'servers';
  let servers = [];
  let catalog = [];
  let meta = null;
  let selected = null;
  let creating = false;

  const serverTable = createTable({
    rowId: (row) => row.name,
    emptyTitle: 'No MCP servers configured',
    emptyNote: 'Add one directly, or install from the catalog.',
    sort: { key: 'name', dir: 'asc' },
    columns: [
      { key: 'name', label: 'Server', sortable: true, render: (row) => el('span', { class: 'cell-strong', text: row.name }) },
      { key: 'transport', label: 'Transport', width: '100px', sortable: true, mono: true },
      { key: 'enabled', label: 'Enabled', width: '92px', sortable: true, render: (row) => boolChip(row.enabled) },
      {
        key: 'status',
        label: 'Status',
        width: '110px',
        sortable: true,
        render: (row) => (row.status ? statusChip(row.status === 'ok' ? 'ok' : 'warn', row.status) : null),
      },
      { key: 'description', label: 'Description', render: (row) => (row.description ? el('span', { class: 'cell-dim', text: row.description }) : null) },
    ],
    rowActions: (row) => [
      iconButton({
        icon: 'plug',
        label: 'Test connection',
        onClick: () => testServer(row),
      }),
      iconButton({
        icon: row.enabled ? 'ban' : 'power',
        label: row.enabled ? 'Disable' : 'Enable',
        onClick: () => runMutation(
          () => api.put(`${SERVERS_PATH}/${encodeURIComponent(row.name)}/enabled`, { enabled: !row.enabled }, { profile }),
          { pending: 'Toggle server', ok: `${row.name} ${row.enabled ? 'disabled' : 'enabled'}`, onDone: load },
        ),
      }),
    ],
    onSelect: (row) => { creating = false; selected = { kind: 'server', row }; renderSide(); },
  });

  const catalogTable = createTable({
    rowId: (row) => row.name,
    emptyTitle: 'Catalog empty',
    sort: { key: 'name', dir: 'asc' },
    columns: [
      { key: 'name', label: 'Entry', sortable: true, render: (row) => el('span', { class: 'cell-strong', text: row.name }) },
      { key: 'transport', label: 'Transport', width: '100px', sortable: true, mono: true },
      { key: 'auth_type', label: 'Auth', width: '90px', sortable: true },
      { key: 'description', label: 'Description', render: (row) => (row.description ? el('span', { class: 'cell-dim', text: row.description, title: row.description }) : null) },
    ],
    rowActions: (row) => [
      iconButton({
        icon: 'download',
        label: `Install ${row.name}`,
        tone: 'accent',
        onClick: () => installCatalogEntry(row),
      }),
    ],
    onSelect: (row) => { creating = false; selected = { kind: 'catalog', row }; renderSide(); },
  });

  main.append(serverTable.node, catalogTable.node);

  // One filter box serving both tables — the catalog is the longer of the two
  // and neither could be searched.
  let query = '';
  const SERVER_FIELDS = ['name', 'command', 'url', 'auth', 'transport', (row) => row.args];
  const CATALOG_FIELDS = ['name', 'title', 'description', 'category', 'url', 'command'];

  function visibleServers() { return filterRows(servers, query, SERVER_FIELDS); }
  function visibleCatalog() { return filterRows(catalog, query, CATALOG_FIELDS); }

  function paintView() {
    serverTable.node.hidden = view !== 'servers';
    catalogTable.node.hidden = view !== 'catalog';
    serverTable.setRows(visibleServers());
    catalogTable.setRows(visibleCatalog());
  }

  async function testServer(row) {
    const res = await runMutation(
      () => api.post(`${SERVERS_PATH}/${encodeURIComponent(row.name)}/test`, {}, { profile }),
      { pending: 'Test connection', ok: `${row.name} responded` },
    );
    if (res && res.data && typeof res.data === 'object') {
      const tools = Array.isArray(res.data.tools) ? res.data.tools.length : null;
      if (tools !== null) toast(`${row.name}: ${tools} tools discovered`, { tone: 'ok' });
    }
  }

  async function load() {
    if (view === 'servers') serverTable.setLoading(); else catalogTable.setLoading();

    const serverResult = await loadEnvelope(api, SERVERS_PATH, {
      profile,
      pick: (raw) => (Array.isArray(raw) ? raw : raw?.servers || null),
    });
    meta = serverResult.meta;
    servers = Array.isArray(serverResult.data) ? serverResult.data : [];
    applyStateToTable(serverTable, { ...serverResult, data: visibleServers() });

    const catalogResult = await loadEnvelope(api, CATALOG_PATH, {
      profile,
      pick: (raw) => (Array.isArray(raw) ? raw : raw?.entries || null),
    });
    catalog = Array.isArray(catalogResult.data) ? catalogResult.data : [];
    applyStateToTable(catalogTable, { ...catalogResult, data: visibleCatalog() });

    if (selected?.kind === 'server') {
      const fresh = servers.find((s) => s.name === selected.row.name);
      selected = fresh ? { kind: 'server', row: fresh } : null;
      serverTable.setSelected(fresh?.name ?? null);
    }
    paintView();
    renderToolbar(toolbar);
    renderSide();
  }

  function renderToolbar(host) {
    if (!host) return;
    paint(host, tabToolbar({
      title: 'MCP',
      subtitle: query
        ? `${filterSummary(visibleServers().length, servers.length, 'server')} · ${filterSummary(visibleCatalog().length, catalog.length, 'catalog entry')}`
        : `${servers.length} server${servers.length === 1 ? '' : 's'} · ${catalog.length} in catalog`,
      meta,
      onRefresh: () => load(),
      filters: [
        segmented(
          VIEWS.map((v) => ({ ...v, count: v.value === 'servers' ? servers.length : catalog.length })),
          {
            value: view,
            ariaLabel: 'MCP view',
            onChange: (next) => { view = next; paintView(); renderToolbar(host); },
          },
        ),
        filterInput({
          value: query,
          placeholder: 'Find a server or catalog entry…',
          ariaLabel: 'Filter MCP servers and catalog',
          onChange: (next) => {
            if (next === query) return;
            query = next;
            paintView();
            renderToolbar(host);
          },
        }),
      ],
      actions: [
        primaryButton('Add server', 'plus', () => { creating = true; selected = null; renderSide(); }),
      ],
    }));
  }

  /**
   * Create-only. Upstream has no per-server edit route — its PUT replaces the
   * whole server map — so changing a server means deleting and re-adding it,
   * which the detail pane says out loud rather than offering a save button
   * that would quietly drop the servers this form never saw.
   */
  function serverForm() {
    return createForm({
      submitLabel: 'Add server',
      submitIcon: 'plus',
      note: 'stdio servers use command + args; remote servers use a URL.',
      onCancel: () => { creating = false; renderSide(); },
      values: { name: '', command: '', args: [], url: '', auth: 'none' },
      fields: [
        { key: 'name', label: 'Name', required: true },
        { key: 'auth', label: 'Auth', type: 'select', options: ['none', 'oauth', 'header'] },
        { key: 'command', label: 'Command', hint: 'stdio only, e.g. uvx', span: 2 },
        { key: 'args', label: 'Arguments', type: 'tags', span: 2 },
        {
          key: 'url',
          label: 'URL',
          hint: 'Remote servers only. Leave blank for stdio.',
          span: 2,
          validate: (value, all) => (!value && !all.command ? 'Give either a command or a URL' : ''),
        },
      ],
      onSubmit: async (diff, all) => {
        const body = { name: all.name, auth: all.auth };
        if (all.command) { body.command = all.command; body.args = all.args || []; }
        if (all.url) body.url = all.url;
        const res = await runMutation(
          () => api.post(SERVERS_PATH, body, { profile }),
          { pending: 'Add server', ok: `${all.name} added` },
        );
        if (res) { creating = false; await load(); }
      },
    });
  }

  function serverDetail(row) {
    return createDetail({
      title: row.name,
      meta,
      chips: [
        boolChip(row.enabled),
        row.transport ? el('span', { class: 'chip', text: row.transport }) : null,
        row.status ? statusChip(row.status === 'ok' ? 'ok' : 'warn', row.status) : null,
      ].filter(Boolean),
      fields: [
        { label: 'Command', value: row.command, mono: true },
        { label: 'Arguments', value: Array.isArray(row.args) ? row.args.join(' ') : null, mono: true },
        { label: 'URL', value: row.url, mono: true },
        { label: 'Tools', value: Array.isArray(row.tools) ? row.tools.length : null },
      ],
      sections: [{
        title: 'Editing',
        node: el('div', { class: 'field-hint', text: 'Hermes has no per-server edit route — its only PUT replaces the whole server map. To change this server, delete it and add it again.' }),
      }],
      actions: [
        iconButton({ icon: 'plug', label: 'Test connection', onClick: () => testServer(row) }),
        confirmButton({
          icon: 'trash',
          label: 'Delete server',
          confirmLabel: `Delete ${row.name}? Click again to confirm`,
          onConfirm: () => runMutation(
            () => api.del(`${SERVERS_PATH}/${encodeURIComponent(row.name)}?confirm=true`, { profile }),
            { pending: 'Delete server', ok: `${row.name} deleted`, onDone: () => { selected = null; return load(); } },
          ),
        }),
      ],
      raw: row,
    });
  }

  function catalogDetail(row) {
    return createDetail({
      title: row.name,
      meta,
      chips: [
        row.transport ? el('span', { class: 'chip', text: row.transport }) : null,
        row.auth_type && row.auth_type !== 'none' ? el('span', { class: 'chip chip-warn', text: row.auth_type }) : null,
      ].filter(Boolean),
      fields: [
        { label: 'Description', value: row.description },
        { label: 'Source', value: row.source, mono: true },
        { label: 'Command', value: row.command, mono: true },
        { label: 'Required env', value: Array.isArray(row.required_env) && row.required_env.length ? row.required_env.join(', ') : 'none', mono: true },
      ],
      actions: [
        iconButton({
          icon: 'download',
          label: 'Install',
          tone: 'accent',
          onClick: () => installCatalogEntry(row),
        }),
      ],
      raw: row,
    });
  }

  function installCatalogEntry(row) {
    return runMutation(
      () => api.post('/api/upstream/api/mcp/catalog/install', { name: row.name, enable: true }, { profile }),
      { pending: 'Install', ok: `${row.name} installed`, onDone: () => { view = 'servers'; return load(); } },
    );
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (creating) {
      paint(inspectorHost, createDetail({ title: 'Add MCP server', sections: [{ node: serverForm().node }] }));
      return;
    }
    if (!selected) {
      paint(inspectorHost, sideHint('Select a server', [
        'MCP servers expose external tools to Hermes over stdio, HTTP or SSE.',
        'Test a connection before enabling it — a broken server fails every tool call it owns.',
        'OAuth-based servers must be authorised on the Hermes host; the browser redirect does not survive this proxy.',
      ]));
      return;
    }
    paint(inspectorHost, selected.kind === 'catalog' ? catalogDetail(selected.row) : serverDetail(selected.row));
  }

  paintView();
  renderSide();

  return {
    mount(container) { clear(container); container.append(root); },
    renderInspector,
    activate() { return load(); },
    deactivate() { return {}; },
    refresh: load,
    renderToolbar,
    get data() { return servers; },
  };
}
