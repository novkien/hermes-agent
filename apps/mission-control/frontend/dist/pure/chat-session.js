// Pure helpers for shaping and ordering chat sessions. No DOM, no api — the
// session browser and the thread header both read from here so the two can
// never disagree about what a session is called or when it was last active.

const MINUTE = 60;
const HOUR = 3600;
const DAY = 86400;

// Sessions carry epoch-seconds timestamps; the shared fmtAge helper expects
// something Date-parseable, so chat does its own relative formatting.
export function relativeTime(epochSeconds, now = Date.now() / 1000) {
  const ts = Number(epochSeconds);
  if (!Number.isFinite(ts) || ts <= 0) return '';
  const delta = Math.max(0, now - ts);
  if (delta < MINUTE) return 'now';
  if (delta < HOUR) return `${Math.floor(delta / MINUTE)}m`;
  if (delta < DAY) return `${Math.floor(delta / HOUR)}h`;
  if (delta < DAY * 7) return `${Math.floor(delta / DAY)}d`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export const SOURCE_ICONS = {
  desktop: 'command-center',
  telegram: 'channels',
  kanban: 'kanban',
  cli: 'logs',
  api: 'webhooks',
  api_server: 'webhooks',
  discord: 'channels',
  cron: 'cron',
  recovered: 'logs',
};

export const SOURCE_LABELS = {
  desktop: 'Desktop',
  telegram: 'Telegram',
  kanban: 'Kanban',
  cli: 'CLI',
  api: 'API',
  api_server: 'API Server',
  discord: 'Discord',
  cron: 'Cron',
  recovered: 'Recovered',
};

export const GROUP_PAGE_SIZE = 5;

export function platformLabel(source) {
  if (!source) return 'Other';
  if (SOURCE_LABELS[source]) return SOURCE_LABELS[source];
  return source.charAt(0).toUpperCase() + source.slice(1);
}

// The cross-profile aggregator reports recency as `last_active`; the
// single-profile /api/sessions fallback uses `last_activity_at`.
//
// When `pure/session-chain.js#attachChainTips` has resolved this row to its
// live chain tip, the tip's own activity wins: the row's own timestamp is the
// moment its chain was last reset, which can be days or weeks stale while the
// tip is running right now. Every consumer of this function — sider sorting,
// group ordering, the sider's relative-time label — becomes tip-aware for
// free, which is the point of centralizing it here rather than in each caller.
export function sessionTimestamp(session) {
  const tip = session?.tip;
  if (tip) {
    const tipTs = Number(tip.last_activity_at || tip.started_at || 0);
    if (tipTs > 0) return tipTs;
  }
  return Number(session?.last_activity_at || session?.last_active || session?.started_at || 0);
}

export function sessionId(session) {
  if (!session) return '';
  return session.id || session.session_id || session.session_key || '';
}

/**
 * Where clicking a row, or acting on it (rename, delete, fork, run trace,
 * export), should actually go: the live chain tip when one was resolved,
 * otherwise the row's own id.
 *
 * `sessionId` stays the row's own identity on purpose — it is the dedupe key
 * the pager merges on and the key `allSessions` is keyed by, and confusing
 * the two was exactly the bug this fixes: a click used to open the listed
 * root (frozen at its last reset, possibly weeks old) instead of the
 * conversation actually running today.
 */
export function openTargetId(session) {
  return session?.tip?.tip_id || sessionId(session);
}

/**
 * The provenance line for a row whose listed identity is not what is
 * actually running: "this is a stand-in for the live chain N resets deep."
 * Shared so the Sessions tab and the Chat tab sider read identically.
 */
export function chainProvenanceLabel(session) {
  const tip = session?.tip;
  if (!tip) return null;
  return `↳ chain root ${sessionId(session)} · depth ${tip.chain_depth}`;
}

/**
 * Unwrap the gateway's session-create response.
 *
 * `POST /api/sessions` answers `{"object": "hermes.session", "session": {...}}`
 * — the record is NESTED. Reading `body.id` (as the tab did) always came back
 * undefined, so "New session" created a real session upstream and then had
 * nothing to open: the thread fell back to a bare id with no title, model or
 * counts, which is exactly what "I made a session and it showed nothing"
 * looked like. Flat shapes stay supported for older gateways.
 */
export function createdSession(body) {
  if (!body || typeof body !== 'object') return null;
  const record = body.session && typeof body.session === 'object' ? body.session : body;
  const id = sessionId(record);
  if (!id) return null;
  return { ...record, id };
}

// Tip-aware for the same reason `sessionTimestamp` is: a row's own title is
// whatever it was called at its last reset, and a daily-reset thread gets a
// fresh title every day (verified live: "Daily Report Orchestrator Task
// Execution" one day, "Kanban tasks completed: Daily reports review" the
// next) — the tip's title is the one actually describing today's work.
export function sessionTitle(session) {
  return session?.tip?.title
    || session?.title
    || session?.display_name
    || (session?.preview ? String(session.preview).slice(0, 60) : '')
    || session?.id
    || 'session';
}

export function sortByActivity(rows) {
  return [...rows].sort((a, b) => sessionTimestamp(b) - sessionTimestamp(a));
}

export function filterSessions(rows, query) {
  const needle = String(query || '').trim().toLowerCase();
  if (!needle) return rows;
  return rows.filter((session) =>
    `${sessionTitle(session)} ${session.id || ''} ${session.preview || ''} ${session.profile || ''} ${session.source || ''}`
      .toLowerCase().includes(needle));
}

// Chat platforms whose sessions are bound to a specific room/thread upstream.
// For these the thread identity is the only way to tell two sessions in the
// same chat apart, so the thread header surfaces it.
const THREADED_SOURCES = new Set(['telegram', 'discord', 'slack', 'matrix', 'whatsapp']);

// Rows carry chat_id/thread_id directly (and a composite `session_key` like
// `agent:main:telegram:group:<chat_id>:<thread_id>`); origin_json is the
// upstream fallback when a row predates those columns.
export function threadIdentity(session) {
  if (!session || !THREADED_SOURCES.has(String(session.source || ''))) return null;

  // Parse the origin blob unconditionally: the dedicated columns cover the ids
  // but the chat's display name only ever lives in origin_json.
  let origin = {};
  if (typeof session.origin_json === 'string') {
    try {
      const parsed = JSON.parse(session.origin_json);
      if (parsed && typeof parsed === 'object') origin = parsed;
    } catch (_err) { /* malformed blob — fall back to the columns */ }
  }

  const chatId = session.chat_id || origin.chat_id;
  const threadId = session.thread_id || origin.thread_id;
  const chatName = session.chat_name || origin.chat_name;
  if (!chatId && !threadId) return null;

  return {
    chatId: chatId ? String(chatId) : '',
    threadId: threadId ? String(threadId) : '',
    chatType: session.chat_type || origin.chat_type ? String(session.chat_type || origin.chat_type) : '',
    chatName: chatName ? String(chatName) : '',
  };
}

/**
 * Merge a freshly created session into the browser's list without waiting for
 * the dashboard aggregator to index it.
 *
 * Creating a session POSTs to the gateway (8642) while the sider reads from the
 * dashboard (9119). The new row does not exist there yet, so the old flow's
 * full reload left the thread header with a bare id and the sider without the
 * session at all. Inserting optimistically is what makes "New session" land on
 * a real, selected thread.
 */
export function withOptimisticSession(rows, session) {
  const id = sessionId(session);
  if (!id) return rows || [];
  const existing = (rows || []).filter((row) => sessionId(row) !== id);
  return [{ ...session, id }, ...existing];
}
