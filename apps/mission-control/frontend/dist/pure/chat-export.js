// Rendering a session to Markdown.
//
// The first version of this scraped `.msg` nodes out of the live transcript,
// which meant the export could only ever contain what the thread view happens
// to paint: no system prompt, no session metadata, no context accounting, and
// none of the roles the transcript filters out. What actually defines a Hermes
// run is mostly *not* the visible bubbles — a 134KB system prompt and 18.8k
// tokens of tool schemas do more to determine the reply than the user's one
// line does. An export that omits them is not a record of the session.
//
// So this builds from data, not DOM: the session row (which carries
// `system_prompt`), the message page, and the context breakdown with
// `details=1`. It is pure, so the whole document is testable in Node.

import { normalizeContextWindow, formatTokens, formatPercent } from './context-window.js';

/** Roles the transcript view collapses or hides, but a record must keep. */
const ROLE_HEADINGS = Object.freeze({
  user: 'user',
  assistant: 'assistant',
  system: 'system',
  session_meta: 'system · session meta',
  tool: 'tool',
});

/**
 * @param {object} input
 * @param {object} input.session   dashboard session row (carries `system_prompt`)
 * @param {Array}  input.messages  chronological message rows
 * @param {object} [input.context] raw `GET /api/sessions/{id}/context?details=1`
 * @param {number} [input.compressionThreshold]
 * @returns {string} Markdown
 */
export function buildTranscriptMarkdown({
  session = {}, messages = [], context = null, compressionThreshold,
} = {}) {
  const out = [];
  const id = session.id || session.session_id || 'session';
  out.push(`# ${session.title || session.display_name || id}`, '');
  out.push(...metadataSection(session, id));
  if (context) out.push(...contextSection(context, compressionThreshold));
  out.push(...systemPromptSection(session));
  out.push(...transcriptSection(messages));
  return `${out.join('\n').replace(/\n{3,}/g, '\n\n')}\n`;
}

function metadataSection(session, id) {
  const pairs = [
    ['session', id],
    ['profile', session.profile || session.profile_name],
    ['parent session', session.parent_session_id],
    ['platform', session.source || session.platform],
    ['model', session.model],
    ['provider', session.billing_provider],
    ['started', isoTime(session.started_at)],
    ['ended', isoTime(session.ended_at)],
    ['end reason', session.end_reason],
    ['messages', session.message_count],
    ['tool calls', session.tool_call_count],
    ['api calls', session.api_call_count],
    ['tokens in / out', tokenPair(session)],
    ['cwd', session.cwd],
    ['git branch', session.git_branch],
  ];
  const rows = pairs
    .filter(([, value]) => value !== undefined && value !== null && value !== '')
    .map(([label, value]) => `- **${label}**: ${value}`);
  return rows.length ? ['## Session', '', ...rows, ''] : [];
}

function tokenPair(session) {
  const input = Number(session.input_tokens) || 0;
  const output = Number(session.output_tokens) || 0;
  if (!input && !output) return '';
  const cached = Number(session.cache_read_tokens) || 0;
  return `${input} / ${output}${cached ? ` (${cached} cached)` : ''}`;
}

/**
 * The same figures the composer's context gauge shows — every category, its
 * token count and its share — plus the per-toolset and per-skill tables that
 * the panel is too small to display. This is the section that answers "what was
 * in the window", which no amount of transcript can reconstruct.
 */
function contextSection(context, compressionThreshold) {
  const view = normalizeContextWindow(context, { compressionThreshold });
  if (!view || (!view.total && !view.used)) return [];

  const out = ['## Context window', ''];
  out.push(view.total
    ? `${formatTokens(view.used)} / ${formatTokens(view.total)} (${formatPercent(view.percent)})`
    : `${formatTokens(view.used)} tokens`);
  out.push('');
  out.push('| Category | Tokens | Share |', '| --- | ---: | ---: |');
  for (const segment of view.segments) {
    out.push(`| ${segment.label} | ${formatTokens(segment.tokens)} | ${formatPercent(segment.percent)} |`);
  }
  out.push('');
  out.push(`${view.measured ? 'Measured prompt size' : 'Estimated (chars/4)'}${view.model ? ` · ${view.model}` : ''}`, '');

  const details = (context && (context.details || (context.data && context.data.details))) || null;
  out.push(...detailTable('Toolsets in context', details && details.toolsets, [
    ['toolset', (row) => row.toolset || row.name],
    ['tools', (row) => row.tool_count],
    ['schema tokens', (row) => row.schema_tokens],
  ]));
  out.push(...detailTable('Skills in context', details && details.skills, [
    ['skill', (row) => row.skill || row.name],
    ['tokens', (row) => row.tokens ?? row.schema_tokens],
  ]));
  return out;
}

function detailTable(title, rows, columns) {
  if (!Array.isArray(rows) || !rows.length) return [];
  const out = [`### ${title}`, ''];
  out.push(`| ${columns.map((c) => c[0]).join(' | ')} |`);
  out.push(`| ${columns.map((_, index) => (index ? '---:' : '---')).join(' | ')} |`);
  for (const row of rows) {
    out.push(`| ${columns.map((c) => cell(c[1](row))).join(' | ')} |`);
  }
  out.push('');
  return out;
}

function cell(value) {
  if (value === undefined || value === null) return '';
  return String(value).replace(/\|/g, '\\|');
}

// Fenced rather than quoted: a Hermes system prompt is largely Markdown itself,
// and its headings would otherwise merge into the document's own outline.
function systemPromptSection(session) {
  const prompt = session.system_prompt;
  if (!prompt) return [];
  const hash = session.system_prompt_hash ? ` · sha ${String(session.system_prompt_hash).slice(0, 12)}` : '';
  return [
    '## System prompt', '',
    `${prompt.length} characters${hash}`, '',
    ...fence(prompt, 'text'),
    '',
  ];
}

function transcriptSection(messages) {
  const out = ['## Transcript', ''];
  if (!messages.length) {
    out.push('_No messages._', '');
    return out;
  }
  for (const message of messages) {
    const role = String(message.role || 'unknown');
    const heading = ROLE_HEADINGS[role] || role;
    const stamp = isoTime(message.timestamp);
    out.push(`### ${heading}${stamp ? ` · ${stamp}` : ''}`, '');

    // Reasoning first: it precedes the answer it produced, and dropping it
    // would silently rewrite what the model actually did.
    const reasoning = message.reasoning_content || message.reasoning;
    if (typeof reasoning === 'string' && reasoning.trim()) {
      out.push('<details><summary>reasoning</summary>', '', ...fence(reasoning, 'text'), '</details>', '');
    }

    if (role === 'tool') {
      if (message.tool_name) out.push(`**tool**: \`${message.tool_name}\``, '');
      out.push(...fence(textOf(message.content), 'text'), '');
      continue;
    }

    const body = textOf(message.content);
    if (body.trim()) out.push(body.trim(), '');

    for (const call of toolCallsOf(message)) {
      out.push(`**calls** \`${call.name}\``, '', ...fence(call.args, 'json'), '');
    }
  }
  return out;
}

function toolCallsOf(message) {
  const raw = message.tool_calls;
  const list = typeof raw === 'string' ? safeParse(raw) : raw;
  if (!Array.isArray(list)) return [];
  return list.map((call) => {
    const fn = call.function || call;
    const args = fn.arguments ?? fn.args ?? '';
    return {
      name: fn.name || call.name || 'tool',
      args: typeof args === 'string' ? args : JSON.stringify(args, null, 2),
    };
  });
}

function safeParse(value) {
  try { return JSON.parse(value); } catch { return null; }
}

// Message content is a string on most rows and a provider content-part array on
// multimodal ones; both have to survive the export.
function textOf(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.map((part) => {
      if (typeof part === 'string') return part;
      if (part && typeof part.text === 'string') return part.text;
      if (part && part.type) return `[${part.type}]`;
      return '';
    }).filter(Boolean).join('\n');
  }
  if (content === undefined || content === null) return '';
  return JSON.stringify(content, null, 2);
}

// A fence must be longer than the longest run of backticks it contains, or a
// system prompt full of code samples tears the document in half.
function fence(text, language) {
  const body = String(text ?? '');
  const longest = (body.match(/`+/g) || []).reduce((max, run) => Math.max(max, run.length), 0);
  const ticks = '`'.repeat(Math.max(3, longest + 1));
  return [`${ticks}${language}`, body, ticks];
}

function isoTime(value) {
  const seconds = Number(value);
  if (!seconds) return '';
  // state.db stores epoch seconds; tolerate milliseconds from other sources.
  const ms = seconds > 1e12 ? seconds : seconds * 1000;
  const date = new Date(ms);
  return Number.isNaN(date.getTime()) ? '' : date.toISOString().replace('.000', '');
}
