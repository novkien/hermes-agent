// "Rack" — the inspector-column entity list: a sticky head (title, count,
// search, filters) over a scrolling stack of selectable cards.
//
// Generic on purpose. Skills is the first tab to use it, but nothing here
// knows about skills: any tab with a "pick one of many, then work on it in the
// centre" shape can mount the same structure.

import { el, clear } from '../ui.js';
import { icon } from '../icons.js';

/**
 * rackShell({...}) → { node, head, toolbar, list, footer, setCount, setNote }
 * The caller fills `toolbar` with its own filter controls and appends cards to
 * `list`; the shell only owns the chrome around them.
 */
export function rackShell({ title = '', iconName = null, searchPlaceholder = 'Search…', onSearch = null } = {}) {
  const count = el('span', { class: 'rack-count', text: '' });
  const note = el('span', { class: 'rack-note' });

  const heading = el('div', { class: 'rack-title' }, [
    iconName ? icon(iconName, { size: 13, className: 'rack-title-icon' }) : null,
    el('span', { text: title }),
    count,
  ].filter(Boolean));

  const toolbar = el('div', { class: 'rack-toolbar' });
  if (onSearch) {
    const search = el('input', {
      class: 'input rack-search',
      type: 'search',
      placeholder: searchPlaceholder,
      'aria-label': searchPlaceholder,
      oninput: (event) => onSearch(event.target.value),
    });
    toolbar.append(el('div', { class: 'rack-search-wrap' }, [
      icon('search', { size: 12, className: 'rack-search-icon' }),
      search,
    ]));
  }

  const head = el('div', { class: 'rack-head' }, [heading, note, toolbar]);
  const list = el('div', { class: 'rack-list', role: 'list' });
  const footer = el('div', { class: 'rack-footer' });
  const node = el('div', { class: 'rack' }, [head, list, footer]);

  return {
    node,
    head,
    toolbar,
    list,
    footer,
    setCount(shown, total) {
      count.textContent = total === undefined || shown === total ? String(shown) : `${shown}/${total}`;
    },
    setNote(text, tone = '') {
      clear(note);
      if (!text) return;
      note.className = `rack-note${tone ? ` rack-note-${tone}` : ''}`;
      note.append(document.createTextNode(text));
    },
  };
}

/**
 * rackCard({...}) — one selectable entity card.
 *
 * The card is an `<article>` rather than a `<button>` because it contains its
 * own action buttons, and a button inside a button is not valid; selection is
 * handled by an explicit click/keyboard handler instead.
 */
export function rackCard({
  id,
  title,
  subtitle = '',
  description = '',
  status = null,
  statusLabel = null,
  badges = [],
  metrics = [],
  actions = [],
  active = false,
  onSelect = () => {},
} = {}) {
  const card = el('article', {
    class: `rack-card${active ? ' active' : ''}`,
    role: 'listitem',
    tabindex: '0',
    'data-id': id === undefined || id === null ? undefined : String(id),
    'data-status': status || undefined,
    onclick: () => onSelect(id),
    onkeydown: (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        onSelect(id);
      }
    },
  });

  const top = el('div', { class: 'rack-card-top' }, [
    status ? el('span', { class: `rack-dot rack-dot-${status}`, title: statusLabel || status }) : null,
    el('span', { class: 'rack-card-title', text: title, title }),
    subtitle ? el('span', { class: 'rack-card-sub', text: subtitle }) : null,
  ].filter(Boolean));
  card.append(top);

  if (description) card.append(el('p', { class: 'rack-card-desc', text: description }));

  if (badges.length || metrics.length) {
    const foot = el('div', { class: 'rack-card-foot' });
    for (const badge of badges) {
      if (!badge || !badge.label) continue;
      foot.append(el('span', { class: `rack-tag${badge.tone ? ` rack-tag-${badge.tone}` : ''}`, text: badge.label }));
    }
    for (const metric of metrics) {
      if (!metric || metric.value === null || metric.value === undefined) continue;
      foot.append(el('span', { class: 'rack-metric', title: metric.title || metric.label }, [
        metric.icon ? icon(metric.icon, { size: 10 }) : null,
        el('span', { text: String(metric.value) }),
      ].filter(Boolean)));
    }
    card.append(foot);
  }

  if (actions.length) {
    const bar = el('div', { class: 'rack-card-actions' });
    for (const action of actions) if (action) bar.append(action);
    card.append(bar);
  }

  return card;
}

/**
 * Group header inside the list — used when the rack is sorted by a facet
 * (category, status, owner) rather than a flat ranking.
 */
export function rackGroup(label, count = null) {
  return el('div', { class: 'rack-group' }, [
    el('span', { text: label }),
    count === null ? null : el('span', { class: 'rack-group-count', text: String(count) }),
  ].filter(Boolean));
}

/**
 * "Show more" sentinel. Long inventories render in batches so the first paint
 * stays cheap on a low-power host.
 */
export function rackMore(remaining, onClick) {
  return el('button', {
    class: 'rack-more',
    type: 'button',
    onclick: onClick,
  }, [icon('chevron-down', { size: 12 }), el('span', { text: `Show ${remaining} more` })]);
}
