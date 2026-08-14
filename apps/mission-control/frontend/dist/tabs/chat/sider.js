// The session browser in the shell's right inspector column: sessions from
// every profile merged, grouped by platform, sorted by recency, searchable, and
// with the per-session admin actions that previously forced a trip to the
// Sessions tab.

import { el, clear, emptyState, skeleton, unavailableState } from '../../ui.js';
import { icon } from '../../icons.js';
import { listFrom, recordFrom } from '../../pure/data-shape.js';
import {
  GROUP_PAGE_SIZE, SOURCE_ICONS, chainProvenanceLabel, filterSessions, openTargetId,
  platformLabel, relativeTime, sessionId as idOf, sessionTimestamp, sessionTitle,
  sortByActivity, threadIdentity,
} from '../../pure/chat-session.js';

// How many extra rows one "Show more" reveals inside a platform group. Small
// enough that the DOM stays proportional to what is on screen even when the
// full 5,000-session list is loaded.
const GROUP_REVEAL_CHUNK = 25;

/**
 * renderSider(container, ctx) paints the whole column.
 *
 * `ctx` is the tab's live state and callbacks — deliberately explicit rather
 * than a closure over the tab, so the sider has no way to reach into the
 * thread's internals.
 */
export function renderSider(container, ctx) {
  const {
    sessions, selectedId, query, expandedPlatforms, gatewayAvailable,
    gatewayUnavailableReason, onOpen, onCreate, onSearch, onRename, onDelete,
    onFork, onTogglePin, api, profile,
    // Sessions with a turn in flight right now. A Set rather than a single id:
    // the fleet runs turns from the CLI, Telegram and cron at the same time,
    // and the sider shows all of them.
    runningIds = new Set(),
    loadedTotal = 0, sessionTotal = 0, pagingComplete = true, loadingMore = false,
    loadAllRequested = false, onLoadMore, onLoadAll, onStopLoading,
  } = ctx;

  clear(container);

  const head = el('div', { class: 'chat-sider-head' });
  const newAttrs = {
    class: 'chat-new-session',
    title: gatewayAvailable ? 'Create a Gateway session' : gatewayUnavailableReason,
    // The button is the anchor for the persona picker the tab opens, so hand
    // the element over rather than making the sider know what happens next.
    onclick: (event) => onCreate(event.currentTarget),
  };
  if (!gatewayAvailable) newAttrs.disabled = 'disabled';
  head.append(el('button', newAttrs, [icon('spark', { size: 14 }), 'New session']));

  const search = el('input', {
    class: 'input chat-sider-search',
    type: 'search',
    placeholder: 'Search sessions…',
    'aria-label': 'Search sessions',
    value: query,
  });
  search.addEventListener('input', (event) => onSearch(event.target.value));
  head.append(search);
  container.append(head);

  const listWrap = el('div', { class: 'chat-sider-list' });
  container.append(listWrap);

  if (!sessions) {
    listWrap.append(unavailableState({ reason: 'Session source unavailable' }));
    return;
  }
  const rows = filterSessions(sessions, query);
  if (!rows.length) {
    // Sessions not yet paged in are the usual reason a search misses: the
    // first page is the newest 500 of (here) 5,295. Say which it is, offer to
    // pull the rest in, and fall through to the adapter's full-text index —
    // which searches every session's content regardless of what is loaded.
    listWrap.append(emptyState({
      title: query ? 'No match in the loaded sessions' : 'No sessions',
      note: query && !pagingComplete
        ? `${loadedTotal.toLocaleString()} of ${sessionTotal.toLocaleString()} loaded — load the rest, or search them all below.`
        : (query ? 'Try a different search term.' : ''),
    }));
    if (query && !pagingComplete && onLoadAll) {
      listWrap.append(el('button', {
        class: 'chat-sider-loadmore', type: 'button', onclick: () => onLoadAll(),
      }, [`Load all ${sessionTotal.toLocaleString()} sessions`]));
    }
    if (query && api) listWrap.append(directIdPanel({ api, profile, query, onOpen }));
    if (query && api) listWrap.append(globalSearchPanel({ api, profile, query, onOpen }));
    appendPagingFooter();
    return;
  }

  const pinned = rows.filter((session) => session.pinned);
  const rest = rows.filter((session) => !session.pinned);
  if (pinned.length) appendGroup('Pinned', sortByActivity(pinned), { unlimited: true });

  // Group by platform (session.source), most-recently-active platform first.
  const groups = new Map();
  for (const session of rest) {
    const key = session.source || 'other';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(session);
  }
  const sortedGroups = [...groups.entries()]
    .map(([key, group]) => [key, sortByActivity(group)])
    .sort((a, b) => sessionTimestamp(b[1][0] || {}) - sessionTimestamp(a[1][0] || {}));
  for (const [key, group] of sortedGroups) {
    appendGroup(platformLabel(key), group, { platformKey: key });
  }
  appendPagingFooter();

  /**
   * A group reveals in chunks rather than all at once. With thousands of
   * sessions loaded, expanding a platform used to build every row in one go —
   * tens of thousands of nodes for a single click. Revealing a chunk at a time
   * keeps the DOM proportional to what is actually on screen.
   */
  function appendGroup(name, groupRows, { platformKey = null, unlimited = false } = {}) {
    listWrap.append(el('div', { class: 'chat-sider-group', text: `${name} · ${groupRows.length.toLocaleString()}` }));
    const revealed = unlimited || !platformKey
      ? groupRows.length
      : (expandedPlatforms.get(platformKey) || GROUP_PAGE_SIZE);
    const visible = groupRows.slice(0, revealed);
    for (const session of visible) listWrap.append(sessionRow(session));

    const hidden = groupRows.length - visible.length;
    if (hidden > 0) {
      const step = Math.min(hidden, GROUP_REVEAL_CHUNK);
      listWrap.append(el('button', {
        class: 'chat-sider-loadmore',
        type: 'button',
        onclick: () => {
          expandedPlatforms.set(platformKey, revealed + step);
          renderSider(container, ctx);
        },
      }, [`Show ${step.toLocaleString()} more… (${hidden.toLocaleString()} hidden)`]));
    }
  }

  /**
   * How much of the real list is held, and the controls to hold more. Without
   * this the sider silently ended at its first page and looked like the whole
   * inventory.
   */
  function appendPagingFooter() {
    if (!sessionTotal) return;
    const foot = el('div', { class: 'chat-sider-paging' });
    const done = pagingComplete;
    foot.append(el('div', {
      class: 'chat-sider-paging-count',
      text: done
        ? `${loadedTotal.toLocaleString()} sessions`
        : `${loadedTotal.toLocaleString()} of ${sessionTotal.toLocaleString()} loaded`,
    }));

    if (!done) {
      const bar = el('div', { class: 'chat-sider-paging-bar' });
      const pct = Math.min(100, Math.round((loadedTotal / sessionTotal) * 100));
      bar.append(el('span', { class: 'chat-sider-paging-fill', style: `width:${pct}%` }));
      foot.append(bar);

      if (loadingMore) {
        foot.append(el('div', { class: 'chat-sider-paging-actions' }, [
          el('span', { class: 'chat-sider-paging-note', text: 'Loading…' }),
          loadAllRequested && onStopLoading
            ? el('button', {
              class: 'chat-sider-paging-btn', type: 'button', onclick: () => onStopLoading(),
            }, ['Stop'])
            : null,
        ]));
      } else {
        foot.append(el('div', { class: 'chat-sider-paging-actions' }, [
          onLoadMore
            ? el('button', {
              class: 'chat-sider-paging-btn', type: 'button', onclick: () => onLoadMore(),
            }, ['Load more'])
            : null,
          onLoadAll
            ? el('button', {
              class: 'chat-sider-paging-btn is-primary', type: 'button', onclick: () => onLoadAll(),
            }, ['Load all'])
            : null,
        ]));
      }
    }
    listWrap.append(foot);
  }

  function sessionRow(session) {
    // The dashboard lists chain ROOTS, not what is actually running: a thread
    // that resets on a schedule (a daily orchestrator is the case that exposed
    // this — 36 resets over six weeks on one thread) shows a row frozen at
    // whatever it was days or weeks ago while the live conversation is
    // invisible to every listing that exists. `session.tip`, resolved in
    // chat.js via the adapter's session-tips route, is that live conversation.
    // `openId` is what clicking (and every row action below) must target;
    // `id` stays the row's own identity for the active-row comparison and for
    // any caller that still needs to key against the listed row itself.
    const id = idOf(session);
    const openId = openTargetId(session);
    const active = openId === selectedId;
    const row = el('div', {
      class: `chat-sider-item${active ? ' active' : ''}${session.unread ? ' unread' : ''}`,
      title: sessionTitle(session),
      role: 'button',
      tabindex: '0',
    });
    row.addEventListener('click', (event) => {
      if (event.target.closest('.chat-sider-item-actions')) return;
      onOpen(openId);
    });
    row.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onOpen(openId); }
    });

    const topRow = el('div', { class: 'chat-sider-item-top' }, [
      icon(SOURCE_ICONS[session.source] || 'chat', { size: 13, className: 'chat-sider-item-icon' }),
      session.profile
        ? el('span', { class: 'chat-sider-item-profile', text: `[${session.profile}]` })
        : null,
      el('span', { class: 'chat-sider-item-title', text: sessionTitle(session) }),
      el('span', { class: 'chat-sider-item-time', text: relativeTime(sessionTimestamp(session)) }),
    ]);
    row.append(topRow);

    // A row with a resolved tip is a stand-in — say so, with the id it stands
    // in for, so a rename/delete/fork acting on the tip is never a surprise.
    const provenance = chainProvenanceLabel(session);
    if (provenance) {
      row.append(el('div', { class: 'chat-sider-item-chain mono', title: `Listed as ${id}; live conversation is ${openId}`, text: provenance }));
    }

    const preview = session.preview || session.last_activity_description || '';
    if (preview) row.append(el('div', { class: 'chat-sider-item-preview', text: String(preview).slice(0, 90) }));

    const foot = el('div', { class: 'chat-sider-item-foot' });
    // "running" means a turn is in flight on this session THIS SECOND — the
    // agent is thinking, calling a tool or writing, whoever set it going. It
    // replaced an "active" chip that meant only "this session record has not
    // ended", which was true of nearly every row and so said nothing: a chip
    // that is always lit is not an indicator.
    if (runningIds.has(openId) || runningIds.has(id)) {
      foot.append(el('span', { class: 'chip chip-live', text: 'running' }));
    } else if (session.archived) {
      foot.append(el('span', { class: 'chip chip-paused', text: 'archived' }));
    }
    const messageCount = session.tip?.message_count ?? session.message_count;
    if (messageCount) foot.append(el('span', { class: 'chat-sider-item-stat', text: `${messageCount} msgs` }));
    const thread = threadIdentity(session);
    if (thread?.threadId) {
      foot.append(el('span', { class: 'chat-sider-item-stat', title: 'Thread id', text: `thread ${thread.threadId}` }));
    }
    if (session.model) foot.append(el('span', { class: 'chat-sider-item-stat', text: session.model }));
    if (foot.childNodes.length) row.append(foot);

    // Session admin, in the browser where the sessions are. These routes were
    // already on the BFF's mutation allowlist; only the Sessions tab exposed
    // them, so renaming a thread meant leaving the conversation.
    const actions = el('div', { class: 'chat-sider-item-actions' });
    actions.append(rowAction('pin', session.pinned ? 'Unpin' : 'Pin', () => onTogglePin(session)));
    actions.append(rowAction('pencil', 'Rename', () => onRename(session)));
    if (gatewayAvailable) actions.append(rowAction('branch', 'Fork', () => onFork(session)));
    actions.append(rowAction('trash', 'Delete', () => onDelete(session), 'danger'));
    row.append(actions);

    return row;
  }
}

/**
 * Resolve the query as a session id, for sessions no listing will ever return.
 *
 * Verified upstream (hermes_state.py, `_LISTABLE_CHILD_SQL`): `list_sessions_rich`
 * — the one query behind BOTH `/api/sessions` and the cross-profile aggregator
 * — deliberately hides child sessions: sub-agent runs, delegates and
 * compression continuations. A delegated session can therefore be live, hold 85
 * messages, and still be absent from every list in the dashboard by design;
 * paging the full 5,296 rows does not surface it, because it was never a row.
 *
 * `GET /api/sessions/{id}` returns it perfectly well, and openThread already
 * falls back to that read for sessions outside the loaded window — so a
 * pasted id is enough to open one. This just makes that reachable from the
 * search box instead of requiring a hand-built URL.
 */
function directIdPanel({ api, profile, query, onOpen }) {
  const panel = el('div', { class: 'chat-sider-direct' });
  const candidate = String(query || '').trim();
  // Cheap shape check: session ids are path-safe tokens, so anything with
  // whitespace or a slash is prose, not an id.
  if (!candidate || /[\s/?#]/.test(candidate) || candidate.length < 8) return panel;

  api.get(`/api/upstream/api/sessions/${encodeURIComponent(candidate)}`, { profile })
    .then((response) => {
      const record = recordFrom(response?.data, ['session']);
      if (!record || !idOf(record)) return;
      clear(panel);
      panel.append(el('div', { class: 'chat-sider-direct-head' }, [
        icon('link', { size: 11 }),
        el('span', { text: 'Found by id — not in any session list' }),
      ]));
      panel.append(el('button', {
        class: 'chat-sider-global-result', type: 'button',
        onclick: () => onOpen(idOf(record)),
      }, [
        el('span', { class: 'chat-sider-global-id mono', text: idOf(record) }),
        el('span', {
          class: 'chat-sider-global-snippet',
          text: [
            sessionTitle(record),
            record.parent_session_id ? 'child session' : null,
            record.message_count ? `${record.message_count} msgs` : null,
          ].filter(Boolean).join(' · '),
        }),
      ]));
    })
    .catch(() => { /* not an id — the full-text panel below still applies */ });

  return panel;
}

/**
 * Full-text fallback over every session, not just the loaded 200. Backed by
 * the adapter's `GET /sessions/search` — real FTS5, no LIKE fallback
 * (`queries.py`), already on the BFF's read allowlist.
 */
function globalSearchPanel({ api, profile, query, onOpen }) {
  const panel = el('div', { class: 'chat-sider-global' });
  const trigger = el('button', {
    class: 'chat-sider-global-btn', type: 'button',
  }, [icon('search', { size: 12 }), el('span', { text: `Search every session for "${query}"` })]);
  panel.append(trigger);

  trigger.addEventListener('click', () => {
    clear(panel);
    panel.append(skeleton({ lines: 3 }));
    api.get(`/api/adapter/sessions/search?q=${encodeURIComponent(query)}&limit=25`, { profile })
      .then((response) => {
        clear(panel);
        const rows = listFrom(response?.data, ['results', 'messages']);
        const bySession = new Map();
        for (const row of rows) {
          const id = row.session_id;
          if (!id || bySession.has(id)) continue;
          bySession.set(id, row);
        }
        if (!bySession.size) {
          panel.append(el('div', { class: 'chat-sider-global-empty', text: 'No matches anywhere.' }));
          return;
        }
        for (const [id, row] of bySession) {
          const item = el('button', {
            class: 'chat-sider-global-result', type: 'button',
            onclick: () => onOpen(id),
          }, [
            el('span', { class: 'chat-sider-global-id mono', text: id }),
            el('span', {
              class: 'chat-sider-global-snippet',
              text: String(row.snippet || '').replace(/\s+/g, ' ').slice(0, 100),
            }),
          ]);
          panel.append(item);
        }
      })
      .catch(() => {
        clear(panel);
        panel.append(el('div', { class: 'chat-sider-global-empty', text: 'Search unavailable.' }));
      });
  });

  return panel;
}

function rowAction(iconName, label, onClick, tone = '') {
  const button = el('button', {
    class: `chat-sider-action${tone ? ` is-${tone}` : ''}`,
    type: 'button',
    title: label,
    'aria-label': label,
  }, [icon(iconName, { size: 11 })]);
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    onClick();
  });
  return button;
}
