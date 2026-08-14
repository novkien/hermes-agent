// Two pickers that make the agent's capabilities visible instead of implied:
// a slash-command menu over the real skill catalogue, and a command palette for
// the chat surface itself.
//
// Both read from routes that already exist — `/api/skills` on the dashboard and
// `/api/gateway/v1/toolsets` on the gateway — rather than hard-coding a list
// that would drift the moment a skill is installed.

import { el, clear, closeMenu, openMenu, skeleton } from '../../ui.js';
import { icon } from '../../icons.js';
import { truncate } from '../../markdown-render.js';
import { listFrom } from '../../pure/data-shape.js';

const MAX_ROWS = 40;

/**
 * Slash-command menu. Opens on `/` at the start of an empty composer and
 * filters as the operator keeps typing, which is the interaction every chat
 * client has trained them to expect.
 */
export function openSlashMenu(anchor, { api, profile, query = '', onPick }) {
  const menu = el('div', { class: 'chat-menu chat-menu-wide' });
  const search = el('input', {
    class: 'input chat-menu-search',
    type: 'search',
    placeholder: 'Search skills…',
    'aria-label': 'Search skills',
    value: query,
  });
  const body = el('div', { class: 'chat-menu-body' });
  menu.append(search, body);
  body.append(skeleton({ lines: 4 }));

  let skills = [];
  api.get('/api/skills', { profile })
    .then((response) => {
      skills = listFrom(response?.data, ['skills'])
        .filter((row) => row && row.name)
        .sort((a, b) => (Number(b.usage || 0) - Number(a.usage || 0))
          || String(a.name).localeCompare(String(b.name)));
      paint(search.value);
    })
    .catch(() => {
      clear(body);
      body.append(el('div', { class: 'chat-menu-empty', text: 'Skill catalogue unavailable' }));
    });

  function paint(needleRaw) {
    clear(body);
    const needle = String(needleRaw || '').trim().toLowerCase();
    const rows = skills.filter((row) => !needle
      || `${row.name} ${row.description || ''} ${row.category || ''}`.toLowerCase().includes(needle));
    if (!rows.length) {
      body.append(el('div', { class: 'chat-menu-empty', text: 'No matching skills' }));
      return;
    }
    for (const row of rows.slice(0, MAX_ROWS)) {
      const item = el('button', {
        class: `chat-menu-item chat-menu-item-rich${row.enabled === false ? ' is-muted' : ''}`,
        type: 'button',
        title: row.description || row.name,
        onclick: () => { closeMenu(); onPick(row); },
      }, [
        el('div', { class: 'chat-menu-item-main' }, [
          el('span', { class: 'chat-menu-item-label', text: `/${row.name}` }),
          row.provenance
            ? el('span', { class: 'chat-menu-item-tag', text: row.provenance })
            : null,
          row.enabled === false
            ? el('span', { class: 'chat-menu-group-note', text: 'disabled' })
            : null,
        ]),
        row.description
          ? el('div', { class: 'chat-menu-item-note', text: truncate(row.description, 120) })
          : null,
      ]);
      body.append(item);
    }
    if (rows.length > MAX_ROWS) {
      body.append(el('div', { class: 'chat-menu-empty', text: `+${rows.length - MAX_ROWS} more — keep typing` }));
    }
  }

  search.addEventListener('input', () => paint(search.value));
  paint(query);
  openMenu(anchor, menu);
  setTimeout(() => search.focus(), 0);
  return menu;
}

/**
 * What the agent can actually call. `GET /v1/toolsets` is the gateway's own
 * deterministic answer — its handler says as much — so this replaces guessing
 * from the model's behaviour.
 */
export function openToolsetMenu(anchor, { api, profile }) {
  const menu = el('div', { class: 'chat-menu chat-menu-wide' });
  const body = el('div', { class: 'chat-menu-body' });
  menu.append(el('div', { class: 'chat-menu-group' }, [el('span', { text: 'Toolsets' })]), body);
  body.append(skeleton({ lines: 4 }));

  api.get('/api/gateway/v1/toolsets', { profile })
    .then((response) => {
      const rows = listFrom(response?.data, ['toolsets', 'data']);
      clear(body);
      if (!rows.length) {
        body.append(el('div', { class: 'chat-menu-empty', text: 'Gateway reported no toolsets' }));
        return;
      }
      const enabled = rows.filter((row) => row.enabled);
      const disabled = rows.filter((row) => !row.enabled);
      for (const group of [['Enabled', enabled], ['Available', disabled]]) {
        if (!group[1].length) continue;
        body.append(el('div', { class: 'chat-menu-group' }, [el('span', { text: group[0] })]));
        for (const row of group[1]) body.append(toolsetRow(row));
      }
    })
    .catch(() => {
      clear(body);
      body.append(el('div', { class: 'chat-menu-empty', text: 'Toolset inventory unavailable' }));
    });

  openMenu(anchor, menu);
  return menu;
}

function toolsetRow(row) {
  const tools = Array.isArray(row.tools) ? row.tools : [];
  const details = el('details', { class: `chat-toolset${row.enabled ? ' is-on' : ''}` });
  details.append(el('summary', {}, [
    el('span', { class: 'chat-menu-item-label', text: row.label || row.name }),
    el('span', { class: 'chat-menu-item-tag', text: `${tools.length}` }),
    row.configured === false
      ? el('span', { class: 'chat-menu-group-note', text: 'not configured' })
      : null,
  ]));
  if (row.description) {
    details.append(el('div', { class: 'chat-menu-item-note', text: row.description }));
  }
  if (tools.length) {
    details.append(el('div', { class: 'chat-toolset-tools mono', text: tools.join('  ') }));
  }
  return details;
}

/**
 * Command palette for the chat surface. A flat, searchable list of the actions
 * that otherwise hide behind icons — and the keyboard route to all of them.
 */
export function openCommandPalette(commands) {
  const layer = el('div', { class: 'chat-palette-layer', role: 'dialog', 'aria-modal': 'true' });
  const box = el('div', { class: 'chat-palette' });
  const search = el('input', {
    class: 'input chat-palette-search',
    type: 'search',
    placeholder: 'Command or session…',
    'aria-label': 'Command palette',
  });
  const body = el('div', { class: 'chat-palette-body' });
  box.append(search, body);
  layer.append(box);
  document.body.append(layer);

  let index = 0;
  let rows = [];

  function paint() {
    const needle = search.value.trim().toLowerCase();
    rows = commands.filter((command) => !needle
      || `${command.label} ${command.hint || ''} ${command.group || ''}`.toLowerCase().includes(needle));
    index = Math.min(index, Math.max(0, rows.length - 1));
    clear(body);
    if (!rows.length) {
      body.append(el('div', { class: 'chat-menu-empty', text: 'Nothing matches' }));
      return;
    }
    let lastGroup = null;
    rows.forEach((command, position) => {
      if (command.group && command.group !== lastGroup) {
        lastGroup = command.group;
        body.append(el('div', { class: 'chat-menu-group' }, [el('span', { text: command.group })]));
      }
      const item = el('button', {
        class: `chat-palette-item${position === index ? ' active' : ''}`,
        type: 'button',
        onclick: () => run(command),
      }, [
        command.icon ? icon(command.icon, { size: 13 }) : null,
        el('span', { class: 'chat-palette-item-label', text: command.label }),
        command.hint ? el('span', { class: 'chat-palette-item-hint', text: command.hint }) : null,
      ]);
      body.append(item);
    });
  }

  function close() {
    document.removeEventListener('keydown', onKey, true);
    layer.remove();
  }

  function run(command) {
    close();
    try {
      command.run();
    } catch (_err) { /* a failed command must not take the palette's teardown with it */ }
  }

  function onKey(event) {
    if (event.key === 'Escape') { event.preventDefault(); close(); return; }
    if (event.key === 'ArrowDown') { event.preventDefault(); index = Math.min(index + 1, rows.length - 1); paint(); return; }
    if (event.key === 'ArrowUp') { event.preventDefault(); index = Math.max(index - 1, 0); paint(); return; }
    if (event.key === 'Enter') {
      event.preventDefault();
      if (rows[index]) run(rows[index]);
    }
  }

  layer.addEventListener('mousedown', (event) => { if (event.target === layer) close(); });
  search.addEventListener('input', () => { index = 0; paint(); });
  document.addEventListener('keydown', onKey, true);
  paint();
  setTimeout(() => search.focus(), 0);
  return { close };
}

/**
 * Find in conversation, with a fall-through to every session.
 *
 * The in-thread pass only sees what is rendered, which after paging is the last
 * page or two. When it finds nothing, the adapter's FTS5 index over all message
 * content does (`GET /sessions/search`, real full-text — `queries.py` refuses to
 * fall back to LIKE), so "not in this thread" turns into "here is the thread it
 * is in" instead of a dead end.
 */
export function createThreadSearch(list, { api = null, profile = null, onOpenSession = null } = {}) {
  const bar = el('div', { class: 'chat-find' });
  const input = el('input', {
    class: 'input chat-find-input', type: 'search',
    placeholder: 'Find in conversation…', 'aria-label': 'Find in conversation',
  });
  const count = el('span', { class: 'chat-find-count', text: '' });
  const prev = el('button', { class: 'chat-find-nav', type: 'button', title: 'Previous match' }, ['↑']);
  const next = el('button', { class: 'chat-find-nav', type: 'button', title: 'Next match' }, ['↓']);
  const close = el('button', { class: 'chat-find-nav', type: 'button', title: 'Close' }, [icon('close', { size: 12 })]);
  const globalButton = el('button', {
    class: 'chat-find-global', type: 'button', title: 'Search every session',
  }, [icon('search', { size: 11 }), el('span', { text: 'All sessions' })]);
  globalButton.hidden = true;
  const results = el('div', { class: 'chat-find-results' });
  results.hidden = true;

  bar.append(input, count, prev, next, globalButton, close);
  const wrap = el('div', { class: 'chat-find-wrap' }, [bar, results]);
  wrap.hidden = true;

  let matches = [];
  let cursor = 0;

  function clearMarks() {
    for (const node of list.querySelectorAll('.chat-find-hit')) {
      node.classList.remove('chat-find-hit', 'is-current');
    }
  }

  function run() {
    clearMarks();
    const needle = input.value.trim().toLowerCase();
    matches = [];
    if (needle) {
      for (const node of list.querySelectorAll('.msg-body, .msg-tool-name, .msg-tool-args')) {
        if (node.textContent.toLowerCase().includes(needle)) {
          node.classList.add('chat-find-hit');
          matches.push(node);
        }
      }
    }
    cursor = 0;
    focusMatch();
    // Offer the wider search exactly when the local one came up empty.
    globalButton.hidden = !(api && needle && matches.length === 0);
    if (!needle) { results.hidden = true; clear(results); }
  }

  async function searchEverywhere() {
    const needle = input.value.trim();
    if (!api || !needle) return;
    clear(results);
    results.hidden = false;
    results.append(el('div', { class: 'chat-find-note', text: 'Searching every session…' }));
    const response = await api
      .get(`/api/adapter/sessions/search?q=${encodeURIComponent(needle)}&limit=25`, { profile })
      .catch(() => null);
    const rows = response ? listFrom(response.data, ['results', 'messages']) : [];
    clear(results);
    if (!rows.length) {
      results.append(el('div', { class: 'chat-find-note', text: 'No matches in any session' }));
      return;
    }
    // One row per session: 25 hits in one thread is one answer, not 25.
    const bySession = new Map();
    for (const row of rows) {
      const id = row.session_id;
      if (!id || bySession.has(id)) continue;
      bySession.set(id, row);
    }
    for (const [id, row] of bySession) {
      results.append(el('button', {
        class: 'chat-find-result', type: 'button',
        onclick: () => { if (onOpenSession) onOpenSession(id); hide(); },
      }, [
        el('span', { class: 'chat-find-result-id mono', text: id }),
        el('span', { class: 'chat-find-result-snippet', text: String(row.snippet || '').replace(/\s+/g, ' ').slice(0, 110) }),
      ]));
    }
  }

  function focusMatch() {
    count.textContent = matches.length ? `${cursor + 1}/${matches.length}` : (input.value ? '0' : '');
    for (const node of matches) node.classList.remove('is-current');
    const node = matches[cursor];
    if (node) {
      node.classList.add('is-current');
      node.scrollIntoView({ block: 'center' });
    }
  }

  function step(delta) {
    if (!matches.length) return;
    cursor = (cursor + delta + matches.length) % matches.length;
    focusMatch();
  }

  input.addEventListener('input', run);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); step(event.shiftKey ? -1 : 1); }
    if (event.key === 'Escape') { event.preventDefault(); hide(); }
  });
  prev.addEventListener('click', () => step(-1));
  next.addEventListener('click', () => step(1));
  globalButton.addEventListener('click', () => searchEverywhere());
  close.addEventListener('click', () => hide());

  function show() {
    wrap.hidden = false;
    input.focus();
    input.select();
  }
  function hide() {
    wrap.hidden = true;
    results.hidden = true;
    clear(results);
    clearMarks();
    matches = [];
  }

  return { node: wrap, show, hide, toggle: () => (wrap.hidden ? show() : hide()) };
}
