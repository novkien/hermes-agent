// Models tab — upstream /api/model/info, /api/model/options, /api/model/moa.
//
// The old tab fetched /api/model/options and never rendered it. What matters
// operationally is which model each slot resolves to right now, so this is a
// provider-grouped catalog with the active assignment called out and a one-
// click switch, plus the auxiliary slots and the MoA preset.

import { listRows, recordView } from '../pure/envelope-list.js';
import { el, clear, statusChip, iconButton, segmented } from '../ui.js';
import { createTable } from '../components/table.js';
import { createDetail } from '../components/detail.js';
import { createStatRow } from '../components/stat.js';
import {
  loadEnvelope, applyStateToTable, tabToolbar, runMutation,
  sideHint, paint,
} from './_kit.js';

export const ROUTE = 'models';
export const LABEL = 'Models';
export const GROUP = 'BUILD & INTEGRATE';

export const SOURCE_ENDPOINTS = Object.freeze([
  '/api/model/info',
  '/api/model/options',
]);

const INFO_PATH = '/api/upstream/api/model/info';
const OPTIONS_PATH = '/api/upstream/api/model/options';
const AUX_PATH = '/api/upstream/api/model/auxiliary';
const MOA_PATH = '/api/upstream/api/model/moa';

export function renderModelInfo(envelope) {
  return recordView(envelope, {
    map: (raw) => ({
      primary: raw.primary ?? raw.default ?? raw.model ?? null,
      provider: raw.provider ?? null,
      capabilities: Array.isArray(raw.capabilities) ? raw.capabilities : null,
      aliases: Array.isArray(raw.aliases) ? raw.aliases : null,
    }),
  });
}

export function renderModelOptions(envelope) {
  return listRows(envelope, {
    pick: (raw) => raw.options || raw.models || raw.providers || raw.items || null,
    map: (m) => ({
      id: m.id ?? m.slug ?? m.name ?? null,
      provider: m.provider ?? m.slug ?? null,
      capabilities: Array.isArray(m.capabilities) ? m.capabilities : null,
      context: m.context ?? m.context_window ?? null,
    }),
  });
}

/** Flatten `providers[{slug,name,models[]}]` into one row per model. */
export function flattenModelCatalog(payload) {
  const providers = Array.isArray(payload?.providers) ? payload.providers : [];
  const rows = [];
  for (const provider of providers) {
    const featured = new Set(Array.isArray(provider.featured_models) ? provider.featured_models : []);
    const caps = provider.capabilities && typeof provider.capabilities === 'object' ? provider.capabilities : {};
    for (const model of (Array.isArray(provider.models) ? provider.models : [])) {
      rows.push({
        id: `${provider.slug}::${model}`,
        model,
        provider: provider.slug,
        provider_name: provider.name || provider.slug,
        featured: featured.has(model),
        authenticated: provider.authenticated !== false,
        is_current: provider.is_current === true && payload.model === model,
        fast: Boolean(caps[model]?.fast),
        reasoning: Boolean(caps[model]?.reasoning),
      });
    }
  }
  return rows;
}

const FILTERS = [
  { value: 'all', label: 'All' },
  { value: 'featured', label: 'Featured' },
  { value: 'reasoning', label: 'Reasoning' },
  { value: 'fast', label: 'Fast' },
];

export function createModels({ api, profile, toolbar }) {
  const root = el('div', { class: 'tab tab-models' });
  const stats = el('div', { class: 'stat-row-host' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(stats, main);

  let info = null;
  let catalog = [];
  let aux = null;
  let moa = null;
  let meta = null;
  let selected = null;
  let filter = 'all';
  let search = '';

  const table = createTable({
    rowId: (row) => row.id,
    emptyTitle: 'No models offered',
    emptyNote: 'No provider in this Hermes reports a model catalog.',
    pinFirst: true,
    sort: { key: 'model', dir: 'asc' },
    columns: [
      {
        key: 'model',
        label: 'Model',
        sortable: true,
        render: (row) => el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong mono', text: row.model }),
          row.is_current ? el('span', { class: 'chip chip-accent', text: 'active' }) : null,
        ].filter(Boolean)),
      },
      { key: 'provider_name', label: 'Provider', width: '140px', sortable: true },
      {
        key: 'reasoning',
        label: 'Reasoning',
        width: '96px',
        align: 'center',
        sortable: true,
        render: (row) => (row.reasoning ? el('span', { class: 'chip chip-ok', text: 'yes' }) : null),
      },
      {
        key: 'fast',
        label: 'Fast',
        width: '70px',
        align: 'center',
        sortable: true,
        render: (row) => (row.fast ? el('span', { class: 'chip', text: 'fast' }) : null),
      },
      {
        key: 'authenticated',
        label: 'Auth',
        width: '84px',
        sortable: true,
        render: (row) => (row.authenticated ? null : statusChip('warn', 'no key')),
      },
    ],
    rowActions: (row) => [
      row.is_current ? null : iconButton({
        icon: 'check',
        label: `Use ${row.model}`,
        tone: 'accent',
        onClick: () => assign(row),
      }),
    ].filter(Boolean),
    onSelect: (row) => { selected = row; renderSide(); },
  });
  main.append(table.node);

  function assign(row) {
    return runMutation(
      () => api.post('/api/upstream/api/model/set', {
        scope: 'main',
        provider: row.provider,
        model: row.model,
      }, { profile }),
      { pending: 'Set model', ok: `Main model set to ${row.model}`, onDone: load },
    );
  }

  function visibleRows() {
    const term = search.trim().toLowerCase();
    return catalog.filter((row) => {
      if (filter === 'featured' && !row.featured) return false;
      if (filter === 'reasoning' && !row.reasoning) return false;
      if (filter === 'fast' && !row.fast) return false;
      if (term && !`${row.model} ${row.provider_name}`.toLowerCase().includes(term)) return false;
      return true;
    });
  }

  async function load() {
    table.setLoading();
    const infoResult = await loadEnvelope(api, INFO_PATH, { profile, allowEmpty: false });
    info = infoResult.state === 'ready' ? infoResult.data : null;
    meta = infoResult.meta;

    const optionsResult = await loadEnvelope(api, OPTIONS_PATH, { profile, allowEmpty: false });
    catalog = optionsResult.state === 'ready' ? flattenModelCatalog(optionsResult.data) : [];
    applyStateToTable(table, {
      ...optionsResult,
      state: optionsResult.state === 'ready' && !catalog.length ? 'empty' : optionsResult.state,
      data: visibleRows(),
    });

    const auxResult = await loadEnvelope(api, AUX_PATH, { profile, allowEmpty: false });
    aux = auxResult.state === 'ready' ? auxResult.data : null;

    const moaResult = await loadEnvelope(api, MOA_PATH, { profile, allowEmpty: false });
    moa = moaResult.state === 'ready' ? moaResult.data : null;

    renderStats();
    renderToolbar(toolbar);
    renderSide();
  }

  function renderStats() {
    clear(stats);
    if (!info) return;
    stats.append(createStatRow([
      { label: 'Main model', value: info.model || '—', iconName: 'models', seriesIndex: 1 },
      { label: 'Provider', value: info.provider || '—', iconName: 'plug', seriesIndex: 2 },
      {
        label: 'Context',
        value: info.effective_context_length ? `${Math.round(info.effective_context_length / 1000)}k` : '—',
        iconName: 'doc',
        seriesIndex: 3,
        foot: info.auto_context_length ? 'auto-detected' : 'from config',
      },
      { label: 'Models offered', value: String(catalog.length), iconName: 'kanban', seriesIndex: 4 },
    ]));
  }

  function renderToolbar(host) {
    if (!host) return;
    const input = el('input', {
      class: 'input input-sm',
      type: 'search',
      placeholder: 'Filter models…',
      value: search,
    });
    input.addEventListener('input', () => {
      search = input.value;
      table.setRows(visibleRows());
    });
    paint(host, tabToolbar({
      title: 'Models',
      subtitle: info?.model ? `main: ${info.model}` : '',
      meta,
      onRefresh: () => load(),
      filters: [
        segmented(FILTERS, {
          value: filter,
          ariaLabel: 'Filter models',
          onChange: (next) => { filter = next; table.setRows(visibleRows()); },
        }),
        input,
      ],
    }));
  }

  function auxSection() {
    const tasks = Array.isArray(aux?.tasks) ? aux.tasks : [];
    if (!tasks.length) return null;
    return el('dl', { class: 'detail-dl' }, tasks.flatMap((slot) => [
      el('dt', { class: 'detail-dt', text: slot.task }),
      el('dd', { class: 'detail-dd mono', text: `${slot.provider || 'auto'} · ${slot.model || 'auto'}` }),
    ]));
  }

  function moaSection() {
    if (!moa || typeof moa !== 'object') return null;
    const presets = moa.presets && typeof moa.presets === 'object' ? Object.keys(moa.presets) : [];
    if (!presets.length) return null;
    const active = moa.active_preset || moa.default_preset || '';
    const host = el('div', { class: 'stack-sm' });
    host.append(el('div', { class: 'field-hint', text: 'Mixture-of-Agents presets. Switching re-sends the whole document, so hand-set slot values are preserved.' }));
    for (const name of presets) {
      const preset = moa.presets[name] || {};
      const refs = Array.isArray(preset.reference_models) ? preset.reference_models.length : 0;
      host.append(el('div', { class: 'choice-row' }, [
        el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: name }),
          el('span', { class: 'cell-dim', text: `${refs} reference model${refs === 1 ? '' : 's'} · aggregator ${preset.aggregator?.model || 'unset'}` }),
        ]),
        name === active
          ? statusChip('ok', 'active')
          : el('button', {
            class: 'btn btn-sm',
            type: 'button',
            onclick: () => runMutation(
              // Full round-trip: upstream warns that a partial body erases
              // hand-set values, so only `active_preset` differs.
              () => api.put(MOA_PATH, { ...moa, active_preset: name }, { profile }),
              { pending: 'Switch preset', ok: `MoA preset ${name} active`, onDone: load },
            ),
          }, 'Activate'),
      ]));
    }
    return host;
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (selected) {
      paint(inspectorHost, createDetail({
        title: selected.model,
        meta,
        chips: [
          selected.is_current ? el('span', { class: 'chip chip-accent', text: 'active' }) : null,
          selected.reasoning ? el('span', { class: 'chip chip-ok', text: 'reasoning' }) : null,
          selected.fast ? el('span', { class: 'chip', text: 'fast' }) : null,
          selected.authenticated ? null : statusChip('warn', 'no key'),
        ].filter(Boolean),
        fields: [
          { label: 'Provider', value: selected.provider_name },
          { label: 'Provider slug', value: selected.provider, mono: true },
          { label: 'Featured', value: selected.featured ? 'yes' : 'no' },
        ],
        actions: [
          selected.is_current ? null : iconButton({
            icon: 'check', label: 'Use as main model', tone: 'accent',
            onClick: () => assign(selected),
          }),
        ].filter(Boolean),
        raw: selected,
      }));
      return;
    }

    const sections = [];
    const auxNode = auxSection();
    if (auxNode) sections.push({ title: 'Auxiliary slots', node: auxNode });
    const moaNode = moaSection();
    if (moaNode) sections.push({ title: 'Mixture of Agents', node: moaNode });

    if (!sections.length) {
      paint(inspectorHost, sideHint('Select a model', [
        'Every model each configured provider offers is listed here.',
        'Choosing one writes the main model slot for this profile.',
      ]));
      return;
    }
    paint(inspectorHost, createDetail({
      title: 'Model assignment',
      meta,
      fields: [
        { label: 'Main model', value: info?.model, mono: true },
        { label: 'Provider', value: info?.provider, mono: true },
      ],
      sections,
    }));
  }

  renderSide();

  return {
    mount(container) { clear(container); container.append(root); },
    renderInspector,
    activate() { return load(); },
    deactivate() { return {}; },
    refresh: load,
    renderToolbar,
    get data() { return catalog; },
  };
}
