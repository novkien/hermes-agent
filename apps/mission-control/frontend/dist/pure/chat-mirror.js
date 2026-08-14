// Mirroring a thread that is being driven from somewhere else.
//
// A Hermes session is not owned by whoever has it open. The same conversation
// can be advanced from Telegram, the CLI, a cron run or a kanban seat while the
// dashboard is looking straight at it, and until now none of that arrived: the
// tab only ever repainted a thread from its own composer, so watching a
// Telegram turn meant hitting refresh over and over.
//
// The repaint path that already exists (`reloadThread`) is a fallback for a
// turn that produced nothing — it clears the list and redraws it from scratch,
// which throws away scroll position, open tool disclosures and any selection.
// That is acceptable once, after a failed turn; it is not acceptable every few
// seconds while someone reads. So mirroring is append-only, and this module is
// the part that decides what "append" means. No DOM here — the tab renders the
// slice this returns.

/**
 * A message's identity across two reads of the same thread.
 *
 * The dashboard gives every persisted message an `id`, which is what makes
 * append-only mirroring safe. The fallback exists for gateways (and optimistic
 * local rows) that have not assigned one yet: role + timestamp + a content
 * prefix is stable enough that re-reading the same page twice does not render
 * the same message twice, which is the only property required here.
 */
export function messageKey(message) {
  if (!message || typeof message !== 'object') return '';
  const id = message.id || message.message_id;
  if (id) return String(id);
  const stamp = message.created_at || message.timestamp || '';
  const body = String(message.content || message.text || '').slice(0, 80);
  return `${message.role || 'unknown'}|${stamp}|${body}`;
}

export function messageKeys(messages) {
  return (messages || []).map(messageKey).filter(Boolean);
}

/**
 * Session-scoped gate for mirror reads that must wait for a streamed turn's
 * persisted rows to become the new baseline. It is reference-counted because
 * local and watched turns can settle close together; one finishing must not
 * reopen mirroring while the other is still synchronising.
 */
export function createMirrorBarrier() {
  const counts = new Map();
  const keyOf = (sessionId) => String(sessionId || '');

  return {
    active(sessionId) {
      return (counts.get(keyOf(sessionId)) || 0) > 0;
    },
    acquire(sessionId) {
      const key = keyOf(sessionId);
      counts.set(key, (counts.get(key) || 0) + 1);
      let released = false;
      return () => {
        if (released) return;
        released = true;
        const remaining = (counts.get(key) || 1) - 1;
        if (remaining > 0) counts.set(key, remaining);
        else counts.delete(key);
      };
    },
  };
}

function toolCallIds(message) {
  const raw = message?.tool_calls;
  let calls = raw;
  if (typeof raw === 'string') {
    try { calls = JSON.parse(raw); } catch (_err) { return []; }
  }
  if (!Array.isArray(calls)) return [];
  return calls.map((call) => call?.id).filter(Boolean);
}

/**
 * Hold back the trailing messages of a turn that is still in flight.
 *
 * The gateway persists one tool round-trip as two rows — the assistant message
 * carrying `tool_calls`, then the `role: "tool"` row with the result — and
 * `renderHistory` pairs them into a single foldable line. A mirror poll that
 * lands between those two writes would render the call as a permanently
 * "pending" row and then, one tick later, the result as a second orphan row for
 * the same call. Waiting one tick for the pair costs a few seconds of latency
 * on a tool that is still running anyway, and keeps the mirrored transcript
 * identical to the one a reload would produce.
 *
 * Only the tail is held: a call whose result was compacted away upstream is a
 * genuine orphan that will never resolve, and blocking on it would freeze the
 * mirror forever. `TAIL` is how many trailing messages count as "still in
 * flight" — beyond that, an unresolved call is treated as history.
 */
const TAIL = 3;

export function trimUnsettledTail(messages) {
  const rows = messages || [];
  const resolved = new Set(
    rows.filter((m) => m.role === 'tool' && m.tool_call_id).map((m) => m.tool_call_id),
  );
  for (let i = 0; i < rows.length; i += 1) {
    const calls = toolCallIds(rows[i]);
    if (!calls.length) continue;
    if (calls.every((id) => resolved.has(id))) continue;
    if (i < rows.length - TAIL) continue; // old orphan, not an in-flight call
    return rows.slice(0, i);
  }
  return rows;
}

/**
 * What to append to an already-rendered transcript, given a fresh read of its
 * newest page.
 *
 * Filtering by key rather than by position is deliberate: the newest page is a
 * sliding window, so the same read can drop messages off the top while adding
 * to the bottom, and an index-based diff would replay the whole page every time
 * the window moved.
 */
export function mirrorAppend(renderedKeys, fetched) {
  const seen = renderedKeys instanceof Set ? renderedKeys : new Set(renderedKeys || []);
  const fresh = (fetched || []).filter((message) => {
    const key = messageKey(message);
    return key && !seen.has(key);
  });
  return trimUnsettledTail(fresh);
}

/**
 * Is the reader parked at the bottom of the transcript?
 *
 * Mirrored messages only steal the scroll position when the reader was already
 * following the live edge. Someone scrolled up reading an earlier tool result
 * should not be yanked to the bottom because a Telegram turn landed.
 */
export function isAtBottom(element, slack = 48) {
  if (!element) return true;
  const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
  return distance <= slack;
}
