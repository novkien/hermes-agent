// Shared provenance badge — renders meta (source_id, freshness,
// schema_fingerprint short, degraded_reason, read_only, request_id) on every
// source-backed panel; distinct visual states for live/fresh/stale/
// unavailable/unsupported/partial/empty. Tooltip shows the full meta JSON.

import { freshnessFromMeta } from './pure/freshness.js';
import { el } from './ui.js';

const STATE_LABELS = {
  live: 'LIVE',
  fresh: 'FRESH',
  stale: 'STALE',
  unavailable: 'UNAVAILABLE',
  unsupported: 'UNSUPPORTED',
  partial: 'PARTIAL',
  empty: 'EMPTY',
};

export function provenanceBadge(meta, { empty = false } = {}) {
  const state = empty ? 'empty' : freshnessFromMeta(meta);
  const label = STATE_LABELS[state] || state.toUpperCase();
  const shortFp = meta?.schema_fingerprint
    ? String(meta.schema_fingerprint).slice(0, 8)
    : null;
  const badge = el('span', {
    class: `prov prov-${state}`,
    role: 'status',
    'aria-label': `provenance ${label}`,
  }, label);
  if (meta) {
    badge.title = JSON.stringify(meta, null, 2);
    badge.classList.add('prov-tooltip');
    const bits = [];
    if (meta.source_id) bits.push(meta.source_id);
    if (shortFp) bits.push(`fp:${shortFp}`);
    if (meta.degraded_reason) bits.push(`degraded: ${meta.degraded_reason}`);
    if (meta.read_only) bits.push('read-only');
    if (meta.request_id) bits.push(`req:${String(meta.request_id).slice(0, 8)}`);
    badge.dataset.detail = bits.join(' · ');
  }
  return badge;
}

export function attachProvenanceDetail(elNode, meta) {
  if (!elNode || !meta) return;
  elNode.title = JSON.stringify(meta, null, 2);
}

export { STATE_LABELS };
