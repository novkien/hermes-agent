// Which persona a new session starts on.
//
// The gateway serves one profile at a time, so this does not move a session to
// another profile — it starts one on another profile's SOUL.md and model while
// still running here. The first item is therefore the plain path: this tab's
// own profile, the behaviour every other entry point keeps.

import { el, closeMenu } from '../../ui.js';
import { icon } from '../../icons.js';

export function buildProfileMenu(profiles, currentProfile, onPick) {
  const menu = el('div', { class: 'chat-menu' });
  const rows = (profiles || []).filter((row) => row && (row.name || row.id));
  if (!rows.length) {
    menu.append(el('div', { class: 'chat-menu-empty', text: 'Profile inventory unavailable' }));
    return menu;
  }

  const search = el('input', {
    class: 'input chat-menu-search',
    type: 'search',
    placeholder: 'Search profiles…',
    'aria-label': 'Search profiles',
  });
  const body = el('div', { class: 'chat-menu-body' });
  menu.append(search, body);

  function item(label, sublabel, value, active) {
    const node = el('button', {
      class: `chat-menu-item${active ? ' active' : ''}`,
      onclick: () => { closeMenu(); onPick(value); },
    }, [
      el('span', { class: 'chat-menu-item-label', text: label }),
      sublabel ? el('span', { class: 'chat-menu-item-note', text: sublabel }) : null,
      active ? icon('check', { size: 12 }) : null,
    ]);
    if (sublabel) node.title = sublabel;
    return node;
  }

  function paint(query) {
    const needle = query.trim().toLowerCase();
    body.replaceChildren();
    if (!needle) body.append(item(currentProfile, 'this profile', null, true));
    let shown = 0;
    for (const row of rows) {
      const name = row.name || row.id;
      if (name === currentProfile) continue;
      if (needle && !String(name).toLowerCase().includes(needle)) continue;
      body.append(item(name, row.model || '', name, false));
      shown += 1;
    }
    if (!shown && needle) {
      body.append(el('div', { class: 'chat-menu-empty', text: 'No profile matches' }));
    }
  }

  search.addEventListener('input', (event) => paint(event.target.value));
  paint('');
  return menu;
}
