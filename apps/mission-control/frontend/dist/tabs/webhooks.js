// Webhooks tab — upstream /api/webhooks.
//
// The payload is a subsystem envelope, not a bare list: `{enabled, base_url,
// subscriptions[]}`. The subsystem toggle is the thing people actually get
// wrong — individual hooks can look healthy while the receiver is off — so it
// is the first thing this tab shows.

import { listRows } from '../pure/envelope-list.js';
import { el, clear, statusChip, iconButton, confirmButton } from '../ui.js';
import { createTable } from '../components/table.js';
import { createForm } from '../components/form.js';
import { createDetail } from '../components/detail.js';
import {
  applyStateToTable, boolChip, filterInput, loadEnvelope, paint, primaryButton, runMutation, sideHint, tabToolbar,
} from './_kit.js';
import { filterRows, filterSummary } from '../pure/text-filter.js';

export const ROUTE = 'webhooks';
export const LABEL = 'Webhooks';
export const GROUP = 'BUILD & INTEGRATE';

export const SOURCE_ENDPOINTS = Object.freeze(['/api/webhooks']);

const BASE = '/api/upstream/api/webhooks';

export function renderWebhooks(envelope) {
  return listRows(envelope, {
    pick: (raw) => raw.subscriptions || raw.webhooks || raw.items || raw.list || null,
    map: (w) => ({
      id: w.name ?? w.id ?? null,
      name: w.name ?? null,
      target: w.deliver ?? w.url ?? w.target ?? null,
      enabled: w.enabled === true || w.state === 'enabled',
      state: w.state ?? (w.enabled === true ? 'enabled' : w.enabled === false ? 'disabled' : null),
      last_delivery: w.last_delivery ?? w.last_delivery_at ?? w.last_status ?? null,
    }),
  });
}

const DELIVERY_MODES = ['log', 'telegram', 'discord', 'slack'];

export function createWebhooks({ api, profile, toolbar }) {
  const root = el('div', { class: 'tab tab-webhooks' });
  const banner = el('div', { class: 'tab-banner' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(banner, main);

  let subsystem = { enabled: false, base_url: '' };
  let rows = [];
  let meta = null;
  let selected = null;
  let creating = false;

  const table = createTable({
    rowId: (row) => row.name,
    emptyTitle: 'No webhooks',
    emptyNote: 'Create one to let an external system trigger Hermes.',
    sort: { key: 'name', dir: 'asc' },
    columns: [
      {
        key: 'name',
        label: 'Webhook',
        sortable: true,
        render: (row) => el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: row.name }),
          row.description ? el('span', { class: 'cell-dim', text: row.description }) : null,
        ].filter(Boolean)),
      },
      { key: 'deliver', label: 'Delivery', width: '110px', sortable: true },
      {
        key: 'events',
        label: 'Events',
        sortable: true,
        sortValue: (row) => (Array.isArray(row.events) ? row.events.length : 0),
        render: (row) => (Array.isArray(row.events) && row.events.length
          ? el('span', { class: 'cell-dim mono', text: row.events.join(', ') })
          : null),
      },
      { key: 'enabled', label: 'Enabled', width: '92px', sortable: true, render: (row) => boolChip(row.enabled) },
    ],
    rowActions: (row) => [
      iconButton({
        icon: row.enabled ? 'ban' : 'power',
        label: row.enabled ? 'Disable' : 'Enable',
        onClick: () => runMutation(
          () => api.put(`${BASE}/${encodeURIComponent(row.name)}/enabled`, { enabled: !row.enabled }, { profile }),
          { pending: 'Toggle webhook', ok: `${row.name} ${row.enabled ? 'disabled' : 'enabled'}`, onDone: load },
        ),
      }),
      confirmButton({
        icon: 'trash',
        label: 'Delete webhook',
        confirmLabel: `Delete ${row.name}? Click again`,
        onConfirm: () => runMutation(
          () => api.del(`${BASE}/${encodeURIComponent(row.name)}?confirm=true`, { profile }),
          { pending: 'Delete webhook', ok: `${row.name} deleted`, onDone: () => { selected = null; return load(); } },
        ),
      }),
    ],
    onSelect: (row) => { creating = false; selected = row; renderSide(); },
  });
  main.append(table.node);

  // Shared filter box: this list had no way to find a row by name.
  let query = '';
  const SEARCH_FIELDS = ['name', 'url', 'target', 'description', 'event', (row) => row.events];

  function visibleRows() {
    return filterRows(rows, query, SEARCH_FIELDS);
  }

  async function load() {
    table.setLoading();
    const result = await loadEnvelope(api, BASE, { profile, allowEmpty: false });
    meta = result.meta;
    const body = result.data && typeof result.data === 'object' ? result.data : {};
    subsystem = { enabled: body.enabled === true, base_url: body.base_url || '' };
    rows = Array.isArray(body.subscriptions) ? body.subscriptions : [];
    applyStateToTable(table, {
      ...result,
      state: result.state === 'ready' && !visibleRows().length ? 'empty' : result.state,
      data: visibleRows(),
    });
    if (selected) {
      selected = rows.find((r) => r.name === selected.name) || null;
      table.setSelected(selected?.name ?? null);
    }
    renderBanner();
    renderToolbar(toolbar);
    renderSide();
  }

  function renderBanner() {
    clear(banner);
    banner.append(el('div', { class: `notice notice-${subsystem.enabled ? 'ok' : 'warn'}` }, [
      el('div', { class: 'notice-text' }, [
        el('strong', { text: subsystem.enabled ? 'Webhook receiver is running' : 'Webhook receiver is off' }),
        el('span', {
          class: 'mono',
          text: subsystem.base_url ? ` ${subsystem.base_url}` : '',
        }),
        subsystem.enabled ? null : el('div', { class: 'field-hint', text: 'Individual webhooks will not fire until the receiver is enabled.' }),
      ].filter(Boolean)),
      el('button', {
        class: `btn btn-sm${subsystem.enabled ? '' : ' btn-accent'}`,
        type: 'button',
        onclick: () => runMutation(
          () => api.post(`${BASE}/enable`, { enabled: !subsystem.enabled }, { profile }),
          {
            pending: 'Toggle receiver',
            ok: `Webhook receiver ${subsystem.enabled ? 'disabled' : 'enabled'}`,
            onDone: load,
          },
        ),
      }, subsystem.enabled ? 'Disable receiver' : 'Enable receiver'),
    ]));
  }

  function renderToolbar(host) {
    if (!host) return;
    paint(host, tabToolbar({
      title: 'Webhooks',
      subtitle: query
        ? filterSummary(visibleRows().length, rows.length, 'webhook')
        : rows.length ? `${rows.filter((r) => r.enabled).length} of ${rows.length} enabled` : '',
      filters: [filterInput({
        value: query,
        placeholder: 'Find a webhook…',
        ariaLabel: 'Filter webhooks',
        onChange: (next) => {
          if (next === query) return;
          query = next;
          table.setRows(visibleRows());
          renderToolbar(host);
        },
      })],
      meta,
      onRefresh: () => load(),
      actions: [primaryButton('New webhook', 'plus', () => { creating = true; selected = null; table.setSelected(null); renderSide(); })],
    }));
  }

  function createForm_() {
    return createForm({
      submitLabel: 'Create webhook',
      submitIcon: 'plus',
      note: 'The secret is generated upstream if you leave it blank.',
      onCancel: () => { creating = false; renderSide(); },
      values: {
        name: '', description: '', events: [], prompt: '', script: '',
        skills: [], deliver: 'log', deliver_only: false, deliver_chat_id: '',
      },
      fields: [
        { key: 'name', label: 'Name', required: true },
        { key: 'deliver', label: 'Delivery', type: 'select', options: DELIVERY_MODES },
        { key: 'description', label: 'Description', span: 2 },
        { key: 'events', label: 'Events', type: 'tags', span: 2, hint: 'Event names this webhook reacts to.' },
        { key: 'prompt', label: 'Prompt', type: 'textarea', rows: 3, span: 2, hint: 'What the agent should do when the hook fires.' },
        { key: 'script', label: 'Script', span: 2, hint: 'Optional script to run instead of the prompt.' },
        { key: 'skills', label: 'Skills', type: 'tags', span: 2 },
        { key: 'deliver_chat_id', label: 'Deliver to chat id', hint: 'Required for chat delivery modes.' },
        { key: 'deliver_only', label: 'Deliver only', type: 'toggle', hint: 'Post the payload without running the agent.' },
      ],
      onSubmit: async (diff, all) => {
        const res = await runMutation(
          () => api.post(BASE, {
            name: all.name,
            description: all.description || null,
            events: all.events || [],
            prompt: all.prompt || null,
            script: all.script || null,
            skills: all.skills || [],
            deliver: all.deliver,
            deliver_only: Boolean(all.deliver_only),
            deliver_chat_id: all.deliver_chat_id || null,
          }, { profile }),
          { pending: 'Create webhook', ok: `${all.name} created` },
        );
        if (res) { creating = false; await load(); }
      },
    });
  }

  function detailPanel(row) {
    return createDetail({
      title: row.name,
      meta,
      chips: [boolChip(row.enabled), row.deliver ? el('span', { class: 'chip', text: row.deliver }) : null].filter(Boolean),
      fields: [
        { label: 'Description', value: row.description },
        { label: 'Events', value: Array.isArray(row.events) ? row.events.join(', ') : null, mono: true },
        { label: 'Prompt', value: row.prompt },
        { label: 'Script', value: row.script, mono: true },
        { label: 'Skills', value: Array.isArray(row.skills) ? row.skills.join(', ') : null },
        { label: 'Deliver to', value: row.deliver_chat_id, mono: true },
        {
          label: 'URL',
          value: subsystem.base_url ? `${subsystem.base_url}/hooks/${row.name}` : null,
          mono: true,
        },
      ],
      sections: [{
        title: 'Editing',
        node: el('div', { class: 'field-hint', text: 'Hermes exposes create, delete and enable only — there is no update route. Delete and recreate to change a webhook.' }),
      }],
      actions: [
        iconButton({
          icon: row.enabled ? 'ban' : 'power',
          label: row.enabled ? 'Disable' : 'Enable',
          onClick: () => runMutation(
            () => api.put(`${BASE}/${encodeURIComponent(row.name)}/enabled`, { enabled: !row.enabled }, { profile }),
            { pending: 'Toggle webhook', ok: `${row.name} ${row.enabled ? 'disabled' : 'enabled'}`, onDone: load },
          ),
        }),
        confirmButton({
          icon: 'trash',
          label: 'Delete webhook',
          confirmLabel: `Delete ${row.name}? Click again`,
          onConfirm: () => runMutation(
            () => api.del(`${BASE}/${encodeURIComponent(row.name)}?confirm=true`, { profile }),
            { pending: 'Delete webhook', ok: `${row.name} deleted`, onDone: () => { selected = null; return load(); } },
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
    if (creating) {
      paint(inspectorHost, createDetail({ title: 'New webhook', sections: [{ node: createForm_().node }] }));
      return;
    }
    if (!selected) {
      paint(inspectorHost, sideHint('Select a webhook', [
        'A webhook lets an external system trigger a Hermes run over HTTP.',
        'The receiver toggle above gates all of them at once.',
      ]));
      return;
    }
    paint(inspectorHost, detailPanel(selected));
  }

  renderBanner();
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
