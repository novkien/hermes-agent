// 9router tab — routed through AgentOS so browser only loads via Mission Control.

import { el, clear } from '../ui.js';
import { bindLiveResources, liveRows } from './_live.js';

export const ROUTE = '9router';
export const LABEL = '9router';
export const GROUP = 'SYSTEM';
export const READ_ONLY_NOTE = 'direct iframe to 9router';

export const ROUTE_ENDPOINTS = Object.freeze(['http://100.100.127.43:20128/dashboard']);
export const MODE = 'direct-iframe';

export function diagnosticsStrip() {
  return { mode: MODE, endpoints: ROUTE_ENDPOINTS };
}

export function create9router({ liveStore, profile } = {}) {
  const root = el('div', { class: 'tab tab-llama-proxy' });
  const toolbar = el('div', { class: 'tab-toolbar' });
  const diagnostics = el('div', {
    class: 'llama-diagnostics',
    role: 'status',
    'aria-live': 'polite',
  });
  const frameWrap = el('div', { class: 'llama-frame-wrap' });
  const frame = el('iframe', {
    class: 'llama-frame',
    title: '9router dashboard',
    loading: 'eager',
    referrerpolicy: 'no-referrer',
    'data-mode': MODE,
  });

  let srcAssigned = false;
  let loadState = 'not-loaded';
  let lastChangedAt = null;
  let backendHealth = null;
  let unsubscribe = null;

  function updateDiagnostics() {
    clear(diagnostics);
    const stateClass = loadState === 'loaded'
      ? 'chip chip-ready'
      : loadState === 'error'
        ? 'chip chip-error'
        : 'chip chip-warning';
    diagnostics.append(
      el('span', { class: stateClass, text: loadState }),
      backendHealth ? el('span', {
        class: `chip ${backendHealth.healthy ? 'chip-ready' : 'chip-error'}`,
        text: `server ${backendHealth.status || (backendHealth.healthy ? 'online' : 'offline')}`,
      }) : null,
      el('span', { class: 'mono', text: ROUTE_ENDPOINTS[0] }),
      el('span', { text: `mode: ${MODE}` }),
      lastChangedAt
        ? el('span', { class: 'mono', text: `updated: ${lastChangedAt}` })
        : null,
    );
  }

  function applyLiveHealth() {
    const live = liveRows(liveStore, 'iframe.health', profile);
    backendHealth = live?.rows?.find((row) => row.service === '9router') || backendHealth;
    updateDiagnostics();
  }

  function bindLive() {
    if (unsubscribe || !liveStore) return;
    unsubscribe = bindLiveResources(liveStore, ['iframe.health'], profile, () => {
      if (root.isConnected) applyLiveHealth();
    });
  }

  function assignSourceOnce() {
    if (srcAssigned) return;
    loadState = 'loading';
    lastChangedAt = new Date().toISOString();
    updateDiagnostics();
    frame.src = ROUTE_ENDPOINTS[0];
    srcAssigned = true;
  }

  frame.addEventListener('load', () => {
    loadState = 'loaded';
    lastChangedAt = new Date().toISOString();
    updateDiagnostics();
  });
  frame.addEventListener('error', () => {
    loadState = 'error';
    lastChangedAt = new Date().toISOString();
    updateDiagnostics();
  });

  toolbar.append(
    el('span', { class: 'tab-title', text: '9router' }),
    el('button', {
      class: 'btn btn-sm',
      text: 'Open standalone',
      onclick: () => window.open(ROUTE_ENDPOINTS[0], '_blank', 'noopener,noreferrer'),
    }),
    el('button', {
      class: 'btn btn-sm',
      text: 'Reload iframe',
      onclick: () => {
        loadState = 'loading';
        lastChangedAt = new Date().toISOString();
        updateDiagnostics();
        // Explicit owner action: reload only when this button is used.
        frame.src = ROUTE_ENDPOINTS[0];
        srcAssigned = true;
      },
    }),
  );
  frameWrap.append(frame);
  root.append(toolbar, diagnostics, frameWrap);
  updateDiagnostics();

  return {
    mount(container) {
      clear(container);
      container.append(root);
    },
    activate() {
      root.hidden = false;
      bindLive();
      applyLiveHealth();
      assignSourceOnce();
      return Promise.resolve();
    },
    deactivate() {
      if (unsubscribe) { unsubscribe(); unsubscribe = null; }
      root.hidden = true;
      return { scroll: 0, loadState };
    },
    refresh() {
      frame.src = ROUTE_ENDPOINTS[0];
      srcAssigned = true;
      loadState = 'loading';
      lastChangedAt = new Date().toISOString();
      updateDiagnostics();
      return Promise.resolve();
    },
    get data() {
      return {
        url: ROUTE_ENDPOINTS[0],
        mode: MODE,
        state: loadState,
      };
    },
  };
}
