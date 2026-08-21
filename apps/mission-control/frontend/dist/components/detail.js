// Standard inspector body. Every tab's right pane renders through this so the
// header, field list, relations and raw payload sit in the same place no matter
// which entity is selected.

import { el, clear } from '../ui.js';
import { provenanceBadge } from '../provenance.js';

function fieldValueNode(value, { mono = false } = {}) {
  if (value === null || value === undefined || value === '') {
    return el('dd', { class: 'detail-dd', text: '—' });
  }
  if (value.nodeType) return el('dd', { class: 'detail-dd' }, [value]);
  return el('dd', { class: `detail-dd${mono ? ' mono' : ''}`, text: String(value) });
}

/**
 * @param {object} opts
 * @param {string} opts.title
 * @param {Array}  [opts.chips]     Nodes (status chips, tags)
 * @param {object} [opts.meta]      envelope meta -> provenance badge
 * @param {Array}  [opts.fields]    {label, value, mono} — value may be a Node
 * @param {Array}  [opts.sections]  {title, node} — arbitrary extra blocks (forms, tables)
 * @param {Array}  [opts.relations] {label, text, onClick} — deep links to related entities
 * @param {Array}  [opts.actions]   Nodes (buttons)
 * @param {*}      [opts.raw]       collapsible raw payload for debugging
 */
export function createDetail({
  title = '',
  chips = [],
  meta = null,
  fields = [],
  sections = [],
  relations = [],
  actions = [],
  raw = undefined,
  rawLabel = 'Raw payload',
} = {}) {
  const root = el('div', { class: 'detail' });

  const head = el('div', { class: 'detail-head' }, [
    el('div', { class: 'detail-title', text: title || '—' }),
  ]);
  const chipRow = el('div', { class: 'detail-chips' });
  for (const chip of chips) if (chip) chipRow.append(chip);
  if (meta) chipRow.append(provenanceBadge(meta));
  if (chipRow.childNodes.length) head.append(chipRow);
  root.append(head);

  if (fields.length) {
    const dl = el('dl', { class: 'detail-dl' });
    for (const field of fields) {
      if (!field) continue;
      dl.append(el('dt', { class: 'detail-dt', text: field.label }));
      dl.append(fieldValueNode(field.value, { mono: field.mono }));
    }
    root.append(dl);
  }

  for (const section of sections) {
    if (!section || !section.node) continue;
    const block = el('div', { class: 'detail-section' });
    if (section.title) block.append(el('div', { class: 'detail-section-title', text: section.title }));
    block.append(section.node);
    root.append(block);
  }

  if (relations.length) {
    const block = el('div', { class: 'detail-section' }, [
      el('div', { class: 'detail-section-title', text: 'Related' }),
    ]);
    for (const rel of relations) {
      if (!rel) continue;
      block.append(el('button', {
        class: 'detail-rel', type: 'button',
        onclick: rel.onClick || (() => {}),
      }, [
        el('span', { class: 'detail-dt', text: rel.label }),
        el('span', { text: rel.text ?? '—' }),
      ]));
    }
    root.append(block);
  }

  const liveActions = actions.filter(Boolean);
  if (liveActions.length) {
    root.append(el('div', { class: 'detail-section' }, [
      el('div', { class: 'detail-section-title', text: 'Actions' }),
      el('div', { class: 'detail-actions' }, liveActions),
    ]));
  }

  if (raw !== undefined) {
    let text;
    try { text = JSON.stringify(raw, null, 2); } catch { text = String(raw); }
    root.append(el('details', { class: 'detail-raw' }, [
      el('summary', { text: rawLabel }),
      el('pre', { text }),
    ]));
  }

  return root;
}

/** Replace an inspector host's contents with a freshly built detail body. */
export function renderDetail(host, opts) {
  clear(host);
  host.append(createDetail(opts));
  return host;
}
