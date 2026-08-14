// llama-proxy tab loaded directly from the dashboard host.

import { el, clear } from '../ui.js';
import { LLaMA_PROXY_URL, LLaMA_PROXY_MODE } from '../pure/llama-proxy-iframe.js';

export const ROUTE = 'llama-proxy';
export const LABEL = 'llama-proxy';
export const GROUP = 'SYSTEM';
export const READ_ONLY_NOTE = 'direct iframe to Llama Proxy';

export const SOURCE_ENDPOINTS = Object.freeze([LLaMA_PROXY_URL]);
export { LLaMA_PROXY_URL, LLaMA_PROXY_MODE };

// Source-first evidence from the previous direct header probe. This is design
// metadata, not a claim that the service is currently healthy.
export const HEADER_PROBE = Object.freeze({
  mode: LLaMA_PROXY_MODE,
  verdict: 'direct-iframe-allowed',
  x_frame_options: 'absent',
  content_security_policy: 'absent',
  cookies: 'none',
  websocket: 'none',
  assets: 'root-relative',
  probed_at: '2026-08-07T13:29:23Z',
});

export function diagnosticsStrip() {
  return { mode: LLaMA_PROXY_MODE, probe: HEADER_PROBE };
}

export function createLlamaProxy() {
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
    title: 'llama-proxy dashboard',
    loading: 'eager',
    referrerpolicy: 'no-referrer',
    'data-mode': LLaMA_PROXY_MODE,
  });

  let srcAssigned = false;
  let loadState = 'not-loaded';
  let lastChangedAt = null;

  function updateDiagnostics() {
    clear(diagnostics);
    const stateClass = loadState === 'loaded'
      ? 'chip-ready'
      : loadState === 'error'
        ? 'chip-error'
        : 'chip-warning';
    diagnostics.append(
      el('span', { class: `chip ${stateClass}`, text: loadState }),
      el('span', { class: 'mono', text: LLaMA_PROXY_URL }),
      el('span', { text: `mode: ${LLaMA_PROXY_MODE}` }),
      lastChangedAt
        ? el('span', { class: 'mono', text: `updated: ${lastChangedAt}` })
        : null,
    );
  }

  function assignSourceOnce() {
    if (srcAssigned) return;
    loadState = 'loading';
    lastChangedAt = new Date().toISOString();
    updateDiagnostics();
    frame.src = LLaMA_PROXY_URL;
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
    el('span', { class: 'tab-title', text: 'llama-proxy' }),
    el('button', {
      class: 'btn btn-sm',
      text: 'Open standalone',
      onclick: () => window.open(LLaMA_PROXY_URL, '_blank', 'noopener,noreferrer'),
    }),
    el('button', {
      class: 'btn btn-sm',
      text: 'Reload iframe',
      onclick: () => {
        loadState = 'loading';
        lastChangedAt = new Date().toISOString();
        updateDiagnostics();
        // Explicit owner action: reload only when this button is used.
        frame.src = LLaMA_PROXY_URL;
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
      assignSourceOnce();
      return Promise.resolve();
    },
    deactivate() {
      root.hidden = true;
      return { scroll: 0, loadState };
    },
    refresh() {
      frame.src = LLaMA_PROXY_URL;
      srcAssigned = true;
      loadState = 'loading';
      lastChangedAt = new Date().toISOString();
      updateDiagnostics();
      return Promise.resolve();
    },
    get data() {
      return {
        url: LLaMA_PROXY_URL,
        mode: LLaMA_PROXY_MODE,
        state: loadState,
      };
    },
  };
}

// Compatibility export retained for existing pure-module tests.
export function createLlamaProxyManager(doc, probeResult = JSON.stringify(HEADER_PROBE)) {
  let node = null;
  let srcSet = false;
  let visible = false;

  function ensureNode() {
    if (node) return node;
    node = doc.createElement('iframe');
    node.setAttribute('data-mode', LLaMA_PROXY_MODE);
    node.setAttribute('data-probe', probeResult || '');
    node.setAttribute('title', 'llama-proxy dashboard (retained iframe)');
    node.style.width = '100%';
    node.style.height = '100%';
    node.style.border = '0';
    node.style.display = 'none';
    doc.body.appendChild(node);
    return node;
  }

  return {
    diagnostics: { mode: LLaMA_PROXY_MODE, probe: probeResult || '' },
    show() {
      const iframe = ensureNode();
      if (!srcSet) {
        iframe.src = LLaMA_PROXY_URL;
        srcSet = true;
      }
      iframe.style.display = 'block';
      visible = true;
    },
    hide() {
      if (!node) return;
      node.style.display = 'none';
      visible = false;
    },
    isVisible() {
      return visible;
    },
    isCreated() {
      return node !== null;
    },
  };
}
