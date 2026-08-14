// Action Audit tab — BFF /api/audit.
//
// Every mutation this dashboard performs writes a row here before the upstream
// call and completes it after, so this table is the answer to "who changed
// what, and did it land". `pure/audit-mapping.js` already defined the nine
// columns; the tab just never rendered them.

import { mapAuditRow, AUDIT_COLUMNS, summarizeAction } from '../pure/audit-mapping.js';
import { el, clear, statusChip, iconButton, segmented, fmtTime, fmtAge } from '../ui.js';
import { createTable } from '../components/table.js';
import { createDetail } from '../components/detail.js';
import { toast } from '../components/toast.js';
import {
  loadEnvelope, applyStateToTable, tabToolbar, sideHint, paint,
} from './_kit.js';

export const ROUTE = 'action-audit';
export const LABEL = 'Action Audit';
export const GROUP = 'GOVERN';
export const READ_ONLY_NOTE = 'append-only by BFF; UI read/export (AC-21)';

export const SOURCE_ENDPOINTS = Object.freeze(['/api/audit']);

export const FILTERS = Object.freeze({ page: 1, pageSize: 50 });

/** Pure: map an audit envelope into render rows. */
export function renderAudit(envelope) {
  const meta = (envelope && envelope.meta) || null;
  const raw = (envelope && envelope.data) || null;

  if (!meta) return { rows: [], meta: null, state: 'unavailable' };
  if (meta.freshness === 'unavailable') return { rows: [], meta, state: 'unavailable' };
  if (meta.freshness === 'unsupported') return { rows: [], meta, state: 'unsupported' };
  if (!raw) return { rows: [], meta, state: meta.freshness === 'partial' ? 'partial' : 'empty' };

  const list = Array.isArray(raw) ? raw : Array.isArray(raw.audit) ? raw.audit : raw.items || [];
  if (list.length === 0) return { rows: [], meta, state: 'empty' };

  const rows = list.map((r) => ({
    ...mapAuditRow(r),
    summary_human: summarizeAction(r),
  }));
  return { rows, meta, state: meta.freshness === 'partial' ? 'partial' : 'ready' };
}

/** `result` is free-form text; classify it into one chip tone. */
export function auditResultTone(result, upstreamStatus) {
  const text = String(result || '').toLowerCase();
  if (!text) return 'idle';
  if (text === 'pending') return 'warn';
  if (text === 'ok' || text === 'success') return 'ok';
  if (typeof upstreamStatus === 'number' && upstreamStatus >= 400) return 'danger';
  if (text.startsWith('error') || text.includes('rejected') || text.includes('failed')) return 'danger';
  return 'idle';
}

/** CSV for the current view. Quoting is RFC4180: double any embedded quote. */
export function auditCsv(rows) {
  const escape = (value) => {
    const text = value === null || value === undefined ? '' : String(value);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const lines = [AUDIT_COLUMNS.join(',')];
  for (const row of rows) lines.push(AUDIT_COLUMNS.map((col) => escape(row[col])).join(','));
  return lines.join('\n');
}

const RESULT_FILTERS = [
  { value: 'all', label: 'All' },
  { value: 'failed', label: 'Failed' },
  { value: 'pending', label: 'Pending' },
];

export function createActionAudit({ api, profile, toolbar }) {
  const root = el('div', { class: 'tab tab-audit' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(main);

  let rows = [];
  let meta = null;
  let selected = null;
  let filter = 'all';
  let search = '';

  const table = createTable({
    rowId: (row) => row.request_id,
    emptyTitle: 'No audited actions yet',
    emptyNote: 'Every mutation made through this dashboard is recorded here.',
    sort: { key: 'timestamp', dir: 'desc' },
    columns: [
      {
        key: 'timestamp',
        label: 'When',
        width: '150px',
        sortable: true,
        render: (row) => el('div', { class: 'cell-stack' }, [
          el('span', { text: fmtAge(row.timestamp) }),
          el('span', { class: 'cell-dim mono', text: fmtTime(row.timestamp) }),
        ]),
      },
      {
        key: 'action',
        label: 'Action',
        sortable: true,
        render: (row) => el('span', { class: 'cell-strong mono', text: row.action || '—' }),
      },
      {
        key: 'target',
        label: 'Target',
        sortable: true,
        render: (row) => (row.target
          ? el('span', { class: 'mono cell-dim', text: row.target, title: row.target })
          : null),
      },
      { key: 'actor', label: 'Actor', width: '90px', sortable: true },
      { key: 'profile', label: 'Profile', width: '100px', sortable: true },
      {
        key: 'upstream_status',
        label: 'Status',
        width: '80px',
        align: 'right',
        sortable: true,
        render: (row) => (row.upstream_status === null || row.upstream_status === undefined
          ? null
          : el('span', {
            class: `mono ${Number(row.upstream_status) >= 400 ? 'cell-danger' : ''}`,
            text: String(row.upstream_status),
          })),
      },
      {
        key: 'result',
        label: 'Result',
        width: '140px',
        sortable: true,
        render: (row) => statusChip(auditResultTone(row.result, row.upstream_status), row.result || 'unknown'),
      },
    ],
    rowClass: (row) => (auditResultTone(row.result, row.upstream_status) === 'danger' ? 'row-danger' : ''),
    onSelect: (row) => { selected = row; renderSide(); },
  });
  main.append(table.node);

  function visibleRows() {
    const term = search.trim().toLowerCase();
    return rows.filter((row) => {
      const tone = auditResultTone(row.result, row.upstream_status);
      if (filter === 'failed' && tone !== 'danger') return false;
      if (filter === 'pending' && row.result !== 'pending') return false;
      if (term) {
        const hay = `${row.action} ${row.target} ${row.request_id} ${row.summary}`.toLowerCase();
        if (!hay.includes(term)) return false;
      }
      return true;
    });
  }

  async function load() {
    table.setLoading();
    const result = await loadEnvelope(api, '/api/audit', {
      profile,
      pick: (raw) => {
        if (Array.isArray(raw)) return raw;
        if (Array.isArray(raw?.audit)) return raw.audit;
        if (Array.isArray(raw?.items)) return raw.items;
        return null;
      },
    });
    meta = result.meta;
    rows = (Array.isArray(result.data) ? result.data : []).map((r) => ({
      ...mapAuditRow(r),
      summary_human: summarizeAction(r),
    }));
    applyStateToTable(table, { ...result, data: visibleRows() });
    if (selected) {
      selected = rows.find((r) => r.request_id === selected.request_id) || null;
      table.setSelected(selected?.request_id ?? null);
    }
    renderToolbar(toolbar);
    renderSide();
  }

  function exportCsv() {
    const csv = auditCsv(visibleRows());
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    const link = el('a', { href: url, download: `action-audit-${Date.now()}.csv` });
    link.click();
    URL.revokeObjectURL(url);
    toast(`Exported ${visibleRows().length} rows`, { tone: 'ok' });
  }

  function renderToolbar(host) {
    if (!host) return;
    const input = el('input', {
      class: 'input input-sm',
      type: 'search',
      placeholder: 'Filter by action, target, request id…',
      value: search,
    });
    input.addEventListener('input', () => {
      search = input.value;
      table.setRows(visibleRows());
    });
    const failed = rows.filter((r) => auditResultTone(r.result, r.upstream_status) === 'danger').length;
    paint(host, tabToolbar({
      title: 'Action Audit',
      subtitle: `${rows.length} recorded${failed ? ` · ${failed} failed` : ''}`,
      meta,
      onRefresh: () => load(),
      filters: [
        segmented(RESULT_FILTERS.map((f) => ({
          ...f,
          count: f.value === 'all' ? rows.length
            : f.value === 'failed' ? failed
              : rows.filter((r) => r.result === 'pending').length,
        })), {
          value: filter,
          ariaLabel: 'Filter audit rows',
          onChange: (next) => { filter = next; table.setRows(visibleRows()); },
        }),
        input,
      ],
      actions: [
        iconButton({ icon: 'download', label: 'Export CSV', onClick: exportCsv }),
      ],
    }));
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (!selected) {
      paint(inspectorHost, sideHint('Select an action', [
        'This log is append-only and written by the BFF, not by the browser.',
        'A row is written before the upstream call and completed after it, so a "pending" row means the call never returned.',
        'Summaries carry key names and short decision values only — never free text or secrets.',
      ]));
      return;
    }
    paint(inspectorHost, createDetail({
      title: selected.action || 'action',
      meta,
      chips: [statusChip(auditResultTone(selected.result, selected.upstream_status), selected.result || 'unknown')],
      fields: [
        { label: 'Request id', value: selected.request_id, mono: true },
        { label: 'Actor', value: selected.actor },
        { label: 'Target', value: selected.target, mono: true },
        { label: 'Profile', value: selected.profile },
        { label: 'When', value: fmtTime(selected.timestamp), mono: true },
        { label: 'Upstream status', value: selected.upstream_status, mono: true },
        { label: 'Summary', value: selected.summary, mono: true },
      ],
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

export { AUDIT_COLUMNS };
