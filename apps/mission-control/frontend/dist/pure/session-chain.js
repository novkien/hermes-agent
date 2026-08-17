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
 * Return one sidebar row for each resolved conversation chain.
 *
 * The session source normally returns roots, but it can temporarily return
 * ancestors and successors together (for example while a Telegram thread is
 * being reset).  Once all of those rows resolve to the same current tip,
 * rendering each one produces several indistinguishable cards for one actual
 * conversation.  Keep the original rows intact for navigation and paging;
 * this helper is deliberately a presentation-only projection.
 *
 * Prefer the real chain root as the representative so the card's identity is
 * stable across refreshes.  If that root is outside the loaded page, prefer
 * the earliest/deepest available ancestor, with source order as the final
 * deterministic tie-breaker.
 */
export function collapseChainRows(rows, idOfRow) {
  if (!Array.isArray(rows)) return [];
  const groups = new Map();

  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index];
    const id = idOfRow(row);
    const tipId = row?.tip?.tip_id || id;
    // A malformed/anonymous record has no safe chain identity. Keep it as an
    // independent row rather than accidentally merging unrelated sessions.
    const key = tipId || `__unidentified_${index}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push({ row, index });
  }

  return [...groups.values()].map((members) => {
    let best = members[0];
    for (const candidate of members.slice(1)) {
      if (preferChainRepresentative(candidate, best)) best = candidate;
    }
    return best.row;
  });
}

function preferChainRepresentative(candidate, current) {
  const candidateRoot = !candidate.row?.parent_session_id;
  const currentRoot = !current.row?.parent_session_id;
  if (candidateRoot !== currentRoot) return candidateRoot;

  const candidateDepth = Number(candidate.row?.tip?.chain_depth);
  const currentDepth = Number(current.row?.tip?.chain_depth);
  const candidateKnownDepth = Number.isFinite(candidateDepth);
  const currentKnownDepth = Number.isFinite(currentDepth);
  if (candidateKnownDepth !== currentKnownDepth) return candidateKnownDepth;
  if (candidateKnownDepth && candidateDepth !== currentDepth) return candidateDepth > currentDepth;

  const candidateStarted = Number(candidate.row?.started_at || 0);
  const currentStarted = Number(current.row?.started_at || 0);
  if (candidateStarted !== currentStarted) return candidateStarted < currentStarted;
  return candidate.index < current.index;
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
