// Pure helpers for the model / reasoning-effort controls and for building the
// chat/stream request body. Kept out of the tab so the shapes the gateway
// actually accepts can be asserted in Node.

import { recordFrom } from './data-shape.js';

// Reasoning levels the gateway's HTTP chat surface actually honours
// (api_server._REASONING_EFFORTS). `max`/`ultra` exist in the CLI constant but
// are silently dropped on this path, so the picker does not offer them.
export const REASONING_EFFORTS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'];
export const DEFAULT_REASONING_EFFORT = 'medium';

export const EFFORT_LABELS = {
  '': 'Auto',
  none: 'Off',
  minimal: 'Min',
  low: 'Low',
  medium: 'Med',
  high: 'High',
  xhigh: 'XHigh',
};

// Mirrors Hermes Desktop's modelDisplayParts: drop the provider prefix and the
// trailing date pin, then prettify so the pill reads like a name.
export function displayModelName(model) {
  const raw = String(model || '').trim();
  if (!raw) return 'No model';
  let base = raw.slice(raw.lastIndexOf('/') + 1).replace(/-\d{8}$/, '');
  if (/^claude-/i.test(base)) base = base.replace(/^claude-/i, '').replace(/-/g, ' ');
  else if (/^gpt-/i.test(base)) return base.replace(/^gpt-/i, 'GPT-');
  else if (/^gemini-/i.test(base)) return base.replace(/^gemini-/i, 'Gemini ').replace(/-/g, ' ');
  else base = base.replace(/-/g, ' ');
  return base.replace(/\b\w/g, (ch) => ch.toUpperCase()).trim() || raw;
}

// Just the model name. Hermes Desktop appends the effort here because its pill
// is the only place that state appears; this composer has a dedicated effort
// control right beside it, so repeating the level would be redundant.
export function modelPillLabel(model) {
  return displayModelName(model);
}

export function unwrapModelOptions(value) {
  const record = recordFrom(value);
  return record && Array.isArray(record.providers) ? record : null;
}

export function providerRows(modelOptions) {
  const list = modelOptions && Array.isArray(modelOptions.providers) ? modelOptions.providers : [];
  return list.filter((item) => item && typeof item === 'object');
}

// `capabilities` is keyed by model id and gates the reasoning control, exactly
// as the desktop picker does. Absent capability data means "assume supported".
export function modelSupportsReasoning(modelOptions, provider, model) {
  if (!model) return true;
  const row = providerRows(modelOptions).find((item) => item.slug === provider);
  const caps = row && row.capabilities ? row.capabilities[model] : null;
  return caps ? caps.reasoning !== false : true;
}

/**
 * Mirrors the gateway's session chat contract: `message` remains plain text
 * and attachments travel in their own array. Hermes stages those attachments
 * and decides whether they become native image parts, PDF pages, or @file refs.
 *
 * `system_message` and `instructions` are forwarded by the BFF's chat-stream
 * key allowlist and accepted by the gateway, which is what lets the composer
 * carry per-session instructions without a new upstream route.
 */
export function chatStreamBody({
  sessionId, sessionProfile, text, attachments = [], prefs = {}, instructions = '',
  systemMessage = '',
}) {
  const body = { session_id: sessionId, profile: sessionProfile };

  body.message = text || (attachments.length ? 'Please review the attached content.' : '');
  if (attachments.length) {
    body.attachments = attachments.filter(Boolean).map((item) => ({
      name: item.name,
      mime_type: item.mime_type || item.mime,
      size: item.size,
      kind: item.kind,
      data: item.data,
    }));
  }

  // The model picker knows exactly which provider each model belongs to, and
  // that provider is what makes the model resolvable. It always travels.
  //
  // Withholding it to dodge a broken upstream check was a mistake worth
  // recording: the gateway then resolved a 9router alias against the wrong
  // catalogue and answered "HTTP 400: smart is not a valid model ID". Trading
  // correct routing data for a guard's approval breaks the request itself.
  if (prefs.model) body.model = prefs.model;
  if (prefs.provider) body.provider = prefs.provider;
  // An empty effort means "inherit the profile default" — send nothing so the
  // gateway keeps its own configured level.
  if (prefs.effort === 'none') body.model_options = { reasoning: { enabled: false } };
  else if (prefs.effort) body.model_options = { reasoning: { enabled: true, effort: prefs.effort } };

  // A deliberate pick has to be an execution contract, not a suggestion.
  //
  // The gateway's documented precedence is: session `/model` override →
  // session-persisted model → per-request selection → global. Every session
  // row carries a model from birth, so the composer's `model` otherwise sits
  // below something that always exists, and the gateway logs "request
  // selection skipped: session-persisted model wins" and answers from the old
  // model. `require_model_lock` is the documented way past that: it beats the
  // session model, disables the global fallback chain, and fails closed. A 409
  // the operator can see beats silently answering from a model they did not
  // choose.
  //
  // Every explicit pick locks — full stop. This used to skip the lock when
  // the pick matched a `sessionModel` snapshot passed in from the caller, on
  // the theory that picking what the session already runs adds a way to fail
  // for no benefit. That reasoning broke in practice: the caller's snapshot
  // was the session object captured once when the thread was opened, and
  // never saw the patches later turns apply to it — so on any session whose
  // model had since changed, a real, deliberate pick that happened to share
  // its name with that stale value (a fresh session's inherited default,
  // still sitting in the closure) silently sent no lock at all. A reader who
  // explicitly clicked a model watched it not apply, with no error to explain
  // why. An explicit pick is by definition a value the picker's own catalogue
  // offered, so locking it can only ever be a harmless no-op or a correction
  // — never a spurious failure — which is exactly why comparing it against
  // anything else was never worth the risk.
  if (prefs.explicit && prefs.model) {
    body.require_model_lock = true;
  }

  if (instructions && instructions.trim()) body.instructions = instructions.trim();
  if (systemMessage && systemMessage.trim()) body.system_message = systemMessage.trim();

  return body;
}

/**
 * Rough token estimate for text the operator is still typing. Hermes' own
 * compaction maths uses chars/4, so the composer uses the same heuristic rather
 * than inventing a second one — and the UI labels it as an estimate.
 */
export function estimateTokens(text) {
  const raw = String(text || '');
  if (!raw) return 0;
  return Math.ceil(raw.length / 4);
}
