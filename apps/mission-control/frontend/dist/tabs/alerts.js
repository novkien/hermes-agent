// Alerts — local rule-engine state with acknowledge and bounded snooze actions.

import {
  el, clear, closeMenu, skeleton, emptyState, openMenu, unavailableState, errorPanel, fmtTime,
} from '../ui.js';
import { provenanceBadge } from '../provenance.js';
import { sortAlerts } from '../pure/alert-sort.js';
import { alertRows } from '../pure/data-shape.js';
import { filterInput, sideHint, paint, tabToolbar } from './_kit.js';
import { filterRows, filterSummary } from '../pure/text-filter.js';

export function createAlerts({ api, profile }) {
  const root = el('div', { class: 'tab tab-alerts' });
  const toolbar = el('div', { class: 'alerts-toolbar-host' });
  const body = el('div', { class: 'alerts-body' });
  root.append(toolbar, body);

  let alerts = null;
  let inspectorHost = null;
  const filters = { severity: '', query: '' };
  const SEARCH_FIELDS = ['rule_id', 'id', 'alert_id', 'title', 'reason', 'severity', 'state'];

  // Hermes' alert engine snoozes for a caller-supplied number of hours. The tab
  // hard-coded one, which meant the only way to silence a noisy rule for a
  // working day was to click Snooze eight times.
  const SNOOZE_OPTIONS = Object.freeze([
    ['1', '1 hour'], ['4', '4 hours'], ['8', '8 hours'], ['24', '24 hours'],
  ]);

  async function load() {
    clear(body);
    body.append(skeleton({ lines: 6 }));
    try {
      alerts = await api.get('/api/alerts', { profile });
    } catch (err) {
      alerts = null;
      clear(body);
      body.append(errorPanel({ message: err.message, requestId: err.request_id, onRetry: load }));
      return;
    }
    render();
  }

  function renderToolbar(all, shown) {
    const severities = [...new Set(all.map((row) => String(row.severity || 'info')))].sort();
    const severitySelect = el('select', {
      class: 'select',
      'aria-label': 'Alert severity',
      onchange: (event) => { filters.severity = event.target.value; render(); },
    });
    for (const [value, label] of [['', 'Severity: all'], ...severities.map((s) => [s, s])]) {
      const option = el('option', { value, text: label });
      option.selected = filters.severity === value;
      severitySelect.append(option);
    }
    const firing = all.filter((row) => !row.acknowledged_at).length;
    paint(toolbar, tabToolbar({
      title: 'Alerts',
      subtitle: all.length
        ? `${firing} unacknowledged · ${filterSummary(shown, all.length, 'alert')}`
        : 'evaluated locally by this dashboard, not by Hermes',
      filters: [severitySelect, filterInput({
        value: filters.query,
        placeholder: 'Filter alerts…',
        ariaLabel: 'Filter alerts',
        onChange: (value) => {
          if (value === filters.query) return;
          filters.query = value;
          render();
        },
      })],
      meta: alerts?.meta || null,
      onRefresh: () => load(),
    }));
  }

  function render() {
    clear(body);
    if (!alerts) {
      paint(toolbar, tabToolbar({ title: 'Alerts', onRefresh: () => load() }));
      body.append(unavailableState({ reason: 'Alerts source unavailable' }));
      return;
    }
    const all = sortAlerts(alertRows(alerts.data));
    let rows = all;
    if (filters.severity) rows = rows.filter((row) => String(row.severity || 'info') === filters.severity);
    rows = filterRows(rows, filters.query, SEARCH_FIELDS);
    renderToolbar(all, rows.length);
    body.append(provenanceBadge(alerts.meta, { empty: rows.length === 0 }));
    if (!rows.length) {
      body.append(all.length
        ? emptyState({ title: 'No alert matches', note: 'Every firing alert is filtered out by the current view.' })
        : emptyState({ title: 'No active alerts', note: 'The alert source is available.' }));
      return;
    }

    const list = el('ul', { class: 'list alerts-list' });
    for (const alert of rows) {
      const id = alert.id || alert.alert_id;
      const item = el('li', { class: 'list-item alerts-item' });
      item.append(
        el('span', { class: `chip chip-${alert.severity || 'info'}`, text: alert.severity || 'info' }),
        el('span', { class: 'mono', text: alert.rule_id || id || 'alert' }),
        el('span', { text: alert.title || alert.reason || '' }),
        el('span', {
          class: 'mono',
          text: `first ${fmtTime(alert.first_seen_at || alert.first_seen)} · last ${fmtTime(alert.last_seen_at || alert.last_seen || alert.first_seen_at)}`,
        }),
        el('span', { class: 'chip', text: alert.state || 'open' }),
      );

      if (id) {
        const actions = el('span', { class: 'alerts-actions' });
        actions.append(
          el('button', {
            class: 'btn btn-sm',
            text: 'Ack',
            onclick: () => mutate(`/api/alerts/${encodeURIComponent(id)}/ack`),
          }),
          el('button', {
            class: 'btn btn-sm',
            text: 'Snooze ▾',
            title: 'Silence this alert here for a bounded window',
            onclick: (event) => {
              const menu = el('div', { class: 'chat-menu chat-menu-narrow' });
              const list = el('div', { class: 'chat-menu-body' });
              for (const [hours, label] of SNOOZE_OPTIONS) {
                list.append(el('button', {
                  class: 'chat-menu-item', type: 'button',
                  onclick: () => {
                    closeMenu();
                    mutate(`/api/alerts/${encodeURIComponent(id)}/snooze?hours=${hours}`);
                  },
                }, [el('span', { class: 'chat-menu-item-label', text: label })]));
              }
              menu.append(list);
              openMenu(event.currentTarget, menu, { placement: 'below', align: 'end' });
            },
          }),
        );
        item.append(actions);
      }
      list.append(item);
    }
    body.append(list);
  }

  async function mutate(path) {
    try {
      await api.post(path, {}, { profile });
      await load();
    } catch (err) {
      body.prepend(errorPanel({ message: err.message, requestId: err.request_id }));
    }
  }

  function renderInspector(container) {
    inspectorHost = container;
    if (!inspectorHost) return;
    const rows = alertRows(alerts?.data) || [];
    const firing = rows.filter((row) => !row.acknowledged_at).length;
    paint(inspectorHost, sideHint('Alerts', [
      'These are evaluated locally by this dashboard\u2019s rule engine, not by Hermes — acknowledging one only silences it here.',
      rows.length ? `${rows.length} alert${rows.length === 1 ? '' : 's'}, ${firing} unacknowledged.` : 'No alerts are firing.',
      'Snoozing is bounded: an alert returns when its window elapses and the condition still holds.',
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
      return {};
    },
    renderInspector,
    refresh: load,
  };
}
