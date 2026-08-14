// Permits tab — the adapter's permit ledger, with a working decision form.
//
// A permit is an owner decision an agent is blocked on: it states the problem,
// the evidence, why it could not be auto-fixed, and two or three options. Until
// now the dashboard could only read them, so the actual decision had to happen
// over SSH. `POST /api/permits/{id}/decision` (BFF → adapter → permits_db.py)
// closes that loop.
//
// The field names below are the real columns of permits.db, not a guess: the
// previous version normalized against request_type/session_id/approver, none of
// which exist, so most of the detail pane rendered as "—".

import { mapPermitRow, PERMIT_COLUMNS } from '../pure/table-mapping.js';
import { el, clear, statusChip, segmented, fmtTime, fmtAge } from '../ui.js';
import { createTable } from '../components/table.js';
import { createDetail } from '../components/detail.js';
import { createForm } from '../components/form.js';
import {
  loadEnvelope, applyStateToTable, tabToolbar, runMutation, sideHint,
  paint, confirmAction,
} from './_kit.js';

export const ROUTE = 'permits';
export const LABEL = 'Permits';
export const GROUP = 'GOVERN';

export const SOURCE_ENDPOINTS = Object.freeze([
  '/api/adapter/permits',
  '/api/adapter/permits/{id}',
]);

export const SSE_EVENTS = Object.freeze(['permit.changed']);

/**
 * `status` is a free-text column in permits_db.py, but these are the values the
 * script itself writes, so the form offers them rather than a text box.
 * permits_db also derives status implicitly — approving without an explicit
 * status sets `approved`, and executed+success sets `executed`.
 */
export const PERMIT_STATUSES = Object.freeze([
  'pending_approval', 'approved', 'rejected', 'executed', 'superseded',
]);

export const RESULT_STATUSES = Object.freeze(['success', 'partial', 'failed']);

/** Statuses that still need a human. Everything else is history. */
const OPEN_STATUSES = new Set(['pending_approval', 'open', 'approved']);

const STATUS_TONE = {
  pending_approval: 'warn',
  approved: 'ok',
  executed: 'ok',
  rejected: 'danger',
  superseded: 'idle',
};

const SEVERITY_TONE = { critical: 'danger', high: 'danger', medium: 'warn', low: 'idle' };

/** permits_db.py's own truthiness check for the free-text `approved` column. */
const TRUTHY = new Set(['ok', 'true', 'yes', '1', 'x', 'checked']);

export function isApproved(value) {
  return TRUTHY.has(String(value ?? '').trim().toLowerCase());
}

function pickPermits(raw) {
  if (Array.isArray(raw)) return raw;
  if (!raw || typeof raw !== 'object') return null;
  for (const key of ['permits', 'items', 'rows', 'list', 'results', 'records']) {
    if (Array.isArray(raw[key])) return raw[key];
  }
  return null;
}

/** Pure: normalize one adapter permit record against the real column set. */
export function normalizePermit(row) {
  if (!row || typeof row !== 'object') return null;
  const base = mapPermitRow(row) || {};
  const id = row.permit_id ?? row.id ?? null;
  return {
    ...base,
    id,
    permit_id: id,
    status: row.status ?? null,
    severity: row.severity ?? null,
    category: row.category ?? null,
    source: row.source ?? null,
    title: row.issue_title ?? row.title ?? row.problem_summary ?? null,
    fingerprint: row.fingerprint ?? null,
    problem_summary: row.problem_summary ?? null,
    evidence: row.evidence ?? null,
    why_not_auto_fix: row.why_not_auto_fix ?? null,
    recommended_action: row.recommended_action ?? null,
    recommended_option: row.recommended_option ?? null,
    options: [
      row.option_a ? { key: 'A', text: row.option_a } : null,
      row.option_b ? { key: 'B', text: row.option_b } : null,
      row.option_c ? { key: 'C', text: row.option_c } : null,
    ].filter(Boolean),
    approved: row.approved ?? '',
    approval_note: row.approval_note ?? '',
    action_plan: row.action_plan ?? '',
    executed: row.executed ?? '',
    executed_at: row.executed_at ?? null,
    execution_result: row.execution_result ?? '',
    expires_at: row.expires_at ?? null,
    last_seen_in_run: row.last_seen_in_run ?? null,
    created_at: row.created_at ?? null,
    updated_at: row.updated_at ?? null,
  };
}

/** Pure: map a permits-list envelope into render rows. */
export function renderPermitsList(envelope) {
  const meta = (envelope && envelope.meta) || null;
  const raw = (envelope && envelope.data) || null;
  if (!meta) return { rows: [], meta: null, state: 'unavailable' };
  if (meta.freshness === 'unavailable') return { rows: [], meta, state: 'unavailable' };
  if (meta.freshness === 'unsupported') return { rows: [], meta, state: 'unsupported' };
  const list = pickPermits(raw);
  if (!list) return { rows: [], meta, state: 'empty' };
  const rows = list.map(normalizePermit).filter(Boolean);
  if (!rows.length) return { rows: [], meta, state: 'empty' };
  return { rows, meta, state: meta.freshness === 'partial' ? 'partial' : 'ready' };
}

/** Pure: map a permit-detail envelope. */
export function renderPermitDetail(envelope) {
  const meta = (envelope && envelope.meta) || null;
  const raw = (envelope && envelope.data) || null;
  if (!meta || meta.freshness === 'unavailable' || !raw) {
    return { permit: null, meta, state: !meta ? 'unavailable' : meta.freshness };
  }
  const record = Array.isArray(raw) ? raw[0] : (raw.permit || raw.item || raw.record || raw);
  return { permit: normalizePermit(record), meta, state: 'ready' };
}

/**
 * Pure: build the decision body from a form diff.
 *
 * Only changed fields are sent. `approved` is a JSON bool here and the adapter
 * spells it the way permits_db reads it back, so the UI never has to know that
 * the column is free text.
 */
export function decisionBody(diff) {
  const body = {};
  for (const key of ['status', 'approval_note', 'action_plan', 'execution_result', 'result_status']) {
    if (key in diff && diff[key] !== null && diff[key] !== '') body[key] = diff[key];
  }
  if ('approved' in diff) body.approved = Boolean(diff.approved);
  if ('executed' in diff) body.executed = diff.executed === true || diff.executed === 'yes' ? 'yes' : 'no';
  return body;
}

export { PERMIT_COLUMNS };

const VIEWS = [
  { value: 'open', label: 'Needs decision' },
  { value: 'decided', label: 'Decided' },
  { value: 'all', label: 'All' },
];

export function createPermits({ api, profile, sse, toolbar, onNavigate: navigate }) {
  const root = el('div', { class: 'tab tab-permits' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(main);

  let rows = [];
  let meta = null;
  let selected = null;
  let view = 'open';
  let search = '';
  let unsubscribe = null;

  const table = createTable({
    rowId: (row) => row.id,
    emptyTitle: 'No permits',
    emptyNote: 'Agents raise a permit when a decision needs a human.',
    sort: { key: 'updated_at', dir: 'desc' },
    columns: [
      {
        key: 'title',
        label: 'Permit',
        sortable: true,
        render: (row) => el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: row.title || row.id, title: row.title || '' }),
          el('span', { class: 'cell-dim mono', text: row.id }),
        ]),
      },
      {
        key: 'status',
        label: 'Status',
        width: '140px',
        sortable: true,
        render: (row) => statusChip(STATUS_TONE[row.status] || 'idle', row.status || 'unknown'),
      },
      {
        key: 'severity',
        label: 'Severity',
        width: '95px',
        sortable: true,
        render: (row) => (row.severity
          ? statusChip(SEVERITY_TONE[row.severity] || 'idle', row.severity)
          : null),
      },
      { key: 'category', label: 'Category', width: '130px', sortable: true, className: 'cell-dim' },
      {
        key: 'updated_at',
        label: 'Updated',
        width: '120px',
        sortable: true,
        render: (row) => (row.updated_at ? el('span', { text: fmtAge(row.updated_at) }) : null),
      },
    ],
    rowClass: (row) => (OPEN_STATUSES.has(row.status) && !isApproved(row.approved) ? 'row-danger' : ''),
    onSelect: (row) => { selected = row; renderSide(); },
  });
  main.append(table.node);

  function visibleRows() {
    const term = search.trim().toLowerCase();
    return rows.filter((row) => {
      const open = OPEN_STATUSES.has(row.status);
      if (view === 'open' && !open) return false;
      if (view === 'decided' && open) return false;
      if (term) {
        const hay = `${row.id} ${row.title} ${row.problem_summary} ${row.category}`.toLowerCase();
        if (!hay.includes(term)) return false;
      }
      return true;
    });
  }

  async function load(initialSelection = null) {
    table.setLoading();
    const result = await loadEnvelope(api, '/api/adapter/permits', { profile, pick: pickPermits });
    meta = result.meta;
    rows = (Array.isArray(result.data) ? result.data : []).map(normalizePermit).filter(Boolean);

    const wanted = initialSelection || selected?.id || null;
    selected = (wanted && rows.find((r) => r.id === wanted)) || null;
    // Landing on "needs decision" with nothing pending should show the list,
    // not an empty pane — fall back to whatever the filter does show.
    if (!selected) selected = visibleRows()[0] || null;

    applyStateToTable(table, { ...result, data: visibleRows() });
    table.setSelected(selected?.id ?? null);
    renderToolbar(toolbar);
    renderSide();
  }

  function refilter() {
    table.setRows(visibleRows());
    table.setSelected(selected?.id ?? null);
    renderToolbar(toolbar);
  }

  function renderToolbar(host) {
    if (!host) return;
    const input = el('input', {
      class: 'input input-sm',
      type: 'search',
      placeholder: 'Search permits…',
      value: search,
    });
    input.addEventListener('input', () => { search = input.value; refilter(); });

    const open = rows.filter((r) => OPEN_STATUSES.has(r.status)).length;
    paint(host, tabToolbar({
      title: 'Permits',
      subtitle: open ? `${open} awaiting a decision` : 'nothing pending',
      meta,
      onRefresh: () => load(),
      filters: [
        segmented(VIEWS.map((v) => ({
          ...v,
          count: v.value === 'all' ? rows.length
            : v.value === 'open' ? open : rows.length - open,
        })), {
          value: view,
          ariaLabel: 'Filter permits',
          onChange: (next) => { view = next; refilter(); },
        }),
        input,
      ],
    }));
  }

  function decisionForm(permit) {
    const form = createForm({
      submitLabel: 'Record decision',
      submitIcon: 'check',
      note: 'Only the fields you change are sent. permits_db.py derives the status '
        + 'when you approve or mark executed without setting one explicitly.',
      values: {
        status: permit.status || 'pending_approval',
        approved: isApproved(permit.approved),
        approval_note: permit.approval_note || '',
        action_plan: permit.action_plan || '',
        executed: permit.executed === 'yes',
        result_status: '',
        execution_result: permit.execution_result || '',
      },
      fields: [
        { key: 'status', label: 'Status', type: 'select', options: PERMIT_STATUSES },
        { key: 'approved', label: 'Approved', type: 'toggle', hint: 'Approving without a status sets it to approved.' },
        {
          key: 'approval_note',
          label: 'Approval note',
          type: 'textarea',
          span: 2,
          hint: 'Why this decision — the agent reads it back before acting.',
        },
        { key: 'action_plan', label: 'Action plan', type: 'textarea', span: 2 },
        { key: 'executed', label: 'Executed', type: 'toggle' },
        { key: 'result_status', label: 'Result', type: 'select', options: ['', ...RESULT_STATUSES] },
        { key: 'execution_result', label: 'Execution result', type: 'textarea', span: 2 },
      ],
      onSubmit: async (diff) => {
        const body = decisionBody(diff);
        if (!Object.keys(body).length) return;
        form.setBusy(true);
        const res = await runMutation(
          () => api.post(`/api/permits/${encodeURIComponent(permit.id)}/decision`, body, { profile }),
          { pending: 'Record decision', ok: `Decision recorded on ${permit.id}` },
        );
        form.setBusy(false);
        if (res) await load(permit.id);
      },
    });
    return form;
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (!selected) {
      paint(inspectorHost, sideHint('Select a permit', [
        'A permit is a decision an agent is blocked on: it carries the problem, the evidence, why it could not be auto-fixed, and the options.',
        'Recording a decision here writes straight to the permit ledger on the Hermes host and is audited.',
        'Red-barred rows are still waiting on you.',
      ]));
      return;
    }

    const permit = selected;
    const sections = [];

    if (permit.problem_summary) {
      sections.push({ title: 'Problem', node: el('p', { class: 'side-hint-line', text: permit.problem_summary }) });
    }
    if (permit.options.length) {
      sections.push({
        title: 'Options',
        node: el('div', { class: 'stack-sm' }, permit.options.map((option) => el('div', {
          class: `sub-card${permit.recommended_option === option.key ? ' is-selected' : ''}`,
        }, [
          el('div', { class: 'inline-chips' }, [
            el('span', { class: 'chip', text: `Option ${option.key}` }),
            permit.recommended_option === option.key
              ? statusChip('ok', 'recommended') : null,
          ].filter(Boolean)),
          el('div', { class: 'side-hint-line', text: option.text }),
        ]))),
      });
    }
    if (permit.recommended_action) {
      sections.push({
        title: 'Recommended action',
        node: el('p', { class: 'side-hint-line', text: permit.recommended_action }),
      });
    }
    if (permit.why_not_auto_fix) {
      sections.push({
        title: 'Why not auto-fixed',
        node: el('p', { class: 'side-hint-line', text: permit.why_not_auto_fix }),
      });
    }
    if (permit.evidence) {
      sections.push({
        title: 'Evidence',
        node: el('pre', { class: 'mono pre-wrap file-preview', text: String(permit.evidence) }),
      });
    }
    sections.push({ title: 'Decision', node: decisionForm(permit).node });

    paint(inspectorHost, createDetail({
      title: permit.title || permit.id,
      meta,
      chips: [
        statusChip(STATUS_TONE[permit.status] || 'idle', permit.status || 'unknown'),
        permit.severity ? statusChip(SEVERITY_TONE[permit.severity] || 'idle', permit.severity) : null,
        permit.category ? el('span', { class: 'chip', text: permit.category }) : null,
      ].filter(Boolean),
      fields: [
        { label: 'Permit id', value: permit.id, mono: true },
        { label: 'Fingerprint', value: permit.fingerprint, mono: true },
        { label: 'Source', value: permit.source },
        { label: 'Created', value: permit.created_at ? fmtTime(permit.created_at) : null, mono: true },
        { label: 'Updated', value: permit.updated_at ? fmtTime(permit.updated_at) : null, mono: true },
        { label: 'Expires', value: permit.expires_at ? fmtTime(permit.expires_at) : null, mono: true },
        { label: 'Executed', value: permit.executed || 'no' },
        { label: 'Executed at', value: permit.executed_at ? fmtTime(permit.executed_at) : null, mono: true },
      ],
      actions: [
        confirmAction({
          label: 'Delete permit',
          iconName: 'trash',
          confirmLabel: 'Delete — confirm?',
          onConfirm: () => runMutation(
            () => api.post(`/api/permits/${encodeURIComponent(permit.id)}/decision`, { delete: true }, { profile }),
            {
              pending: 'Delete permit',
              ok: `Permit ${permit.id} deleted`,
              onDone: () => { selected = null; return load(); },
            },
          ),
        }),
      ],
      raw: permit,
    }));
  }

  function bindEvents() {
    if (!sse || unsubscribe) return;
    const handles = SSE_EVENTS.map((name) => sse.on(name, () => {
      if (!root.isConnected) return;
      load(selected?.id ?? null).catch(() => null);
    }));
    unsubscribe = () => handles.forEach((off) => off());
  }

  renderSide();

  return {
    mount(container) { clear(container); container.append(root); },
    renderInspector,
    activate(params = {}) {
      bindEvents();
      return load(params.permit || params.id || null);
    },
    deactivate() {
      if (unsubscribe) { unsubscribe(); unsubscribe = null; }
      return { selection: selected?.id ?? null };
    },
    refresh: () => load(selected?.id ?? null),
    renderToolbar,
    setFilter(next) {
      if (next?.status !== undefined) view = next.status === 'pending_approval' ? 'open' : 'all';
      if (next?.q !== undefined) search = next.q;
      refilter();
      return Promise.resolve();
    },
    get data() { return rows; },
  };
}
