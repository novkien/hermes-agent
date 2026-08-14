// Settings / System tab — upstream /api/config.
//
// 90 top-level config sections. The whole document is browsable, but only the
// paths the BFF's allow-tree permits are editable — everything else renders
// read-only with the reason attached, which is more honest than hiding it.
// Secrets are already masked server-side; this tab never sees key material.

import { recordView } from '../pure/envelope-list.js';
import { el, clear, iconButton, statusChip } from '../ui.js';
import { icon } from '../icons.js';
import { createForm } from '../components/form.js';
import { createDetail } from '../components/detail.js';
import { unsupportedState } from '../ui.js';
import {
  loadEnvelope, tabToolbar, runMutation, sideHint, paint,
} from './_kit.js';

export const ROUTE = 'settings';
export const LABEL = 'Settings / System';
export const GROUP = 'SYSTEM';

export const SOURCE_ENDPOINTS = Object.freeze([
  '/api/config',
  '/api/config/defaults',
  '/api/config/schema',
]);

// Never render these key material fields even if the payload contains them.
// The BFF redacts server-side; this is the second line of the same defence.
const REDACTED_KEYS = /(api[_-]?key|secret|token|password|credential|private[_-]?key)/i;

const SENTINEL = '[redacted]';

export function redactConfig(config) {
  if (!config || typeof config !== 'object') return config;
  if (Array.isArray(config)) return config.map(redactConfig);
  const out = {};
  for (const [k, v] of Object.entries(config)) {
    if (REDACTED_KEYS.test(k)) {
      out[k] = SENTINEL;
      continue;
    }
    out[k] = v && typeof v === 'object' ? redactConfig(v) : v;
  }
  return out;
}

export function renderConfig(envelope) {
  const view = recordView(envelope, {
    map: (raw) => redactConfig(raw),
  });
  return { ...view, redactionNote: 'secrets redacted server-side; UI renders no key material' };
}

/**
 * The writable surface, mirroring the BFF's CONFIG_WRITE_ALLOW_TREE. Kept as an
 * explicit list rather than inferred: a UI that guesses at writability produces
 * save buttons that 400, which is worse than no button.
 */
export const EDITABLE_SECTIONS = Object.freeze({
  agent: {
    label: 'Agent',
    fields: [
      {
        key: 'disabled_toolsets',
        path: ['agent', 'disabled_toolsets'],
        label: 'Disabled toolsets',
        type: 'tags',
        hint: 'Toolsets the agent may not use, regardless of per-toolset config.',
      },
    ],
  },
});

const NOT_WRITABLE_REASON = 'Hermes applies this only at startup, or it carries credentials. '
  + 'Edit it in config.yaml on the Hermes host.';

function typeOf(value) {
  if (value === null || value === undefined) return 'null';
  if (Array.isArray(value)) return `list(${value.length})`;
  return typeof value;
}

function summarize(value) {
  if (value === null || value === undefined) return '—';
  if (Array.isArray(value)) return value.length ? value.slice(0, 4).map(String).join(', ') + (value.length > 4 ? ` +${value.length - 4}` : '') : 'empty';
  if (typeof value === 'object') {
    const keys = Object.keys(value);
    return `${keys.length} key${keys.length === 1 ? '' : 's'}`;
  }
  return String(value);
}

export function createSettings({ api, profile, toolbar }) {
  const root = el('div', { class: 'tab tab-settings' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(main);

  let config = null;
  let meta = null;
  let selectedKey = null;
  let search = '';

  const sectionList = el('div', { class: 'section-list' });
  main.append(sectionList);

  async function load() {
    clear(sectionList);
    sectionList.append(el('div', { class: 'field-hint', text: 'loading configuration…' }));
    const result = await loadEnvelope(api, '/api/config', { profile, allowEmpty: false });
    meta = result.meta;
    if (result.state !== 'ready') {
      paint(sectionList, result.state === 'unsupported'
        ? unsupportedState({ title: 'Config not exposed', reason: result.reason })
        : el('div', { class: 'notice notice-warn' }, [el('div', { text: result.reason || 'Config unavailable' })]));
      renderToolbar(toolbar);
      return;
    }
    config = result.data && typeof result.data === 'object' ? result.data : {};
    renderSections();
    renderToolbar(toolbar);
    renderSide();
  }

  function sectionKeys() {
    const term = search.trim().toLowerCase();
    return Object.keys(config || {})
      .filter((key) => !term || key.toLowerCase().includes(term))
      .sort();
  }

  function renderSections() {
    clear(sectionList);
    const keys = sectionKeys();
    if (!keys.length) {
      sectionList.append(el('div', { class: 'field-hint', text: 'No section matches that filter.' }));
      return;
    }
    for (const key of keys) {
      const editable = Boolean(EDITABLE_SECTIONS[key]);
      const row = el('button', {
        class: `section-row${key === selectedKey ? ' is-selected' : ''}`,
        type: 'button',
        onclick: () => { selectedKey = key; renderSections(); renderSide(); },
      }, [
        icon(editable ? 'pencil' : 'lock', { size: 12, className: 'section-row-icon' }),
        el('span', { class: 'section-row-key', text: key }),
        el('span', { class: 'section-row-type', text: typeOf(config[key]) }),
        el('span', { class: 'section-row-value', text: summarize(config[key]) }),
      ]);
      sectionList.append(row);
    }
  }

  function renderToolbar(host) {
    if (!host) return;
    const input = el('input', {
      class: 'input input-sm',
      type: 'search',
      placeholder: 'Find a section…',
      value: search,
    });
    input.addEventListener('input', () => { search = input.value; renderSections(); });
    const total = Object.keys(config || {}).length;
    const editable = Object.keys(EDITABLE_SECTIONS).length;
    paint(host, tabToolbar({
      title: 'Settings',
      subtitle: total ? `${total} sections · ${editable} editable here` : '',
      meta,
      onRefresh: () => load(),
      filters: [input],
      actions: [iconButton({ icon: 'lock', label: 'Secrets are redacted before they reach this browser', onClick: () => {} })],
    }));
  }

  function editableForm(key) {
    const spec = EDITABLE_SECTIONS[key];
    const section = config[key] || {};
    const values = Object.fromEntries(spec.fields.map((f) => [f.key, section[f.key] ?? (f.type === 'tags' ? [] : '')]));
    return createForm({
      submitLabel: 'Save section',
      note: 'Applies live; Hermes deep-merges the fields you changed.',
      values,
      fields: spec.fields.map((f) => ({ key: f.key, label: f.label, type: f.type, hint: f.hint })),
      onSubmit: async (diff) => {
        // Send only the changed leaves, nested under their real config path, so
        // the BFF's allow-tree prune keeps the body exactly as narrow as it
        // looks. A whole-section PUT would carry untouched neighbours along.
        const body = {};
        for (const field of spec.fields) {
          if (!(field.key in diff)) continue;
          let cursor = body;
          for (const segment of field.path.slice(0, -1)) {
            cursor[segment] = cursor[segment] || {};
            cursor = cursor[segment];
          }
          cursor[field.path[field.path.length - 1]] = diff[field.key];
        }
        if (!Object.keys(body).length) return;
        const res = await runMutation(
          () => api.put('/api/config', body, { profile }),
          { pending: 'Save config', ok: `${key} saved` },
        );
        if (res) await load();
      },
    });
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (!selectedKey || !config) {
      paint(inspectorHost, sideHint('Select a section', [
        'Every Hermes config section is listed; the ones with a pencil can be edited from here.',
        'The rest are read-only on purpose — they either apply only at startup or carry credentials.',
        'Values matching an api key, token, secret or password pattern are masked before they leave the server.',
      ]));
      return;
    }

    const value = config[selectedKey];
    const spec = EDITABLE_SECTIONS[selectedKey];
    const sections = [];

    if (spec) {
      sections.push({ title: 'Editable fields', node: editableForm(selectedKey).node });
    } else {
      sections.push({
        title: 'Read-only',
        node: el('div', { class: 'notice notice-info' }, [el('div', { text: NOT_WRITABLE_REASON })]),
      });
    }

    paint(inspectorHost, createDetail({
      title: selectedKey,
      meta,
      chips: [
        spec ? statusChip('ok', 'editable') : statusChip('idle', 'read-only'),
        el('span', { class: 'chip', text: typeOf(value) }),
      ],
      sections,
      raw: value,
      rawLabel: 'Current value',
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
    get data() { return config; },
  };
}
