// Alert sorting + SSE event dedupe — pure helpers.

const SEVERITY_ORDER = { critical: 0, high: 1, warning: 2, medium: 3, info: 4, low: 5, unknown: 6 };

function severityRank(value) {
  return SEVERITY_ORDER[String(value || 'unknown').toLowerCase()] ?? SEVERITY_ORDER.unknown;
}

export function sortAlerts(alerts) {
  return [...(alerts || [])].sort((left, right) => {
    const severity = severityRank(left.severity) - severityRank(right.severity);
    if (severity !== 0) return severity;
    const leftTime = left.last_seen_at || left.last_seen || left.first_seen_at || left.first_seen || left.created_at || '';
    const rightTime = right.last_seen_at || right.last_seen || right.first_seen_at || right.first_seen || right.created_at || '';
    return String(rightTime).localeCompare(String(leftTime));
  });
}

export function dedupeEvents(events) {
  const seen = new Set();
  const output = [];
  for (const event of events || []) {
    if (!event || event.event_id === undefined || event.event_id === null) continue;
    if (seen.has(event.event_id)) continue;
    seen.add(event.event_id);
    output.push(event);
  }
  return output;
}
