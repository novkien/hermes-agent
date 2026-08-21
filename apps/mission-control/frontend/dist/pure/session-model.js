// Which model does THIS session run, and how do we know?
//
// That single question used to be answered independently by four surfaces —
// the header chip, the sider row, the composer pill and the body builder for
// `chat/stream` — each from its own snapshot, taken at a different moment:
//
//   * the header chip re-read `session.model` and was patched after every turn
//     with the runtime the gateway REPORTED having used,
//   * the composer pill was seeded once, when the thread was opened, and never
//     heard about that patch,
//   * a brand-new session had its row stamped with the gateway's GLOBAL model,
//     which is a value the session itself may never run,
//   * and the "did the operator change the model?" test in `chatStreamBody`
//     compared the live pick against a `session.model` captured in a closure.
//
// Four writers and no reader in common is why the same session could show
// three different model names at once. This module is the one owner: prefs are
// created from the session, updated as better evidence arrives, and read back
// through `effectiveModel`. Everything else derives.

import { providerRows } from './chat-model.js';

/** How the current selection came to be, weakest to strongest. */
export const MODEL_SOURCE = {
  none: '',
  session: 'session', // the session row's own persisted model
  run: 'run',         // what the gateway reported actually running
  pick: 'pick',       // the operator chose it in the picker
};

export const MODEL_SOURCE_NOTE = {
  '': 'No model of its own — this session follows the gateway default',
  session: "The session's own model",
  run: 'What the last turn actually ran on',
  pick: 'Your pick for this session',
};

/**
 * Provider ids arrive spelled three ways: the picker's slug (`9router`), the
 * session row's billing string (`custom:9router`) and the gateway's resolved
 * provider KIND (`custom`, which every custom provider collapses to). Only a
 * value that matches a slug the picker actually offers is usable, so anything
 * else is dropped rather than displayed as if it were selectable.
 */
export function normalizeProvider(options, value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  const rows = providerRows(options);
  const candidates = raw.includes(':') ? [raw.slice(raw.indexOf(':') + 1), raw] : [raw];
  for (const candidate of candidates) {
    if (rows.some((row) => row.slug === candidate)) return candidate;
  }
  return '';
}

/**
 * True when the picker can actually offer this model. A model that is NOT in
 * the catalogue is a routing alias (Hermes `model_routes`, e.g. `normal`) or a
 * stale config value: real enough for the gateway to resolve, but impossible
 * for the operator to re-select, so the UI must never pretend it was picked.
 */
export function catalogHas(options, provider, model) {
  if (!model) return false;
  const wanted = String(model);
  return providerRows(options).some((row) => {
    if (provider && row.slug !== provider) return false;
    const listed = Array.isArray(row.models) ? row.models : [];
    const featured = Array.isArray(row.featured_models) ? row.featured_models : [];
    return listed.includes(wanted) || featured.includes(wanted);
  });
}

/** The per-session composer state. Mutated in place so live references stay valid. */
export function createModelPrefs(session, options = null) {
  const model = String(session?.model || '').trim();
  return {
    model,
    provider: normalizeProvider(options, session?.provider || session?.billing_provider),
    effort: '',
    explicit: false,
    source: model ? MODEL_SOURCE.session : MODEL_SOURCE.none,
  };
}

/** Session-list evidence: the row's persisted model. Never overrides a pick. */
export function observeSessionModel(prefs, session, options = null) {
  const model = String(session?.model || '').trim();
  if (!prefs || prefs.explicit || !model) return prefs;
  prefs.model = model;
  const provider = normalizeProvider(options, session?.provider || session?.billing_provider);
  if (provider) prefs.provider = provider;
  prefs.source = MODEL_SOURCE.session;
  return prefs;
}

/**
 * `run.completed` evidence: what the gateway says it ran. This is the strongest
 * non-deliberate signal — it is the only one measured rather than declared —
 * so it wins over the row, and it is exactly the update the composer pill used
 * to miss while the header chip took it.
 */
export function observeRunModel(prefs, runtime, options = null) {
  const model = String(runtime?.model || '').trim();
  if (!prefs || prefs.explicit || !model) return prefs;
  prefs.model = model;
  const provider = normalizeProvider(options, runtime?.provider);
  if (provider) prefs.provider = provider;
  prefs.source = MODEL_SOURCE.run;
  return prefs;
}

/** A deliberate pick. Sticky: nothing observed afterwards may quietly undo it. */
export function pickSessionModel(prefs, model, provider) {
  if (!prefs) return prefs;
  prefs.model = String(model || '');
  prefs.provider = String(provider || '');
  prefs.explicit = true;
  prefs.source = MODEL_SOURCE.pick;
  return prefs;
}

/**
 * The gateway's own confirmation that a durable browser lock is active for
 * this session — read from `session.model_config`, which `POST
 * /api/sessions/{id}/model` writes and which survives a reload, a different
 * tab, or a turn driven from Telegram/cron. The session-LIST read this app
 * otherwise uses for `session.model` does not carry this field at all (only
 * the single-session read does), so without this a reload has no way to
 * distinguish "the session's model happens to be X" from "X is locked and
 * every future turn will honour it" — the pick and the lock icon would
 * silently disagree with what the gateway is actually enforcing.
 */
export function observeConfirmedLock(prefs, modelConfig) {
  if (!prefs) return prefs;
  let parsed = modelConfig;
  if (typeof parsed === 'string') {
    try { parsed = JSON.parse(parsed); } catch (_err) { return prefs; }
  }
  const lock = parsed && typeof parsed === 'object' ? parsed.browser_model_lock : null;
  if (!lock || !lock.confirmed) return prefs;
  const model = String(lock.model || '').trim();
  if (!model) return prefs;
  return pickSessionModel(prefs, model, lock.provider || prefs.provider);
}

/**
 * What this session runs, and how sure we are.
 *
 * `inherited` means the session has no model of its own and the gateway's
 * global default is what will answer. The pill shows that value so the
 * operator is not left staring at "No model", but it is NOT written into the
 * prefs — writing it would turn a guess into a request and pin the session to
 * a model nobody chose, which is the exact bug this replaced.
 */
export function effectiveModel(prefs, options = null) {
  const own = String(prefs?.model || '').trim();
  const globalModel = String(options?.model || '').trim();
  const model = own || globalModel;
  const provider = own
    ? String(prefs?.provider || '')
    : normalizeProvider(options, options?.provider);
  const source = own ? (prefs?.source || MODEL_SOURCE.session) : MODEL_SOURCE.none;
  return {
    model,
    provider,
    source,
    inherited: !own,
    // An alias resolves to something real but cannot be selected back, so the
    // UI labels it instead of implying the picker is showing a current row.
    alias: Boolean(model) && !catalogHas(options, provider, model),
    note: MODEL_SOURCE_NOTE[source] || MODEL_SOURCE_NOTE[''],
  };
}
