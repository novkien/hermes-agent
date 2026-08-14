// Session transcript + actions — single source of truth for rendering a
// session's timeline and its rename/archive/delete controls into ANY
// container. The Sessions tab's detail pane and the Fleet/Topology inspector
// popup both call this instead of each keeping their own copy, so a change
// here (a new action, a different timeline shape) reaches both at once.

import { el, clear, skeleton, emptyState, errorPanel, fmtTime } from '../ui.js';
import { provenanceBadge } from '../provenance.js';
import { listFrom } from '../pure/data-shape.js';

/** Render a session's transcript + actions into `container`. Safe to call
 * again on the same container (e.g. on retry) — it clears first. */
export async function renderSessionDetail({ api, profile, id, container, onChanged }) {
  if (!id || !container) return;
  clear(container);
  container.append(skeleton({ lines: 6 }));

  let response = null;
  try {
    response = await api.get(`/api/adapter/sessions/${encodeURIComponent(id)}/timeline`, { profile });
  } catch (err) {
    clear(container);
    container.append(errorPanel({
      message: err.message,
      requestId: err.request_id,
      onRetry: () => renderSessionDetail({ api, profile, id, container, onChanged }),
    }));
    return;
  }

  clear(container);
  container.append(el('div', { class: 'detail-head' }, [
    el('span', { class: 'detail-title', text: `Session ${id}` }),
    provenanceBadge(response.meta),
  ]));

  const timeline = listFrom(response.data, ['timeline', 'messages', 'events']);
  if (!timeline.length) {
    container.append(emptyState({ title: 'No timeline entries' }));
  } else {
    const list = el('ul', { class: 'list' });
    for (const message of timeline.slice(0, 200)) {
      list.append(el('li', { class: 'list-item' }, [
        el('span', { class: 'mono', text: fmtTime(message.created_at || message.timestamp || message.ts) }),
        el('span', { class: 'chip', text: message.role || message.event_type || message.kind || 'entry' }),
        el('span', { class: 'mono pre-wrap', text: truncate(message.content || message.text || message.summary || '') }),
      ]));
    }
    container.append(list);
  }

  const actions = el('div', { class: 'detail-actions' });
  actions.append(
    el('button', { class: 'btn btn-sm', text: 'Rename', onclick: rename }),
    el('button', { class: 'btn btn-sm', text: 'Archive', onclick: () => updateArchived(true) }),
    el('button', { class: 'btn btn-sm btn-danger', text: 'Delete', onclick: remove }),
  );
  container.append(actions);

  async function rename() {
    const name = prompt('New session name:');
    if (!name) return;
    await api.patch(`/api/upstream/api/sessions/${encodeURIComponent(id)}`, { name }, { profile });
    if (onChanged) await onChanged();
  }

  async function updateArchived(archived) {
    if (!confirm(`Confirm archive session ${id}?`)) return;
    await api.patch(`/api/upstream/api/sessions/${encodeURIComponent(id)}`, { archived }, { profile });
    if (onChanged) await onChanged();
  }

  async function remove() {
    if (!confirm(`Confirm delete session ${id}?`)) return;
    await api.del(`/api/upstream/api/sessions/${encodeURIComponent(id)}`, { profile });
    clear(container);
    if (onChanged) await onChanged();
  }
}

function truncate(value, limit = 500) {
  const text = String(value || '');
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

/**
 * The overlay shell every tab-launched popup uses: a dialog that floats over
 * whatever tab is currently mounted, without navigating away from it. It owns
 * nothing but the chrome (title, close button, Escape, backdrop click) and
 * hands the caller a `body` to render into — so a second kind of popup means a
 * second `title`, not a second modal implementation.
 *
 * `size: 'wide'` is for popups hosting a full surface (chat); the default suits
 * a read-only panel (transcript).
 *
 * `onClose` runs after the overlay hides, for callers that must tear something
 * down (abort a stream, drop an SSE subscription).
 */
export function createModal({ title = '', size = '', onClose = null } = {}) {
  const overlay = el('div', { class: 'session-modal-overlay hidden' });
  const box = el('div', {
    class: `session-modal-box${size ? ` session-modal-${size}` : ''}`,
    role: 'dialog', 'aria-modal': 'true', 'aria-label': title || 'Dialog',
  });
  const titleNode = el('span', { class: 'detail-title', text: title });
  const head = el('div', { class: 'session-modal-head' }, [
    titleNode,
    el('button', { class: 'btn btn-sm session-modal-close', text: 'Close ✕', onclick: () => close() }),
  ]);
  const body = el('div', { class: 'session-modal-body' });
  box.append(head, body);
  overlay.append(box);
  document.body.append(overlay);

  let open = false;

  function onKey(event) {
    if (event.key === 'Escape') close();
  }

  function show() {
    open = true;
    overlay.classList.remove('hidden');
    document.addEventListener('keydown', onKey, true);
  }

  function close() {
    if (!open) return;
    open = false;
    overlay.classList.add('hidden');
    document.removeEventListener('keydown', onKey, true);
    if (onClose) onClose();
    clear(body);
  }

  overlay.addEventListener('mousedown', (event) => {
    if (event.target === overlay) close();
  });

  return {
    body, show, close,
    setTitle(text) { titleNode.textContent = text; },
    get isOpen() { return open; },
  };
}

/**
 * Transcript popup: the shared overlay hosting `renderSessionDetail`, so the
 * Fleet/Topology inspector shows exactly what the Sessions tab's detail pane
 * shows.
 */
export function createSessionDetailModal({ api, profile, onChanged } = {}) {
  const modal = createModal({ title: 'Transcript' });

  async function openSession(id) {
    modal.show();
    await renderSessionDetail({ api, profile, id, container: modal.body, onChanged });
  }

  return { open: openSession, close: modal.close, get isOpen() { return modal.isOpen; } };
}
