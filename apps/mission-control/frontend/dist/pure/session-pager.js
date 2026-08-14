// Paging the full session list across every profile.
//
// Why this exists — measured against the live dashboard, 2026-08-12:
//
// The cross-profile aggregator (`GET /api/profiles/sessions?profile=all`) is
// the fast way to get "the newest N sessions everywhere", and it reports an
// honest `total` (5,295 here) plus exact `profile_totals`. But it CANNOT page
// deeply: its handler computes
//
//     per_profile = min(max(limit + offset, limit), 500)
//
// so every profile contributes at most 500 rows to the merged pool no matter
// what `offset` asks for. The reachable ceiling is therefore
// `sum(min(profile_count, 500))` — exactly 3,169 of the 5,295 rows on this
// deployment, verified by walking the offsets until they ran dry. Raising that
// cap upstream is not the fix: it exists (#39200) because the endpoint fans a
// query out across every profile's state.db at once.
//
// The single-profile endpoint (`GET /api/sessions?profile=<name>`) does page
// correctly all the way to the end — verified to offset 2,550 of `default`'s
// 2,551 — but caps `limit` at 100.
//
// So: the aggregator gives the first screenful instantly, and everything past
// its ceiling is reached by paging each profile independently and merging.
// This module owns that arithmetic; it does no I/O, so the plan can be tested
// without a dashboard.

/** The single-profile endpoint's hard cap on `limit`. */
export const PROFILE_PAGE_SIZE = 100;

/** Rows one "load more" pulls in before handing control back to the operator. */
export const LOAD_MORE_BUDGET = 600;

/**
 * Seed the pager from the aggregator's first response.
 *
 * `cursors` counts how many of each profile's rows are already held. The
 * aggregator returns each profile's rows newest-first, and the single-profile
 * endpoint orders the same way, so a profile's count doubles as its next
 * offset.
 */
export function createPager({ total = 0, profileTotals = {}, sessions = [] } = {}) {
  const cursors = {};
  for (const name of Object.keys(profileTotals)) cursors[name] = 0;
  for (const session of sessions) {
    const name = session.profile;
    if (!name) continue;
    cursors[name] = (cursors[name] || 0) + 1;
  }
  return {
    total: Number(total) || 0,
    profileTotals: { ...profileTotals },
    cursors,
    // Profiles whose paging came back short — treat as finished even if the
    // reported total disagrees, so a miscount can never loop forever.
    done: {},
  };
}

/** How many rows are held so far, across every profile. */
export function loadedCount(pager) {
  return Object.values(pager.cursors).reduce((sum, n) => sum + n, 0);
}

export function remainingCount(pager) {
  return Math.max(0, pager.total - loadedCount(pager));
}

export function isComplete(pager) {
  return pendingProfiles(pager).length === 0;
}

function pendingProfiles(pager) {
  return Object.keys(pager.profileTotals).filter((name) => {
    if (pager.done[name]) return false;
    return (pager.cursors[name] || 0) < (pager.profileTotals[name] || 0);
  });
}

/**
 * The next round of requests, within `budget` rows.
 *
 * Largest-remainder first: `default` holds 2,551 of this deployment's 5,295
 * sessions, so draining the big profiles first is what makes the loaded count
 * actually move. Each entry is a ready-to-issue `{profile, offset, limit}`.
 */
export function planNextPages(pager, budget = LOAD_MORE_BUDGET, pageSize = PROFILE_PAGE_SIZE) {
  const pending = pendingProfiles(pager).sort((a, b) => {
    const left = (pager.profileTotals[b] || 0) - (pager.cursors[b] || 0);
    const right = (pager.profileTotals[a] || 0) - (pager.cursors[a] || 0);
    return left - right;
  });

  const plan = [];
  // Offsets are advanced locally as pages are planned so one round can take
  // several consecutive pages from the same (large) profile.
  const offsets = { ...pager.cursors };
  let spent = 0;

  while (spent < budget && plan.length < 64) {
    let progressed = false;
    for (const name of pending) {
      if (spent >= budget) break;
      const offset = offsets[name] || 0;
      const total = pager.profileTotals[name] || 0;
      if (offset >= total) continue;
      const limit = Math.min(pageSize, total - offset, budget - spent);
      if (limit <= 0) continue;
      plan.push({ profile: name, offset, limit });
      offsets[name] = offset + limit;
      spent += limit;
      progressed = true;
    }
    if (!progressed) break;
  }
  return plan;
}

/**
 * Record a completed page. `received` is how many rows came back; a short page
 * marks the profile finished, which is the safety valve against a `total` that
 * over-reports (pinned rows are back-filled past the limit upstream, so the
 * counts can legitimately drift by a row or two).
 */
export function applyPage(pager, profile, received, requested = PROFILE_PAGE_SIZE) {
  const cursors = { ...pager.cursors, [profile]: (pager.cursors[profile] || 0) + received };
  const done = { ...pager.done };
  if (received < requested) done[profile] = true;
  else if (cursors[profile] >= (pager.profileTotals[profile] || 0)) done[profile] = true;
  return { ...pager, cursors, done };
}

/**
 * Merge new rows into the list, newest first, without duplicates.
 *
 * Dedupe is by session id and the INCOMING row wins: a row re-fetched from its
 * own profile is fresher than the aggregator's copy, and pinned rows legitimately
 * arrive twice because upstream back-fills them past each profile's limit.
 */
export function mergeSessions(existing, incoming, idOf, timestampOf) {
  const byId = new Map();
  for (const row of existing || []) {
    const id = idOf(row);
    if (id) byId.set(id, row);
  }
  for (const row of incoming || []) {
    const id = idOf(row);
    if (!id) continue;
    const previous = byId.get(id);
    // Keep any client-side flags (a local pin) that the server copy lacks.
    byId.set(id, previous ? { ...previous, ...row } : row);
  }
  return [...byId.values()].sort((a, b) => timestampOf(b) - timestampOf(a));
}
