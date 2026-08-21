// "Was the payload actually sent to the provider, and where is it stuck?"
//
// The chat stream tells you what the agent produced; it says nothing about the
// HTTP calls underneath. The adapter's session timeline does: `provider_requests`
// is the last 100 LLM requests recorded in state.db, with model, status and
// timing. Reading it here is what turns "the run seems stuck" into a specific
// answer — the request went out and is pending, or it never went out at all.
//
// The route is already on the BFF's adapter allowlist
// (`^/sessions/[^/]+/timeline$`), so this needs no new proxy surface.

import { el, clear, emptyState, fmtTime, skeleton, unavailableState } from '../../ui.js';
import { icon } from '../../icons.js';
import { recordFrom } from '../../pure/data-shape.js';
import { formatCount, formatDuration } from './transcript.js';

export function createRunTrace({ api, sessionId }) {
  const root = el('div', { class: 'chat-run-trace' });
  const head = el('div', { class: 'chat-run-trace-head' }, [
    icon('activity', { size: 12 }),
    el('span', { text: 'Run trace' }),
  ]);
  const refreshButton = el('button', {
    class: 'chat-run-trace-refresh', type: 'button',
    title: 'Reload the provider trace', 'aria-label': 'Reload the provider trace',
  }, [icon('refresh', { size: 11 })]);
  head.append(refreshButton);
  const body = el('div', { class: 'chat-run-trace-body' });
  root.append(head, body);

  async function refresh() {
    if (!sessionId) return;
    clear(body);
    body.append(skeleton({ lines: 3 }));

    const response = await api
      .get(`/api/adapter/sessions/${encodeURIComponent(sessionId)}/timeline?limit=200`)
      .catch(() => null);
    clear(body);
    if (!response) {
      body.append(unavailableState({ reason: 'Adapter timeline unavailable' }));
      return;
    }

    const data = recordFrom(response.data) || {};
    const requests = Array.isArray(data.provider_requests) ? data.provider_requests : [];
    const usage = Array.isArray(data.model_usage) ? data.model_usage : [];

    if (usage.length) body.append(usageTable(usage));

    if (!requests.length) {
      // `provider_requests` comes from `llm_provider_requests`, a table only
      // newer Hermes builds write; the adapter returns [] when it is missing.
      // Claiming "nothing was dispatched" while the usage table right above
      // reports 25 API calls is simply false, so the two cases get different
      // copy — and the honest per-call count comes from the usage rows.
      const calls = usage.reduce((sum, row) => sum + (Number(row.api_call_count) || 0), 0);
      body.append(calls > 0 ? emptyState({
        title: `${formatCount(calls)} provider ${calls === 1 ? 'call' : 'calls'} billed, none traced`,
        note: 'This Hermes build does not record per-request metadata (llm_provider_requests). The totals above come from the session usage ledger.',
      }) : emptyState({
        title: 'No provider requests recorded',
        note: 'Nothing has been dispatched to a model API for this session yet.',
      }));
      return;
    }

    // Newest first: when a run is stuck, the interesting row is the last one.
    const rows = [...requests].sort((a, b) => reqTime(b) - reqTime(a));
    const list = el('div', { class: 'chat-trace-list' });
    for (const row of rows.slice(0, 50)) list.append(requestRow(row));
    body.append(list);
  }

  refreshButton.addEventListener('click', () => refresh().catch(() => null));
  return { node: root, refresh };
}

// `captured_at` is the adapter's own ordering column for llm_provider_requests;
// the rest are fallbacks for builds that name it differently.
function reqTime(row) {
  return Number(row.captured_at || row.created_at || row.started_at || row.ts || 0);
}

function requestRow(row) {
  const status = String(row.status || row.state || (row.error ? 'error' : 'ok')).toLowerCase();
  // An in-flight row is the whole point of this panel, so it gets its own tone
  // rather than being lumped in with "ok".
  const tone = /err|fail/.test(status) ? 'failed'
    : /pending|running|in_progress|started/.test(status) ? 'running' : 'done';

  const node = el('details', { class: 'chat-trace-row', 'data-status': tone });
  const durationMs = Number(row.duration_ms ?? (Number(row.duration || 0) * 1000)) || null;
  node.append(el('summary', {}, [
    el('span', { class: `chip chip-${tone === 'running' ? 'running' : tone === 'failed' ? 'failed' : 'done'}`, text: status }),
    el('span', { class: 'chat-trace-model', text: row.model || row.provider || 'model' }),
    el('span', { class: 'chat-trace-time mono', text: fmtTime(reqTime(row) || null) }),
    durationMs ? el('span', { class: 'chat-trace-dur mono', text: formatDuration(durationMs) }) : null,
  ]));

  const detail = el('dl', { class: 'chat-trace-detail' });
  // Ordered to match the adapter's own metadata column set, which is all this
  // route ever returns (payload_json is deliberately never selected upstream).
  const pairs = [
    ['provider', row.provider],
    ['model', row.model],
    ['api mode', row.api_mode],
    ['transport', row.transport],
    ['turn', row.turn_id],
    ['call #', row.api_call_count],
    ['attempt', row.attempt],
    ['api request id', row.api_request_id || row.request_id || row.id],
    ['base request id', row.base_request_id],
    ['prompt tokens', row.input_tokens ?? row.prompt_tokens],
    ['completion tokens', row.output_tokens ?? row.completion_tokens],
    ['endpoint', row.endpoint || row.url || row.base_url],
    ['error', row.error],
  ];
  for (const [label, value] of pairs) {
    if (value === undefined || value === null || value === '') continue;
    detail.append(el('dt', { text: label }));
    detail.append(el('dd', { class: 'mono', text: String(value) }));
  }
  node.append(detail);
  return node;
}

// `session_model_usage.task` names why a model was called. It is the difference
// between "the agent secretly switched models" and "Hermes spent one haiku call
// naming the thread" — which is exactly the row that made this panel look like
// it was showing another session's data.
const TASK_LABELS = {
  title_generation: 'titles',
  compression: 'compaction',
  summarization: 'summary',
};

function usageTable(usage) {
  const wrap = el('div', { class: 'chat-trace-usage' });
  wrap.append(el('div', { class: 'chat-trace-usage-head', text: 'Model usage for this session' }));

  // The conversation model first, then the auxiliary tasks: reading top-down
  // should answer "what ran this thread" before "what else was billed to it".
  const rows = [...usage].sort((a, b) => {
    const auxiliary = Number(Boolean(a.task)) - Number(Boolean(b.task));
    if (auxiliary) return auxiliary;
    return (Number(b.input_tokens) || 0) - (Number(a.input_tokens) || 0);
  });

  for (const row of rows) {
    const calls = Number(row.api_call_count) || 0;
    const cacheRead = Number(row.cache_read_tokens) || 0;
    const cost = Number(row.actual_cost_usd) || Number(row.estimated_cost_usd) || 0;
    const task = String(row.task || '').trim();
    wrap.append(el('div', { class: `chat-trace-usage-row${task ? ' is-aux' : ''}` }, [
      el('span', { class: 'chat-trace-model', text: row.model || 'model' }),
      task
        ? el('span', {
          class: 'chat-trace-task',
          text: TASK_LABELS[task] || task.replace(/_/g, ' '),
          title: `Auxiliary model call: ${task}`,
        })
        : null,
      calls ? el('span', { class: 'chat-trace-calls mono', text: `${formatCount(calls)}×` }) : null,
      el('span', {
        class: 'mono',
        text: `${formatCount(Number(row.input_tokens) || 0)}↑ ${formatCount(Number(row.output_tokens) || 0)}↓`,
      }),
      cacheRead
        ? el('span', { class: 'chat-trace-cache mono', text: `${formatCount(cacheRead)} cached`, title: 'Tokens served from the prompt cache' })
        : null,
      cost ? el('span', { class: 'mono', text: `$${cost.toFixed(4)}` }) : null,
    ]));
  }
  return wrap;
}
