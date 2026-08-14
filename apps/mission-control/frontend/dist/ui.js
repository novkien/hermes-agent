// DOM helpers: element factory, status chips, skeletons, error/retry panels,
// request-id display. No framework — small building blocks for tab modules.

import { icon } from './icons.js';
import { fitPopover } from './pure/popover-fit.js';

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v; // caller-controlled only
    else if (k.startsWith('on') && typeof v === 'function') {
      node.addEventListener(k.slice(2), v);
    } else if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  for (const c of [].concat(children || [])) {
    if (c === null || c === undefined) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function statusChip(state, label) {
  return el('span', { class: `chip chip-${String(state || 'unknown').toLowerCase()}` }, label || state || 'unknown');
}

/**
 * Square icon-only button. `disabledReason` is what turns an unavailable
 * action into an explanation instead of a dead control: the button stays
 * visible, goes disabled, and says why on hover.
 */
export function iconButton({
  icon: iconName, label, tone = '', disabled = false, disabledReason = '', onClick = () => {}, size = 13,
} = {}) {
  const classes = ['icon-btn', tone ? `icon-btn-${tone}` : null].filter(Boolean).join(' ');
  const node = el('button', {
    class: classes,
    type: 'button',
    title: disabled && disabledReason ? `${label} — ${disabledReason}` : label,
    'aria-label': label,
    onclick: (event) => {
      event.stopPropagation();
      onClick(event);
    },
  }, [icon(iconName, { size })]);
  if (disabled) node.disabled = true;
  return node;
}

/**
 * Destructive actions arm on the first click and fire on the second, so a
 * mis-click costs nothing. The armed state disarms itself after `window` ms.
 */
export function confirmButton({
  icon: iconName, label, confirmLabel = 'Confirm?', tone = 'danger',
  disabled = false, disabledReason = '', onConfirm = () => {}, window: timeout = 3000,
} = {}) {
  let armed = false;
  let timer = null;

  const node = iconButton({
    icon: iconName,
    label,
    tone,
    disabled,
    disabledReason,
    onClick: () => {
      if (armed) {
        disarm();
        onConfirm();
        return;
      }
      armed = true;
      node.classList.add('is-armed');
      node.title = confirmLabel;
      clearTimeout(timer);
      timer = setTimeout(disarm, timeout);
    },
  });

  function disarm() {
    clearTimeout(timer);
    armed = false;
    node.classList.remove('is-armed');
    node.title = disabled && disabledReason ? `${label} — ${disabledReason}` : label;
  }

  node.addEventListener('blur', disarm);
  return node;
}

/**
 * Segmented single-choice control. Returns the node with a `setValue` so a
 * caller can re-sync it after restoring persisted filter state.
 */
export function segmented(options = [], { value = null, onChange = () => {}, ariaLabel = '' } = {}) {
  const node = el('div', { class: 'seg', role: 'tablist', 'aria-label': ariaLabel || undefined });
  let current = value;

  const buttons = options.map((option) => {
    const button = el('button', {
      class: `seg-item${option.value === current ? ' active' : ''}`,
      type: 'button',
      role: 'tab',
      title: option.title || option.label,
      'aria-selected': String(option.value === current),
      onclick: () => {
        if (option.value === current) return;
        node.setValue(option.value);
        onChange(option.value);
      },
    }, [
      el('span', { text: option.label }),
      option.count === undefined || option.count === null
        ? null
        : el('span', { class: 'seg-count', text: String(option.count) }),
    ].filter(Boolean));
    node.append(button);
    return { option, button };
  });

  node.setValue = (next) => {
    current = next;
    for (const { option, button } of buttons) {
      const active = option.value === current;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
    }
  };

  return node;
}

/** Small label/value pair used across detail headers. */
export function metaItem(label, value, { mono = false } = {}) {
  return el('span', { class: 'meta-item' }, [
    el('span', { class: 'meta-item-k', text: label }),
    el('span', { class: `meta-item-v${mono ? ' mono' : ''}`, text: value === null || value === undefined || value === '' ? '—' : String(value) }),
  ]);
}

export function skeleton({ lines = 3, height = 12 } = {}) {
  const box = el('div', { class: 'skeleton', 'aria-hidden': 'true' });
  for (let i = 0; i < lines; i++) {
    box.append(el('div', { class: 'skeleton-line', style: `height:${height}px` }));
  }
  return box;
}

export function errorPanel({ message, requestId, onRetry } = {}) {
  const box = el('div', { class: 'panel-error', role: 'alert' });
  box.append(el('div', { class: 'panel-error-title' }, [icon('warning', { size: 14, className: 'state-icon' }), 'Load failed']));
  if (message) box.append(el('div', { class: 'panel-error-msg', text: message }));
  if (requestId) box.append(el('div', { class: 'panel-error-req', text: `request-id: ${requestId}` }));
  if (onRetry) {
    box.append(el('button', { class: 'btn btn-sm', onclick: onRetry }, [icon('retry', { size: 12 }), ' Retry']));
  }
  return box;
}

export function emptyState({ title = 'Empty', note = '' } = {}) {
  const box = el('div', { class: 'state-empty' });
  box.append(el('div', { class: 'state-empty-title' }, [icon('inbox', { size: 14, className: 'state-icon' }), title]));
  if (note) box.append(el('div', { class: 'state-empty-note', text: note }));
  return box;
}

export function unavailableState({ reason = 'Source unavailable', requestId } = {}) {
  const box = el('div', { class: 'state-unavailable', role: 'status' });
  box.append(el('div', { class: 'state-unavailable-title' }, [icon('plug', { size: 14, className: 'state-icon' }), reason]));
  if (requestId) box.append(el('div', { class: 'panel-error-req', text: `request-id: ${requestId}` }));
  return box;
}

/**
 * The fourth data state. Distinct from `unavailableState`: the capability does
 * not exist upstream, so there is nothing to retry — offering a retry button
 * here trains operators to mash a control that can never succeed.
 */
export function unsupportedState({ title = 'Not supported', reason = '', hint = '' } = {}) {
  const box = el('div', { class: 'state-unsupported', role: 'status' });
  box.append(el('div', { class: 'state-unsupported-title' }, [icon('ban', { size: 14, className: 'state-icon' }), title]));
  if (reason) box.append(el('div', { class: 'state-unsupported-note', text: reason }));
  if (hint) box.append(el('div', { class: 'state-unsupported-note', text: hint }));
  return box;
}

export function panel(title, contentNode, { badge, toolbar, icon: iconName, tone } = {}) {
  const head = el('div', { class: 'panel-head' });
  const titleChildren = [];
  if (iconName) titleChildren.push(icon(iconName, { size: 13, className: 'panel-title-icon' }));
  titleChildren.push(el('span', { text: title }));
  head.append(el('div', { class: 'panel-title' }, titleChildren));
  if (badge) head.append(badge);
  if (toolbar) head.append(toolbar);
  const body = el('div', { class: 'panel-body' });
  body.append(contentNode);
  const classes = ['panel', tone ? `panel-tone-${tone}` : null].filter(Boolean).join(' ');
  return el('section', { class: classes }, [head, body]);
}

// Small inline sparkline — only ever called with real observed values, never
// fabricated series (callers must skip this when the source has no history).
export function sparkline(values = [], { width = 72, height = 22 } = {}) {
  const nums = values.filter((v) => typeof v === 'number' && Number.isFinite(v));
  if (nums.length < 2) return null;
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min || 1;
  const stepX = width / (nums.length - 1);
  const points = nums.map((v, i) => `${(i * stepX).toFixed(1)},${(height - ((v - min) / span) * height).toFixed(1)}`);
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('class', 'kpi-spark');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('aria-hidden', 'true');
  const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
  line.setAttribute('points', points.join(' '));
  svg.append(line);
  return svg;
}

// Stat tile used across Overview/Kanban/etc. `sparkValues`/`trend` are
// optional and only rendered when the caller has real data to show.
export function kpi({ label, value, iconName, trend, sparkValues, tone } = {}) {
  const classes = ['kpi', tone ? `kpi-tone-${tone}` : null].filter(Boolean).join(' ');
  const box = el('div', { class: classes });
  const topRow = el('div', { class: 'kpi-icon-row' });
  if (iconName) topRow.append(icon(iconName, { size: 14, className: 'kpi-icon' }));
  if (trend) {
    const dir = trend > 0 ? 'up' : trend < 0 ? 'down' : 'flat';
    const sign = trend > 0 ? '+' : '';
    topRow.append(el('span', { class: `kpi-trend kpi-trend-${dir}`, text: `${sign}${trend}%` }));
  }
  if (topRow.childNodes.length) box.append(topRow);
  box.append(el('div', { class: 'kpi-value', text: value === undefined || value === null ? '—' : String(value) }));
  box.append(el('div', { class: 'kpi-label', text: label || '' }));
  const spark = sparkline(sparkValues || []);
  if (spark) box.append(spark);
  return box;
}

// Some upstream sources (the alert engine's first_seen_at/last_seen_at, for
// instance) report Unix seconds rather than an ISO string or millisecond
// epoch. `new Date()` treats a bare number as milliseconds, so a raw seconds
// value silently resolved to a date ~20 days after epoch. Any number well
// below the millisecond-epoch range (today's ms epoch is ~1.7e12) is seconds.
const SECONDS_EPOCH_CEILING = 1e12;

function toEpochMs(value) {
  if (typeof value === 'number') return value < SECONDS_EPOCH_CEILING ? value * 1000 : value;
  return value;
}

export function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(toEpochMs(iso));
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString();
}

export function fmtAge(iso, now = Date.now()) {
  if (!iso) return '—';
  const t = new Date(toEpochMs(iso)).getTime();
  if (Number.isNaN(t)) return String(iso);
  const s = Math.max(0, Math.floor((now - t) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

/* ---------------------------------------------------------------- popover -- */

// One popover at a time, anchored to its trigger. This lived inside the chat
// tab while several surfaces wanted it; it belongs here so there is exactly one
// implementation of "open a floating menu, close it on outside click or Escape,
// and never leave two of them on screen".
let openMenuNode = null;
let closeMenuListener = null;
let menuTeardown = null;
let anchorSeq = 0;

function anchorId(node) {
  if (!node.dataset.menuAnchorId) {
    anchorSeq += 1;
    node.dataset.menuAnchorId = `anchor-${anchorSeq}`;
  }
  return node.dataset.menuAnchorId;
}

export function closeMenu() {
  if (closeMenuListener) {
    document.removeEventListener('mousedown', closeMenuListener, true);
    document.removeEventListener('keydown', closeMenuListener, true);
    closeMenuListener = null;
  }
  if (menuTeardown) {
    menuTeardown();
    menuTeardown = null;
  }
  if (openMenuNode) {
    openMenuNode.remove();
    openMenuNode = null;
  }
}

/**
 * `placement: 'above'` is the default because the composer sits at the bottom
 * of the workspace and its menus have to open upward; 'below' is for triggers
 * in a header. Either way the popover is clamped inside the viewport.
 */
export function openMenu(anchor, content, { placement = 'above', align = 'end' } = {}) {
  const reopening = openMenuNode && openMenuNode.dataset.anchorId === anchorId(anchor);
  closeMenu();
  if (reopening) return null;

  const wrap = el('div', { class: 'chat-menu-layer', role: 'dialog' });
  wrap.dataset.anchorId = anchorId(anchor);
  wrap.append(content);
  document.body.append(wrap);
  openMenuNode = wrap;

  // Anchored to one edge, capped on the other: a menu that fetches its contents
  // then grows does so within the room it was given, without a second
  // measurement. Only the width is measured, and width does not change as
  // content loads.
  const surface = wrap.firstElementChild || wrap;
  function fit() {
    const box = fitPopover(
      anchor.getBoundingClientRect(),
      { width: wrap.offsetWidth },
      { width: window.innerWidth, height: window.innerHeight },
      { placement, align },
    );
    surface.style.maxHeight = `${box.maxHeight}px`;
    surface.style.maxWidth = `${box.maxWidth}px`;
    wrap.style.top = box.top === null ? '' : `${box.top}px`;
    wrap.style.bottom = box.bottom === null ? '' : `${box.bottom}px`;
    wrap.style.left = `${box.left}px`;
    wrap.dataset.placement = box.placement;
  }

  wrap.style.visibility = 'hidden';
  fit();
  wrap.style.visibility = '';

  // The anchor moves when the window changes size; the popover's own growth
  // needs no handling here, which is the point of anchoring by edge.
  const refit = () => fit();
  window.addEventListener('resize', refit);
  menuTeardown = () => window.removeEventListener('resize', refit);

  closeMenuListener = (event) => {
    if (event.type === 'keydown') {
      if (event.key === 'Escape') closeMenu();
      return;
    }
    if (!wrap.contains(event.target) && event.target !== anchor && !anchor.contains(event.target)) {
      closeMenu();
    }
  };
  document.addEventListener('mousedown', closeMenuListener, true);
  document.addEventListener('keydown', closeMenuListener, true);
  return wrap;
}

export function debounce(fn, ms = 250) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}
