// Command palette + federated search — Ctrl/Cmd+K; navigation commands for
// every tab + profile switch; debounced federated search with per-source
// bounded results, source chips + type, deep links; Esc/blur closes; focus
// trap; slow-source timeouts show partial results; no mutation commands.

import { createSearcher, buildNavCommands } from './pure/search.js';
import { buildDeepLink } from './pure/hash-router.js';
import { el, clear } from './ui.js';
import { icon, ROUTE_ICONS } from './icons.js';

export function createPalette({ routes, onNavigate, onProfileSwitch, searchSources }) {
  const searcher = createSearcher({ debounceMs: 250, perSourceLimit: 8, timeoutMs: 3500 });
  const overlay = el('div', { class: 'palette-overlay hidden', 'data-testid': 'palette' });
  const input = el('input', {
    class: 'palette-input',
    type: 'search',
    placeholder: 'Search tabs, records…  (Esc to close)',
    'aria-label': 'Command palette and search',
    autocomplete: 'off',
  });
  const results = el('div', { class: 'palette-results', role: 'listbox' });
  overlay.append(el('div', { class: 'palette-box', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Command palette' }, [
    input,
    results,
  ]));
  document.body.append(overlay);

  let commands = buildNavCommands(routes, []);
  let open = false;
  let lastFocused = null;

  function setCommands(profiles) {
    commands = buildNavCommands(routes, profiles || []);
  }

  function openPalette() {
    lastFocused = document.activeElement;
    open = true;
    overlay.classList.remove('hidden');
    input.value = '';
    clear(results);
    renderCommands(commands);
    input.focus();
    document.addEventListener('keydown', onKey, true); // focus trap
  }

  function closePalette() {
    open = false;
    overlay.classList.add('hidden');
    document.removeEventListener('keydown', onKey, true);
    if (lastFocused && lastFocused.focus) lastFocused.focus();
  }

  function onKey(e) {
    if (!open) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      closePalette();
      return;
    }
    if (e.key === 'Tab') {
      e.preventDefault();
      const items = results.querySelectorAll('.palette-item');
      if (items.length === 0) return;
      const idx = [...items].indexOf(document.activeElement);
      const next = e.shiftKey ? (idx <= 0 ? items.length - 1 : idx - 1) : (idx + 1) % items.length;
      items[next].focus();
    }
  }

  function renderCommands(cmds) {
    clear(results);
    if (cmds.length === 0) {
      results.append(el('div', { class: 'palette-empty', text: 'No commands' }));
      return;
    }
    for (const c of cmds) {
      const item = el('button', {
        class: 'palette-item',
        type: 'button',
        role: 'option',
        onclick: () => {
          if (c.type === 'nav') onNavigate(c.route);
          else if (c.type === 'profile') onProfileSwitch(c.profile);
          closePalette();
        },
      });
      const iconName = c.type === 'nav' ? (ROUTE_ICONS[c.route] || 'overview') : 'spark';
      item.append(el('span', { class: 'palette-item-label' }, [icon(iconName, { size: 14 }), ' ', c.label]));
      item.append(el('span', { class: 'palette-item-kind', text: c.type }));
      results.append(item);
    }
  }

  function renderSearchResults(bySource) {
    clear(results);
    let any = false;
    for (const [source, items] of Object.entries(bySource || {})) {
      if (!Array.isArray(items) || items.length === 0) continue;
      any = true;
      const group = el('div', { class: 'palette-group' });
      group.append(el('div', { class: 'palette-group-title', text: source }));
      for (const item of items.slice(0, 8)) {
        const row = el('button', {
          class: 'palette-item',
          type: 'button',
          onclick: () => {
            const link = buildDeepLink(item.route || '/overview', item.params || {}, { selected: item.id });
            window.location.hash = link;
            closePalette();
          },
        });
        row.append(el('span', { class: 'palette-item-label' }, [icon('search', { size: 13 }), ' ', item.title || item.id || 'record']));
        row.append(el('span', { class: `chip chip-${source}`, text: item.type || source }));
        group.append(row);
      }
      results.append(group);
    }
    if (!any) {
      results.append(el('div', { class: 'palette-empty', text: 'No results' }));
    }
  }

  let searchSeq = 0;
  searcher.onQuery(async (q) => {
    const my = ++searchSeq;
    const sources = {};
    if (searchSources) {
      for (const [name, fn] of Object.entries(searchSources)) {
        sources[name] = fn(q);
      }
    }
    // Federated: race all sources; slow ones time out → partial results shown.
    const settled = await Promise.all(
      Object.entries(sources).map(async ([name, p]) => [name, await searcher.withTimeout(p, name)]),
    );
    const out = {};
    for (const [name, r] of settled) {
      if (r.status === 'ok' && r.value) out[name] = Array.isArray(r.value) ? r.value : [r.value];
    }
    if (my !== searchSeq) return { cancelled: true };
    return searcher.boundResults(out);
  });

  input.addEventListener('input', (e) => {
    const q = e.target.value.trim();
    if (!q) {
      renderCommands(commands);
      return;
    }
    searcher.input(q).then((res) => {
      if (res && res.cancelled) return;
      if (res && res.error) {
        renderSearchResults({ error: [{ title: `Search failed: ${res.error}`, id: 'err', type: 'error' }] });
        return;
      }
      renderSearchResults(res);
    });
  });

  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      open ? closePalette() : openPalette();
    }
  });

  overlay.addEventListener('mousedown', (e) => {
    if (e.target === overlay) closePalette();
  });

  return { open: openPalette, close: closePalette, setCommands, get isOpen() { return open; } };
}
