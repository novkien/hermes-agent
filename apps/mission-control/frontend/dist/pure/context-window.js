// Context-window breakdown shaping.
//
// Source: the dashboard's `GET /api/sessions/{id}/context`, which returns
// Hermes' own `agent.context_breakdown` payload:
//
//   {categories: [{id, label, tokens, color}], context_max, context_used,
//    context_percent, estimated_total, model, details?}
//
// Two things the upstream payload deliberately does not carry, added here:
//
//   * the autocompact reserve — Hermes compresses at `compression.threshold`
//     (0.9 by default) of the window, so the top slice of the window is not
//     usable conversation space and must not read as free;
//   * free space — the remainder, so the bar always sums to the full window.
//
// Upstream's `color` is a `var(--context-usage-*)` reference that only the
// Hermes Desktop stylesheet defines, so this SPA maps its own ramp by id.

const CATEGORY_ORDER = [
  'conversation',
  'tool_definitions',
  'system_prompt',
  'rules',
  'memory',
  'mcp',
  'subagent_definitions',
  'skills',
];

// Labels mirror what the CLI's /context prints, so a figure quoted from the
// terminal and one read off this panel name the same thing.
const CATEGORY_LABELS = Object.freeze({
  conversation: 'Messages',
  tool_definitions: 'System tools',
  system_prompt: 'System prompt',
  rules: 'Rules',
  memory: 'Memory files',
  mcp: 'MCP tools',
  subagent_definitions: 'Subagents',
  skills: 'Skills',
  autocompact_buffer: 'Autocompact buffer',
  free_space: 'Free space',
});

// Categorical ramp, not status colors — these slices carry no health meaning.
const CATEGORY_COLORS = Object.freeze({
  conversation: 'var(--series-1)',
  tool_definitions: 'var(--series-2)',
  system_prompt: 'var(--series-3)',
  rules: 'var(--series-4)',
  memory: 'var(--series-5)',
  mcp: 'var(--series-6)',
  subagent_definitions: 'var(--series-7)',
  skills: 'var(--series-8)',
});

export const AUTOCOMPACT_KEY = 'autocompact_buffer';
export const FREE_SPACE_KEY = 'free_space';

// Hermes' `compression.threshold` default. The caller passes the real value
// read from `GET /api/config`; this is only the fallback when that read fails.
export const DEFAULT_COMPRESSION_THRESHOLD = 0.9;

function toInt(value) {
  const num = Number(value);
  return Number.isFinite(num) && num > 0 ? Math.round(num) : 0;
}

function orderIndex(id) {
  const index = CATEGORY_ORDER.indexOf(id);
  return index === -1 ? CATEGORY_ORDER.length : index;
}

/**
 * Shape one context payload into bar segments.
 *
 * @param {object} payload  the upstream breakdown (already unwrapped from the
 *   BFF envelope by api.js).
 * @param {object} [options]
 * @param {number} [options.compressionThreshold]  fraction of the window at
 *   which Hermes compacts; the remainder becomes the autocompact reserve.
 * @returns {{total:number, used:number, percent:number, measured:boolean,
 *   model:string, segments:Array<{key,label,tokens,percent,color}>}}
 */
export function normalizeContextWindow(payload, options = {}) {
  const data = payload && typeof payload === 'object' ? payload : {};
  const total = toInt(data.context_max);
  const estimated = toInt(data.estimated_total);

  const threshold = Number(options.compressionThreshold);
  const ratio = Number.isFinite(threshold) && threshold > 0 && threshold <= 1
    ? threshold
    : DEFAULT_COMPRESSION_THRESHOLD;

  const raw = Array.isArray(data.categories) ? data.categories : [];
  const segments = raw
    .map((item) => ({
      key: String(item?.id || ''),
      label: CATEGORY_LABELS[item?.id] || String(item?.label || item?.id || ''),
      tokens: toInt(item?.tokens),
      color: CATEGORY_COLORS[item?.id] || 'var(--text-faint)',
    }))
    .filter((item) => item.key && item.tokens > 0)
    .sort((a, b) => orderIndex(a.key) - orderIndex(b.key));

  // Summing the categories keeps the gauge honest when an older payload omits
  // the roll-ups: better a bar with no denominator than a bar reading zero.
  const categoriesTotal = segments.reduce((sum, item) => sum + item.tokens, 0);
  const used = toInt(data.context_used) || estimated || categoriesTotal;

  if (total > 0) {
    const reserve = Math.max(0, Math.round(total * (1 - ratio)));
    if (reserve > 0) {
      segments.push({
        key: AUTOCOMPACT_KEY,
        label: CATEGORY_LABELS[AUTOCOMPACT_KEY],
        tokens: reserve,
        color: 'var(--context-reserve)',
      });
    }
    // Everything the categories and the reserve do not account for. Clamped so
    // a window smaller than its own fixed prompt cannot render a negative bar.
    const accounted = segments.reduce((sum, item) => sum + item.tokens, 0);
    segments.push({
      key: FREE_SPACE_KEY,
      label: CATEGORY_LABELS[FREE_SPACE_KEY],
      tokens: Math.max(0, total - accounted),
      color: 'var(--context-free)',
    });
  }

  const denominator = total || used;
  for (const segment of segments) {
    segment.percent = denominator > 0 ? (segment.tokens / denominator) * 100 : 0;
  }

  // Used exceeding the window is not "completely full" — it means the
  // denominator is wrong. Hermes resolves the window from config and falls back
  // to a 256K default for any model no registry knows (every 9router combo
  // alias is one), so a 1M model can report a 256K window and then overrun it.
  // Painting that as a confident 100% with no free space hides the only signal
  // that the number is not to be trusted.
  const overflow = total > 0 && used > total;

  return {
    total,
    used,
    overflow,
    percent: total > 0 ? Math.max(0, Math.min(100, (used / total) * 100)) : 0,
    // Hermes reports a MEASURED prompt size once a turn has run
    // (`compressor.last_prompt_tokens`) and falls back to its chars/4 estimate
    // before that. The two being equal means nothing has been measured yet.
    measured: toInt(data.context_used) > 0 && toInt(data.context_used) !== estimated,
    model: String(data.model || ''),
    segments,
  };
}

/** Compact token count for dense UI: 135700 → "135.7k". */
export function formatTokens(value) {
  const num = Number(value) || 0;
  if (num >= 1e6) return `${(num / 1e6).toFixed(1)}M`;
  if (num >= 1e3) return `${(num / 1e3).toFixed(1)}k`;
  return String(Math.round(num));
}

/** Percent for dense UI: one decimal below 10, none above. */
export function formatPercent(percent) {
  const num = Number(percent);
  if (!Number.isFinite(num)) return '0%';
  return num >= 10 ? `${Math.round(num)}%` : `${num.toFixed(1)}%`;
}

/**
 * Read Hermes' compaction threshold out of a `GET /api/config` payload.
 * Returns the default when the key is absent or nonsensical.
 */
export function compressionThresholdFrom(config) {
  const value = Number(config?.compression?.threshold);
  return Number.isFinite(value) && value > 0 && value <= 1
    ? value
    : DEFAULT_COMPRESSION_THRESHOLD;
}

// Neutral start and alarm end of the prompt-size heat ramp, as raw [r,g,b] —
// `--text-dim` and `--danger` respectively. Read once here rather than at
// paint time: this module has no DOM, so it cannot resolve a CSS custom
// property, and the two colors are already meant to travel together.
const HEAT_FROM = [135, 148, 171];
const HEAT_TO = [248, 113, 113];

/**
 * Color for a prompt-size figure as it approaches the model's context window.
 * `null` when the window is unknown, so the caller falls back to no color
 * rather than guessing a ratio against a denominator that might be wrong.
 *
 * Eased with the square of the ratio rather than a straight line: a turn
 * using a small slice of the window should read as perfectly ordinary, and
 * the color is meant to be a late warning as the limit is actually
 * approached, not a running commentary on every prompt size.
 */
export function contextHeatColor(tokens, total) {
  const used = Number(tokens);
  const max = Number(total);
  if (!Number.isFinite(used) || used <= 0 || !Number.isFinite(max) || max <= 0) return null;
  const ratio = Math.max(0, Math.min(1, used / max));
  const eased = ratio * ratio;
  const [r, g, b] = HEAT_FROM.map((from, i) => Math.round(from + (HEAT_TO[i] - from) * eased));
  return `rgb(${r}, ${g}, ${b})`;
}

/** Duration as seconds only — no minute split, for a figure meant to stay a single number. */
export function formatSecondsOnly(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value) || value < 0) return '';
  return `${(value / 1000).toFixed(1)}s`;
}
