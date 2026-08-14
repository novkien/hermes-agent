// Stage 7 placeholder — renders a route stub so every locked surface stays
// reachable from nav + palette. Stage 7 fills the real module.

import { el, clear } from '../ui.js';

export function createPlaceholder({ route, onNavigate }) {
  const root = el('div', { class: 'tab tab-placeholder' });
  return {
    mount(container) {
      clear(container);
      container.append(root);
    },
    activate() {
      clear(root);
      root.append(
        el('div', { class: 'tab-toolbar' }, [el('span', { class: 'tab-title', text: route.label })]),
        el('div', { class: 'placeholder-note' }, [
          el('div', { class: 'state-empty-title', text: `${route.label} — Stage 7` }),
          el('div', { class: 'state-empty-note', text: `Route stub registered for ${route.key}; governance/integration implementation arrives in Stage 7.` }),
        ]),
      );
    },
    deactivate() {
      return {};
    },
  };
}
