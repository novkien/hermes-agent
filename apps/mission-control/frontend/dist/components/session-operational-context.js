import { el, fmtAge } from '../ui.js';
import { buildHash } from '../pure/hash-router.js';
import { workerStateLabel } from '../pure/session-operational-context.js';

/** Small reusable block used wherever one specific session is named. */
export function workerContextNodes(link, { compact = false } = {}) {
  if (!link) return [];
  if (link.resolution !== 'verified') {
    return [el('span', { class: 'chip chip-warn', title: link.reason || 'Kanban card could not be verified', text: workerStateLabel(link) })];
  }
  const openCard = () => {
    const url = new URL(window.location.href);
    url.hash = buildHash('/kanban', { task: link.task_id });
    window.location.assign(`${url.pathname}${url.search}${url.hash}`);
  };
  const result = [el('button', {
    class: 'chip chip-accent', type: 'button', text: workerStateLabel(link), title: 'Open Kanban card', onclick: (event) => { event.stopPropagation(); openCard(); },
  })];
  result.push(el('span', { class: `chip chip-${link.status === 'running' ? 'live' : link.status === 'blocked' ? 'warn' : link.status === 'done' ? 'ok' : 'idle'}`, text: link.status }));
  if (!compact && link.last_heartbeat_at) result.push(el('span', { class: 'chip', title: 'Last Kanban worker heartbeat', text: `heartbeat ${fmtAge(link.last_heartbeat_at)}` }));
  return result;
}
