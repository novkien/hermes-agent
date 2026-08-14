// Artifacts tab — adapter task attachments.
//
// An artifact here means "a file a task produced", which is a different
// question from "what is on disk" (that is the Files tab). The old version
// pointed at /api/files and silently dropped the task-attachment endpoint
// because the fallback loader only ever fetched the first source endpoint.

import { listRows } from '../pure/envelope-list.js';
import { el, clear, iconButton, fmtTime, fmtAge } from '../ui.js';
import { createTable } from '../components/table.js';
import { createDetail } from '../components/detail.js';
import {
  applyStateToTable, filterInput, loadEnvelope, paint, sideHint, tabToolbar,
} from './_kit.js';
import { filterRows, filterSummary } from '../pure/text-filter.js';
import { humanSize } from './files.js';

export const ROUTE = 'artifacts';
export const LABEL = 'Artifacts';
export const GROUP = 'BUILD & INTEGRATE';

export const SOURCE_ENDPOINTS = Object.freeze([
  '/api/adapter/kanban/tasks',
  '/api/adapter/kanban/tasks/{id}/attachments',
]);

// Attachments are per-task upstream, so a board-wide view means walking the
// most recent tasks. Bounded on purpose: this is a browse surface, not a scan.
const TASK_SCAN_LIMIT = 25;

export function renderArtifacts(envelope) {
  return listRows(envelope, {
    pick: (raw) => raw.attachments || raw.files || raw.artifacts || raw.items || raw.list || null,
    map: (a) => ({
      id: a.id ?? a.name ?? null,
      name: a.name ?? a.filename ?? null,
      size: a.size ?? a.byte_size ?? null,
      mime: a.content_type ?? a.mime ?? null,
      task_id: a.task_id ?? a.task ?? null,
      task_link: a.task_id ? `#/kanban?task=${encodeURIComponent(a.task_id)}` : null,
      created_at: a.created_at ?? a.uploaded_at ?? null,
    }),
  });
}

/** Pure: map adapter task_attachments rows into artifact rows. */
export function renderTaskAttachments(rows, task = null) {
  if (!Array.isArray(rows)) return [];
  return rows.map((a) => ({
    id: `${a.task_id ?? task?.id ?? ''}:${a.id ?? a.name ?? ''}`,
    attachment_id: a.id ?? null,
    name: a.name ?? a.filename ?? null,
    size: a.size ?? a.byte_size ?? null,
    mime: a.content_type ?? a.mime ?? null,
    kind: a.kind ?? null,
    path: a.path ?? null,
    task_id: a.task_id ?? task?.id ?? null,
    task_title: task?.title ?? null,
    task_link: (a.task_id ?? task?.id) ? `#/kanban?task=${encodeURIComponent(a.task_id ?? task.id)}` : null,
    created_at: a.created_at ?? null,
  }));
}

export function createArtifacts({ api, profile, toolbar, onNavigate: navigate }) {
  const root = el('div', { class: 'tab tab-artifacts' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(main);

  let rows = [];
  let meta = null;
  let scanned = 0;
  let selected = null;

  const table = createTable({
    rowId: (row) => row.id,
    emptyTitle: 'No artifacts',
    emptyNote: `No attachments on the ${TASK_SCAN_LIMIT} most recent tasks.`,
    sort: { key: 'created_at', dir: 'desc' },
    columns: [
      {
        key: 'name',
        label: 'Artifact',
        sortable: true,
        render: (row) => el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: row.name || '(unnamed)' }),
          row.path ? el('span', { class: 'cell-dim mono', text: row.path, title: row.path }) : null,
        ].filter(Boolean)),
      },
      {
        key: 'task_id',
        label: 'Task',
        width: '190px',
        sortable: true,
        render: (row) => el('div', { class: 'cell-stack' }, [
          el('span', { class: 'mono', text: row.task_id || '—' }),
          row.task_title ? el('span', { class: 'cell-dim', text: row.task_title, title: row.task_title }) : null,
        ].filter(Boolean)),
      },
      { key: 'kind', label: 'Kind', width: '100px', sortable: true },
      {
        key: 'size',
        label: 'Size',
        width: '90px',
        align: 'right',
        sortable: true,
        sortValue: (row) => Number(row.size ?? 0),
        render: (row) => humanSize(row.size),
      },
      {
        key: 'created_at',
        label: 'Created',
        width: '130px',
        sortable: true,
        render: (row) => (row.created_at ? fmtAge(row.created_at) : null),
      },
    ],
    rowActions: (row) => [
      row.task_id && navigate
        ? iconButton({
          icon: 'link',
          label: 'Open task',
          onClick: () => navigate('kanban', { task: row.task_id }),
        })
        : null,
    ].filter(Boolean),
    onSelect: (row) => { selected = row; renderSide(); },
  });
  main.append(table.node);

  // Shared filter box: this list had no way to find a row by name.
  let query = '';
  const SEARCH_FIELDS = ['name', 'path', 'task_id', 'task_title', 'kind', 'content_type'];

  function visibleRows() {
    return filterRows(rows, query, SEARCH_FIELDS);
  }

  async function load() {
    table.setLoading();
    const taskResult = await loadEnvelope(api, `/api/adapter/kanban/tasks?limit=${TASK_SCAN_LIMIT}`, {
      profile,
      pick: (raw) => (Array.isArray(raw) ? raw : raw?.tasks || raw?.items || null),
    });
    meta = taskResult.meta;
    if (taskResult.state === 'unavailable' || taskResult.state === 'unsupported') {
      applyStateToTable(table, taskResult);
      renderToolbar(toolbar);
      return;
    }

    const tasks = Array.isArray(taskResult.data) ? taskResult.data : [];
    scanned = tasks.length;
    const collected = [];
    // Sequential rather than parallel: the adapter is a single small service
    // and a 25-way fan-out is a self-inflicted load spike.
    for (const task of tasks) {
      const attachments = await loadEnvelope(
        api,
        `/api/adapter/kanban/tasks/${encodeURIComponent(task.id)}/attachments`,
        { profile, pick: (raw) => (Array.isArray(raw) ? raw : raw?.attachments || null) },
      );
      if (attachments.state === 'ready') {
        collected.push(...renderTaskAttachments(attachments.data, task));
      }
    }
    rows = collected;
    applyStateToTable(table, {
      ...taskResult,
      state: visibleRows().length ? 'ready' : 'empty',
      data: visibleRows(),
    });
    if (selected) {
      selected = rows.find((r) => r.id === selected.id) || null;
      table.setSelected(selected?.id ?? null);
    }
    renderToolbar(toolbar);
    renderSide();
  }

  function renderToolbar(host) {
    if (!host) return;
    paint(host, tabToolbar({
      title: 'Artifacts',
      subtitle: query
        ? filterSummary(visibleRows().length, rows.length, 'artifact')
        : `${rows.length} from the ${scanned} most recent tasks`,
      filters: [filterInput({
        value: query,
        placeholder: 'Find an artifact…',
        ariaLabel: 'Filter artifacts',
        onChange: (next) => {
          if (next === query) return;
          query = next;
          table.setRows(visibleRows());
          renderToolbar(host);
        },
      })],
      meta,
      onRefresh: () => load(),
    }));
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (!selected) {
      paint(inspectorHost, sideHint('Select an artifact', [
        'Artifacts are files produced by task runs, recorded against the task that made them.',
        `Only the ${TASK_SCAN_LIMIT} most recent tasks are scanned — open a task in Kanban to see all of its attachments.`,
      ]));
      return;
    }
    paint(inspectorHost, createDetail({
      title: selected.name || '(unnamed)',
      meta,
      chips: [
        selected.kind ? el('span', { class: 'chip', text: selected.kind }) : null,
        selected.mime ? el('span', { class: 'chip', text: selected.mime }) : null,
      ].filter(Boolean),
      fields: [
        { label: 'Task', value: selected.task_id, mono: true },
        { label: 'Task title', value: selected.task_title },
        { label: 'Path', value: selected.path, mono: true },
        { label: 'Size', value: humanSize(selected.size) },
        { label: 'Created', value: selected.created_at ? fmtTime(selected.created_at) : null, mono: true },
      ],
      relations: selected.task_id && navigate
        ? [{ label: 'Task', text: selected.task_id, onClick: () => navigate('kanban', { task: selected.task_id }) }]
        : [],
      raw: selected,
    }));
  }

  renderSide();

  return {
    mount(container) { clear(container); container.append(root); },
    renderInspector,
    activate() { return load(); },
    deactivate() { return {}; },
    refresh: load,
    renderToolbar,
    get data() { return rows; },
  };
}
