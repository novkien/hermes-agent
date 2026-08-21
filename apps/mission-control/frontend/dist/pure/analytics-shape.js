// Analytics shaping — pure, DOM-free.
//
// `/api/analytics/usage` returns `{daily, by_model, by_task, totals, skills,
// tools, period_days}`; `/api/analytics/models` returns `{models, totals,
// period_days}`. The old Analytics tab read `data.input_tokens` and
// `data.total_tokens`, which are not fields either endpoint has ever returned,
// so it rendered four em dashes. Everything below is keyed off the real shape.

const DAY_KEYS = Object.freeze([
  'input_tokens', 'output_tokens', 'cache_read_tokens', 'reasoning_tokens',
  'estimated_cost', 'actual_cost', 'sessions', 'api_calls',
]);

function num(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** Pure: the daily series, oldest first, with every numeric key coerced. */
export function dailySeries(usage) {
  const daily = Array.isArray(usage?.daily) ? usage.daily : [];
  return daily
    .map((row) => {
      const out = { day: String(row.day || '') };
      for (const key of DAY_KEYS) out[key] = num(row[key]);
      out.total_tokens = out.input_tokens + out.output_tokens;
      return out;
    })
    .sort((a, b) => (a.day < b.day ? -1 : a.day > b.day ? 1 : 0));
}

/**
 * Pure: totals over a (possibly brushed) slice of the daily series.
 *
 * Recomputed from the days rather than read from `usage.totals` so a brushed
 * window reports the window, not the whole period — the two disagree by
 * design and silently showing the period total under a brush would be a lie.
 */
export function sliceTotals(days) {
  const list = Array.isArray(days) ? days : [];
  const totals = {
    days: list.length,
    input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, reasoning_tokens: 0,
    estimated_cost: 0, actual_cost: 0, sessions: 0, api_calls: 0,
  };
  for (const day of list) {
    for (const key of DAY_KEYS) totals[key] += num(day[key]);
  }
  totals.total_tokens = totals.input_tokens + totals.output_tokens;
  totals.from = list.length ? list[0].day : null;
  totals.to = list.length ? list[list.length - 1].day : null;
  return totals;
}

/** Pure: model rows from either the usage payload's by_model or /models. */
export function modelRows(payload) {
  const list = Array.isArray(payload?.models)
    ? payload.models
    : Array.isArray(payload?.by_model) ? payload.by_model : [];
  return list.map((row) => ({
    model: row.model || row.model_id || 'unknown',
    provider: row.provider || null,
    input_tokens: num(row.input_tokens),
    output_tokens: num(row.output_tokens),
    cache_read_tokens: num(row.cache_read_tokens),
    reasoning_tokens: num(row.reasoning_tokens),
    total_tokens: num(row.input_tokens) + num(row.output_tokens),
    estimated_cost: num(row.estimated_cost),
    actual_cost: num(row.actual_cost),
    sessions: num(row.sessions),
    api_calls: num(row.api_calls),
    tool_calls: num(row.tool_calls),
    last_used_at: row.last_used_at ?? null,
    avg_tokens_per_session: num(row.avg_tokens_per_session),
  })).sort((a, b) => b.total_tokens - a.total_tokens);
}

/** Pure: per-task rows from the usage payload. */
export function taskRowsFromUsage(usage) {
  const list = Array.isArray(usage?.by_task) ? usage.by_task : [];
  return list.map((row) => ({
    task: row.task || 'unknown',
    input_tokens: num(row.input_tokens),
    output_tokens: num(row.output_tokens),
    total_tokens: num(row.input_tokens) + num(row.output_tokens),
    estimated_cost: num(row.estimated_cost),
    api_calls: num(row.api_calls),
    models: Array.isArray(row.models) ? row.models : [],
  })).sort((a, b) => b.total_tokens - a.total_tokens);
}

/** Pure: tool-usage rows, already percentage-bearing upstream. */
export function toolRows(usage) {
  const list = Array.isArray(usage?.tools) ? usage.tools : [];
  return list.map((row) => ({
    tool: row.tool || 'unknown',
    count: num(row.count),
    percentage: num(row.percentage),
  })).sort((a, b) => b.count - a.count);
}

/** Pure: skill-usage summary + top rows. */
export function skillUsage(usage) {
  const skills = usage?.skills && typeof usage.skills === 'object' ? usage.skills : {};
  const summary = skills.summary && typeof skills.summary === 'object' ? skills.summary : {};
  const top = Array.isArray(skills.top_skills) ? skills.top_skills : [];
  return {
    totalLoads: num(summary.total_skill_loads),
    totalEdits: num(summary.total_skill_edits),
    totalActions: num(summary.total_skill_actions),
    distinct: num(summary.distinct_skills_used),
    top: top.map((row) => ({
      skill: row.skill || 'unknown',
      views: num(row.view_count),
      manages: num(row.manage_count ?? row.manage_calls),
    })).sort((a, b) => b.views - a.views),
  };
}

/**
 * Pure: compact a large token count for a KPI tile.
 *
 * Tiles are 100px wide; "33,212,254" does not fit and "3.3e7" is unreadable, so
 * the metric gets an SI suffix and the exact figure goes in the table view.
 */
export function compactNumber(value) {
  const n = num(value);
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${(n / 1e9).toFixed(abs >= 1e10 ? 0 : 1)}B`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(abs >= 1e7 ? 0 : 1)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(abs >= 1e4 ? 0 : 1)}k`;
  return String(n);
}

/** Pure: cost formatted without ever implying more precision than exists. */
export function formatCost(value) {
  const n = num(value);
  if (n === 0) return '$0.00';
  if (n < 0.01) return '<$0.01';
  return `$${n.toFixed(2)}`;
}
