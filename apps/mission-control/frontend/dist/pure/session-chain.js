// Resolving a dashboard-listed session ROOT to the conversation Hermes is
// actually running right now.
//
// Every session listing this dashboard reads — the single-profile
// `/api/sessions` and the cross-profile aggregator alike — is backed by
// upstream's `list_sessions_rich`, which deliberately excludes "child"
// sessions: the successor Hermes starts every time it resets or compresses a
// thread. What the list returns is the chain ROOT, frozen at whatever its
// state was the moment the first reset happened.
//
// For a thread that resets on a schedule (a daily cron/orchestrator is the
// case that exposed this — 36+ resets over six weeks on one thread), the
// listed row can be frozen days or weeks in the past while the actual live
// conversation is invisible to every listing that exists. `GET
// /adapter/session-tips` is upstream's own answer to "what is this root
// right now"; this module is what the Sessions tab and the Chat tab's sider
// both use to apply it consistently, so a thread that is live right now
// surfaces as live everywhere, not just in the one tab that happened to get
// the fix first.

/**
 * Attach `.tip` to each row from a `{rootId: tip}` map (the shape
 * `GET /adapter/session-tips` returns). A root whose only "tip" is itself —
 * no successor exists — gets `tip: null`: nothing to say, render the row
 * exactly as the dashboard described it.
 */
export function attachChainTips(rows, tips, idOfRow) {
  if (!Array.isArray(rows)) return [];
  return rows.map((row) => {
    const id = idOfRow(row);
    const tip = id && tips ? tips[id] : null;
    return { ...row, tip: tip && tip.tip_id && tip.tip_id !== id ? tip : null };
  });
}

/**
 * Split a batch of ids into chunks `/adapter/session-tips` accepts.
 *
 * The adapter's `state_session_tips` hard-rejects more than 200 ids in one
 * call (`ValueError: session_ids accepts at most 200 ids per call`) — it runs
 * one recursive CTE over the whole batch, not a per-id query, so the limit is
 * a deliberate query-cost bound, not a rate limit to route around cleverly.
 * Duplicates are deduped first so a page that repeats an id (a pinned row
 * back-filled past a per-profile page boundary) does not spend two of the 200
 * slots on the same session.
 */
export function chainIdBatches(ids, size = 200) {
  const unique = [...new Set((ids || []).filter(Boolean))];
  const batches = [];
  for (let i = 0; i < unique.length; i += size) batches.push(unique.slice(i, i + size));
  return batches;
}
