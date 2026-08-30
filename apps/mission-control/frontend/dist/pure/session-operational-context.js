// Shared, transient operational context for individual sessions.  It is kept
// in each mounted tab instance; no Web Storage means an old card state cannot
// survive a profile switch or a later dashboard deployment.

export const CONTEXT_REFRESH_MS = 10_000;

export function normalizeWorkerLink(value) {
  if (!value || value.kind !== 'kanban_worker') return null;
  if (value.resolution !== 'verified') {
    return { kind: 'kanban_worker', resolution: 'unresolved', reason: value.reason || 'unknown' };
  }
  return {
    kind: 'kanban_worker', resolution: 'verified', task_id: value.task_id || null,
    status: String(value.status || 'unknown').toLowerCase(), current_run_id: value.current_run_id || null,
    last_heartbeat_at: value.last_heartbeat_at || null, board: value.board || null, assignee: value.assignee || null,
  };
}

export function isWorkerRunning(link) {
  return link?.kind === 'kanban_worker' && link.resolution === 'verified' && link.status === 'running';
}

export function showWorkerStatusOnSessionCard(link, sessionRunning = false) {
  return !(sessionRunning && isWorkerRunning(link));
}

export function mergeTaskChanged(link, event) {
  if (!link || link.resolution !== 'verified') return link;
  const payload = event?.payload || event || {};
  if ((event?.entity_id || payload.task_id || payload.id) !== link.task_id) return link;
  return normalizeWorkerLink({ ...link, status: payload.status ?? link.status,
    current_run_id: payload.current_run_id ?? link.current_run_id,
    last_heartbeat_at: payload.last_heartbeat_at ?? link.last_heartbeat_at });
}

export async function fetchWorkerLinks({ api, profile, sessionIds }) {
  const ids = [...new Set((sessionIds || []).filter(Boolean))].slice(0, 50);
  if (!ids.length) return new Map();
  const query = ids.map(encodeURIComponent).join(',');
  const response = await api.get(`/api/session-context/kanban-workers?session_ids=${query}`, { profile });
  const links = response?.data?.links || response?.links || {};
  return new Map(Object.entries(links).map(([id, link]) => [id, normalizeWorkerLink(link)]).filter(([, link]) => link));
}

export function workerStateLabel(link) {
  if (!link) return null;
  if (link.resolution !== 'verified') return 'Kanban worker · card unresolved';
  return `Kanban · ${link.task_id}`;
}
