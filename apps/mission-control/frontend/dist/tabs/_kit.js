// Shared scaffolding for resource tabs.
//
// Every tab below used to hand-roll the same four things — load an envelope,
// pick one of the four data states, paint a toolbar, and report a mutation —
// and each did it slightly differently. That drift is what made a third of the
// dashboard read as unfinished, so the behaviour lives here once.

import { el, clear, iconButton, fmtAge } from '../ui.js';
import { icon } from '../icons.js';
import { toast } from '../components/toast.js';
import { recordFrom } from '../pure/data-shape.js';
import { chainIdBatches } from '../pure/session-chain.js';

/**
 * Load an envelope and classify it into exactly one of the four data states.
 * Never throws: an unreachable source is data about the source, not an error.
 *
 * @returns {{state:'ready'|'empty'|'unavailable'|'unsupported', data, meta, reason, requestId}}
 */
export async function loadEnvelope(api, path, { profile, pick = null, allowEmpty = true } = {}) {
  let envelope;
  try {
    envelope = await api.get(path, { profile });
  } catch (err) {
    // 404/501 from the BFF means the capability is not proxied at all, which is
    // a different thing from an upstream that is merely down right now.
    const missing = err.status === 404 || err.status === 501;
    return {
      state: missing ? 'unsupported' : 'unavailable',
      data: null,
      meta: null,
      reason: err.message || 'source unavailable',
      requestId: err.request_id || null,
    };
  }
  const meta = envelope.meta || null;
  const raw = envelope.data;
  const data = pick ? pick(raw) : raw;
  const empty = data === null || data === undefined
    || (Array.isArray(data) && data.length === 0);
  return {
    state: empty && allowEmpty ? 'empty' : 'ready',
    data,
    meta,
    reason: meta?.degraded_reason || '',
    requestId: meta?.request_id || null,
  };
}

/**
 * Resolve dashboard-listed session roots to their live chain tips.
 *
 * Batches respect the adapter's hard cap of 200 ids per `/session-tips` call;
 * batches run with bounded concurrency so resolving a full page of sessions
 * costs a handful of round trips, not one per session. Best-effort: a batch
 * that fails contributes no tips rather than failing the whole page — a
 * frozen-but-correctly-labelled root is a better failure mode than an empty
 * session list.
 */
export async function fetchChainTips({ api, profile, ids, concurrency = 4 }) {
  const batches = chainIdBatches(ids);
  const tips = {};
  for (let i = 0; i < batches.length; i += concurrency) {
    const slice = batches.slice(i, i + concurrency);
    // eslint-disable-next-line no-await-in-loop -- bounded batches, not a full unroll
    const results = await Promise.all(slice.map((batch) => api
      .get(`/api/adapter/session-tips?ids=${encodeURIComponent(batch.join(','))}`, { profile })
      .then((response) => recordFrom(response?.data)?.tips || {})
      .catch(() => ({}))));
    for (const part of results) Object.assign(tips, part);
  }
  return tips;
}

/** Apply a loaded envelope's state to a table built by `components/table.js`. */
export function applyStateToTable(table, result, { emptyRows = [] } = {}) {
  if (result.state === 'unsupported') {
    table.setUnsupported({
      title: 'Not supported upstream',
      reason: result.reason,
      hint: 'This capability is not exposed by the Hermes API this dashboard proxies.',
    });
    return false;
  }
  if (result.state === 'unavailable') {
    table.setUnavailable({ reason: result.reason, requestId: result.requestId });
    return false;
  }
  table.setRows(result.state === 'empty' ? emptyRows : (Array.isArray(result.data) ? result.data : []));
  return true;
}

/**
 * The per-tab topbar contents. `app.js` clears the slot before each activation,
 * so a tab just describes what belongs there.
 */
export function tabToolbar({ title, subtitle = '', actions = [], filters = [], onRefresh = null, meta = null }) {
  const wrap = el('div', { class: 'tab-toolbar' });
  const heading = el('div', { class: 'tab-toolbar-head' }, [
    el('span', { class: 'tab-title', text: title }),
    subtitle ? el('span', { class: 'tab-subtitle', text: subtitle }) : null,
  ].filter(Boolean));
  wrap.append(heading);

  if (filters.length) {
    const group = el('div', { class: 'tab-toolbar-filters' });
    for (const node of filters) if (node) group.append(node);
    wrap.append(group);
  }

  const right = el('div', { class: 'tab-toolbar-actions' });
  for (const node of actions) if (node) right.append(node);
  if (meta?.fetched_at) {
    right.append(el('span', {
      class: 'tab-toolbar-stamp',
      text: `updated ${fmtAge(new Date(meta.fetched_at * 1000).toISOString())}`,
    }));
  }
  if (onRefresh) {
    right.append(iconButton({ icon: 'refresh', label: 'Refresh', onClick: onRefresh }));
  }
  if (right.childNodes.length) wrap.append(right);
  return wrap;
}

/**
 * The dashboard's one filter box.
 *
 * Marked with `data-tab-filter` so the global `/` shortcut can find whichever
 * one the active tab put on screen — the shortcut has no tab registry to
 * consult and should not need one.
 *
 * Debounced because every caller filters in memory on each keystroke, and a
 * 5,000-row list re-rendered per character is the difference between instant
 * and laggy.
 */
export function filterInput({
  value = '', placeholder = 'Filter…', ariaLabel = 'Filter rows',
  onChange = () => {}, delay = 120,
} = {}) {
  const node = el('input', {
    class: 'input input-sm tab-filter-input',
    type: 'search',
    value,
    placeholder,
    'aria-label': ariaLabel,
    'data-tab-filter': '1',
    autocomplete: 'off',
    spellcheck: 'false',
  });
  let timer = null;
  const fire = () => onChange(node.value);
  node.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(fire, delay);
  });
  // Enter and the type=search clear button must not wait out the debounce.
  node.addEventListener('search', () => { clearTimeout(timer); fire(); });
  node.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { clearTimeout(timer); fire(); }
    if (event.key === 'Escape' && node.value) {
      event.stopPropagation();
      node.value = '';
      clearTimeout(timer);
      fire();
    }
  });
  return node;
}

/** A primary (accent) toolbar button. */
export function primaryButton(label, iconName, onClick) {
  return el('button', { class: 'btn btn-accent btn-sm', type: 'button', onclick: onClick }, [
    icon(iconName, { size: 12 }), ` ${label}`,
  ]);
}

/**
 * Labelled two-click confirm button.
 *
 * `ui.js`'s confirmButton is icon-only, which is fine for a row action where
 * the row says what the target is. For a standalone control — "restart the
 * gateway" — the icon alone is a guess, so this variant keeps the arming
 * behaviour and shows the word.
 */
export function confirmAction({
  label, iconName = 'warning', confirmLabel = 'Click again to confirm',
  tone = 'danger', disabled = false, onConfirm = () => {},
  window: timeout = 3000,
} = {}) {
  const text = el('span', { text: ` ${label}` });
  const node = el('button', {
    class: `btn btn-sm${tone && tone !== 'danger' ? ` btn-${tone}` : tone === 'danger' ? ' btn-danger' : ''}`,
    type: 'button',
    title: label,
  }, [icon(iconName, { size: 12 }), text]);
  if (disabled) node.disabled = true;

  let armed = false;
  let timer = null;

  function disarm() {
    clearTimeout(timer);
    armed = false;
    node.classList.remove('is-armed');
    text.textContent = ` ${label}`;
  }

  node.addEventListener('click', () => {
    if (armed) {
      disarm();
      onConfirm();
      return;
    }
    armed = true;
    node.classList.add('is-armed');
    text.textContent = ` ${confirmLabel}`;
    clearTimeout(timer);
    timer = setTimeout(disarm, timeout);
  });
  node.addEventListener('blur', disarm);
  return node;
}

/**
 * Run a mutation and report the outcome, always with the request id.
 *
 * Returns the response envelope on success and `null` on failure, so a caller
 * can reload only when something actually changed. `meta.restart_required`
 * (plugin toggles) is surfaced rather than swallowed.
 */
export async function runMutation(fn, { pending, ok, onDone = null } = {}) {
  try {
    const res = await fn();
    const requestId = res?.meta?.request_id ? `request ${res.meta.request_id}` : '';
    if (res?.meta?.restart_required) {
      toast(ok || 'Saved', {
        tone: 'warn',
        detail: 'Takes effect after a gateway restart.',
        timeout: 8000,
      });
    } else {
      toast(ok || 'Saved', { tone: 'ok', detail: requestId });
    }
    if (onDone) await onDone(res);
    return res;
  } catch (err) {
    toast(err.message || `${pending || 'Action'} failed`, {
      tone: 'danger',
      detail: err.request_id ? `request ${err.request_id}` : '',
      timeout: 9000,
    });
    return null;
  }
}

/** Standard yes/no chip for a boolean column. */
export function boolChip(value, { on = 'enabled', off = 'disabled' } = {}) {
  const state = value ? on : off;
  return el('span', { class: `chip chip-${value ? 'ok' : 'idle'}`, text: state });
}

/** Empty-side placeholder that explains the tab instead of showing nothing. */
export function sideHint(title, lines = []) {
  return el('div', { class: 'side-hint' }, [
    el('div', { class: 'side-hint-title', text: title }),
    ...lines.map((line) => el('p', { class: 'side-hint-line', text: line })),
  ]);
}

/** Clear a host and paint one node into it. */
export function paint(host, node) {
  clear(host);
  if (node) host.append(node);
  return host;
}
