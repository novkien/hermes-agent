// Tools / Toolsets tab — upstream /api/tools/toolsets.
//
// Was a read-only bullet list. A toolset is really four settings (enabled,
// model, provider, env) spread over four upstream routes, so the inspector
// saves each one independently and only for the fields actually touched — env
// values arrive redacted, and resubmitting an untouched one would write the
// mask over a real credential.

import { listRows } from '../pure/envelope-list.js';
import { el, clear, statusChip, iconButton, segmented } from '../ui.js';
import { createTable } from '../components/table.js';
import { createForm } from '../components/form.js';
import { createDetail } from '../components/detail.js';
import { applyStateToTable, boolChip, filterInput, loadEnvelope, paint, runMutation, sideHint, tabToolbar } from './_kit.js';
import { filterRows, filterSummary } from '../pure/text-filter.js';

export const ROUTE = 'tools';
export const LABEL = 'Tools / Toolsets';
export const GROUP = 'BUILD & INTEGRATE';

export const SOURCE_ENDPOINTS = Object.freeze(['/api/tools/toolsets']);

const LIST_PATH = '/api/upstream/api/tools/toolsets';
const BACKENDS_PATH = '/api/upstream/api/tools/terminal/backends';

export function renderToolsets(envelope) {
  return listRows(envelope, {
    pick: (raw) => (Array.isArray(raw) ? raw : raw.toolsets || raw.items || raw.list || null),
    map: (t) => ({
      id: t.name ?? t.id ?? null,
      name: t.name ?? null,
      enabled: t.enabled === true || t.state === 'enabled',
      state: t.state ?? (t.enabled === true ? 'enabled' : t.enabled === false ? 'disabled' : null),
      description: t.description ?? null,
    }),
  });
}

const FILTERS = [
  { value: 'all', label: 'All' },
  { value: 'enabled', label: 'Enabled' },
  { value: 'available', label: 'Available' },
  { value: 'unconfigured', label: 'Needs setup' },
];

function matchesFilter(row, filter) {
  if (filter === 'enabled') return row.enabled === true;
  if (filter === 'available') return row.available === true;
  if (filter === 'unconfigured') return row.available === true && row.configured === false;
  return true;
}

export function createTools({ api, profile, toolbar }) {
  const root = el('div', { class: 'tab tab-tools' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(main);

  let rows = [];
  let meta = null;
  let selected = null;
  let filter = 'all';
  let backends = null;

  const table = createTable({
    rowId: (row) => row.name,
    emptyTitle: 'No toolsets',
    sort: { key: 'label', dir: 'asc' },
    columns: [
      {
        key: 'label',
        label: 'Toolset',
        sortable: true,
        render: (row) => el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: row.label || row.name }),
          el('span', { class: 'cell-dim mono', text: row.name }),
        ]),
      },
      { key: 'platform_label', label: 'Platform', width: '110px', sortable: true },
      {
        key: 'enabled',
        label: 'Enabled',
        width: '92px',
        sortable: true,
        render: (row) => boolChip(row.enabled),
      },
      {
        key: 'configured',
        label: 'Status',
        width: '120px',
        sortable: true,
        render: (row) => {
          if (row.available === false) return statusChip('idle', 'unavailable');
          return row.configured ? statusChip('ok', 'configured') : statusChip('warn', 'needs setup');
        },
      },
      {
        key: 'tools',
        label: 'Tools',
        align: 'right',
        width: '70px',
        sortable: true,
        sortValue: (row) => (Array.isArray(row.tools) ? row.tools.length : 0),
        render: (row) => (Array.isArray(row.tools) ? row.tools.length : null),
      },
      {
        key: 'description',
        label: 'Provides',
        render: (row) => (row.description
          ? el('span', { class: 'cell-dim mono', text: row.description, title: row.description })
          : null),
      },
    ],
    rowActions: (row) => [
      iconButton({
        icon: row.enabled ? 'ban' : 'power',
        label: row.enabled ? 'Disable toolset' : 'Enable toolset',
        tone: row.enabled ? '' : 'accent',
        onClick: () => runMutation(
          () => api.put(`/api/upstream/api/tools/toolsets/${encodeURIComponent(row.name)}`, { enabled: !row.enabled }, { profile }),
          { pending: 'Toggle toolset', ok: `${row.name} ${row.enabled ? 'disabled' : 'enabled'}`, onDone: load },
        ),
      }),
    ],
    onSelect: (row) => { selected = row; renderSide(); },
  });
  main.append(table.node);

  // The segmented control narrows by state; this narrows by name. A list
  // this long is unusable without both.
  let query = '';
  const SEARCH_FIELDS = ['name', 'label', 'description', 'category', (row) => row.tools];

  function visibleRows() {
    return filterRows(rows.filter((row) => matchesFilter(row, filter)), query, SEARCH_FIELDS);
  }

  async function load() {
    table.setLoading();
    const result = await loadEnvelope(api, LIST_PATH, {
      profile,
      pick: (raw) => (Array.isArray(raw) ? raw : raw?.toolsets || raw?.items || null),
    });
    meta = result.meta;
    rows = Array.isArray(result.data) ? result.data : [];
    applyStateToTable(table, { ...result, data: visibleRows() });
    if (selected) {
      selected = rows.find((r) => r.name === selected.name) || null;
      table.setSelected(selected?.name ?? null);
    }
    // The terminal backend selector is a separate upstream surface that only
    // makes sense on this tab, so it rides along with the toolset load.
    backends = await loadEnvelope(api, BACKENDS_PATH, { profile, allowEmpty: false });
    renderToolbar(toolbar);
    renderSide();
  }

  function renderToolbar(host) {
    if (!host) return;
    const counts = {
      all: rows.length,
      enabled: rows.filter((r) => r.enabled).length,
      available: rows.filter((r) => r.available).length,
      unconfigured: rows.filter((r) => r.available && !r.configured).length,
    };
    paint(host, tabToolbar({
      title: 'Tools / Toolsets',
      subtitle: query
        ? filterSummary(visibleRows().length, counts.all, 'toolset')
        : `${counts.enabled} of ${counts.all} enabled`,
      meta,
      onRefresh: () => load(),
      filters: [
        filterInput({
          value: query,
          placeholder: 'Find a toolset or tool…',
          ariaLabel: 'Filter toolsets',
          onChange: (next) => {
            if (next === query) return;
            query = next;
            table.setRows(visibleRows());
            renderToolbar(host);
          },
        }),
        segmented(FILTERS.map((f) => ({ ...f, count: counts[f.value] })), {
          value: filter,
          ariaLabel: 'Filter toolsets',
          onChange: (next) => {
            filter = next;
            table.setRows(visibleRows());
            renderToolbar(host);
          },
        }),
      ],
    }));
  }

  function terminalBackendSection() {
    if (!backends || backends.state !== 'ready') return null;
    const data = backends.data || {};
    const list = Array.isArray(data.backends) ? data.backends : [];
    if (!list.length) return null;
    const wrap = el('div', { class: 'stack-sm' });
    for (const backend of list) {
      wrap.append(el('div', { class: 'choice-row' }, [
        el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: backend.label || backend.name }),
          el('span', { class: 'cell-dim', text: backend.description || '' }),
        ]),
        backend.active
          ? statusChip('ok', 'active')
          : el('button', {
            class: 'btn btn-sm',
            type: 'button',
            disabled: backend.status !== 'ready',
            title: backend.status !== 'ready' ? (backend.detail || backend.status) : '',
            onclick: () => runMutation(
              () => api.put('/api/upstream/api/tools/terminal/backend', { backend: backend.name }, { profile }),
              { pending: 'Set backend', ok: `Terminal backend set to ${backend.label || backend.name}`, onDone: load },
            ),
          }, 'Use'),
      ]));
    }
    return wrap;
  }

  function detailPanel(row) {
    const sections = [];

    if (Array.isArray(row.tools) && row.tools.length) {
      sections.push({
        title: `Tools (${row.tools.length})`,
        node: el('div', { class: 'taglist taglist-static' },
          row.tools.map((t) => el('span', { class: 'taglist-tag', text: String(t) }))),
      });
    }

    sections.push({ title: 'Backend & credentials', node: configSection(row) });

    const backendSection = row.name === 'terminal' ? terminalBackendSection() : null;
    if (backendSection) sections.push({ title: 'Terminal backend', node: backendSection });

    return createDetail({
      title: row.label || row.name,
      meta,
      chips: [
        boolChip(row.enabled),
        row.available === false ? statusChip('idle', 'unavailable') : null,
        row.configured === false ? statusChip('warn', 'needs setup') : null,
      ].filter(Boolean),
      fields: [
        { label: 'Name', value: row.name, mono: true },
        { label: 'Platform', value: row.platform_label || row.platform },
        { label: 'Description', value: row.description },
      ],
      sections,
      actions: [
        iconButton({
          icon: row.enabled ? 'ban' : 'power',
          label: row.enabled ? 'Disable' : 'Enable',
          tone: row.enabled ? '' : 'accent',
          onClick: () => runMutation(
            () => api.put(`/api/upstream/api/tools/toolsets/${encodeURIComponent(row.name)}`, { enabled: !row.enabled }, { profile }),
            { pending: 'Toggle toolset', ok: `${row.name} ${row.enabled ? 'disabled' : 'enabled'}`, onDone: load },
          ),
        }),
      ],
      raw: row,
    });
  }

  /**
   * A toolset's real configuration surface: one card per provider (the backend
   * that actually implements the tools), each with its own env vars.
   *
   * Upstream never sends credential values back — an env var reports only
   * `is_set` — so this form starts blank and a blank field means "leave
   * unchanged", matching the upstream contract exactly.
   */
  function configSection(row) {
    const host = el('div');
    host.append(el('div', { class: 'field-hint', text: 'loading configuration…' }));

    api.get(`/api/upstream/api/tools/toolsets/${encodeURIComponent(row.name)}/config`, { profile })
      .then((res) => {
        const cfg = res.data || {};
        const providers = Array.isArray(cfg.providers) ? cfg.providers : [];
        clear(host);
        if (!providers.length) {
          host.append(el('div', { class: 'field-hint', text: 'This toolset has no configurable backend.' }));
          return;
        }
        if (cfg.active_provider) {
          host.append(el('div', { class: 'field-hint', text: `Active backend: ${cfg.active_provider}` }));
        }
        for (const provider of providers) host.append(providerCard(row, provider));
      })
      .catch((err) => {
        paint(host, el('div', { class: 'field-hint', text: `configuration unavailable: ${err.message}` }));
      });

    return host;
  }

  function providerCard(row, provider) {
    const base = `/api/upstream/api/tools/toolsets/${encodeURIComponent(row.name)}`;
    const envVars = Array.isArray(provider.env_vars) ? provider.env_vars : [];
    const card = el('div', { class: 'sub-card' });

    card.append(el('div', { class: 'sub-card-head' }, [
      el('div', { class: 'cell-stack' }, [
        el('span', { class: 'cell-strong', text: provider.name }),
        provider.tag ? el('span', { class: 'cell-dim', text: provider.tag }) : null,
      ].filter(Boolean)),
      el('div', { class: 'row-actions' }, [
        provider.badge ? el('span', { class: 'chip', text: provider.badge }) : null,
        provider.is_active
          ? statusChip('ok', 'active')
          : el('button', {
            class: 'btn btn-sm',
            type: 'button',
            onclick: () => runMutation(
              () => api.put(`${base}/provider`, { provider: provider.name }, { profile }),
              { pending: 'Select backend', ok: `${provider.name} selected`, onDone: load },
            ),
          }, 'Use'),
      ].filter(Boolean)),
    ]));

    if (provider.status && provider.status !== 'ready') {
      card.append(el('div', { class: 'field-hint', text: `status: ${provider.status}` }));
    }

    if (envVars.length) {
      const form = createForm({
        submitLabel: 'Save credentials',
        submitIcon: 'lock',
        note: 'Blank fields are left unchanged upstream.',
        values: Object.fromEntries(envVars.map((v) => [v.key, ''])),
        fields: envVars.map((v) => ({
          key: v.key,
          label: v.key,
          placeholder: v.is_set ? '•••••• (set)' : (v.default || ''),
          hint: v.prompt || (v.is_set ? 'A value is set. Type a new one to replace it.' : ''),
        })),
        onSubmit: async (diff) => {
          const env = Object.fromEntries(
            Object.entries(diff).filter(([, value]) => String(value ?? '').trim() !== ''),
          );
          if (!Object.keys(env).length) return;
          const saved = await runMutation(
            () => api.put(`${base}/env`, { env }, { profile }),
            { pending: 'Save credentials', ok: 'Credentials saved' },
          );
          if (saved) await load();
        },
      });
      card.append(form.node);
    }

    if (provider.post_setup) {
      card.append(el('div', { class: 'form-actions' }, [
        el('button', {
          class: 'btn btn-sm', type: 'button',
          onclick: () => runMutation(
            () => api.post(`${base}/post-setup`, { key: provider.post_setup }, { profile }),
            { pending: 'Post-setup', ok: 'Post-setup finished', onDone: load },
          ),
        }, 'Run post-setup'),
      ]));
    }

    return card;
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (!selected) {
      paint(inspectorHost, sideHint('Select a toolset', [
        'Enable or disable it, override its model and provider, and set the credentials it needs.',
        'Credential values are masked on read — only what you retype is written back.',
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
