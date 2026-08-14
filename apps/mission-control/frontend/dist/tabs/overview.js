// Overview — the answer to "what is the fleet doing right now, and is anything
// wrong?" on one screen.
//
// Rebuilt on `components/stat.js` + `components/chart.js`. Two rules shaped it:
//
//  1. Every tile is a drill-down. A number with no way to reach the rows behind
//     it makes the operator re-navigate and re-filter by hand, which is what
//     the old key/value dump forced.
//  2. Series colour never means health. Trend marks use the validated
//     `--series-*` ramp; only the health strip uses ok/warn/danger.

import {
  el, clear, skeleton, statusChip, segmented, emptyState, unavailableState,
  errorPanel, fmtAge,
} from '../ui.js';
import { provenanceBadge } from '../provenance.js';
import { createStatRow } from '../components/stat.js';
import { barChart, chartLegend } from '../components/chart.js';
import { classifyCost, costLabel } from '../pure/cost-classify.js';
import {
  dailySeries, sliceTotals, compactNumber, formatCost,
} from '../pure/analytics-shape.js';
import {
  alertRows, capabilityRegistry, listFrom, pulseRecord, summarizeSourceHealth, taskRows,
} from '../pure/data-shape.js';
import { loadEnvelope, tabToolbar, paint, sideHint } from './_kit.js';

export const GATEWAY_UNAVAILABLE_REASON =
  'Hermes Gateway is not reachable from the Pi BFF; read-only dashboard sources remain available.';

export const PULSE_WINDOWS = Object.freeze(['1h', '24h', '7d']);

/** How many days of analytics each pulse window pulls for its trend chart. */
export const WINDOW_DAYS = Object.freeze({ '1h': 7, '24h': 14, '7d': 30 });

/** Token series colours: `--series-1` / `--series-5`, the widest-separated pair. */
const TOKEN_SERIES = Object.freeze([
  { key: 'input_tokens', label: 'Input', seriesIndex: 1 },
  { key: 'output_tokens', label: 'Output', seriesIndex: 5 },
]);

export function createOverview({ api, profile, toolbar, onNavigate: navigate }) {
  const root = el('div', { class: 'tab tab-overview' });
  const statsPane = el('div');
  const trendPane = el('div');
  const gridPane = el('div', { class: 'grid-cards' });
  root.append(statsPane, trendPane, gridPane);

  let currentWindow = '24h';
  let pulse = null;
  let usage = null;
  let days = [];
  let brush = null;
  let tasks = null;
  let permits = null;
  let issues = null;
  let alerts = null;
  let sourceHealth = null;
  let inspectorHost = null;
  let events = null;
  let chartNode = null;

  // -------------------------------------------------------------------- data

  async function load() {
    clear(statsPane);
    clear(trendPane);
    clear(gridPane);
    statsPane.append(skeleton({ lines: 3 }));

    const period = WINDOW_DAYS[currentWindow] || 14;
    const [pulseResult, usageResult, taskResult, permitResult, issueResult, alertResult, capResult] =
      await Promise.all([
        loadEnvelope(api, `/api/pulse?window=${encodeURIComponent(currentWindow)}`, { profile, allowEmpty: false }),
        loadEnvelope(api, `/api/upstream/api/analytics/usage?days=${period}`, { profile, allowEmpty: false }),
        loadEnvelope(api, '/api/adapter/kanban/tasks?limit=100', { profile, pick: taskRows }),
        loadEnvelope(api, '/api/adapter/permits?limit=100', { profile, pick: (raw) => listFrom(raw, ['permits']) }),
        loadEnvelope(api, '/api/adapter/issues?limit=100', { profile, pick: (raw) => listFrom(raw, ['issues']) }),
        loadEnvelope(api, '/api/alerts', { profile, pick: alertRows }),
        loadEnvelope(api, '/api/capabilities', { profile, allowEmpty: false }),
      ]);

    pulse = pulseResult;
    usage = usageResult;
    days = dailySeries(usageResult.data);
    brush = null;
    tasks = taskResult;
    permits = permitResult;
    issues = issueResult;
    alerts = alertResult;
    // The shell also pushes capabilities in via setSourceHealth; whichever
    // arrives last wins, and both carry the same envelope shape.
    sourceHealth = capResult;
    render();
  }

  // ------------------------------------------------------------------- tiles

  function selectedDays() {
    if (!brush) return days;
    return days.slice(brush[0], brush[1] + 1);
  }

  function renderStats() {
    clear(statsPane);
    const p = pulseRecord(pulse?.data);
    const window = selectedDays();
    const totals = sliceTotals(window);
    const costClass = classifyCost(p.cost_class);

    const activeTasks = (Array.isArray(tasks?.data) ? tasks.data : [])
      .filter((task) => ['running', 'in_progress', 'active'].includes(String(task.status || '').toLowerCase()));
    const openPermits = (Array.isArray(permits?.data) ? permits.data : [])
      .filter((permit) => String(permit.status || '').toLowerCase() === 'pending_approval');
    const openIssues = (Array.isArray(issues?.data) ? issues.data : [])
      .filter((issue) => String(issue.status || 'open').toLowerCase() === 'open');
    const activeAlerts = (Array.isArray(alerts?.data) ? alerts.data : [])
      .filter((alert) => String(alert.state || 'active').toLowerCase() === 'active');

    statsPane.append(createStatRow([
      {
        label: 'Running tasks', value: activeTasks.length, iconName: 'kanban',
        foot: `${(tasks?.data || []).length} tracked`,
        onClick: navigate ? () => navigate('kanban') : null,
      },
      {
        label: 'Pending permits', value: openPermits.length, iconName: 'permits',
        foot: openPermits.length ? 'awaiting a decision' : 'none waiting',
        onClick: navigate ? () => navigate('permits') : null,
      },
      {
        label: 'Open issues', value: openIssues.length, iconName: 'issues',
        foot: `${(issues?.data || []).length} in ledger`,
        onClick: navigate ? () => navigate('issues') : null,
      },
      {
        label: 'Active alerts', value: activeAlerts.length, iconName: 'alerts',
        foot: activeAlerts.length ? activeAlerts[0].severity || '' : 'all clear',
        onClick: navigate ? () => navigate('alerts') : null,
      },
      {
        label: 'Sessions', value: totals.sessions, iconName: 'sessions',
        spark: window.map((day) => day.sessions), seriesIndex: 1,
        foot: totals.days ? `${totals.days}d window` : '',
        onClick: navigate ? () => navigate('sessions') : null,
      },
      {
        label: 'API calls', value: compactNumber(totals.api_calls), iconName: 'activity',
        spark: window.map((day) => day.api_calls), seriesIndex: 3,
        foot: totals.days ? `${totals.days}d window` : '',
        onClick: navigate ? () => navigate('analytics') : null,
      },
      {
        label: 'Tokens', value: compactNumber(totals.total_tokens), iconName: 'analytics',
        spark: window.map((day) => day.total_tokens), seriesIndex: 5,
        foot: `${compactNumber(totals.input_tokens)} in / ${compactNumber(totals.output_tokens)} out`,
        onClick: navigate ? () => navigate('analytics') : null,
      },
      {
        label: 'Estimated cost', value: formatCost(totals.estimated_cost), iconName: 'analytics',
        foot: costLabel(costClass),
        onClick: navigate ? () => navigate('analytics') : null,
      },
    ]));
  }

  // ------------------------------------------------------------------- trend

  function renderTrend() {
    clear(trendPane);
    if (chartNode?.dispose) chartNode.dispose();
    chartNode = null;

    const head = el('div', { class: 'panel-head' }, [
      el('div', { class: 'panel-title', text: 'Token usage per day' }),
      provenanceBadge(usage?.meta),
      chartLegend(TOKEN_SERIES),
    ]);
    const body = el('div', { class: 'panel-body' });

    if (!days.length) {
      body.append(usage?.state === 'unavailable'
        ? unavailableState({ reason: usage.reason, requestId: usage.requestId })
        : emptyState({ title: 'No usage recorded', note: 'Analytics has no daily rows for this window.' }));
      trendPane.append(el('section', { class: 'panel' }, [head, body]));
      return;
    }

    chartNode = barChart({
      labels: days.map((day) => day.day.slice(5)),
      series: TOKEN_SERIES.map((spec) => ({ ...spec, values: days.map((day) => day[spec.key]) })),
      formatValue: (value) => compactNumber(Math.round(value)),
      ariaLabel: `Input and output tokens per day over ${days.length} days`,
      brush,
      onBrush: (from, to) => { brush = [from, to]; renderStats(); renderTrend(); },
      onSelect: () => { brush = null; renderStats(); renderTrend(); },
    });
    body.append(chartNode);

    const window = selectedDays();
    const totals = sliceTotals(window);
    const foot = el('div', { class: 'chart-foot' }, [
      el('span', {
        class: 'cell-dim',
        text: brush
          ? `Selected ${totals.from} → ${totals.to} (${totals.days}d)`
          : 'Drag across the chart to select a range; click once to clear.',
      }),
    ]);
    if (brush) {
      foot.append(el('button', {
        class: 'btn btn-sm', type: 'button', text: 'Clear',
        onclick: () => { brush = null; renderStats(); renderTrend(); },
      }));
      if (navigate) {
        foot.append(el('button', {
          class: 'btn btn-sm btn-accent', type: 'button', text: 'Open in Analytics',
          onclick: () => navigate('analytics', { days: String(totals.days), from: totals.from, to: totals.to }),
        }));
      }
    }
    body.append(foot);
    trendPane.append(el('section', { class: 'panel' }, [head, body]));
  }

  // ------------------------------------------------------------------- cards

  function healthCard() {
    const sources = capabilityRegistry(sourceHealth?.data);
    const health = summarizeSourceHealth(sourceHealth?.data);
    const body = el('div', { class: 'panel-body' });
    const entries = Object.entries(sources);
    if (!entries.length) {
      body.append(unavailableState({ reason: 'Source registry unavailable', requestId: sourceHealth?.meta?.request_id }));
    } else {
      const list = el('div', { class: 'section-list' });
      for (const [id, source] of entries) {
        list.append(el('div', { class: 'section-row section-row-flex' }, [
          el('span', { class: 'cell-strong mono', text: id }),
          statusChip(source?.healthy === true ? 'ok' : 'warn', source?.healthy === true ? 'healthy' : 'degraded'),
          el('span', {
            class: 'cell-dim',
            text: source?.last_checked_at
              ? fmtAge(new Date(source.last_checked_at * 1000).toISOString())
              : 'never probed',
          }),
          el('span', {
            class: 'cell-dim mono',
            title: (source?.routes_checked || []).join('\n'),
            text: (source?.routes_checked || [])[0] || source?.error || '',
          }),
        ]));
      }
      body.append(list);
    }
    return el('section', { class: 'panel' }, [
      el('div', { class: 'panel-head' }, [
        el('div', { class: 'panel-title', text: 'Source health' }),
        statusChip(health.state === 'healthy' ? 'ok' : health.state === 'degraded' ? 'warn' : 'idle', health.state),
        provenanceBadge(sourceHealth?.meta),
      ]),
      body,
    ]);
  }

  function pulseCard() {
    const p = pulseRecord(pulse?.data);
    const body = el('div', { class: 'panel-body' });
    if (!pulse || pulse.state === 'unavailable') {
      body.append(unavailableState({ reason: pulse?.reason || 'Pulse unavailable' }));
    } else {
      const list = el('div', { class: 'section-list' });
      const rows = [
        ['Events', p.event_count, 'activity'],
        ['Failures', p.failures, null],
        ['Active sessions', p.active_sessions, 'sessions'],
        ['Running tasks', p.running_tasks ?? p.active_tasks, 'kanban'],
        ['Pending permits', p.pending_permits, 'permits'],
        ['Open issues', p.open_issues, 'issues'],
      ];
      for (const [label, value, route] of rows) {
        const node = el(route && navigate ? 'button' : 'div', {
          class: 'section-row section-row-flex', type: route && navigate ? 'button' : null,
          onclick: route && navigate ? () => navigate(route) : null,
        }, [
          el('span', { class: 'cell-strong', text: label }),
          el('span', { class: 'cell-dim mono', text: value === undefined || value === null ? '—' : String(value) }),
        ]);
        list.append(node);
      }
      body.append(list);
    }
    return el('section', { class: 'panel' }, [
      el('div', { class: 'panel-head' }, [
        el('div', { class: 'panel-title', text: `Pulse · ${currentWindow}` }),
        provenanceBadge(pulse?.meta),
      ]),
      body,
    ]);
  }

  function listCard(title, rows, { renderRow, empty, route, meta }) {
    const body = el('div', { class: 'panel-body' });
    if (!rows) {
      body.append(unavailableState({ reason: `${title} source unavailable` }));
    } else if (!rows.length) {
      body.append(emptyState({ title: empty }));
    } else {
      const list = el('div', { class: 'section-list' });
      for (const row of rows.slice(0, 8)) list.append(renderRow(row));
      body.append(list);
    }
    return el('section', { class: 'panel' }, [
      el('div', { class: 'panel-head' }, [
        el('div', { class: 'panel-title', text: title }),
        rows ? el('span', { class: 'chip', text: String(rows.length) }) : null,
        provenanceBadge(meta, { empty: Array.isArray(rows) && rows.length === 0 }),
        route && navigate
          ? el('button', { class: 'btn btn-sm', type: 'button', text: 'Open', onclick: () => navigate(route) })
          : null,
      ].filter(Boolean)),
      body,
    ]);
  }

  function render() {
    renderStats();
    renderTrend();
    clear(gridPane);

    const activeTasks = (Array.isArray(tasks?.data) ? tasks.data : [])
      .filter((task) => ['running', 'in_progress', 'active'].includes(String(task.status || '').toLowerCase()));
    const activeAlerts = (Array.isArray(alerts?.data) ? alerts.data : []);
    const eventRows = listFrom(events?.data, ['events']);

    gridPane.append(
      healthCard(),
      pulseCard(),
      listCard('Running tasks', tasks?.data ? activeTasks : null, {
        meta: tasks?.meta,
        empty: 'No running tasks',
        route: 'kanban',
        renderRow: (task) => el('button', {
          class: 'section-row section-row-flex', type: 'button',
          onclick: () => navigate && navigate('run-inspector', { task: task.id }),
        }, [
          el('span', { class: 'cell-strong', text: task.title || task.id, title: task.title || '' }),
          statusChip('running', task.status || 'running'),
          el('span', { class: 'cell-dim', text: task.assignee || '' }),
        ]),
      }),
      listCard('Alerts', alerts?.data ? activeAlerts : null, {
        meta: alerts?.meta,
        empty: 'No alerts',
        route: 'alerts',
        renderRow: (alert) => el('button', {
          class: 'section-row section-row-flex', type: 'button',
          onclick: () => navigate && navigate('alerts'),
        }, [
          el('span', { class: 'cell-strong', text: alert.title || alert.reason || alert.rule_id }),
          statusChip(alert.severity === 'critical' ? 'danger' : alert.severity === 'warning' ? 'warn' : 'info',
            alert.severity || 'info'),
          el('span', { class: 'cell-dim mono', text: alert.entity_id || alert.source_id || '' }),
        ]),
      }),
      listCard('Recent events', eventRows.length ? eventRows : (events ? [] : null), {
        meta: events?.meta,
        empty: 'No recent events',
        route: 'activity',
        renderRow: (event) => el('div', { class: 'section-row section-row-flex' }, [
          el('span', { class: 'cell-strong', text: event.event_type || event.type || 'event' }),
          el('span', { class: 'cell-dim mono', text: event.entity_id || '' }),
          el('span', { class: 'cell-dim', text: fmtAge(event.occurred_at || event.created_at) }),
        ]),
      }),
    );

    renderToolbar(toolbar);
  }

  function renderToolbar(host) {
    if (!host) return;
    paint(host, tabToolbar({
      title: 'Overview',
      subtitle: 'fleet state, spend trend and everything currently waiting on a human',
      filters: [segmented(PULSE_WINDOWS.map((value) => ({ value, label: value })), {
        value: currentWindow,
        ariaLabel: 'Pulse window',
        onChange: (next) => { currentWindow = next; load().catch(() => null); },
      })],
      onRefresh: () => load(),
      meta: pulse?.meta,
    }));
  }

  function setSourceHealth(envelope) {
    if (!envelope) return;
    sourceHealth = { data: envelope.data, meta: envelope.meta, state: 'ready' };
    if (root.isConnected) render();
  }

  function setEvents(envelope) {
    events = envelope;
    if (root.isConnected) render();
  }

  function renderInspector(container) {
    inspectorHost = container;
    if (!inspectorHost) return;
    const health = summarizeSourceHealth(sourceHealth?.data);
    const p = pulseRecord(pulse?.data);
    paint(inspectorHost, sideHint('At a glance', [
      'Overview reads across every source at once, so it is the fastest way to spot which subsystem is degraded before opening its tab.',
      `Sources: ${health.state}${health.unhealthyCount ? ` — ${health.unhealthyCount} degraded` : ' — all reporting'}.`,
      `${p.running_tasks ?? p.active_tasks ?? 0} running tasks · ${p.pending_permits ?? 0} pending permits · ${p.open_issues ?? 0} open issues.`,
      'Drag a window across the Pulse strip to scope Activity and Analytics to that range.',
    ]));
  }

  return {
    mount(container) { clear(container); container.append(root); },
    activate() {
      renderToolbar(toolbar);
      return load().catch((err) => {
        clear(statsPane);
        statsPane.append(errorPanel({ message: err.message, requestId: err.request_id, onRetry: load }));
      });
    },
    deactivate() {
      if (chartNode?.dispose) chartNode.dispose();
      return { scroll: root.scrollTop || 0, window: currentWindow };
    },
    renderInspector,
    setSourceHealth,
    setEvents,
    refresh: load,
    renderToolbar,
    get pulseWindow() { return currentWindow; },
    get data() { return { pulse: pulse?.data || null, days }; },
  };
}
