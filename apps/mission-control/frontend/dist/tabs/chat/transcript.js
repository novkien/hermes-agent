// Transcript rendering: messages, reasoning disclosures and tool rows.
//
// Everything here takes plain data and returns DOM. It is the one place that
// knows how a Hermes turn looks on screen, so the live stream and the stored
// history render identically — which is what stopped the thread from visibly
// rearranging itself the moment a turn finished.

import { el, clear } from '../../ui.js';
import { icon } from '../../icons.js';
import { renderMarkdown, truncate, copyText } from '../../markdown-render.js';

export function formatCount(value) {
  const num = Number(value) || 0;
  if (num >= 1e6) return `${(num / 1e6).toFixed(1)}M`;
  if (num >= 1e3) return `${(num / 1e3).toFixed(1)}k`;
  return String(num);
}

export function formatDuration(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value) || value < 0) return '';
  if (value < 1000) return `${Math.round(value)}ms`;
  if (value < 60000) return `${(value / 1000).toFixed(1)}s`;
  const minutes = Math.floor(value / 60000);
  const seconds = Math.round((value % 60000) / 1000);
  return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
}

/* --------------------------------------------------------------- messages -- */

/**
 * One message bubble. Returns the body node so a streaming turn can keep
 * writing into it.
 *
 * `onRetry` turns a failed turn from a dead line of text into something the
 * operator can act on — the previous surface printed "send failed: …" and left
 * them to retype the prompt.
 */
export function appendMessage(list, role, text, message = {}) {
  const wrap = el('div', { class: `msg msg-${role}`, 'data-role': role });
  // A live turn builds its transcript out of order sometimes — a thought that
  // lands after prose has started belongs above that prose — so the caller may
  // name the node to insert in front of.
  const before = message.before || null;
  if (message.messageId) wrap.dataset.messageId = message.messageId;

  const reasoning = message.reasoning || message.reasoning_content;
  if (reasoning) wrap.append(reasoningDisclosure(reasoning, { done: true }));

  // Tool calls are not summarised here: `renderHistory` gives each round-trip
  // its own row so the call and its result stay together.
  const body = el('div', { class: 'msg-body' });
  renderMarkdown(body, text);
  wrap.append(body);

  if (Array.isArray(message.attachments) && message.attachments.length) {
    const nodes = message.attachments
      .map((item) => {
        if (!item) return null;
        if ((item.kind === 'image' || (item.kind == null && item.url)) && item.url) {
          return el('img', { class: 'msg-attachment', src: item.url, alt: item.name || '' });
        }
        const label = item.name || 'attachment';
        const mime = item.mime ? ` · ${item.mime}` : '';
        return el('span', { class: 'msg-attachment-file', title: `${label}${mime}` }, [
          icon('doc', { size: 14, className: 'msg-attachment-icon' }),
          el('span', { class: 'msg-attachment-file-name', text: label }),
          mime ? el('span', { class: 'msg-attachment-file-meta', text: mime }) : null,
        ]);
      })
      .filter(Boolean);
    if (nodes.length) wrap.append(el('div', { class: 'msg-attachments' }, nodes));
  }

  const actions = el('div', { class: 'msg-actions' });
  const final = isTurnAnswer(role, text, message);
  if (final) actions.append(copyButton(body));
  if (typeof message.onRetry === 'function') {
    actions.append(msgAction('retry', 'Retry this turn', () => message.onRetry()));
  }
  if (typeof message.onRegenerate === 'function') {
    actions.append(msgAction('refresh', 'Regenerate with another model', () => message.onRegenerate()));
  }
  if (typeof message.onFork === 'function') {
    actions.append(msgAction('branch', 'Fork the conversation here', () => message.onFork()));
  }
  // Copy and permalink are one group: both point at "this answer", so they
  // appear and disappear together.
  if (final && message.messageId && typeof message.onPermalink === 'function') {
    actions.append(msgAction('link', 'Copy a link to this message',
      (button) => message.onPermalink(message.messageId, button)));
  }
  if (actions.childNodes.length) wrap.append(actions);

  insertNode(list, wrap, before);
  return body;
}

/**
 * Copy and permalink belong to the answer, not to the work that produced it. A
 * turn can span several assistant rows — reasoning, an explanation, a batch of
 * tool calls — and only the row that ends the turn is the thing a reader wants
 * on their clipboard or behind a link. So: it must carry text, and it must not
 * be handing off to a tool.
 */
function isTurnAnswer(role, text, message) {
  if (role === 'error') return false;
  if (!String(text || '').trim()) return false;
  if (role === 'assistant' && parseToolCalls(message.tool_calls).length) return false;
  return true;
}

function copyButton(body) {
  return msgAction('copy', 'Copy message', (button) => copyText(body.textContent, button));
}

/**
 * The streaming path opens its bubble empty, so it cannot know at creation
 * whether the turn will end in an answer or in another tool call. `turn-view`
 * settles that at `finish()` through here.
 */
export function setMessageCopyable(bodyNode, enabled) {
  const wrap = bodyNode && bodyNode.closest ? bodyNode.closest('.msg') : null;
  if (!wrap) return;
  const existing = wrap.querySelector('.msg-action-copy');
  if (!enabled) {
    if (existing) existing.remove();
    return;
  }
  if (existing) return;
  let actions = wrap.querySelector('.msg-actions');
  if (!actions) {
    actions = el('div', { class: 'msg-actions' });
    wrap.append(actions);
  }
  actions.prepend(copyButton(bodyNode));
}

/**
 * Put the end of the transcript on screen. The one place that knows how, so
 * the live painter and the send path cannot drift into two answers.
 */
export function scrollToLatest(list) {
  if (list) list.scrollTop = list.scrollHeight;
}

/** Append, or splice in ahead of a node already on screen. */
export function insertNode(list, node, before = null) {
  if (before && before.parentNode === list) list.insertBefore(node, before);
  else list.append(node);
  return node;
}

function msgAction(iconName, label, onClick) {
  const button = el('button', {
    class: `msg-action msg-action-${iconName}`,
    type: 'button',
    title: label,
    'aria-label': label,
  }, [icon(iconName, { size: 12 })]);
  button.addEventListener('click', () => onClick(button));
  return button;
}

/**
 * Collapsible reasoning, following Hermes Desktop's ThinkingDisclosure: open
 * while it streams, closed once the turn lands, and the operator's first
 * explicit toggle wins from then on.
 */
export function reasoningDisclosure(text, { done = false, durationMs = null } = {}) {
  const details = el('details', { class: 'msg-reasoning' });
  const summary = el('summary');
  const label = el('span', { class: 'msg-reasoning-label' });
  summary.append(icon('spark', { size: 11 }), label);
  details.append(summary);

  const body = el('div', { class: 'msg-reasoning-body' });
  details.append(body);

  let userToggled = false;
  summary.addEventListener('click', () => { userToggled = true; });

  function paint(next, opts = {}) {
    // The reasoning body is a scroller of its own (capped height while live),
    // so following the transcript is not enough: the newest thought has to be
    // brought into view INSIDE this box too, or the card freezes on the first
    // few lines while the text goes on growing out of sight below.
    const wasAtEnd = body.scrollHeight - body.scrollTop - body.clientHeight <= 24;
    body.textContent = truncate(String(next || ''), 8000);
    const finished = opts.done ?? done;
    // Only while it is still being written, and only if the reader had not
    // scrolled back inside it to re-read an earlier line.
    if (!finished && wasAtEnd) body.scrollTop = body.scrollHeight;
    const ms = opts.durationMs ?? durationMs;
    label.textContent = finished
      ? (ms === null || ms === undefined
        ? 'Thought'
        : (ms < 1000 ? 'Thought briefly' : `Thought for ${formatDuration(ms)}`))
      : 'Thinking…';
    details.classList.toggle('is-streaming', !finished);
    // Auto-open while live, auto-close when finished — unless the reader has
    // said what they want, in which case leave it alone.
    if (!userToggled) details.open = !finished;
  }

  paint(text);
  details.update = paint;
  return details;
}

/* ------------------------------------------------------------- tool calls -- */

// The gateway stores a call as `{id, function: {name, arguments}}` with the
// arguments themselves JSON-encoded — a string inside an object inside a
// string. Unwrap both layers so the row can show real fields.
export function parseToolCalls(value) {
  let parsed = value;
  if (typeof value === 'string') {
    try {
      parsed = JSON.parse(value);
    } catch (_err) {
      return [];
    }
  }
  if (!Array.isArray(parsed)) return [];
  return parsed.map((call) => {
    const fn = call?.function || {};
    let args = fn.arguments ?? call?.args ?? null;
    if (typeof args === 'string') {
      try {
        args = JSON.parse(args);
      } catch (_err) { /* not JSON — show the raw string */ }
    }
    return { id: call?.id || call?.call_id || '', name: fn.name || call?.name || '', args };
  }).filter((call) => call.name);
}

// Results arrive wrapped in a provenance envelope that tells the model to treat
// the payload as data. That warning is addressed to the model, not the operator,
// so the transcript shows only what the tool actually returned.
const TOOL_RESULT_ENVELOPE = /^\s*<untrusted_tool_result[^>]*>([\s\S]*?)<\/untrusted_tool_result>\s*$/;
const TOOL_RESULT_PREAMBLE = /^The following content was retrieved from an external source\.[^\n]*\n+/;

export function toolResultText(content) {
  let text = String(content ?? '');
  const wrapped = text.match(TOOL_RESULT_ENVELOPE);
  // Trim before stripping the preamble: unwrapping leaves the newline that
  // followed the opening tag, which would defeat the anchored pattern.
  if (wrapped) text = wrapped[1].trim();
  return text.replace(TOOL_RESULT_PREAMBLE, '').trim();
}

export function toolArgsPreview(args, limit = 96) {
  if (args === null || args === undefined || args === '') return '';
  if (typeof args !== 'object') {
    return truncate(String(args).replace(/\s+/g, ' ').trim(), limit);
  }
  const parts = Object.entries(args).map(([key, value]) => {
    const rendered = typeof value === 'string' ? value : JSON.stringify(value);
    return `${key}=${truncate(String(rendered).replace(/\s+/g, ' ').trim(), 40)}`;
  });
  return truncate(parts.join('  '), limit);
}

/**
 * A tool round-trip as one foldable row.
 *
 * Returns the row with an `update(patch)` so a live turn can fill in the
 * duration and result the moment `tool.completed` lands, instead of waiting for
 * the whole thread to be re-read.
 */
export function appendToolCall(list, {
  name, args, preview = null, result = null, status = 'done', durationMs = null,
  risk = null, before = null,
}) {
  const row = el('details', {
    class: 'msg msg-tool', 'data-role': 'tool', 'data-status': status,
  });

  const nameNode = el('span', { class: 'msg-tool-name', text: String(name || 'tool') });
  const argsNode = el('span', { class: 'msg-tool-args mono', text: toolArgsPreview(args) || String(preview || '') });
  const timerNode = el('span', { class: 'msg-tool-timer' });
  const riskNode = el('span', { class: 'msg-tool-risk' });
  row.append(el('summary', { class: 'msg-tool-head' }, [
    icon('tools', { size: 11 }),
    nameNode, argsNode, riskNode, timerNode,
  ]));

  const body = el('div', { class: 'msg-tool-body' });
  row.append(body);

  // A tool that is still running is the one thing worth reading, so it opens
  // itself and folds away once it lands.
  let userToggled = false;
  row.querySelector('summary').addEventListener('click', () => { userToggled = true; });

  function paint(state) {
    row.setAttribute('data-status', state.status);
    if (state.name) nameNode.textContent = String(state.name);
    const argPreview = toolArgsPreview(state.args) || String(state.preview || '');
    if (argPreview) argsNode.textContent = argPreview;
    timerNode.textContent = state.durationMs === null || state.durationMs === undefined
      ? (state.status === 'running' ? '…' : '')
      : formatDuration(state.durationMs);
    riskNode.textContent = state.risk
      ? `risk: ${state.risk.risk || state.risk}` : '';
    riskNode.hidden = !state.risk;

    clear(body);
    const detail = state.args && (typeof state.args !== 'object' || Object.keys(state.args).length)
      ? (typeof state.args === 'string' ? state.args : JSON.stringify(state.args, null, 2))
      : '';
    if (detail) {
      body.append(sectionHead('arguments', detail));
      body.append(el('div', { class: 'msg-tool-pre mono', text: truncate(detail, 4000) }));
    }
    if (state.result !== null && state.result !== undefined && state.result !== '') {
      const text = toolResultText(state.result);
      body.append(sectionHead(state.status === 'failed' ? 'error' : 'result', text));
      body.append(renderToolResult(el('div', { class: 'msg-tool-result-body' }), text));
    } else if (state.status === 'running' || state.status === 'pending') {
      body.append(el('div', { class: 'msg-tool-section msg-tool-result', text: 'result' }));
      body.append(el('div', { class: 'msg-tool-waiting', text: 'waiting for the tool to return…' }));
    }
    if (!userToggled) row.open = state.status === 'running';
  }

  const state = { name, args, preview, result, status, durationMs, risk };
  paint(state);

  row.update = (patch) => {
    Object.assign(state, patch);
    paint(state);
  };

  insertNode(list, row, before);
  return row;
}

function sectionHead(label, copyable) {
  const head = el('div', { class: `msg-tool-section${label === 'result' || label === 'error' ? ' msg-tool-result' : ''}` });
  head.append(el('span', { text: label }));
  const button = el('button', {
    class: 'msg-tool-copy', type: 'button', title: `Copy ${label}`, 'aria-label': `Copy ${label}`,
  }, [icon('copy', { size: 11 })]);
  button.addEventListener('click', (event) => {
    event.preventDefault();
    copyText(copyable, button);
  });
  head.append(button);
  return head;
}

// Tool output splits two ways: a JSON payload reads best indented in the mono
// block, while prose output (skill docs, reports) is Markdown. Running the
// Markdown pass over JSON would italicise whatever sits between two asterisks.
export function renderToolResult(target, result) {
  const trimmed = String(result).trim();
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try {
      target.textContent = truncate(JSON.stringify(JSON.parse(trimmed), null, 2), 8000);
      target.classList.add('mono');
      return target;
    } catch (_err) { /* truncated or not JSON after all — fall through */ }
  }
  return renderMarkdown(target, truncate(result, 8000));
}

export function setToolStatus(row, status) {
  if (row.update) row.update({ status });
  else row.setAttribute('data-status', status);
}

/* --------------------------------------------------------------- history -- */

/**
 * The gateway stores one tool round-trip as two rows: the assistant message
 * carrying `tool_calls`, then a `role: "tool"` row holding the result keyed by
 * `tool_call_id`. Pairing them turns a wall of JSON into a single foldable line.
 */
export function renderHistory(list, messages, options = {}) {
  const results = new Map();
  for (const message of messages) {
    if (message.role === 'tool' && message.tool_call_id) {
      results.set(message.tool_call_id, message);
    }
  }

  const paired = new Set();
  for (const message of messages) {
    const role = message.role || 'unknown';
    if (role === 'tool') {
      // Orphan result (its call was compacted away) — still worth showing.
      if (message.tool_call_id && paired.has(message.tool_call_id)) continue;
      appendToolCall(list, {
        name: message.tool_name || 'tool',
        args: null,
        result: toolResultText(message.content),
      });
      continue;
    }

    const calls = parseToolCalls(message.tool_calls);
    const text = message.content || message.text || '';
    if (text || message.reasoning || message.reasoning_content || !calls.length) {
      appendMessage(list, role, text, {
        ...message,
        messageId: message.id || message.message_id || null,
        onFork: role === 'user' && options.onFork
          ? () => options.onFork(message) : undefined,
        onRegenerate: role === 'user' && options.onRegenerate
          ? () => options.onRegenerate(message) : undefined,
        onPermalink: options.onPermalink,
      });
    }
    for (const call of calls) {
      if (call.id) paired.add(call.id);
      const resultMessage = call.id ? results.get(call.id) : null;
      appendToolCall(list, {
        name: call.name,
        args: call.args,
        result: resultMessage ? toolResultText(resultMessage.content) : null,
        status: resultMessage ? 'done' : 'pending',
      });
    }
  }
}
