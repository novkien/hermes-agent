// Issues tab — the adapter's issue ledger, with working status transitions.
//
// Same story as Permits: the data was readable but every transition had to
// happen over SSH. `POST /api/issues/{id}/update` (BFF → adapter →
// agent_notes_db.py) closes the loop, and the form mirrors the script's own
// rules — resolved needs a resolution *and* a verification, dismissed needs a
// reason, merged needs a target — so the constraint is visible before the round
// trip rather than as a 400 afterwards.

import { mapIssueRow, ISSUE_COLUMNS } from '../pure/table-mapping.js';
import { el, clear, statusChip, segmented, fmtTime, fmtAge } from '../ui.js';
import { createTable } from '../components/table.js';
import { createDetail } from '../components/detail.js';
import { createForm } from '../components/form.js';
import { createStatRow } from '../components/stat.js';
import { toast } from '../components/toast.js';
import {
  loadEnvelope, applyStateToTable, tabToolbar, runMutation, sideHint, paint, confirmAction,
} from './_kit.js';

export const ROUTE = 'issues';
export const LABEL = 'Issues';
export const GROUP = 'GOVERN';

export const SOURCE_ENDPOINTS = Object.freeze([
  '/api/adapter/issues',
  '/api/adapter/issues/{id}',
]);

export const SSE_EVENTS = Object.freeze(['issue.changed']);

export const ISSUE_STATUSES = Object.freeze(['open', 'resolved', 'dismissed', 'merged']);

export const ISSUE_EVENT_TYPES = Object.freeze([
  'observed', 'recurred', 'investigation', 'workaround', 'recovered',
  'reproduced', 'not_reproduced', 'verification_failed', 'resolved',
  'dismissed', 'merged',
]);

export const SEVERITIES = Object.freeze(['critical', 'high', 'medium', 'low']);

const STATUS_TONE = { open: 'warn', resolved: 'ok', dismissed: 'idle', merged: 'info' };
const SEVERITY_TONE = { critical: 'danger', high: 'danger', medium: 'warn', low: 'idle' };

/**
 * Pure: the script's own preconditions, so the form can refuse locally with the
 * same message the adapter would return. Kept as one function rather than
 * per-field validators because the rules are cross-field.
 */
export function validateIssueUpdate(body) {
  if (body.status && !ISSUE_STATUSES.includes(body.status)) {
    return `status must be one of: ${ISSUE_STATUSES.join(', ')}`;
  }
  if (body.event_type && !ISSUE_EVENT_TYPES.includes(body.event_type)) {
    return `event_type must be one of: ${ISSUE_EVENT_TYPES.join(', ')}`;
  }
  if (body.status === 'resolved' && !(body.resolution && body.verification)) {
    return 'resolved requires both resolution and verification';
  }
  if (body.status === 'dismissed' && !body.resolution) {
    return 'dismissed requires a resolution explaining why it is not a defect';
  }
  if (body.status === 'merged' && !body.merge_into_id) {
    return 'merged requires merge_into_id';
  }
  return null;
}

function pickIssues(raw) {
  if (Array.isArray(raw)) return raw;
  if (!raw || typeof raw !== 'object') return null;
  for (const key of ['issues', 'items', 'rows', 'list', 'results', 'records']) {
    if (Array.isArray(raw[key])) return raw[key];
  }
  return null;
}

/** Pure: normalize one adapter issue record. */
export function normalizeIssue(row) {
  if (!row || typeof row !== 'object') return null;
  const base = mapIssueRow(row) || {};
  const id = row.id ?? row.issue_id ?? null;
  return {
    ...base,
    id: id === null ? null : String(id),
    title: row.issue ?? row.title ?? null,
    occurrence_count: Number(row.occurrence_count ?? 0),
  };
}

/** Pure: map an issues-list envelope into render rows. */
export function renderIssuesList(envelope) {
  const meta = (envelope && envelope.meta) || null;
  const raw = (envelope && envelope.data) || null;
  if (!meta) return { rows: [], meta: null, state: 'unavailable' };
  if (meta.freshness === 'unavailable') return { rows: [], meta, state: 'unavailable' };
  if (meta.freshness === 'unsupported') return { rows: [], meta, state: 'unsupported' };
  const list = pickIssues(raw);
  if (!list) return { rows: [], meta, state: 'empty' };
  const rows = list.map(normalizeIssue).filter(Boolean);
  if (!rows.length) return { rows: [], meta, state: 'empty' };
  return { rows, meta, state: meta.freshness === 'partial' ? 'partial' : 'ready' };
}

/** Pure: map an issue-detail envelope. */
export function renderIssueDetail(envelope) {
  const meta = (envelope && envelope.meta) || null;
  const raw = (envelope && envelope.data) || null;
  if (!meta || meta.freshness === 'unavailable' || !raw) {
    return { issue: null, meta, state: !meta ? 'unavailable' : meta.freshness };
  }
  const record = Array.isArray(raw) ? raw[0] : (raw.issue || raw.item || raw.record || raw);
  return { issue: normalizeIssue(record), meta, state: 'ready' };
}

export { ISSUE_COLUMNS };

const VIEWS = [
  { value: 'open', label: 'Open' },
  { value: 'closed', label: 'Closed' },
  { value: 'all', label: 'All' },
];

export function createIssues({ api, profile, sse, toolbar }) {
  const root = el('div', { class: 'tab tab-issues' });
  const stats = el('div', { class: 'stat-row-host' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(stats, main);

  let rows = [];
  let meta = null;
  let selected = null;
  let view = 'open';
  let severity = '';
  let search = '';
  let unsubscribe = null;

  const table = createTable({
    rowId: (row) => row.id,
    emptyTitle: 'No issues',
    emptyNote: 'Agents file an issue when they hit a defect worth remembering.',
    sort: { key: 'last_seen_at', dir: 'desc' },
    columns: [
      { key: 'id', label: '#', width: '56px', align: 'right', sortable: true, sortValue: (r) => Number(r.id) || 0, mono: true },
      {
        key: 'title',
        label: 'Issue',
        sortable: true,
        render: (row) => el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: row.title || '(untitled)', title: row.title || '' }),
          row.impact ? el('span', { class: 'cell-dim', text: row.impact, title: row.impact }) : null,
        ].filter(Boolean)),
      },
      {
        key: 'severity',
        label: 'Severity',
        width: '95px',
        sortable: true,
        // Severity sorts by urgency, not alphabetically — "critical" before
        // "low" is the only ordering an operator ever wants.
        sortValue: (row) => SEVERITIES.indexOf(row.severity),
        render: (row) => (row.severity ? statusChip(SEVERITY_TONE[row.severity] || 'idle', row.severity) : null),
      },
      {
        key: 'status',
        label: 'Status',
        width: '110px',
        sortable: true,
        render: (row) => statusChip(STATUS_TONE[row.status] || 'idle', row.status || 'unknown'),
      },
      {
        key: 'occurrence_count',
        label: 'Seen',
        width: '64px',
        align: 'right',
        sortable: true,
        render: (row) => el('span', { class: 'mono', text: `×${row.occurrence_count || 1}` }),
      },
      {
        key: 'last_seen_at',
        label: 'Last seen',
        width: '120px',
        sortable: true,
        render: (row) => (row.last_seen_at ? el('span', { text: fmtAge(row.last_seen_at) }) : null),
      },
    ],
    rowClass: (row) => (row.status === 'open' && (row.severity === 'critical' || row.severity === 'high') ? 'row-danger' : ''),
    onSelect: (row) => { selected = row; renderSide(); },
  });
  main.append(table.node);

  function visibleRows() {
    const term = search.trim().toLowerCase();
    return rows.filter((row) => {
      if (view === 'open' && row.status !== 'open') return false;
      if (view === 'closed' && row.status === 'open') return false;
      if (severity && row.severity !== severity) return false;
      if (term) {
        const hay = `${row.id} ${row.title} ${row.context} ${row.impact}`.toLowerCase();
        if (!hay.includes(term)) return false;
      }
      return true;
    });
  }

  async function load(initialSelection = null) {
    table.setLoading();
    const result = await loadEnvelope(api, '/api/adapter/issues', { profile, pick: pickIssues });
    meta = result.meta;
    rows = (Array.isArray(result.data) ? result.data : []).map(normalizeIssue).filter(Boolean);

    const wanted = initialSelection ? String(initialSelection) : selected?.id ?? null;
    selected = (wanted && rows.find((r) => r.id === wanted)) || null;

    applyStateToTable(table, { ...result, data: visibleRows() });
    table.setSelected(selected?.id ?? null);
    renderStats();
    renderToolbar(toolbar);
    renderSide();
  }

  function refilter() {
    table.setRows(visibleRows());
    table.setSelected(selected?.id ?? null);
    renderToolbar(toolbar);
  }

  function renderStats() {
    const open = rows.filter((r) => r.status === 'open');
    const bySeverity = (name) => open.filter((r) => r.severity === name).length;
    clear(stats);
    stats.append(createStatRow([
      { label: 'Open', value: String(open.length), iconName: 'issues', seriesIndex: 1 },
      { label: 'Critical', value: String(bySeverity('critical')), iconName: 'flame', seriesIndex: 2 },
      { label: 'High', value: String(bySeverity('high')), iconName: 'warning', seriesIndex: 3 },
      { label: 'Resolved', value: String(rows.filter((r) => r.status === 'resolved').length), iconName: 'check', seriesIndex: 4 },
      {
        label: 'Recurring',
        value: String(open.filter((r) => r.occurrence_count > 1).length),
        iconName: 'retry',
        seriesIndex: 5,
        foot: 'seen more than once',
      },
    ]));
  }

  function renderToolbar(host) {
    if (!host) return;
    const input = el('input', {
      class: 'input input-sm',
      type: 'search',
      placeholder: 'Search issues…',
      value: search,
    });
    input.addEventListener('input', () => { search = input.value; refilter(); });

    const severitySelect = el('select', { class: 'select input-sm' });
    for (const value of ['', ...SEVERITIES]) {
      const option = el('option', { value, text: value ? `severity: ${value}` : 'severity: any' });
      option.selected = severity === value;
      severitySelect.append(option);
    }
    severitySelect.addEventListener('change', () => { severity = severitySelect.value; refilter(); });

    const open = rows.filter((r) => r.status === 'open').length;
    paint(host, tabToolbar({
      title: 'Issues',
      subtitle: `${open} open of ${rows.length}`,
      meta,
      onRefresh: () => load(),
      filters: [
        segmented(VIEWS.map((v) => ({
          ...v,
          count: v.value === 'all' ? rows.length : v.value === 'open' ? open : rows.length - open,
        })), {
          value: view,
          ariaLabel: 'Filter issues',
          onChange: (next) => { view = next; refilter(); },
        }),
        severitySelect,
        input,
      ],
    }));
  }

  function updateForm(issue) {
    const form = createForm({
      submitLabel: 'Apply update',
      submitIcon: 'check',
      note: 'Each update also appends an occurrence event to the issue history.',
      values: {
        status: issue.status || 'open',
        event_type: 'investigation',
        severity: issue.severity || '',
        resolution: issue.resolution || '',
        verification: issue.verification || '',
        merge_into_id: issue.merged_into_id || '',
        context: '',
      },
      fields: [
        { key: 'status', label: 'Status', type: 'select', options: ISSUE_STATUSES },
        { key: 'event_type', label: 'Event type', type: 'select', options: ISSUE_EVENT_TYPES },
        { key: 'severity', label: 'Severity', type: 'select', options: ['', ...SEVERITIES] },
        {
          key: 'merge_into_id',
          label: 'Merge into issue',
          hint: 'Required when the status is merged.',
        },
        {
          key: 'resolution',
          label: 'Resolution',
          type: 'textarea',
          span: 2,
          hint: 'Required to resolve or dismiss — what was done, or why this is not a defect.',
        },
        {
          key: 'verification',
          label: 'Verification',
          type: 'textarea',
          span: 2,
          hint: 'Required to resolve — how you confirmed the fix holds.',
        },
        {
          key: 'context',
          label: 'Occurrence note',
          type: 'textarea',
          span: 2,
          hint: 'Appended to the history without changing the issue body.',
        },
      ],
      onSubmit: async (diff, all) => {
        const body = {};
        for (const key of ['status', 'event_type', 'severity', 'resolution', 'verification', 'merge_into_id', 'context']) {
          if (key in diff && diff[key] !== '' && diff[key] !== null) body[key] = diff[key];
        }
        if (!Object.keys(body).length) return;
        // The cross-field rules read the whole form, not just the diff: an
        // unchanged resolution still satisfies "resolved requires one".
        const problem = validateIssueUpdate({ ...all, ...body });
        if (problem) {
          toast(problem, { tone: 'warn', detail: 'the issue ledger enforces this rule', timeout: 8000 });
          return;
        }
        form.setBusy(true);
        const res = await runMutation(
          () => api.post(`/api/issues/${encodeURIComponent(issue.id)}/update`, body, { profile }),
          { pending: 'Apply update', ok: `Issue ${issue.id} updated` },
        );
        form.setBusy(false);
        if (res) await load(issue.id);
      },
    });
    return form;
  }

  function textSection(title, value) {
    if (!value) return null;
    return { title, node: el('p', { class: 'side-hint-line', text: String(value) }) };
  }

  /** Soft-delete: the ledger sets deleted_at/deleted_reason and keeps the row,
   * it just stops appearing in issue_list — so this hides the issue rather
   * than destroying its history. A reason is required by the upstream script
   * itself, so it is collected before the button ever arms. */
  function deleteSection(issue) {
    const note = el('p', {
      class: 'field-hint',
      text: 'Removes this issue from the list. Its record and history are kept, not erased.',
    });
    const button = confirmAction({
      label: 'Delete issue',
      iconName: 'trash',
      confirmLabel: 'Click again to delete',
      onConfirm: async () => {
        const reason = window.prompt(`Why is issue ${issue.id} being deleted?`);
        if (!reason || !reason.trim()) return;
        const res = await runMutation(
          () => api.post(`/api/issues/${encodeURIComponent(issue.id)}/update`, { delete: true, reason: reason.trim() }, { profile }),
          { pending: 'Delete issue', ok: `Issue ${issue.id} deleted` },
        );
        if (res) {
          selected = null;
          await load();
        }
      },
    });
    return el('div', { class: 'stack-sm' }, [note, button]);
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (!selected) {
      paint(inspectorHost, sideHint('Select an issue', [
        'Issues are the agents\' shared defect memory: each one carries reproduction steps, impact, and the expected behaviour.',
        'Transitions here write to the issue ledger on the Hermes host and are audited.',
        'Resolving needs both a resolution and a verification — that rule comes from the ledger itself, not from this UI.',
      ]));
      return;
    }

    const issue = selected;
    const sections = [
      textSection('Context', issue.context),
      textSection('Reproduction', issue.reproduction),
      textSection('Expected behaviour', issue.expected_behavior),
      textSection('Impact', issue.impact),
      textSection('Initial assessment', issue.initial_assessment),
      textSection('Resolution', issue.resolution),
      textSection('Verification', issue.verification),
    ].filter(Boolean);
    sections.push({ title: 'Update', node: updateForm(issue).node });
    sections.push({ title: 'Delete', node: deleteSection(issue) });

    paint(inspectorHost, createDetail({
      title: issue.title || `Issue ${issue.id}`,
      meta,
      chips: [
        statusChip(STATUS_TONE[issue.status] || 'idle', issue.status || 'unknown'),
        issue.severity ? statusChip(SEVERITY_TONE[issue.severity] || 'idle', issue.severity) : null,
        issue.occurrence_count > 1 ? el('span', { class: 'chip', text: `seen ×${issue.occurrence_count}` }) : null,
      ].filter(Boolean),
      fields: [
        { label: 'Issue id', value: issue.id, mono: true },
        { label: 'Fingerprint', value: issue.fingerprint, mono: true },
        { label: 'Merged into', value: issue.merged_into_id, mono: true },
        { label: 'First seen', value: issue.first_seen_at ? fmtTime(issue.first_seen_at) : null, mono: true },
        { label: 'Last seen', value: issue.last_seen_at ? fmtTime(issue.last_seen_at) : null, mono: true },
        { label: 'Resolved', value: issue.resolved_at ? fmtTime(issue.resolved_at) : null, mono: true },
        { label: 'Created by', value: issue.created_by },
        { label: 'Last updated by', value: issue.last_updated_by },
      ],
      sections,
      raw: issue,
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
      return load(params.issue || params.id || null);
    },
    deactivate() {
      if (unsubscribe) { unsubscribe(); unsubscribe = null; }
      return { selection: selected?.id ?? null };
    },
    refresh: () => load(selected?.id ?? null),
    renderToolbar,
    setFilter(next) {
      if (next?.status !== undefined) view = next.status === 'open' ? 'open' : 'all';
      if (next?.severity !== undefined) severity = next.severity;
      if (next?.q !== undefined) search = next.q;
      refilter();
      return Promise.resolve();
    },
    get data() { return rows; },
  };
}
