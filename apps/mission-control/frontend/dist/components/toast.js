// Transient action feedback. One host element, appended to <body> on first use.
//
// A toast reports what actually happened — including the upstream reason when
// an action was refused — so a failed mutation is never silent.

import { el, clear } from '../ui.js';
import { icon } from '../icons.js';

const TONE_ICONS = { ok: 'check', danger: 'warning', warn: 'warning', info: 'spark' };

let host = null;

function ensureHost() {
  if (host && host.isConnected) return host;
  host = el('div', { class: 'toast-host', role: 'status', 'aria-live': 'polite' });
  document.body.append(host);
  return host;
}

/**
 * toast(message, opts) → dismiss()
 * `detail` carries the secondary line (a request id, an upstream message).
 */
export function toast(message, { tone = 'info', detail = '', timeout = 4200 } = {}) {
  const box = el('div', { class: `toast toast-${tone}` }, [
    icon(TONE_ICONS[tone] || 'spark', { size: 14, className: 'toast-icon' }),
    el('div', { class: 'toast-text' }, [
      el('div', { class: 'toast-msg', text: String(message) }),
      detail ? el('div', { class: 'toast-detail', text: String(detail) }) : null,
    ].filter(Boolean)),
  ]);

  const close = el('button', {
    class: 'toast-close',
    type: 'button',
    'aria-label': 'Dismiss',
    onclick: () => dismiss(),
  }, [icon('close', { size: 12 })]);
  box.append(close);

  ensureHost().append(box);
  let timer = timeout ? setTimeout(dismiss, timeout) : null;

  function dismiss() {
    clearTimeout(timer);
    timer = null;
    box.classList.add('is-leaving');
    setTimeout(() => box.remove(), 180);
  }

  return dismiss;
}

export function clearToasts() {
  if (host) clear(host);
}
