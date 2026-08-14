// Analytics / Spend — token and cost accounting across days, models, tasks,
// tools and skills.
//
// The previous version read `data.input_tokens` / `data.total_tokens` off the
// usage payload. Neither field exists: `/api/analytics/usage` returns `{daily,
// by_model, by_task, totals, skills, tools}`. So the tab showed four em dashes
// over a working data source. Everything here is keyed off the real shape via
// `pure/analytics-shape.js`.
//
// Cost discipline is unchanged and deliberate: every figure carries its
// 4-class origin label, and an estimate is never presented as an actual.

import {
  el, clear, skeleton, statusChip, segmented, emptyState, unavailableState, fmtAge,
} from '../ui.js';
import { provenanceBadge } from '../provenance.js';
import { classifyCost, costLabel } from '../pure/cost-classify.js';
import { createStatRow } from '../components/stat.js';
import { barChart, chartLegend } from '../components/chart.js';
import { createTable } from '../components/table.js';
import {
  dailySeries, sliceTotals, modelRows, taskRowsFromUsage, toolRows, skillUsage,
  compactNumber, formatCost,
} from '../pure/analytics-shape.js';
import { loadEnvelope, tabToolbar, paint, sideHint } from './_kit.js';

export const ROUTE = 'analytics';
export const LABEL = 'Analytics / Spend';

export const PERIODS = Object.freeze([7, 14, 30, 90]);

const TOKEN_SERIES = Object.freeze([
  { key: 'input_tokens', label: 'Input', seriesIndex: 1 },
  { key: 'output_tokens', label: 'Output', seriesIndex: 5 },
]);

const VIEWS = Object.freeze([
  { value: 'models', label: 'Models' },
  { value: 'tasks', label: 'Tasks' },
  { value: 'tools', label: 'Tools' },
  { value: 'skills', label: 'Skills' },
]);

export function createAnalytics({ api, profile, toolbar, onNavigate: navigate }) {
  const root = el('div', { class: 'tab tab-analytics' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(main);

  const statsPane = el('div');
  const trendPane = el('div');
  const breakdownPane = el('div');
  main.append(statsPane, trendPane, breakdownPane);

  let period = 30;
  let view = 'models';
  let usage = null;
  let models = null;
  let days = [];
  let range = null; // [fromDay, toDay] carried in from an Overview brush
  let chartNode = null;
  let selectedRow = null;

  function windowDays() {
    if (!range) return days;
    return days.filter((day) => day.day >= range[0] && day.day <= range[1]);
  }

  async function load() {
    clear(statsPane);
    clear(trendPane);
    clear(breakdownPane);
    statsPane.append(skeleton({ lines: 3 }));

    const [usageResult, modelResult] = await Promise.all([
      loadEnvelope(api, `/api/upstream/api/analytics/usage?days=${period}`, { profile, allowEmpty: false }),
      loadEnvelope(api, `/api/upstream/api/analytics/models?days=${period}`, { profile, allowEmpty: false }),
    ]);
    usage = usageResult;
    models = modelResult;
    days = dailySeries(usageResult.data);
    render();
  }

  // ------------------------------------------------------------------- tiles

  function renderStats() {
    clear(statsPane);
    const window = windowDays();
    const totals = sliceTotals(window);
    // Hermes reports estimated and actual separately; the class tells the
    // operator which one they are looking at, so it is derived from which
    // number is non-zero rather than assumed.
    const costClass = classifyCost(totals.actual_cost > 0 ? 'actual' : 'estimated');

    statsPane.append(createStatRow([
      {
        label: 'Input tokens', value: compactNumber(totals.input_tokens), seriesIndex: 1,
        spark: window.map((day) => day.input_tokens),
        foot: `${totals.days}d${range ? ' (selected)' : ''}`,
      },
      {
        label: 'Output tokens', value: compactNumber(totals.output_tokens), seriesIndex: 5,
        spark: window.map((day) => day.output_tokens),
        foot: `${totals.days}d${range ? ' (selected)' : ''}`,
      },
      {
        label: 'Cache reads', value: compactNumber(totals.cache_read_tokens), seriesIndex: 6,
        spark: window.map((day) => day.cache_read_tokens),
        foot: 'not billed as input',
      },
      {
        label: 'Reasoning', value: compactNumber(totals.reasoning_tokens), seriesIndex: 4,
        spark: window.map((day) => day.reasoning_tokens),
        foot: 'included in output',
      },
      {
        label: 'Sessions', value: totals.sessions, seriesIndex: 3,
        spark: window.map((day) => day.sessions),
        onClick: navigate ? () => navigate('sessions') : null,
      },
      {
        label: 'API calls', value: compactNumber(totals.api_calls), seriesIndex: 2,
        spark: window.map((day) => day.api_calls),
      },
      {
        label: 'Estimated cost', value: formatCost(totals.estimated_cost),
        foot: costLabel(classifyCost('estimated')),
      },
      {
        label: 'Actual cost', value: formatCost(totals.actual_cost),
        foot: costLabel(costClass),
      },
    ]));
  }

  // ------------------------------------------------------------------- trend

  function renderTrend() {
    clear(trendPane);
    if (chartNode?.dispose) chartNode.dispose();
    chartNode = null;

    const body = el('div', { class: 'panel-body' });
    if (!days.length) {
      body.append(usage?.state === 'unavailable'
        ? unavailableState({ reason: usage.reason, requestId: usage.requestId })
        : emptyState({ title: 'No daily usage rows', note: `Analytics reported nothing for the last ${period} days.` }));
    } else {
      const brushIndexes = range
        ? [days.findIndex((d) => d.day === range[0]), days.findIndex((d) => d.day === range[1])]
        : null;
      chartNode = barChart({
        labels: days.map((day) => day.day.slice(5)),
        series: TOKEN_SERIES.map((spec) => ({ ...spec, values: days.map((day) => day[spec.key]) })),
        formatValue: (value) => compactNumber(Math.round(value)),
        ariaLabel: `Input and output tokens per day over ${days.length} days`,
        brush: brushIndexes && brushIndexes[0] >= 0 ? brushIndexes : null,
        onBrush: (from, to) => { range = [days[from].day, days[to].day]; render(); },
        onSelect: () => { range = null; render(); },
      });
      body.append(chartNode);
      body.append(el('div', { class: 'chart-foot' }, [
        el('span', {
          class: 'cell-dim',
          text: range
            ? `Selected ${range[0]} → ${range[1]}`
            : 'Drag across the chart to select a range; click once to clear.',
        }),
        range
          ? el('button', {
            class: 'btn btn-sm', type: 'button', text: 'Clear',
            onclick: () => { range = null; render(); },
          })
          : null,
      ].filter(Boolean)));
    }

    trendPane.append(el('section', { class: 'panel' }, [
      el('div', { class: 'panel-head' }, [
        el('div', { class: 'panel-title', text: 'Token usage per day' }),
        provenanceBadge(usage?.meta),
        chartLegend(TOKEN_SERIES),
      ]),
      body,
    ]));
  }

  // --------------------------------------------------------------- breakdown

  function modelTable() {
    const rows = modelRows(models?.data).length
      ? modelRows(models?.data)
      : modelRows(usage?.data);
    const table = createTable({
      rowId: (row) => row.model,
      columns: [
        { key: 'model', label: 'Model', width: '1.4fr' },
        { key: 'provider', label: 'Provider', width: '0.8fr', render: (row) => row.provider || '—' },
        { key: 'total_tokens', label: 'Tokens', align: 'right', sortable: true, render: (row) => compactNumber(row.total_tokens) },
        { key: 'api_calls', label: 'Calls', align: 'right', sortable: true, render: (row) => compactNumber(row.api_calls) },
        { key: 'sessions', label: 'Sessions', align: 'right', sortable: true },
        {
          key: 'estimated_cost', label: 'Cost', align: 'right',
          render: (row) => el('span', { class: 'cell-stack' }, [
            el('span', { class: 'cell-strong mono', text: formatCost(row.actual_cost || row.estimated_cost) }),
            el('span', { class: 'cell-dim', text: costLabel(classifyCost(row.actual_cost > 0 ? 'actual' : 'estimated')) }),
          ]),
        },
        {
          key: 'last_used_at', label: 'Last used', align: 'right',
          render: (row) => (row.last_used_at
            ? fmtAge(new Date(Number(row.last_used_at) * 1000).toISOString())
            : '—'),
        },
      ],
      emptyTitle: 'No model usage',
      onSelect: (row) => { selectedRow = { kind: 'model', row }; renderSide(); },
    });
    table.setRows(rows);
    return table.node;
  }

  function taskTable() {
    const rows = taskRowsFromUsage(usage?.data);
    const table = createTable({
      rowId: (row) => row.task,
      columns: [
        { key: 'task', label: 'Task kind', width: '1.4fr' },
        { key: 'total_tokens', label: 'Tokens', align: 'right', sortable: true, render: (row) => compactNumber(row.total_tokens) },
        { key: 'api_calls', label: 'Calls', align: 'right', sortable: true, render: (row) => compactNumber(row.api_calls) },
        {
          key: 'models', label: 'Models', width: '1.6fr',
          render: (row) => el('span', { class: 'taglist-static', text: row.models.join(', ') || '—' }),
        },
      ],
      emptyTitle: 'No task breakdown',
      onSelect: (row) => { selectedRow = { kind: 'task', row }; renderSide(); },
    });
    table.setRows(rows);
    return table.node;
  }

  function toolPanel() {
    const rows = toolRows(usage?.data);
    if (!rows.length) return emptyState({ title: 'No tool usage recorded' });
    // A share-of-total bar per row: one measure, one axis, ranked — no pie.
    const list = el('div', { class: 'section-list' });
    for (const row of rows.slice(0, 20)) {
      list.append(el('div', { class: 'bar-row' }, [
        el('span', { class: 'cell-strong', text: row.tool }),
        el('span', { class: 'bar-track' }, [
          el('span', { class: 'bar-fill chart-series-1', style: `width:${Math.max(1, row.percentage).toFixed(1)}%` }),
        ]),
        el('span', { class: 'cell-dim mono', text: `${compactNumber(row.count)} · ${row.percentage.toFixed(1)}%` }),
      ]));
    }
    return list;
  }

  function skillPanel() {
    const skills = skillUsage(usage?.data);
    if (!skills.top.length && !skills.totalActions) return emptyState({ title: 'No skill usage recorded' });
    const list = el('div', { class: 'section-list' });
    for (const row of skills.top.slice(0, 20)) {
      list.append(el('button', {
        class: 'section-row', type: 'button',
        onclick: () => navigate && navigate('skills', { skill: row.skill }),
      }, [
        el('span', { class: 'cell-strong', text: row.skill }),
        el('span', { class: 'cell-dim mono', text: `${row.views} views` }),
        el('span', { class: 'cell-dim mono', text: row.manages ? `${row.manages} edits` : '' }),
      ]));
    }
    return el('div', { class: 'stack-sm' }, [
      el('div', { class: 'inline-chips' }, [
        el('span', { class: 'chip', text: `${skills.distinct} distinct` }),
        el('span', { class: 'chip', text: `${skills.totalLoads} loads` }),
        el('span', { class: 'chip', text: `${skills.totalEdits} edits` }),
      ]),
      list,
    ]);
  }

  function renderBreakdown() {
    clear(breakdownPane);
    const body = el('div', { class: 'panel-body' });
    if (view === 'models') body.append(modelTable());
    else if (view === 'tasks') body.append(taskTable());
    else if (view === 'tools') body.append(toolPanel());
    else body.append(skillPanel());

    breakdownPane.append(el('section', { class: 'panel' }, [
      el('div', { class: 'panel-head' }, [
        el('div', { class: 'panel-title', text: 'Breakdown' }),
        segmented(VIEWS, {
          value: view,
          ariaLabel: 'Breakdown dimension',
          onChange: (next) => { view = next; selectedRow = null; renderBreakdown(); renderSide(); },
        }),
        provenanceBadge(view === 'models' ? models?.meta : usage?.meta),
      ]),
      body,
    ]));
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (!selectedRow) {
      paint(inspectorHost, sideHint('Spend accounting', [
        `Totals cover the last ${period} days${range ? `, narrowed to ${range[0]} → ${range[1]}` : ''}.`,
        'Cache reads are shown separately because they are not billed as input tokens.',
        'Every cost carries its origin: provider-reported, Hermes-calculated, estimated from a verified rate, or unavailable.',
      ]));
      return;
    }
    const { kind, row } = selectedRow;
    const fields = kind === 'model'
      ? [
        { label: 'Model', value: row.model, mono: true },
        { label: 'Provider', value: row.provider },
        { label: 'Input tokens', value: row.input_tokens.toLocaleString(), mono: true },
        { label: 'Output tokens', value: row.output_tokens.toLocaleString(), mono: true },
        { label: 'Cache reads', value: row.cache_read_tokens.toLocaleString(), mono: true },
        { label: 'Reasoning', value: row.reasoning_tokens.toLocaleString(), mono: true },
        { label: 'Sessions', value: String(row.sessions) },
        { label: 'API calls', value: row.api_calls.toLocaleString(), mono: true },
        { label: 'Tool calls', value: row.tool_calls.toLocaleString(), mono: true },
        { label: 'Avg tokens/session', value: Math.round(row.avg_tokens_per_session).toLocaleString(), mono: true },
        { label: 'Estimated cost', value: formatCost(row.estimated_cost), mono: true },
        { label: 'Actual cost', value: formatCost(row.actual_cost), mono: true },
      ]
      : [
        { label: 'Task kind', value: row.task, mono: true },
        { label: 'Input tokens', value: row.input_tokens.toLocaleString(), mono: true },
        { label: 'Output tokens', value: row.output_tokens.toLocaleString(), mono: true },
        { label: 'API calls', value: row.api_calls.toLocaleString(), mono: true },
        { label: 'Models', value: row.models.join(', ') },
      ];

    const dl = el('dl', { class: 'detail-dl' });
    for (const field of fields) {
      if (field.value === null || field.value === undefined || field.value === '') continue;
      dl.append(el('dt', { class: 'detail-dt', text: field.label }));
      dl.append(el('dd', { class: `detail-dd${field.mono ? ' mono' : ''}`, text: String(field.value) }));
    }
    paint(inspectorHost, el('div', { class: 'detail' }, [
      el('div', { class: 'detail-head' }, [
        el('div', { class: 'detail-title', text: kind === 'model' ? row.model : row.task }),
        el('div', { class: 'detail-chips' }, [
          statusChip('accent', kind),
          el('span', { class: 'chip', text: `${compactNumber(row.total_tokens)} tokens` }),
        ]),
      ]),
      dl,
    ]));
  }

  function render() {
    renderStats();
    renderTrend();
    renderBreakdown();
    renderSide();
    renderToolbar(toolbar);
  }

  function renderToolbar(host) {
    if (!host) return;
    paint(host, tabToolbar({
      title: 'Analytics / Spend',
      subtitle: range
        ? `${range[0]} → ${range[1]} of the last ${period} days`
        : `last ${period} days`,
      filters: [segmented(PERIODS.map((value) => ({ value: String(value), label: `${value}d` })), {
        value: String(period),
        ariaLabel: 'Period',
        onChange: (next) => { period = Number(next); range = null; load().catch(() => null); },
      })],
      onRefresh: () => load(),
      meta: usage?.meta,
    }));
  }

  return {
    mount(container) { clear(container); container.append(root); },
    renderInspector,
    activate(params = {}) {
      // The Overview's brush deep-links here with the exact window it selected.
      if (params.days) {
        const wanted = Number(params.days);
        period = PERIODS.find((value) => value >= wanted) || PERIODS[PERIODS.length - 1];
      }
      range = params.from && params.to ? [params.from, params.to] : null;
      renderToolbar(toolbar);
      return load().catch(() => render());
    },
    deactivate() {
      if (chartNode?.dispose) chartNode.dispose();
      return { period, view };
    },
    refresh: load,
    renderToolbar,
    get data() { return { days, models: modelRows(models?.data) }; },
  };
}
