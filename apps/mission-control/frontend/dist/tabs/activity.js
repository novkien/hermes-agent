// Activity — the operational event feed: what the upstream sources did.
//
// Deliberately distinct from Action Audit, which records what an operator asked
// *this* BFF to do. The previous version conflated the two: it read `/api/audit`
// and then called `.filter` on the envelope's `{items, total}` object, so the
// tab threw on every load. It now reads the event-bus replay buffer
// (`/api/events/recent`) for history and takes live updates from the same SSE
// stream that buffer replays, so the two never disagree.

import {
  el, clear, skeleton, statusChip, segmented, emptyState, unavailableState, fmtTime, fmtAge,
} from '../ui.js';
import { createDetail } from '../components/detail.js';
import {
  filterInput, loadEnvelope, paint, sideHint, tabToolbar,
} from './_kit.js';
import { filterRows, filterSummary } from '../pure/text-filter.js';

export const ROUTE = 'activity';
export const LABEL = 'Activity';
export const GROUP = 'OPERATE';

export const COVERAGE_LEVELS = Object.freeze(['native', 'polled', 'derived', 'partial']);

const COVERAGE_TONE = { native: 'ok', polled: 'info', derived: 'warn', partial: 'warn' };

const TYPE_TONE = {
  'task.changed': 'info', 'run.changed': 'accent', 'permit.changed': 'warn',
  'issue.changed': 'danger', 'cron.changed': 'info', 'health.changed': 'warn',
  'capabilities.changed': 'idle', 'room_binding.changed': 'info',
};

/** Pure: normalize one replay/SSE frame into the row shape the feed renders. */
export function eventRow(event) {
  const occurred = event.occurred_at ?? event.ts ?? event.timestamp ?? null;
  const iso = typeof occurred === 'number'
    ? new Date(occurred * 1000).toISOString()
    : (occurred || null);
  return {
    id: event.event_id || event.id || `${event.event_type}:${event.entity_id}:${occurred}`,
    type: event.event_type || event.type || 'event',
    source: event.source_id || 'unknown',
    entityType: event.entity_type || '',
    entityId: event.entity_id != null ? String(event.entity_id) : '',
    coverage: event.coverage || 'unknown',
    payload: event.payload && typeof event.payload === 'object' ? event.payload : {},
    iso,
    sort: iso ? Date.parse(iso) : 0,
  };
}

/** Pure: newest first, de-duplicated by event id. */
export function mergeEvents(existing, incoming) {
  const byId = new Map();
  for (const row of [...incoming, ...existing]) {
    if (!byId.has(row.id)) byId.set(row.id, row);
  }
  return [...byId.values()].sort((a, b) => b.sort - a.sort);
}

export function createActivity({ api, profile, sse, toolbar, onNavigate: navigate }) {
  const root = el('div', { class: 'tab tab-activity' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(main);

  const feedPane = el('div');
  main.append(feedPane);

  const MAX_ROWS = 300;
  let rows = [];
  let meta = null;
  let state = 'loading';
  let reason = '';
  let selected = null;
  let unsubscribe = null;
  const filters = { source: '', coverage: '', type: '', query: '' };
  // The three dropdowns narrow by facet; this narrows by content. An event feed
  // without a text filter means scrolling 300 rows to find the one that
  // mentions a session id you already know.
  const SEARCH_FIELDS = ['type', 'source', 'title', 'summary', 'detail', 'id', 'entity_id', 'coverage'];

  function sources() {
    return [...new Set(rows.map((row) => row.source))].sort();
  }

  function types() {
    return [...new Set(rows.map((row) => row.type))].sort();
  }

  function visible() {
    const faceted = rows.filter((row) =>
      (!filters.source || row.source === filters.source)
      && (!filters.coverage || row.coverage === filters.coverage)
      && (!filters.type || row.type === filters.type));
    return filterRows(faceted, filters.query, SEARCH_FIELDS);
  }

  function selectOne(label, value, options, onChange) {
    const node = el('select', {
      class: 'select input-sm', 'aria-label': label,
      onchange: (event) => onChange(event.target.value),
    });
    node.append(el('option', { value: '', text: label }));
    for (const option of options) {
      const item = el('option', { value: option, text: option });
      if (option === value) item.setAttribute('selected', 'selected');
      node.append(item);
    }
    return node;
  }

  function renderFeed() {
    clear(feedPane);
    const list = visible();

    const body = el('div', { class: 'panel-body' });
    if (state === 'unavailable') {
      body.append(unavailableState({ reason, requestId: meta?.request_id }));
    } else if (!rows.length) {
      body.append(emptyState({
        title: 'No events buffered',
        note: 'The event bus replay buffer is empty. New events appear here live as the source workers publish them.',
      }));
    } else if (!list.length) {
      body.append(emptyState({ title: 'No events match these filters' }));
    } else {
      const track = el('ol', { class: 'timeline' });
      for (const row of list.slice(0, MAX_ROWS)) {
        const isSelected = selected && selected.id === row.id;
        track.append(el('li', { class: `timeline-item${isSelected ? ' is-selected' : ''}` }, [
          el('div', { class: 'timeline-marker', 'aria-hidden': 'true' }),
          el('button', {
            class: 'timeline-body timeline-button', type: 'button',
            onclick: () => { selected = row; renderFeed(); renderSide(); },
          }, [
            el('div', { class: 'timeline-head' }, [
              statusChip(TYPE_TONE[row.type] || 'idle', row.type),
              el('span', { class: 'cell-dim mono', text: `${row.entityType} ${row.entityId}`.trim() }),
              statusChip(COVERAGE_TONE[row.coverage] || 'unknown', row.coverage),
              el('span', { class: 'timeline-when mono', text: row.iso ? fmtAge(row.iso) : '' }),
            ]),
            Object.keys(row.payload).length
              ? el('div', { class: 'timeline-payload mono', text: JSON.stringify(row.payload).slice(0, 220) })
              : null,
          ].filter(Boolean)),
        ]));
      }
      body.append(track);
    }

    feedPane.append(el('section', { class: 'panel' }, [
      el('div', { class: 'panel-head' }, [
        el('div', { class: 'panel-title', text: 'Event feed' }),
        el('span', { class: 'chip', text: `${list.length}${list.length === rows.length ? '' : ` of ${rows.length}`}` }),
        sse ? statusChip('ok', 'live') : null,
      ].filter(Boolean)),
      body,
    ]));
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (!selected) {
      paint(inspectorHost, sideHint('Operational events', [
        'This is what the upstream sources did — task transitions, run completions, permit and issue changes, health flips.',
        'Action Audit is the other half: what an operator asked this dashboard to do.',
        'History comes from the event-bus replay buffer, and new frames arrive on the same SSE stream.',
      ]));
      return;
    }
    const row = selected;
    const relations = [];
    if (navigate && row.entityType === 'task' && row.entityId) {
      relations.push({ label: 'Inspect', text: row.entityId, onClick: () => navigate('run-inspector', { task: row.entityId }) });
      relations.push({ label: 'Board', text: 'Kanban', onClick: () => navigate('kanban', { task: row.entityId }) });
    }
    if (navigate && row.entityType === 'permit') {
      relations.push({ label: 'Permit', text: row.entityId, onClick: () => navigate('permits', { permit: row.entityId }) });
    }
    if (navigate && row.entityType === 'issue') {
      relations.push({ label: 'Issue', text: row.entityId, onClick: () => navigate('issues', { issue: row.entityId }) });
    }
    paint(inspectorHost, createDetail({
      title: row.type,
      chips: [
        statusChip(COVERAGE_TONE[row.coverage] || 'unknown', row.coverage),
        el('span', { class: 'chip', text: row.source }),
      ],
      fields: [
        { label: 'Event id', value: row.id, mono: true },
        { label: 'Source', value: row.source },
        { label: 'Entity', value: `${row.entityType} ${row.entityId}`.trim(), mono: true },
        { label: 'Occurred', value: row.iso ? fmtTime(row.iso) : null, mono: true },
        { label: 'Coverage', value: row.coverage },
      ],
      relations,
      raw: row.payload,
    }));
  }

  async function load() {
    clear(feedPane);
    feedPane.append(skeleton({ lines: 8 }));
    const result = await loadEnvelope(api, '/api/events/recent?limit=300', {
      profile,
      pick: (raw) => (Array.isArray(raw) ? raw : raw?.events || []),
      allowEmpty: false,
    });
    meta = result.meta;
    state = result.state === 'ready' ? 'ready' : result.state;
    reason = result.reason;
    rows = (Array.isArray(result.data) ? result.data : []).map(eventRow)
      .sort((a, b) => b.sort - a.sort);
    renderFeed();
    renderToolbar(toolbar);
    renderSide();
  }

  function renderToolbar(host) {
    if (!host) return;
    paint(host, tabToolbar({
      title: 'Activity',
      subtitle: rows.length
        ? `${filterSummary(visible().length, rows.length, 'event')} published by the source workers`
        : 'operational events published by the source workers',
      filters: [
        selectOne('source: all', filters.source, sources(), (value) => { filters.source = value; renderFeed(); renderToolbar(host); }),
        selectOne('type: all', filters.type, types(), (value) => { filters.type = value; renderFeed(); renderToolbar(host); }),
        segmented([{ value: '', label: 'all' }, ...COVERAGE_LEVELS.map((value) => ({ value, label: value }))], {
          value: filters.coverage,
          ariaLabel: 'Coverage filter',
          onChange: (value) => { filters.coverage = value; renderFeed(); renderToolbar(host); },
        }),
        filterInput({
          value: filters.query,
          placeholder: 'Search events…',
          ariaLabel: 'Search the activity feed',
          onChange: (value) => {
            if (value === filters.query) return;
            filters.query = value;
            renderFeed();
            renderToolbar(host);
          },
        }),
      ],
      onRefresh: () => load(),
      meta,
    }));
  }

  function bindEvents() {
    if (!sse || unsubscribe) return;
    unsubscribe = sse.on('*', (event) => {
      if (!root.isConnected || !event) return;
      rows = mergeEvents(rows, [eventRow(event)]).slice(0, MAX_ROWS);
      renderFeed();
      renderToolbar(toolbar);
    });
  }

  return {
    mount(container) { clear(container); container.append(root); },
    renderInspector,
    activate() {
      bindEvents();
      renderToolbar(toolbar);
      return load();
    },
    deactivate() {
      if (unsubscribe) { unsubscribe(); unsubscribe = null; }
      return { selected: selected?.id || null };
    },
    refresh: load,
    renderToolbar,
    get data() { return rows; },
  };
}
