// Action Audit row mapping — BFF /api/audit (paginated).
// Columns per architecture-freeze §9 action_audit (request_id, actor, action,
// target, profile, timestamp, summary, upstream_status, result) — redacted
// summaries; append-only note displayed in the tab.

export const AUDIT_COLUMNS = Object.freeze([
  'request_id', 'actor', 'action', 'target', 'profile',
  'timestamp', 'summary', 'upstream_status', 'result',
]);

export function mapAuditRow(row) {
  if (!row) return null;
  const out = {};
  for (const col of AUDIT_COLUMNS) out[col] = row[col] ?? null;
  return out;
}

/** Redacted human summary: never includes request bodies or key material. */
export function summarizeAction(row) {
  if (!row) return 'unknown action (redacted)';
  const actor = row.actor || 'unknown';
  const action = row.action || 'unknown action';
  return `${actor} → ${action} (details redacted)`;
}
