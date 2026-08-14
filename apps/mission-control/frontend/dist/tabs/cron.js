// Cron tab — the scheduler, with real CRUD.
//
// The old version could fire and (allegedly) pause: its pause sent
// `PATCH {state: …}`, which upstream never accepted, so the button silently did
// nothing. Pause/resume are their own routes and now go through them, and
// create/edit/delete exist for the first time.
//
// Two shapes matter and they are not the same: create takes flat fields with
// `schedule` as a plain string, while update takes `{updates: {...}}`. Sending
// flat fields to update produces a 422, which is why cron edits never worked.

import { el, clear, statusChip, iconButton, segmented, fmtTime, fmtAge } from '../ui.js';
import { createTable } from '../components/table.js';
import { createDetail } from '../components/detail.js';
import { createForm } from '../components/form.js';
import { createStatRow } from '../components/stat.js';
import {
  loadEnvelope, applyStateToTable, tabToolbar, runMutation, sideHint,
  paint, primaryButton, confirmAction,
} from './_kit.js';

export const ROUTE = 'cron';
export const LABEL = 'Cron';
export const GROUP = 'OPERATE';

export const SOURCE_ENDPOINTS = Object.freeze(['/api/upstream/api/cron/jobs']);

export const SSE_EVENTS = Object.freeze(['cron.changed']);

export const DELIVERY_TARGETS = Object.freeze(['local', 'telegram', 'whatsapp', 'discord', 'slack']);

const STATE_TONE = { scheduled: 'ok', running: 'info', paused: 'idle', error: 'danger', disabled: 'idle' };
const RESULT_TONE = { ok: 'ok', success: 'ok', error: 'danger', failed: 'danger', timeout: 'danger', skipped: 'idle' };

/** Pure: flatten one upstream cron job into a render row. */
export function cronRow(job) {
  if (!job || typeof job !== 'object') return null;
  const id = job.id ?? job.name ?? null;
  return {
    id: id === null ? null : String(id),
    name: job.name || job.id || '(unnamed)',
    prompt: job.prompt || '',
    script: job.script || null,
    no_agent: job.no_agent === true,
    schedule: job.schedule_display || job.schedule?.expr || job.schedule?.display || '',
    schedule_kind: job.schedule?.kind || null,
    state: job.state || (job.enabled === false ? 'disabled' : 'scheduled'),
    enabled: job.enabled !== false,
    paused_reason: job.paused_reason || null,
    last_run_at: job.last_run_at || null,
    next_run_at: job.next_run_at || null,
    last_status: job.last_status || null,
    last_error: job.last_error || null,
    deliver: job.deliver || 'local',
    model: job.model || null,
    provider: job.provider || null,
    base_url: job.base_url || null,
    skills: Array.isArray(job.skills) ? job.skills : [],
    enabled_toolsets: Array.isArray(job.enabled_toolsets) ? job.enabled_toolsets : [],
    workdir: job.workdir || null,
    context_from: job.context_from ?? null,
    profile: job.profile || null,
    origin: job.origin || null,
    completed: job.repeat?.completed ?? null,
    created_at: job.created_at || null,
  };
}

/** Pure: is this job in a state a human should look at? */
export function isFailing(row) {
  return row.last_status === 'error' || row.last_status === 'failed' || row.state === 'error';
}

const VIEWS = [
  { value: 'all', label: 'All' },
  { value: 'failing', label: 'Failing' },
  { value: 'paused', label: 'Paused' },
];

export function createCron({ api, profile, sse, toolbar }) {
  const root = el('div', { class: 'tab tab-cron' });
  const stats = el('div', { class: 'stat-row-host' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(stats, main);

  let rows = [];
  let meta = null;
  let selected = null;
  let view = 'all';
  let search = '';
  let creating = false;
  let unsubscribe = null;

  const table = createTable({
    rowId: (row) => row.id,
    emptyTitle: 'No cron jobs',
    emptyNote: 'Scheduled prompts and scripts appear here.',
    sort: { key: 'next_run_at', dir: 'asc' },
    columns: [
      {
        key: 'name',
        label: 'Job',
        sortable: true,
        render: (row) => el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: row.name }),
          el('span', { class: 'cell-dim mono', text: row.script ? `script: ${row.script}` : row.id }),
        ]),
      },
      { key: 'schedule', label: 'Schedule', width: '140px', sortable: true, mono: true },
      {
        key: 'state',
        label: 'State',
        width: '100px',
        sortable: true,
        render: (row) => statusChip(STATE_TONE[row.state] || 'idle', row.state),
      },
      {
        key: 'last_status',
        label: 'Last run',
        width: '150px',
        sortable: true,
        render: (row) => el('div', { class: 'cell-stack' }, [
          row.last_status
            ? statusChip(RESULT_TONE[row.last_status] || 'idle', row.last_status)
            : el('span', { class: 'cell-dim', text: 'never' }),
          row.last_run_at ? el('span', { class: 'cell-dim', text: fmtAge(row.last_run_at) }) : null,
        ].filter(Boolean)),
      },
      {
        key: 'next_run_at',
        label: 'Next run',
        width: '120px',
        sortable: true,
        render: (row) => (row.next_run_at
          ? el('span', { text: fmtAge(row.next_run_at) })
          : el('span', { class: 'cell-dim', text: '—' })),
      },
    ],
    rowClass: (row) => (isFailing(row) ? 'row-danger' : ''),
    rowActions: (row) => [
      iconButton({
        icon: 'play',
        label: `Fire ${row.name} now`,
        onClick: () => runMutation(
          () => api.post(`/api/upstream/api/cron/jobs/${encodeURIComponent(row.id)}/fire`, {}, { profile }),
          { pending: 'Fire job', ok: `${row.name} fired`, onDone: () => load(row.id) },
        ),
      }),
      iconButton({
        icon: row.state === 'paused' ? 'play' : 'pause',
        label: row.state === 'paused' ? `Resume ${row.name}` : `Pause ${row.name}`,
        onClick: () => runMutation(
          () => api.post(
            `/api/upstream/api/cron/jobs/${encodeURIComponent(row.id)}/${row.state === 'paused' ? 'resume' : 'pause'}`,
            {},
            { profile },
          ),
          {
            pending: 'Toggle job',
            ok: `${row.name} ${row.state === 'paused' ? 'resumed' : 'paused'}`,
            onDone: () => load(row.id),
          },
        ),
      }),
    ],
    onSelect: (row) => { selected = row; creating = false; renderSide(); },
  });
  main.append(table.node);

  function visibleRows() {
    const term = search.trim().toLowerCase();
    return rows.filter((row) => {
      if (view === 'failing' && !isFailing(row)) return false;
      if (view === 'paused' && row.state !== 'paused') return false;
      if (term) {
        const hay = `${row.name} ${row.id} ${row.schedule} ${row.prompt} ${row.script}`.toLowerCase();
        if (!hay.includes(term)) return false;
      }
      return true;
    });
  }

  async function load(initialSelection = null) {
    table.setLoading();
    const result = await loadEnvelope(api, '/api/upstream/api/cron/jobs', {
      profile,
      pick: (raw) => (Array.isArray(raw) ? raw : raw?.jobs || raw?.items || null),
    });
    meta = result.meta;
    rows = (Array.isArray(result.data) ? result.data : []).map(cronRow).filter(Boolean);

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
    clear(stats);
    const failing = rows.filter(isFailing).length;
    const next = rows
      .map((r) => r.next_run_at)
      .filter(Boolean)
      .sort()[0];
    stats.append(createStatRow([
      { label: 'Jobs', value: String(rows.length), iconName: 'cron', seriesIndex: 1 },
      { label: 'Scheduled', value: String(rows.filter((r) => r.state === 'scheduled').length), iconName: 'check', seriesIndex: 2 },
      { label: 'Paused', value: String(rows.filter((r) => r.state === 'paused').length), iconName: 'pause', seriesIndex: 3 },
      { label: 'Failing', value: String(failing), iconName: 'warning', seriesIndex: 4 },
      { label: 'Next fire', value: next ? fmtAge(next) : '—', iconName: 'play', seriesIndex: 5, foot: next ? fmtTime(next) : '' },
    ]));
  }

  function renderToolbar(host) {
    if (!host) return;
    const input = el('input', {
      class: 'input input-sm',
      type: 'search',
      placeholder: 'Search jobs…',
      value: search,
    });
    input.addEventListener('input', () => { search = input.value; refilter(); });

    paint(host, tabToolbar({
      title: 'Cron',
      subtitle: rows.length ? `${rows.length} job${rows.length === 1 ? '' : 's'}` : '',
      meta,
      onRefresh: () => load(),
      filters: [
        segmented(VIEWS.map((v) => ({
          ...v,
          count: v.value === 'all' ? rows.length
            : v.value === 'failing' ? rows.filter(isFailing).length
              : rows.filter((r) => r.state === 'paused').length,
        })), {
          value: view,
          ariaLabel: 'Filter cron jobs',
          onChange: (next) => { view = next; refilter(); },
        }),
        input,
      ],
      actions: [
        primaryButton('New job', 'plus', () => { creating = true; selected = null; table.setSelected(null); renderSide(); }),
      ],
    }));
  }

  /** The field set is shared by create and edit; only the submit differs. */
  function jobFields() {
    return [
      { key: 'name', label: 'Name', required: true },
      {
        key: 'schedule',
        label: 'Schedule',
        required: true,
        hint: 'A cron expression ("0 9 * * 1-5") or plain English ("every day at 9am").',
      },
      {
        key: 'prompt',
        label: 'Prompt',
        type: 'textarea',
        span: 2,
        hint: 'What the agent should do. Leave empty when running a script.',
      },
      { key: 'script', label: 'Script', hint: 'Runs instead of a prompt, relative to the workdir.' },
      { key: 'no_agent', label: 'Script only', type: 'toggle', hint: 'Run the script without starting an agent.' },
      { key: 'deliver', label: 'Deliver to', type: 'select', options: DELIVERY_TARGETS },
      { key: 'workdir', label: 'Working directory' },
      { key: 'model', label: 'Model' },
      { key: 'provider', label: 'Provider' },
      { key: 'skills', label: 'Skills', type: 'tags', span: 2 },
      { key: 'enabled_toolsets', label: 'Enabled toolsets', type: 'tags', span: 2 },
    ];
  }

  function createJobForm() {
    const form = createForm({
      submitLabel: 'Create job',
      submitIcon: 'plus',
      note: 'Hermes validates the schedule; an unparseable one comes back as a 4xx with the reason.',
      values: {
        name: '', schedule: '', prompt: '', script: '', no_agent: false,
        deliver: 'local', workdir: '', model: '', provider: '',
        skills: [], enabled_toolsets: [],
      },
      fields: jobFields(),
      onSubmit: async (diff, all) => {
        const body = { name: all.name, schedule: all.schedule, deliver: all.deliver || 'local' };
        // Upstream treats null and "" differently on some fields, so only send
        // what the operator actually filled in.
        for (const key of ['prompt', 'script', 'workdir', 'model', 'provider']) {
          if (all[key]) body[key] = all[key];
        }
        for (const key of ['skills', 'enabled_toolsets']) {
          if (Array.isArray(all[key]) && all[key].length) body[key] = all[key];
        }
        if (all.no_agent) body.no_agent = true;

        form.setBusy(true);
        const res = await runMutation(
          () => api.post('/api/upstream/api/cron/jobs', body, { profile }),
          { pending: 'Create job', ok: `Job ${all.name} created` },
        );
        form.setBusy(false);
        if (res) {
          creating = false;
          await load(res.data?.id ?? res.data?.job?.id ?? null);
        }
      },
    });
    return form;
  }

  function editJobForm(job) {
    const form = createForm({
      submitLabel: 'Save changes',
      note: 'Only changed fields are sent, wrapped in the {updates} envelope upstream expects.',
      values: {
        name: job.name,
        schedule: job.schedule,
        prompt: job.prompt,
        script: job.script || '',
        no_agent: job.no_agent,
        deliver: job.deliver,
        workdir: job.workdir || '',
        model: job.model || '',
        provider: job.provider || '',
        skills: job.skills,
        enabled_toolsets: job.enabled_toolsets,
      },
      fields: jobFields().map((field) => ({ ...field, required: false })),
      onSubmit: async (diff) => {
        if (!Object.keys(diff).length) return;
        form.setBusy(true);
        const res = await runMutation(
          () => api.patch(`/api/upstream/api/cron/jobs/${encodeURIComponent(job.id)}`, { updates: diff }, { profile }),
          { pending: 'Save job', ok: `${job.name} updated` },
        );
        form.setBusy(false);
        if (res) await load(job.id);
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
    if (creating) {
      paint(inspectorHost, createDetail({
        title: 'New cron job',
        chips: [el('span', { class: 'chip', text: 'draft' })],
        sections: [{ title: 'Definition', node: createJobForm().node }],
        actions: [
          el('button', {
            class: 'btn btn-sm',
            type: 'button',
            text: 'Cancel',
            onclick: () => { creating = false; renderSide(); },
          }),
        ],
      }));
      return;
    }

    if (!selected) {
      paint(inspectorHost, sideHint('Select a job', [
        'Cron jobs run a prompt or a script on a schedule and deliver the result to a channel.',
        'Fire runs a job immediately without touching its schedule; pause keeps the definition but stops the timer.',
        'Red-barred rows failed on their last run — the error is in the detail pane.',
      ]));
      return;
    }

    const job = selected;
    const sections = [];
    if (job.last_error) {
      sections.push({
        title: 'Last error',
        node: el('pre', { class: 'mono pre-wrap file-preview', text: job.last_error }),
      });
    }
    if (job.prompt) {
      sections.push({ title: 'Prompt', node: el('p', { class: 'side-hint-line', text: job.prompt }) });
    }
    sections.push({ title: 'Edit', node: editJobForm(job).node });

    paint(inspectorHost, createDetail({
      title: job.name,
      meta,
      chips: [
        statusChip(STATE_TONE[job.state] || 'idle', job.state),
        job.last_status ? statusChip(RESULT_TONE[job.last_status] || 'idle', `last: ${job.last_status}`) : null,
        job.no_agent ? el('span', { class: 'chip', text: 'script only' }) : null,
      ].filter(Boolean),
      fields: [
        { label: 'Job id', value: job.id, mono: true },
        { label: 'Schedule', value: job.schedule, mono: true },
        { label: 'Next run', value: job.next_run_at ? fmtTime(job.next_run_at) : null, mono: true },
        { label: 'Last run', value: job.last_run_at ? fmtTime(job.last_run_at) : null, mono: true },
        { label: 'Runs completed', value: job.completed, mono: true },
        { label: 'Deliver to', value: job.deliver },
        { label: 'Delivery target', value: job.origin?.chat_name || job.origin?.chat_id || null },
        { label: 'Working dir', value: job.workdir, mono: true },
        { label: 'Model', value: job.model },
        { label: 'Paused because', value: job.paused_reason },
      ],
      sections,
      actions: [
        primaryButton('Fire now', 'play', () => runMutation(
          () => api.post(`/api/upstream/api/cron/jobs/${encodeURIComponent(job.id)}/fire`, {}, { profile }),
          { pending: 'Fire job', ok: `${job.name} fired`, onDone: () => load(job.id) },
        )),
        confirmAction({
          label: 'Delete job',
          iconName: 'trash',
          confirmLabel: 'Delete — confirm?',
          onConfirm: () => runMutation(
            () => api.del(`/api/upstream/api/cron/jobs/${encodeURIComponent(job.id)}?confirm=true`, { profile }),
            {
              pending: 'Delete job',
              ok: `${job.name} deleted`,
              onDone: () => { selected = null; return load(); },
            },
          ),
        }),
      ],
      raw: job,
    }));
  }

  function bindEvents() {
    if (!sse || unsubscribe) return;
    const off = sse.on('cron.changed', () => {
      if (!root.isConnected) return;
      load(selected?.id ?? null).catch(() => null);
    });
    unsubscribe = off;
  }

  renderSide();

  return {
    mount(container) { clear(container); container.append(root); },
    renderInspector,
    activate(params = {}) {
      bindEvents();
      return load(params.job || params.id || null);
    },
    deactivate() {
      if (unsubscribe) { unsubscribe(); unsubscribe = null; }
      return { selection: selected?.id ?? null };
    },
    refresh: () => load(selected?.id ?? null),
    renderToolbar,
    get data() { return rows; },
  };
}
