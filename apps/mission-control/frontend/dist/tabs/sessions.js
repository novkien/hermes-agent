// Sessions — Hermes dashboard list contract plus adapter timeline detail.

import { el, clear, skeleton, emptyState, errorPanel, unavailableState, fmtTime, fmtAge } from '../ui.js';
import { provenanceBadge } from '../provenance.js';
import { recordFrom, sessionRows } from '../pure/data-shape.js';
import { sessionId as idOf, sessionTimestamp as chainActivity } from '../pure/chat-session.js';
import { attachChainTips } from '../pure/session-chain.js';
import { fetchChainTips, filterInput, sideHint, paint, tabToolbar } from './_kit.js';
import { filterRows, filterSummary } from '../pure/text-filter.js';
import { renderSessionDetail } from './session-detail.js';

export const SSE_EVENTS = Object.freeze(['session.changed']);

// The dashboard is the session list authority — it owns the curation (archived,
// delegate exclusion, source filters), the paging and every mutation. But it
// lists chain ROOTS: when a thread resets, Hermes ends its session with
// end_reason='session_reset' and starts a successor carrying
// parent_session_id, and the dashboard's listable-child rule hides that
// successor. So an actively-running thread is listed frozen at its last reset,
// carrying the dead root's message count and timestamps.
//
// Rather than build a second session list, each page is overlaid with the
// adapter's chain-tip resolution: the dashboard still says WHICH conversations
// exist, the overlay says what each one currently IS. Rows are then re-sorted
// by the tip's activity over a window wider than the page, so a thread that is
// working right now surfaces instead of sinking to its root's timestamp.
const WINDOW_SIZE = 100; // dashboard /api/sessions caps `limit` at 100

export function createSessions({ api, profile, sse }) {
  const root = el('div', { class: 'tab tab-sessions' });
  const toolbarHost = el('div', { class: 'sessions-toolbar-host' });
  const kpiRow = el('div', { class: 'kpi-row' });
  const listPane = el('div', { class: 'pane pane-sessions' });
  const detailPane = el('div', { class: 'pane pane-detail' });
  root.append(toolbarHost, kpiRow, listPane, detailPane);

  let sessions = null;
  let windowRows = [];
  let page = 0;
  let inspectorHost = null;
  let unsubscribe = null;
  let openSessionId = null;
  let loading = false;
  let lastLoadedAt = 0;
  const pageSize = 20;
  // Search is over the LOADED window, not the server: /api/sessions has no
  // text filter, and the palette's own session search already covers the whole
  // corpus via the adapter's FTS index. This one answers "which of the rows in
  // front of me", which is the question a list on screen actually raises.
  const filters = { status: '', query: '' };
  const SEARCH_FIELDS = [
    'title', 'display_name', 'id', 'session_id', 'session_key', 'model',
    'source', 'thread_id', 'end_reason',
    (row) => row.tip?.title, (row) => row.tip?.tip_id,
  ];

  function renderToolbar() {
    const statusSelect = el('select', {
      class: 'select',
      'aria-label': 'Session status',
      onchange: (event) => {
        filters.status = event.target.value;
        page = 0;
        load();
      },
    });
    for (const [value, label] of [['', 'Status: all'], ['active', 'live only'], ['archived', 'archived']]) {
      const option = el('option', { value, text: label });
      option.selected = filters.status === value;
      statusSelect.append(option);
    }
    const live = windowRows.filter(isLive).length;
    const shown = visibleRows().length;
    paint(toolbarHost, tabToolbar({
      title: 'Sessions',
      subtitle: windowRows.length
        ? `${live} live · ${filterSummary(shown, windowRows.length, 'loaded row')}`
        : 'Hermes conversations, resolved to their live successor',
      filters: [statusSelect, filterInput({
        value: filters.query,
        placeholder: 'Filter loaded sessions…',
        ariaLabel: 'Filter loaded sessions',
        onChange: (value) => {
          if (value === filters.query) return;
          filters.query = value;
          page = 0;
          render();
        },
      })],
      meta: sessions?.meta || null,
      onRefresh: () => load(),
    }));
  }

  /** The loaded window after the status filter and the search box. */
  function visibleRows() {
    let rows = windowRows;
    if (filters.status === 'active') {
      rows = rows.filter((item) => (item.tip ? item.tip.is_active : item.is_active === true) && item.archived !== true);
    }
    if (filters.status === 'archived') rows = rows.filter((item) => item.archived === true);
    return filterRows(rows, filters.query, SEARCH_FIELDS);
  }

  /** A row is live when its resolved tip has not ended. */
  function isLive(row) {
    return row.tip ? row.tip.is_active === true : row.is_active === true;
  }

  function renderKpis() {
    clear(kpiRow);
    if (!windowRows.length) return;
    const metrics = [
      ['live', windowRows.filter(isLive).length, 'ok'],
      ['loaded', windowRows.length, ''],
      ['reset chains', windowRows.filter((row) => row.tip).length, ''],
      ['archived', windowRows.filter((row) => row.archived === true).length, ''],
    ];
    for (const [label, value, tone] of metrics) {
      kpiRow.append(el('div', { class: `kpi${tone ? ` kpi-tone-${tone}` : ''}` }, [
        el('div', { class: 'kpi-value mono', text: String(value) }),
        el('div', { class: 'kpi-label', text: label }),
      ]));
    }
  }

  /** Index of the WINDOW_SIZE-row block of dashboard rows holding `page`. */
  function windowIndex() {
    return Math.floor((page * pageSize) / WINDOW_SIZE);
  }

  async function load() {
    loading = true;
    try {
      await loadOnce();
    } finally {
      loading = false;
      lastLoadedAt = Date.now();
    }
  }

  async function loadOnce() {
    renderToolbar();
    clear(listPane);
    listPane.append(skeleton({ lines: 7 }));
    const query = new URLSearchParams({
      offset: String(windowIndex() * WINDOW_SIZE),
      limit: String(WINDOW_SIZE),
      order: 'recent',
    });
    if (filters.status) query.set('status', filters.status);
    try {
      sessions = await api.get(`/api/upstream/api/sessions?${query.toString()}`, { profile });
    } catch (err) {
      sessions = null;
      windowRows = [];
      clear(listPane);
      listPane.append(errorPanel({ message: err.message, requestId: err.request_id, onRetry: load }));
      return;
    }

    const rows = sessionRows(sessions.data);
    // Same resolution the Chat tab's sider uses (`pure/session-chain.js`), so
    // the two can never disagree about which conversation a listed root
    // currently is.
    const tips = await fetchChainTips({ api, profile, ids: rows.map(idOf) });
    windowRows = attachChainTips(rows, tips, idOf);
    windowRows.sort((a, b) => chainActivity(b) - chainActivity(a));

    render();
  }

  function render() {
    clear(listPane);
    if (!sessions) {
      listPane.append(unavailableState({ reason: 'Sessions source unavailable' }));
      return;
    }

    const payload = recordFrom(sessions.data) || {};
    const rows = visibleRows();
    const total = Number(payload.total ?? rows.length);

    // A filtered view shows every match in the loaded window on one page:
    // paging is server-side and the server cannot apply this filter, so
    // "next" would silently fetch unfiltered rows. The pager is hidden below
    // for the same reason.
    const searching = Boolean(filters.query);
    const start = searching ? 0 : page * pageSize - windowIndex() * WINDOW_SIZE;
    const items = searching ? rows : rows.slice(start, start + pageSize);

    // Re-run now that the window is loaded: the toolbar's subtitle counts rows.
    renderToolbar();
    renderKpis();
    listPane.append(provenanceBadge(sessions.meta, { empty: items.length === 0 }));
    if (!items.length) {
      listPane.append(filters.query
        ? emptyState({
          title: 'No loaded session matches',
          note: `Nothing in this page of ${windowRows.length} rows matches “${filters.query}”. Press ⌘K to search every session instead.`,
        })
        : emptyState({ title: 'No sessions' }));
    } else {
      const body = el('tbody');
      for (const session of items) {
        const rootId = session.id || session.session_id || session.session_key;
        // The tip is the conversation the operator means: open it, name it,
        // and act on it, not the reset root the dashboard listed.
        const id = session.tip?.tip_id || rootId;
        const live = isLive(session);
        const messageCount = session.tip?.message_count ?? session.message_count;
        const activity = chainActivity(session);

        const identity = el('div', { class: 'session-identity' }, [
          el('span', { class: 'session-name', text: session.tip?.title || session.title || session.display_name || id || 'session' }),
          el('span', { class: 'session-id mono', text: id || '' }),
        ]);
        if (session.tip) {
          // Provenance for the substitution: the dashboard listed this row
          // under `rootId`, so name what was replaced rather than silently
          // swapping ids under the operator.
          identity.append(el('span', {
            class: 'session-chain mono',
            title: `Hermes reset this conversation ${session.tip.chain_depth} time(s); the dashboard lists it under its chain root`,
            text: `↳ chain root ${rootId} · depth ${session.tip.chain_depth}`,
          }));
        }

        const row = el('tr', {
          tabindex: '0',
          onclick: () => openDetail(id),
          onkeydown: (event) => {
            if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openDetail(id); }
          },
        }, [
          el('td', {}, [identity]),
          el('td', {}, [el('span', {
            class: `chip chip-${live ? 'running' : session.archived ? 'paused' : 'unknown'}`,
            text: live ? 'live' : session.archived ? 'archived' : session.end_reason || 'idle',
          })]),
          el('td', { class: 'mono', text: session.tip?.thread_id || session.thread_id || '—' }),
          el('td', { class: 'mono right', text: Number.isFinite(Number(messageCount)) ? String(messageCount) : '—' }),
          el('td', { class: 'mono right', title: fmtTime(activity || null), text: activity ? fmtAge(new Date(activity * 1000).toISOString()) : '—' }),
        ]);
        if (id === openSessionId) row.classList.add('is-selected');
        body.append(row);
      }

      const table = el('table', { class: 'table' }, [
        el('thead', {}, [el('tr', {}, [
          el('th', { text: 'Conversation' }),
          el('th', { text: 'State' }),
          el('th', { text: 'Thread' }),
          el('th', { class: 'right', text: 'Msgs' }),
          el('th', { class: 'right', text: 'Active' }),
        ])]),
        body,
      ]);
      listPane.append(el('div', { class: 'table-wrap' }, [table]));
    }

    const previous = el('button', {
      class: 'btn btn-sm',
      text: '‹ prev',
      onclick: () => {
        if (page > 0) {
          page -= 1;
          load();
        }
      },
    });
    if (page === 0) previous.setAttribute('disabled', 'disabled');
    const next = el('button', {
      class: 'btn btn-sm',
      text: 'next ›',
      onclick: () => {
        page += 1;
        load();
      },
    });
    if ((page + 1) * pageSize >= total) next.setAttribute('disabled', 'disabled');
    listPane.append(searching
      ? el('div', { class: 'pager' }, [
        el('span', {
          class: 'mono',
          text: `${items.length} match${items.length === 1 ? '' : 'es'} in this page of ${windowRows.length} · clear the filter to page further`,
        }),
      ])
      : el('div', { class: 'pager' }, [
        previous,
        el('span', { class: 'mono', text: `page ${page + 1} · ${total} total` }),
        next,
      ]));
  }

  // Transcript rendering lives in `session-detail.js` so the Fleet/Topology
  // popup renders the identical thing from the identical code.
  async function openDetail(id) {
    if (!id) return;
    openSessionId = id;
    for (const row of listPane.querySelectorAll('tbody tr')) row.classList.remove('is-selected');
    render();
    await renderSessionDetail({ api, profile, id, container: detailPane, onChanged: load });
    detailPane.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function bindEvents() {
    if (!sse || unsubscribe) return;
    const handles = SSE_EVENTS.map((name) => sse.on(name, () => {
      if (!root.isConnected) return;
      // A load costs two upstream reads (dashboard page + tip resolution), and
      // the stream replays its buffer on connect, so an event that lands while
      // a load is in flight or right after one would just duplicate it.
      if (loading || Date.now() - lastLoadedAt < 2000) return;
      load().catch(() => null);
    }));
    unsubscribe = () => handles.forEach((off) => off?.());
  }

  function setFilter(next) {
    Object.assign(filters, next);
    page = 0;
    return load();
  }

  function renderInspector(container) {
    inspectorHost = container;
    if (!inspectorHost) return;
    const resolved = windowRows.filter((row) => row.tip).length;
    const live = windowRows.filter((row) => (row.tip ? row.tip.is_active : row.is_active === true)).length;
    paint(inspectorHost, sideHint('Sessions', [
      'Pick a session to load its transcript and timeline below the list — a transcript needs the full width, so it does not render here.',
      windowRows.length ? `${windowRows.length} session${windowRows.length === 1 ? '' : 's'} loaded, ${live} live.` : 'No sessions loaded.',
      resolved
        ? `${resolved} row${resolved === 1 ? ' was' : 's were'} listed under a reset chain root; each shows its live successor instead.`
        : 'No reset chains on this page — every row is its own live session.',
      'Rows re-sort by real activity, so a thread working right now stays at the top.',
    ]));
  }

  return {
    mount(container) {
      clear(container);
      container.append(root);
    },
    async activate(params = {}) {
      bindEvents();
      await load();
      const selected = params.s || params.session || params.session_id || openSessionId;
      if (selected) await openDetail(selected);
    },
    deactivate() {
      if (unsubscribe) unsubscribe();
      unsubscribe = null;
      return { scroll: root.scrollTop || 0 };
    },
    renderInspector,
    setFilter,
    refresh: load,
  };
}
