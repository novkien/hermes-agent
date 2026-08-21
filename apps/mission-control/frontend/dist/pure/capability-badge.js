// Pure capability-badge derivation from envelope meta.
// No DOM, no fetch — unit-testable with node --test.
// Contract: every source-backed response exposes meta{source_id, schema_fingerprint,
// profile_id, fetched_at, freshness, read_only, mutations_supported, degraded_reason,
// request_id} (architecture-freeze §4). The UI renders states from meta, never from a 200.

export const BADGE_STATES = Object.freeze({
  LIVE: 'live',
  FRESH: 'fresh',
  STALE: 'stale',
  UNAVAILABLE: 'unavailable',
  UNSUPPORTED: 'unsupported',
  PARTIAL: 'partial',
  EMPTY: 'empty',
});

// Order matters: first match wins. Unsupported/unavailable are the strongest signals.
const STATE_RULES = [
  { when: (m) => m && m.freshness === 'unsupported', state: BADGE_STATES.UNSUPPORTED },
  { when: (m) => m && m.freshness === 'unavailable', state: BADGE_STATES.UNAVAILABLE },
  { when: (m) => m && m.empty === true && m.freshness !== 'unsupported' && m.freshness !== 'unavailable', state: BADGE_STATES.EMPTY },
  { when: (m) => m && m.degraded_reason && m.freshness !== 'unsupported' && m.freshness !== 'unavailable', state: BADGE_STATES.PARTIAL },
  { when: (m) => m && m.freshness === 'stale', state: BADGE_STATES.STALE },
  { when: (m) => m && m.freshness === 'partial', state: BADGE_STATES.PARTIAL },
  { when: (m) => m && (m.freshness === 'live' || m.freshness === 'fresh'), state: BADGE_STATES.FRESH },
];

/** Derive the single visual state for an envelope meta object. */
export function badgeFor(meta) {
  if (!meta) return BADGE_STATES.UNAVAILABLE;
  for (const rule of STATE_RULES) {
    if (rule.when(meta)) return rule.state;
  }
  return BADGE_STATES.PARTIAL; // unknown freshness never treated as live
}

/**
 * Derive capability descriptors from envelope meta.
 * Conservative default: unknown/missing meta renders read-only.
 */
export function deriveCapabilities(meta) {
  const caps = [];
  if (!meta) return ['read-only'];
  if (meta.read_only === true) caps.push('read-only');
  else caps.push('read-write');
  const mut = meta.mutations_supported;
  if (Array.isArray(mut) && mut.length > 0) {
    caps.push(`mutations: ${mut.join(', ')}`);
  } else if (typeof mut === 'string' && mut) {
    caps.push(`mutations: ${mut}`);
  }
  if (meta.degraded_reason) caps.push(`degraded: ${meta.degraded_reason}`);
  if (meta.mode) caps.push(`mode: ${meta.mode}`);
  return caps;
}

/** Fixed read-only badges per feature matrix §10 (v1). */
const READ_ONLY_NOTES = Object.freeze({
  issues: 'read-only; mutations via native issue tooling',
  permits: 'decisions via native permit tooling (unsupported in dashboard v1)',
});

export function readOnlyBadge(featureId) {
  return READ_ONLY_NOTES[featureId] || 'read-only in dashboard v1';
}
