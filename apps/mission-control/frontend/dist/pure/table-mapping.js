// Pure table mapping for Issues and Permits.
// Column sets frozen to the authoritative schemas:
//  - issues: u01-issue-store.md §2 (21 columns, schema v2)
//  - permits: architecture-freeze §1 (25 columns; evidence/action-plan/execution-result visible)

export const ISSUE_COLUMNS = Object.freeze([
  'id', 'fingerprint', 'issue', 'context', 'reproduction',
  'initial_assessment', 'severity', 'impact', 'expected_behavior',
  'status', 'resolution', 'verification', 'merged_into_id',
  'first_seen_at', 'last_seen_at', 'resolved_at', 'created_at',
  'updated_at', 'occurrence_count', 'created_by', 'last_updated_by',
]);

export const PERMIT_COLUMNS = Object.freeze([
  'id', 'permit_id', 'request_type', 'status', 'severity', 'profile',
  'task_id', 'session_id', 'request', 'request_summary', 'context',
  'evidence', 'action_plan', 'execution_result', 'approver',
  'approved', 'approval_note', 'approval_ts', 'expires_at',
  'created_at', 'updated_at', 'last_seen_at', 'created_by',
  'last_updated_by', 'source',
]);

export function mapIssueRow(row) {
  if (!row) return null;
  const out = {};
  for (const col of ISSUE_COLUMNS) out[col] = row[col] ?? null;
  return out;
}

export function mapPermitRow(row) {
  if (!row) return null;
  const out = {};
  for (const col of PERMIT_COLUMNS) out[col] = row[col] ?? null;
  return out;
}

export function mapOccurrenceRow(row) {
  if (!row) return null;
  return {
    id: row.id ?? null,
    issue_id: row.issue_id ?? null,
    event_type: row.event_type ?? null,
    context: row.context ?? null,
    reproduction: row.reproduction ?? null,
    initial_assessment: row.initial_assessment ?? null,
    resolution: row.resolution ?? null,
    verification: row.verification ?? null,
    evidence_ref: row.evidence_ref ?? null,
    occurred_at: row.occurred_at ?? null,
    reporter: row.reporter ?? null,
    session_ref: row.session_ref ?? null,
    task_ref: row.task_ref ?? null,
    tool_ref: row.tool_ref ?? null,
  };
}

/**
 * Build a hash-route deep link from a correlation ref when resolvable.
 * Only session/task refs resolve in v1; tool refs are name-only (correlation
 * coverage §6: issue ↔ tool_call MISSING -> label unsupported, no link).
 */
export function deepLinkForRef({ type, value }) {
  if (!value || typeof value !== 'string') return null;
  if (type === 'session') return `#/sessions?session=${encodeURIComponent(value)}`;
  if (type === 'task') return `#/kanban?task=${encodeURIComponent(value)}`;
  return null;
}
