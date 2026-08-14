// Kanban — adapter-native board summary, bounded task list and task detail.

import {
  el, clear, skeleton, emptyState, unavailableState, errorPanel, fmtTime,
} from '../ui.js';
import { provenanceBadge } from '../provenance.js';
import { listFrom, recordFrom, taskRows } from '../pure/data-shape.js';
import { filterInput, sideHint, paint, tabToolbar } from './_kit.js';
import { filterRows, filterSummary } from '../pure/text-filter.js';
import { buildHash } from '../pure/hash-router.js';
import { createDetail } from '../components/detail.js';
import { createChatModal } from './chat.js';

const SSE_EVENTS = Object.freeze(['task.changed', 'run.changed']);

export function createKanban({ api, profile, sse }) {
  const root = el('div', { class: 'tab tab-kanban' });
  const toolbar = el('div', { class: 'tab-toolbar' });
  const kpiRow = el('div', { class: 'kpi-row' });
  const tableWrap = el('div', { class: 'table-wrap' });
  root.append(toolbar, kpiRow, tableWrap);

  let summary = null;
  let tasks = null;
  let boards = [];
  let inspectorHost = null;
  let selectedId = null;
  let unsubscribe = null;
  let chatModalInstance = null;
  function chatModal() {
    if (!chatModalInstance) chatModalInstance = createChatModal({ api, profile, sse });
    return chatModalInstance;
  }
  // The selected task's fetched detail (task + events + runs + attachments),
  // kept so renderSide() can repaint the inspector column without a refetch —
  // e.g. when the column is handed to this tab again after a re-render.
  let selectedDetail = null;
  // Default to every board: this deployment keeps most of its tasks on
  // non-default boards, so scoping to the adapter's default board alone
  // showed a small fraction of the real backlog.
  const filters = { status: '', assignee: '', priority: '', board: 'all', query: '' };
  // The dropdowns cover the columns the board knows about; the text box covers
  // the one thing they cannot — finding a specific card among the ~1,900 this
  // deployment holds when you remember a word of its title, not its status.
  const SEARCH_FIELDS = ['id', 'title', 'assignee', 'board', 'status', 'priority', 'created_by'];

  function filterBar() {
    const bar = el('div', { class: 'filter-bar' });
    const boardSelect = el('select', {
      class: 'select',
      'aria-label': 'Board',
      onchange: (event) => { filters.board = event.target.value; load(); },
    });
    const boardOptions = [['all', 'Board: all']].concat(
      boards.map((b) => [b.board, `${b.board} (${b.task_count})`]),
    );
    for (const [value, label] of boardOptions) {
      const option = el('option', { value, text: label });
      option.selected = filters.board === value;
      boardSelect.append(option);
    }
    const selects = [boardSelect];
    for (const [key, label] of [['status', 'Status'], ['assignee', 'Assignee'], ['priority', 'Priority']]) {
      const select = el('select', {
        class: 'select',
        'aria-label': label,
        onchange: (event) => {
          filters[key] = event.target.value;
          load();
        },
      });
      select.append(el('option', { value: '', text: `${label}: all` }));
      for (const value of FILTER_VALUES[key]) {
        const option = el('option', { value, text: value });
        option.selected = filters[key] === value;
        select.append(option);
      }
      selects.push(select);
    }
    selects.push(filterInput({
      value: filters.query,
      placeholder: 'Find a card…',
      ariaLabel: 'Find a kanban card',
      onChange: (value) => {
        if (value === filters.query) return;
        filters.query = value;
        render();
      },
    }));
    for (const node of selects) bar.append(node);
    return bar;
  }

  async function load() {
    clear(tableWrap);
    tableWrap.append(skeleton({ lines: 7 }));
    const query = new URLSearchParams({ page: '1', limit: '100' });
    if (filters.status) query.set('status', filters.status);
    if (filters.assignee) query.set('assignee', filters.assignee);
    if (filters.board) query.set('board', filters.board);

    const [summaryResult, taskResult, boardResult] = await Promise.allSettled([
      api.get('/api/adapter/kanban/board/summary', { profile }),
      api.get(`/api/adapter/kanban/tasks?${query.toString()}`, { profile }),
      api.get('/api/adapter/kanban/boards', { profile }),
    ]);
    summary = summaryResult.status === 'fulfilled' ? summaryResult.value : null;
    tasks = taskResult.status === 'fulfilled' ? taskResult.value : null;
    boards = boardResult.status === 'fulfilled'
      ? (listFrom(boardResult.value.data, ['boards']) || [])
      : [];
    render();
  }

  function render() {
    renderSummary();
    renderSide();
    clear(tableWrap);
    if (!tasks) {
      paint(toolbar, tabToolbar({ title: 'Kanban', onRefresh: () => load() }));
      tableWrap.append(unavailableState({ reason: 'Kanban source unavailable' }));
      return;
    }

    const loaded = taskRows(tasks.data);
    let rows = loaded;
    if (filters.priority) rows = rows.filter((task) => String(task.priority || '').toLowerCase() === filters.priority);
    rows = filterRows(rows, filters.query, SEARCH_FIELDS);

    // The standard tab header, so this tab reports its freshness and offers a
    // refresh like every other one — it used to be the only board in the
    // dashboard you could not reload without switching tabs and back.
    paint(toolbar, tabToolbar({
      title: 'Kanban',
      subtitle: filterSummary(rows.length, loaded.length, 'card')
        + (filters.board && filters.board !== 'all' ? ` on ${filters.board}` : ' across every board'),
      filters: [filterBar()],
      actions: [provenanceBadge(tasks?.meta)],
      meta: tasks?.meta || null,
      onRefresh: () => load(),
    }));

    if (!rows.length) {
      tableWrap.append(emptyState({
        title: filters.query ? 'No card matches' : 'No tasks match',
        note: filters.query
          ? `Nothing in the ${loaded.length} loaded cards matches “${filters.query}”.`
          : 'The source is available; the current filters returned no rows.',
      }));
      return;
    }

    const table = el('table', { class: 'table' });
    table.append(el('thead', {}, [
      el('tr', {}, ['ID', 'Title', 'Status', 'Assignee', 'Priority', 'Board']
        .map((heading) => el('th', { text: heading }))),
    ]));
    const body = el('tbody');
    for (const task of rows) {
      const row = el('tr', { 'data-task-id': task.id || '', onclick: () => openDetail(task.id) }, [
        el('td', { class: 'mono', text: task.id || '' }),
        el('td', { text: task.title || '' }),
        el('td', {}, [statusChip(task.status)]),
        el('td', { text: task.assignee || '' }),
        el('td', { text: task.priority || '' }),
        el('td', { text: task.board || '' }),
      ]);
      if (task.id === selectedId) row.classList.add('is-selected');
      body.append(row);
    }
    table.append(body);
    tableWrap.append(table);
  }

  function renderSummary() {
    clear(kpiRow);
    if (!summary) return;
    const data = recordFrom(summary.data) || {};
    const counts = new Map(listFrom(data.tasks_by_status).map((item) => [String(item.status), Number(item.count || 0)]));
    const metrics = {
      total: data.total_tasks ?? 0,
      running: data.running_count ?? counts.get('running') ?? 0,
      done: counts.get('done') ?? counts.get('completed') ?? 0,
      blocked: data.blocked_count ?? counts.get('blocked') ?? 0,
    };
    for (const [key, value] of Object.entries(metrics)) {
      kpiRow.append(el('div', { class: 'kpi' }, [
        el('div', { class: 'kpi-value mono', text: String(value) }),
        el('div', { class: 'kpi-label', text: key }),
      ]));
    }
  }

  async function openDetail(id) {
    if (!id) return;
    selectedId = id;
    selectedDetail = null;
    highlightSelectedRow();
    syncSelectionToUrl(id);
    if (inspectorHost) paint(inspectorHost, skeleton({ lines: 6 }));

    const [taskResult, eventResult, runResult, attachmentResult, workerSessionResult] = await Promise.allSettled([
      api.get(`/api/adapter/kanban/tasks/${encodeURIComponent(id)}`, { profile }),
      api.get(`/api/adapter/kanban/tasks/${encodeURIComponent(id)}/events`, { profile }),
      api.get(`/api/adapter/kanban/tasks/${encodeURIComponent(id)}/runs`, { profile }),
      api.get(`/api/adapter/kanban/tasks/${encodeURIComponent(id)}/attachments`, { profile }),
      // 404s when no worker ever claimed the card (or it predates the anchor
      // convention) — that is a normal, expected outcome, not a fetch error.
      api.get(`/api/adapter/kanban/tasks/${encodeURIComponent(id)}/worker-session`, { profile }),
    ]);
    // A newer click can land while this fetch is still in flight; only the
    // latest selection may paint.
    if (selectedId !== id) return;
    selectedDetail = { id, taskResult, eventResult, runResult, attachmentResult, workerSessionResult };
    renderSide();
  }

  function highlightSelectedRow() {
    // Keyed by the row's own id rather than by matching cell text and counting
    // indexes: the old version located the row by the position of the first
    // `.mono` cell, which silently highlighted the wrong card as soon as any
    // other column was rendered monospaced.
    for (const row of tableWrap.querySelectorAll('tbody tr.is-selected')) row.classList.remove('is-selected');
    if (!selectedId) return;
    const match = tableWrap.querySelector(`tbody tr[data-task-id="${cssEscape(selectedId)}"]`);
    if (match) match.classList.add('is-selected');
  }

  /**
   * The selected card belongs in the URL. `activate()` has always accepted
   * `params.task`, so the tab could be linked INTO but never linked FROM: a
   * refresh dropped the selection and a card could not be shared. Written
   * straight to the address bar rather than through navigate(), which would
   * re-activate this very tab and reload the board.
   */
  function syncSelectionToUrl(id) {
    const url = new URL(window.location.href);
    url.hash = id ? buildHash('/kanban', { task: id }) : buildHash('/kanban', {});
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
  }

  function taskDetailNode() {
    const { id, taskResult, eventResult, runResult, attachmentResult, workerSessionResult } = selectedDetail;
    const taskEnvelope = taskResult.status === 'fulfilled' ? taskResult.value : null;
    const task = recordFrom(taskEnvelope?.data);
    // task.session_id is the session of the agent that CREATED the card, not
    // the worker that ran it — the two are frequently different agents. The
    // worker-session resolver finds the session whose first stored message
    // is `work kanban task <id>`, which is the one that actually executed it.
    const workerSession = workerSessionResult?.status === 'fulfilled'
      ? recordFrom(workerSessionResult.value?.data)
      : null;
    const workerSessionId = workerSession?.session_id || null;
    const chatSessionId = workerSessionId || task?.session_id || null;
    if (!task) {
      return el('div', { class: 'stack-sm' }, [
        provenanceBadge(taskEnvelope?.meta),
        unavailableState({ reason: 'Task detail unavailable' }),
      ]);
    }

    const sections = [];
    const eventRows = listFrom(fulfilledData(eventResult), ['events']);
    if (eventRows.length) {
      sections.push({ title: 'Events', node: listNode(eventRows, (event) => [
        el('span', { class: 'mono', text: fmtTime(event.created_at || event.ts) }),
        el('span', { text: event.event_type || event.kind || 'event' }),
      ]) });
    }
    const runRows = listFrom(fulfilledData(runResult), ['runs']);
    if (runRows.length) {
      sections.push({ title: 'Runs', node: listNode(runRows, (run) => [
        el('span', { class: 'mono', text: run.run_id || run.id || '' }),
        statusChip(run.status),
      ]) });
    }
    const attachmentRows = listFrom(fulfilledData(attachmentResult), ['attachments']);
    if (attachmentRows.length) {
      sections.push({ title: 'Attachments', node: listNode(attachmentRows, (attachment) => [
        el('span', { text: attachment.filename || attachment.name || attachment.id || 'attachment' }),
        el('span', { class: 'mono', text: attachment.content_type || '' }),
      ]) });
    }

    return createDetail({
      title: task.title || `Task ${id}`,
      meta: taskEnvelope?.meta,
      chips: [statusChip(task.status)],
      fields: [
        { label: 'id', value: id, mono: true },
        { label: 'assignee', value: task.assignee },
        { label: 'priority', value: task.priority, mono: true },
        { label: 'board', value: task.board },
        { label: 'created_by session_id', value: task.session_id, mono: true },
        {
          label: 'worker session_id',
          value: workerSessionId || (workerSessionResult?.status === 'fulfilled' ? '(none)' : 'resolving…'),
          mono: true,
        },
        { label: 'current_run_id', value: task.current_run_id, mono: true },
      ],
      relations: chatSessionId ? [
        {
          label: 'Chat',
          text: workerSessionId ? 'Open worker chat' : 'Open chat (creator session)',
          onClick: () => chatModal().open(chatSessionId),
        },
      ] : [],
      sections,
    });
  }

  function listNode(rows, renderRow) {
    const list = el('ul', { class: 'list' });
    for (const row of rows.slice(0, 100)) list.append(el('li', { class: 'list-item' }, renderRow(row)));
    return list;
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (selectedDetail) {
      paint(inspectorHost, taskDetailNode());
      return;
    }
    const rows = taskRows(tasks?.data) || [];
    const byStatus = rows.reduce((acc, row) => {
      const key = row.status || 'unknown';
      acc[key] = (acc[key] || 0) + 1;
      return acc;
    }, {});
    const spread = Object.entries(byStatus)
      .sort((a, b) => b[1] - a[1])
      .map(([status, count]) => `${status}: ${count}`);
    paint(inspectorHost, sideHint('Board', [
      'Select a card to see its full detail — events, runs and attachments — here.',
      rows.length ? `${rows.length} task${rows.length === 1 ? '' : 's'} loaded.` : 'No tasks loaded yet.',
      ...(spread.length ? [spread.join(' · ')] : []),
    ]));
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function bindEvents() {
    if (!sse || unsubscribe) return;
    const handles = SSE_EVENTS.map((name) => sse.on(name, () => {
      if (!root.isConnected) return;
      // The list is a point-in-time snapshot with no live refresh otherwise,
      // so a card can sit on a stale status (e.g. "ready" in the table, the
      // real task already "running") until something reloads it. Re-fetch
      // the open detail too — its own snapshot goes stale the same way.
      load().catch(() => null);
      if (selectedId) openDetail(selectedId).catch(() => null);
    }));
    unsubscribe = () => handles.forEach((off) => off?.());
  }

  return {
    mount(container) {
      clear(container);
      container.append(root);
    },
    async activate(params = {}) {
      bindEvents();
      try {
        await load();
        if (params.task) await openDetail(params.task);
      } catch (err) {
        clear(tableWrap);
        tableWrap.append(errorPanel({ message: err.message, requestId: err.request_id, onRetry: load }));
      }
    },
    deactivate() {
      if (unsubscribe) unsubscribe();
      unsubscribe = null;
      return {};
    },
    renderInspector,
    refresh: load,
    setFilter(next) {
      Object.assign(filters, next);
      return load();
    },
  };
}

const FILTER_VALUES = {
  status: ['ready', 'running', 'blocked', 'done', 'todo'],
  assignee: ['executor', 'tdd-guide', 'reviewer', 'verifier'],
  priority: ['high', 'medium', 'low'],
};

/** Minimal CSS.escape for the ids kanban actually uses (`t_1a2b3c`). */
function cssEscape(value) {
  return String(value).replace(/["\\]/g, '\\$&');
}

function statusChip(status) {
  return el('span', {
    class: `chip chip-${String(status || 'unknown').toLowerCase()}`,
    text: status || 'unknown',
  });
}

function fulfilledData(result) {
  return result.status === 'fulfilled' ? result.value.data : null;
}
