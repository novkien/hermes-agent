// Logs — bounded read-only Hermes log viewer.

import { el, clear, skeleton, emptyState, unavailableState, errorPanel } from '../ui.js';
import { provenanceBadge } from '../provenance.js';
import { listRows } from '../pure/envelope-list.js';
import { isSafeManagedPath } from '../pure/path-guard.js';
import { readOnlyBadge } from '../pure/capability-badge.js';
import { logLines, recordFrom } from '../pure/data-shape.js';
import { filterInput, sideHint, paint, tabToolbar } from './_kit.js';
import { filterRows, filterSummary } from '../pure/text-filter.js';

export const ROUTE = 'logs';
export const LABEL = 'Logs';
export const GROUP = 'SYSTEM';
export const READ_ONLY_NOTE = readOnlyBadge('logs');
export const SOURCE_ENDPOINTS = Object.freeze(['/api/logs']);
export const MAX_LINES = 500;
export const FILTERS = Object.freeze({ lines: 200, level: 'all' });

export function boundedLines(requested) {
  const value = Number(requested);
  if (!Number.isFinite(value) || value <= 0) return 100;
  return Math.min(Math.floor(value), MAX_LINES);
}

export function renderLogs(envelope, { path = null } = {}) {
  if (path != null && !isSafeManagedPath('/api/logs', path)) {
    return {
      rows: [],
      meta: envelope?.meta || null,
      state: 'unsupported',
      guard: 'path rejected: unrestricted filesystem path',
    };
  }
  const result = listRows(envelope, {
    pick: (raw) => Array.isArray(raw) ? raw : raw.lines || raw.logs || raw.items || null,
    map: (line) => typeof line === 'string'
      ? { id: null, message: line }
      : {
        id: line.id ?? null,
        ts: line.timestamp ?? line.ts ?? line.time ?? null,
        level: line.level ?? line.severity ?? null,
        message: line.message ?? line.msg ?? JSON.stringify(line),
        source: line.source ?? line.logger ?? null,
      },
  });
  if (path != null) result.guard = 'ok';
  return result;
}

export function createLogs({ api, profile }) {
  const root = el('div', { class: 'tab tab-logs' });
  const toolbar = el('div', { class: 'tab-toolbar' });
  const body = el('div', { class: 'logs-body' });
  root.append(toolbar, body);

  let envelope = null;
  let lineLimit = 200;
  let inspectorHost = null;
  // `FILTERS.level` has been exported by this module since it was written and
  // nothing ever read it: the tab tailed the log and showed every line, so
  // finding one error in a 500-line dump meant reading a 500-line dump. Levels
  // are matched against the line text because the managed tail arrives as
  // plain strings, not structured records.
  const filters = { level: 'all', query: '' };

  const LEVELS = Object.freeze(['all', 'error', 'warning', 'info', 'debug']);
  const LEVEL_PATTERNS = Object.freeze({
    error: /\b(error|critical|fatal|exception|traceback)\b/i,
    warning: /\b(warn|warning)\b/i,
    info: /\binfo\b/i,
    debug: /\bdebug\b/i,
  });

  function visibleLines() {
    const all = logLines(envelope?.data) || [];
    const pattern = LEVEL_PATTERNS[filters.level];
    const levelled = pattern ? all.filter((line) => pattern.test(line)) : all;
    return filterRows(levelled, filters.query, [(line) => line]);
  }

  function renderToolbar() {
    const select = el('select', {
      class: 'select',
      'aria-label': 'Log line limit',
      onchange: (event) => {
        lineLimit = boundedLines(event.target.value);
        load();
      },
    });
    for (const value of [100, 200, 500]) {
      const option = el('option', { value: String(value), text: `${value} lines` });
      option.selected = value === lineLimit;
      select.append(option);
    }
    const levelSelect = el('select', {
      class: 'select',
      'aria-label': 'Log level',
      onchange: (event) => { filters.level = event.target.value; render(); },
    });
    for (const value of LEVELS) {
      const option = el('option', { value, text: value === 'all' ? 'Level: all' : value });
      option.selected = filters.level === value;
      levelSelect.append(option);
    }
    const all = logLines(envelope?.data) || [];
    const shown = envelope ? visibleLines().length : 0;
    paint(toolbar, tabToolbar({
      title: 'Logs',
      subtitle: envelope ? filterSummary(shown, all.length, 'line') : 'bounded, read-only tail',
      filters: [select, levelSelect, filterInput({
        value: filters.query,
        placeholder: 'Grep the tail…',
        ariaLabel: 'Filter log lines',
        onChange: (value) => {
          if (value === filters.query) return;
          filters.query = value;
          render();
        },
      })],
      actions: [el('button', {
        class: 'btn btn-sm', type: 'button', text: 'Export',
        title: 'Download exactly the lines currently shown',
        onclick: exportVisible,
      })],
      meta: envelope?.meta || null,
      onRefresh: () => load(),
    }));
  }

  async function load() {
    renderToolbar();
    clear(body);
    body.append(skeleton({ lines: 8 }));
    try {
      envelope = await api.get(`/api/logs?lines=${lineLimit}`, { profile });
    } catch (err) {
      envelope = null;
      clear(body);
      body.append(errorPanel({ message: err.message, requestId: err.request_id, onRetry: load }));
      return;
    }
    render();
  }

  function render() {
    clear(body);
    if (!envelope) {
      body.append(unavailableState({ reason: 'Log source unavailable' }));
      return;
    }
    const payload = recordFrom(envelope.data) || {};
    const all = logLines(envelope.data);
    const lines = visibleLines();
    renderToolbar();
    body.append(el('div', { class: 'logs-meta' }, [
      provenanceBadge(envelope.meta, { empty: lines.length === 0 }),
      el('span', { class: 'mono', text: payload.file || 'managed Hermes log' }),
      el('span', { class: 'mono', text: filterSummary(lines.length, all.length, 'line') }),
    ]));
    if (!lines.length) {
      body.append(all.length
        ? emptyState({
          title: 'No line matches',
          note: `${all.length} lines were returned; none match the current level and filter.`,
        })
        : emptyState({ title: 'No log lines returned' }));
      return;
    }
    body.append(el('pre', {
      class: 'mono log-output',
      text: lines.join('\n'),
      tabindex: '0',
      'aria-label': 'Hermes log output',
    }));
  }

  function exportVisible() {
    if (!envelope) return;
    // Named exportVisible and it exported everything. With a level and a filter
    // in play, "visible" finally means visible.
    const text = visibleLines().join('\n');
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `hermes-logs-${new Date().toISOString().replace(/[:.]/g, '-')}.txt`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function renderInspector(container) {
    inspectorHost = container;
    if (!inspectorHost) return;
    const lines = logLines(envelope?.data) || [];
    paint(inspectorHost, sideHint('Logs', [
      'A bounded, read-only tail of the Hermes gateway log. There is no write surface here by design.',
      `Showing at most ${lineLimit} lines; ${lines.length} returned, ${visibleLines().length} after the level and filter.`,
      'Raise or lower the line budget from the toolbar. The cap is deliberate — an unbounded tail would stall the BFF.',
    ]));
  }

  return {
    mount(container) {
      clear(container);
      container.append(root);
    },
    activate() {
      return load();
    },
    deactivate() {
      return { scroll: root.scrollTop || 0 };
    },
    renderInspector,
    refresh: load,
  };
}
