// Profiles tab — upstream /api/profiles.
//
// A profile is a whole Hermes personality: its model, its provider, and its
// "soul" (the per-profile system prompt). The soul is the reason this tab is a
// two-pane editor rather than a list — it is the highest-leverage text in the
// system and previously had no interface at all.

import { listRows } from '../pure/envelope-list.js';
import { el, clear, statusChip, confirmButton, iconButton } from '../ui.js';
import { createTable } from '../components/table.js';
import { createForm } from '../components/form.js';
import { createDetail } from '../components/detail.js';
import { createCodeEditor } from '../components/code-editor.js';
import {
  applyStateToTable, filterInput, loadEnvelope, paint, primaryButton, runMutation, sideHint, tabToolbar,
} from './_kit.js';
import { filterRows, filterSummary } from '../pure/text-filter.js';

export const ROUTE = 'profiles';
export const LABEL = 'Profiles';
export const GROUP = 'BUILD & INTEGRATE';

export const SOURCE_ENDPOINTS = Object.freeze(['/api/profiles']);

const LIST_PATH = '/api/upstream/api/profiles';

export function renderProfiles(envelope, activeName = null) {
  return listRows(envelope, {
    pick: (raw) => raw.profiles || raw.items || raw.list || null,
    map: (p) => {
      const name = p.name ?? p.id ?? null;
      return {
        name,
        is_active: name === activeName || p.is_active === true,
        is_default: p.is_default === true || name === 'default',
        gateway_state: p.gateway_state ?? p.status ?? null,
        description: p.description ?? null,
        model: p.model ?? null,
      };
    },
  });
}

export function createProfiles({ api, profile, toolbar }) {
  const root = el('div', { class: 'tab tab-profiles' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(main);

  let rows = [];
  let meta = null;
  let selected = null;
  let creating = false;

  const table = createTable({
    rowId: (row) => row.name,
    emptyTitle: 'No profiles',
    emptyNote: 'Hermes reports no profiles for this installation.',
    sort: { key: 'name', dir: 'asc' },
    columns: [
      {
        key: 'name',
        label: 'Profile',
        sortable: true,
        render: (row) => el('span', { class: 'cell-strong' }, [
          row.name,
          row.is_default ? el('span', { class: 'chip chip-accent', text: 'default' }) : null,
        ].filter(Boolean)),
      },
      {
        key: 'gateway_running',
        label: 'Gateway',
        width: '110px',
        sortable: true,
        render: (row) => statusChip(row.gateway_running ? 'ok' : 'idle', row.gateway_running ? 'running' : 'stopped'),
      },
      { key: 'model', label: 'Model', sortable: true, mono: true },
      { key: 'provider', label: 'Provider', sortable: true, mono: true },
      { key: 'skill_count', label: 'Skills', align: 'right', width: '70px', sortable: true },
      {
        key: 'description',
        label: 'Description',
        render: (row) => (row.description
          ? el('span', { class: 'cell-dim', text: row.description, title: row.description })
          : null),
      },
    ],
    onSelect: (row) => { creating = false; selected = row; renderSide(); },
  });
  main.append(table.node);

  // Shared filter box: this list had no way to find a row by name.
  let query = '';
  const SEARCH_FIELDS = ['name', 'id', 'label', 'description', 'path', 'model', 'provider'];

  function visibleRows() {
    return filterRows(rows, query, SEARCH_FIELDS);
  }

  async function load() {
    table.setLoading();
    const result = await loadEnvelope(api, LIST_PATH, {
      profile,
      pick: (raw) => (Array.isArray(raw) ? raw : raw?.profiles || raw?.items || null),
    });
    meta = result.meta;
    rows = Array.isArray(result.data) ? result.data : [];
    applyStateToTable(table, { ...result, data: visibleRows() });
    if (selected) {
      const fresh = rows.find((r) => r.name === selected.name);
      selected = fresh || null;
      table.setSelected(selected?.name ?? null);
    }
    renderToolbar(toolbar);
    renderSide();
  }

  function renderToolbar(host) {
    if (!host) return;
    paint(host, tabToolbar({
      title: 'Profiles',
      subtitle: query
        ? filterSummary(visibleRows().length, rows.length, 'profile')
        : rows.length ? `${rows.length} configured` : '',
      filters: [filterInput({
        value: query,
        placeholder: 'Find a profile…',
        ariaLabel: 'Filter profiles',
        onChange: (next) => {
          if (next === query) return;
          query = next;
          table.setRows(visibleRows());
          renderToolbar(host);
        },
      })],
      meta,
      onRefresh: () => load(),
      actions: [
        primaryButton('New profile', 'plus', () => { creating = true; selected = null; table.setSelected(null); renderSide(); }),
      ],
    }));
  }

  function createPanel() {
    const form = createForm({
      submitLabel: 'Create profile',
      submitIcon: 'plus',
      note: 'A new profile starts from Hermes defaults.',
      onCancel: () => { creating = false; renderSide(); },
      fields: [
        { key: 'name', label: 'Name', required: true, span: 2, hint: 'Lowercase identifier, no spaces.' },
        { key: 'description', label: 'Description', type: 'textarea', rows: 2, span: 2 },
      ],
      values: { name: '', description: '' },
      onSubmit: async (diff, all) => {
        const res = await runMutation(
          () => api.post('/api/upstream/api/profiles', { name: all.name, description: all.description || '' }, { profile }),
          { pending: 'Create profile', ok: `Profile ${all.name} created` },
        );
        if (res) { creating = false; await load(); }
      },
    });
    return createDetail({ title: 'New profile', sections: [{ node: form.node }] });
  }

  function soulSection(name) {
    const host = el('div', { class: 'soul-editor' });
    const status = el('div', { class: 'field-hint', text: 'loading soul…' });
    host.append(status);

    api.get(`/api/upstream/api/profiles/${encodeURIComponent(name)}/soul`, { profile })
      .then((res) => {
        const body = res.data || {};
        const text = typeof body === 'string' ? body : (body.content ?? '');
        clear(host);
        const editor = createCodeEditor({
          value: String(text || ''),
          language: 'markdown',
          wrap: true,
        });
        const save = el('button', { class: 'btn btn-accent btn-sm', type: 'button' }, 'Save soul');
        save.addEventListener('click', async () => {
          save.disabled = true;
          const next = editor.getValue();
          const res = await runMutation(
            () => api.put(`/api/upstream/api/profiles/${encodeURIComponent(name)}/soul`, { content: next }, { profile }),
            { pending: 'Save soul', ok: `Soul saved for ${name}` },
          );
          if (res) editor.markClean(next);
          save.disabled = false;
        });
        host.append(
          el('p', { class: 'field-hint', text: 'The system prompt this profile runs with. Applies to new turns.' }),
          editor.node,
          el('div', { class: 'form-actions' }, [save]),
        );
      })
      .catch((err) => {
        status.textContent = `soul unavailable: ${err.message}`;
      });

    return host;
  }

  function detailPanel(row) {
    const settings = createForm({
      submitLabel: 'Save',
      values: {
        description: row.description ?? '',
        model: row.model ?? '',
      },
      fields: [
        { key: 'description', label: 'Description', type: 'textarea', rows: 2, span: 2 },
        { key: 'model', label: 'Model', hint: 'Model alias or full id assigned to this profile.' },
      ],
      onSubmit: async (diff) => {
        const base = `/api/upstream/api/profiles/${encodeURIComponent(row.name)}`;
        // Description and model are separate upstream routes, so only the
        // fields the operator actually touched are sent.
        if ('description' in diff) {
          await runMutation(() => api.put(`${base}/description`, { description: diff.description }, { profile }),
            { pending: 'Save description', ok: 'Description saved' });
        }
        if ('model' in diff) {
          await runMutation(() => api.put(`${base}/model`, { model: diff.model }, { profile }),
            { pending: 'Save model', ok: 'Model saved' });
        }
        await load();
      },
    });

    return createDetail({
      title: row.name,
      meta,
      chips: [
        row.is_default ? el('span', { class: 'chip chip-accent', text: 'default' }) : null,
        statusChip(row.gateway_running ? 'ok' : 'idle', row.gateway_running ? 'gateway running' : 'gateway stopped'),
      ].filter(Boolean),
      fields: [
        { label: 'Path', value: row.path, mono: true },
        { label: 'Provider', value: row.provider, mono: true },
        { label: 'Skills', value: row.skill_count },
        { label: 'Env configured', value: row.has_env ? 'yes' : 'no' },
        { label: 'Distribution', value: row.distribution_name ? `${row.distribution_name} ${row.distribution_version ?? ''}`.trim() : null },
      ],
      sections: [
        { title: 'Settings', node: settings.node },
        { title: 'Soul (system prompt)', node: soulSection(row.name) },
      ],
      actions: [
        iconButton({
          icon: 'spark',
          label: 'Describe automatically',
          onClick: () => runMutation(
            () => api.post(`/api/upstream/api/profiles/${encodeURIComponent(row.name)}/describe-auto`, {}, { profile }),
            { pending: 'Describe', ok: 'Description regenerated', onDone: load },
          ),
        }),
        row.is_default ? null : confirmButton({
          icon: 'trash',
          label: 'Delete profile',
          confirmLabel: `Delete ${row.name}? Click again to confirm`,
          onConfirm: () => runMutation(
            () => api.del(`/api/upstream/api/profiles/${encodeURIComponent(row.name)}?confirm=true`, { profile }),
            { pending: 'Delete profile', ok: `Profile ${row.name} deleted`, onDone: () => { selected = null; return load(); } },
          ),
        }),
      ].filter(Boolean),
      raw: row,
    });
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (creating) { paint(inspectorHost, createPanel()); return; }
    if (!selected) {
      paint(inspectorHost, sideHint('Select a profile', [
        'Each profile carries its own model, provider, skills and soul.',
        'The soul is the system prompt Hermes runs that profile with.',
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
