#!/usr/bin/env node
// Render smoke harness — mount every tab against the real BFF and assert it
// produces a tree instead of throwing.
//
// There is no browser on this host, so a full visual walk-through has to happen
// on the owner's machine. What this catches is everything below the pixels: a
// missing import, a helper called with the wrong signature, a normalizer that
// assumes a field the live payload does not have, a null deref on an empty
// source. Those are the failures that made a third of this dashboard render as
// a blank pane, so they are worth a harness.
//
// Usage:  node tools/render_smoke.mjs [base-url] [tab ...]
//         node tools/render_smoke.mjs http://127.0.0.1:51799
//
// It talks to a running dev BFF (see tools/dev-bff.sh), keeps one session
// cookie for the whole run — minting a session per request trips the BFF's
// per-IP issue limit — and prints a one-line outline per tab.

import { pathToFileURL } from 'node:url';
import path from 'node:path';

const BASE = process.argv[2] || 'http://127.0.0.1:51799';
const ONLY = process.argv.slice(3);
const DIST = path.resolve(process.cwd(), 'frontend/dist');

// ---------------------------------------------------------------- DOM shim

const listeners = new WeakMap();

class ClassList {
  constructor(node) { this.node = node; this.set = new Set(); }
  add(...names) { for (const n of names) if (n) this.set.add(n); this.sync(); }
  remove(...names) { for (const n of names) this.set.delete(n); this.sync(); }
  toggle(name, force) {
    const on = force === undefined ? !this.set.has(name) : Boolean(force);
    if (on) this.set.add(name); else this.set.delete(name);
    this.sync();
    return on;
  }
  contains(name) { return this.set.has(name); }
  sync() { this.node.attributes.class = [...this.set].join(' '); }
}

class Node {
  constructor(tag, ns = null) {
    // `ui.js` uses `child.nodeType ? child : createTextNode(String(child))`,
    // so a shim node without it gets stringified to "[object Object]".
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.namespaceURI = ns;
    this.children = [];
    this.parentNode = null;
    this.attributes = Object.create(null);
    this.style = new Proxy({}, { set: (t, k, v) => { t[k] = v; return true; } });
    this.dataset = Object.create(null);
    this._text = '';
    this.classList = new ClassList(this);
    this.hidden = false;
    this.value = '';
    this.checked = false;
    this.disabled = false;
  }

  get isConnected() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node === globalThis.document.documentElement || node.__root === true;
  }

  set className(value) {
    this.classList.set = new Set(String(value).split(/\s+/).filter(Boolean));
    this.classList.sync();
  }

  get className() { return this.attributes.class || ''; }

  set textContent(value) { this._text = value === null || value === undefined ? '' : String(value); this.children = []; }
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join('');
    return this._text;
  }

  set innerHTML(value) { this._text = String(value); this.children = []; }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'class') this.classList.set = new Set(String(value).split(/\s+/).filter(Boolean));
  }

  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }
  hasAttribute(name) { return name in this.attributes; }

  append(...nodes) {
    for (const child of nodes) {
      if (child === null || child === undefined || child === false) continue;
      const node = typeof child === 'object' && child instanceof Node
        ? child
        : globalThis.document.createTextNode(String(child));
      node.parentNode = this;
      this.children.push(node);
    }
  }

  appendChild(child) { this.append(child); return child; }
  prepend(...nodes) { const before = this.children; this.children = []; this.append(...nodes); this.children.push(...before); }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  insertBefore(node, ref) {
    const index = this.children.indexOf(ref);
    node.parentNode = this;
    if (index < 0) this.children.push(node);
    else this.children.splice(index, 0, node);
    return node;
  }

  removeChild(child) {
    this.children = this.children.filter((c) => c !== child);
    child.parentNode = null;
    return child;
  }

  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  contains(other) {
    if (other === this) return true;
    return this.children.some((c) => c.contains && c.contains(other));
  }

  get firstChild() { return this.children[0] || null; }
  get childNodes() { return this.children; }
  get lastChild() { return this.children[this.children.length - 1] || null; }

  addEventListener(type, fn) {
    if (!listeners.has(this)) listeners.set(this, new Map());
    const map = listeners.get(this);
    if (!map.has(type)) map.set(type, []);
    map.get(type).push(fn);
  }

  removeEventListener(type, fn) {
    const map = listeners.get(this);
    if (!map || !map.has(type)) return;
    map.set(type, map.get(type).filter((f) => f !== fn));
  }

  // A real dispatch, so a tool can drive a selection instead of only observing
  // the first paint. `ui.js:el()` binds every `on*` attr with addEventListener,
  // so firing the stored listeners (and bubbling to ancestors, which is how the
  // table binds row clicks) is enough to exercise the click paths.
  dispatchEvent(event = {}) {
    const type = typeof event === 'string' ? event : event.type;
    if (!type) return true;
    const evt = {
      ...(typeof event === 'object' ? event : {}),
      type,
      target: this,
      currentTarget: this,
      defaultPrevented: false,
      preventDefault() { evt.defaultPrevented = true; },
      stopPropagation() { evt._stopped = true; },
    };
    let node = this;
    while (node) {
      const map = listeners.get(node);
      const fns = map && map.get(type);
      if (fns) {
        evt.currentTarget = node;
        for (const fn of [...fns]) fn.call(node, evt);
      }
      if (evt._stopped) break;
      node = node.parentNode;
    }
    return !evt.defaultPrevented;
  }

  click() { return this.dispatchEvent({ type: 'click' }); }
  focus() {}
  blur() {}
  scrollIntoView() {}
  getBoundingClientRect() { return { top: 0, left: 0, width: 640, height: 200, right: 640, bottom: 200 }; }

  matches(selector) {
    const cls = selector.replace(/^\./, '');
    return this.classList.contains(cls) || this.tagName === selector.toUpperCase();
  }

  closest(selector) {
    let node = this;
    while (node) { if (node.matches && node.matches(selector)) return node; node = node.parentNode; }
    return null;
  }

  querySelectorAll(selector) {
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (child.matches && child.matches(selector)) out.push(child);
        if (child.children) walk(child);
      }
    };
    walk(this);
    return out;
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

class TextNode extends Node {
  constructor(text) { super('#text'); this.nodeType = 3; this._text = String(text); }
  get textContent() { return this._text; }
  set textContent(value) { this._text = String(value); }
}

function installDom() {
  const documentElement = new Node('html');
  documentElement.__root = true;
  const body = new Node('body');
  body.parentNode = documentElement;
  documentElement.children.push(body);

  globalThis.document = {
    documentElement,
    body,
    activeElement: null,
    createElement: (tag) => new Node(tag),
    createElementNS: (ns, tag) => new Node(tag, ns),
    createTextNode: (text) => new TextNode(text),
    createDocumentFragment: () => new Node('#fragment'),
    getElementById: () => null,
    addEventListener: () => {},
    removeEventListener: () => {},
    querySelector: (s) => documentElement.querySelector(s),
    querySelectorAll: (s) => documentElement.querySelectorAll(s),
  };
  globalThis.window = {
    document: globalThis.document,
    location: { href: `${BASE}/`, search: '', hash: '', pathname: '/' },
    // Deep-linking tabs (chat) call history.replaceState to keep the URL bar
    // in sync without going through app.js's navigate(); a shim without it
    // turned every such tab's deep-link activation into a thrown TypeError.
    history: { replaceState: () => {}, pushState: () => {}, back: () => {}, forward: () => {} },
    addEventListener: () => {},
    removeEventListener: () => {},
    matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    confirm: () => true,
    setTimeout,
    clearTimeout,
  };
  // Node ships a read-only `navigator`; the tabs only ever touch `clipboard`.
  if (!globalThis.navigator?.clipboard) {
    Object.defineProperty(globalThis, 'navigator', {
      configurable: true,
      value: { clipboard: { writeText: async () => {} }, userAgent: 'render-smoke' },
    });
  }
  globalThis.Node = Node;
  globalThis.EventSource = class { constructor() { this.readyState = 0; } close() {} addEventListener() {} };
  return { documentElement, body };
}

// ------------------------------------------------------------- API client

const cookies = new Map();

function cookieHeader() {
  return [...cookies.entries()].map(([k, v]) => `${k}=${v}`).join('; ');
}

function absorbCookies(response) {
  const raw = response.headers.getSetCookie?.() || [];
  for (const line of raw) {
    const [pair] = line.split(';');
    const index = pair.indexOf('=');
    if (index > 0) cookies.set(pair.slice(0, index).trim(), pair.slice(index + 1).trim());
  }
}

async function rawRequest(pathname, init = {}) {
  const response = await fetch(`${BASE}${pathname}`, {
    ...init,
    headers: { ...(init.headers || {}), cookie: cookieHeader() },
  });
  absorbCookies(response);
  return response;
}

const stats = { requests: 0, failures: [] };

/** The same unwrap-once contract `frontend/dist/api.js` implements. */
function makeApi() {
  let csrf = null;
  async function request(pathname, { method = 'GET', body, profile } = {}) {
    stats.requests += 1;
    const headers = {};
    if (body !== undefined) headers['content-type'] = 'application/json';
    if (!['GET', 'HEAD'].includes(method)) {
      if (!csrf) {
        const res = await rawRequest('/api/csrf');
        const payload = await res.json();
        csrf = payload.token || payload?.data?.token;
      }
      headers['x-csrf-token'] = csrf;
    }
    // Mirrors api.js's withProfile: a path that already carries `profile=` is
    // asking for a specific scope on purpose — the cross-profile aggregator
    // asks for `profile=all` — so the explicit value wins over the ambient one.
    // Appending unconditionally (as this did) produced `?profile=all&profile=
    // default`, and FastAPI takes the LAST value, so the harness silently saw
    // one profile's sessions where the real client sees every profile's.
    const url = profile && !/[?&]profile=/.test(pathname)
      ? `${pathname}${pathname.includes('?') ? '&' : '?'}profile=${encodeURIComponent(profile)}`
      : pathname;
    const response = await rawRequest(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    const text = await response.text();
    let payload = null;
    try { payload = text ? JSON.parse(text) : null; } catch { payload = text; }
    if (!response.ok) {
      const error = new Error(payload?.error?.message || `HTTP ${response.status}`);
      error.status = response.status;
      error.request_id = payload?.request_id || null;
      throw error;
    }
    if (payload && typeof payload === 'object' && 'data' in payload && 'meta' in payload) {
      return { data: payload.data, meta: payload.meta };
    }
    return { data: payload, meta: null };
  }
  return {
    request,
    get: (p, o) => request(p, { ...o, method: 'GET' }),
    post: (p, body, o) => request(p, { ...o, method: 'POST', body }),
    patch: (p, body, o) => request(p, { ...o, method: 'PATCH', body }),
    put: (p, body, o) => request(p, { ...o, method: 'PUT', body }),
    del: (p, o) => request(p, { ...o, method: 'DELETE' }),
    streamPost: async () => ({ close() {} }),
    ensureCsrf: async () => 'smoke',
  };
}

// ------------------------------------------------------------------- tabs

const TABS = [
  ['overview', 'createOverview'],
  ['chat', 'createChat'],
  ['fleet', 'createFleet'],
  ['run-inspector', 'createRunInspector'],
  ['analytics', 'createAnalytics'],
  ['kanban', 'createKanban'],
  ['sessions', 'createSessions'],
  ['cron', 'createCron'],
  ['permits', 'createPermits'],
  ['issues', 'createIssues'],
  ['threads', 'createThreads'],
  ['room-binding', 'createRoomBinding'],
  ['alerts', 'createAlerts'],
  ['activity', 'createActivity'],
  ['logs', 'createLogs'],
  ['skills', 'createSkills'],
  ['plugins', 'createPlugins'],
  ['profiles', 'createProfiles'],
  ['models', 'createModels'],
  ['tools', 'createTools'],
  ['mcp', 'createMcp'],
  ['memory', 'createMemory'],
  ['webhooks', 'createWebhooks'],
  ['channels', 'createChannels'],
  ['files', 'createFiles'],
  ['artifacts', 'createArtifacts'],
  ['action-audit', 'createActionAudit'],
  ['settings', 'createSettings'],
  ['command-center', 'createCommandCenter'],
];

// A second activation per tab with an entity param, because the deep-link path
// renders completely different markup from the list path — Run Inspector in
// particular only draws its graph once an entity is chosen.
const DEEP_LINKS = {
  // Chat's thread path renders nothing like its hero: header, paged history,
  // tool rows and the whole composer only exist once a session is selected.
  chat: { s: process.env.SMOKE_SESSION_ID || 'api_1786535961_f70efe27' },
  'run-inspector': { task: process.env.SMOKE_TASK_ID || 't_c68f413a' },
  kanban: { task: process.env.SMOKE_TASK_ID || 't_c68f413a' },
  analytics: { days: '7', from: '2026-08-04', to: '2026-08-06' },
};

function outline(node, depth = 0, acc = { nodes: 0, text: [] }) {
  acc.nodes += 1;
  if (node instanceof TextNode) {
    const t = node.textContent.trim();
    if (t) acc.text.push(t);
    return acc;
  }
  if (!node.children.length && node._text.trim()) acc.text.push(node._text.trim());
  for (const child of node.children) outline(child, depth + 1, acc);
  return acc;
}

async function run() {
  // Several tabs fire-and-forget their initial load; an unhandled rejection
  // there is a real finding, but it must be attributed, not fatal.
  const stray = [];
  process.on('unhandledRejection', (err) => stray.push(err));

  const { body } = installDom();
  const api = makeApi();
  // One session for the whole run: the BFF rate-limits session issuance per IP.
  await rawRequest('/api/csrf');

  const sse = { on: () => () => {}, close: () => {} };
  const results = [];

  for (const [route, factoryName] of TABS) {
    if (ONLY.length && !ONLY.includes(route)) continue;
    const container = new Node('div');
    body.append(container);
    const toolbar = new Node('div');
    body.append(toolbar);

    let status = 'ok';
    let detail = '';
    try {
      const module = await import(pathToFileURL(path.join(DIST, 'tabs', `${route}.js`)).href);
      const factory = module[factoryName];
      if (typeof factory !== 'function') throw new Error(`no export ${factoryName}`);
      const instance = factory({
        api, profile: null, events: null, sse, toolbar,
        onNavigate: () => {}, refreshInspector: () => {},
      });
      instance.mount(container);
      await instance.activate({});
      // Let any fire-and-forget load settle before measuring.
      await new Promise((resolve) => setTimeout(resolve, 250));
      const stat = outline(container);
      if (stat.nodes < 3) { status = 'EMPTY'; detail = `${stat.nodes} nodes`; }
      else detail = `${stat.nodes} nodes · ${stat.text.slice(0, 4).join(' | ').slice(0, 90)}`;

      if (DEEP_LINKS[route]) {
        await instance.activate(DEEP_LINKS[route]);
        await new Promise((resolve) => setTimeout(resolve, 250));
        const deep = outline(container);
        detail += `  ⟶ deep-link ${deep.nodes} nodes · ${deep.text.slice(0, 3).join(' | ').slice(0, 60)}`;
        if (deep.nodes < 3) { status = 'EMPTY'; }
      }

      if (typeof instance.deactivate === 'function') instance.deactivate();
      if (stray.length) {
        status = 'FAIL';
        detail = `unhandled rejection: ${stray[0].message}\n     ${(stray[0].stack || '').split('\n').slice(1, 4).join('\n     ')}`;
        stats.failures.push(route);
        stray.length = 0;
      }
    } catch (err) {
      status = 'FAIL';
      detail = `${err.message}\n     ${(err.stack || '').split('\n').slice(1, 4).join('\n     ')}`;
      stats.failures.push(route);
    }
    results.push({ route, status, detail });
    const mark = status === 'ok' ? ' ok ' : status === 'EMPTY' ? 'EMPT' : 'FAIL';
    console.log(`[${mark}] ${route.padEnd(16)} ${detail}`);
  }

  console.log(`\n${results.length} tabs · ${stats.requests} BFF requests · ${stats.failures.length} failures`);
  if (stats.failures.length) {
    console.log(`FAILED: ${stats.failures.join(', ')}`);
    process.exitCode = 1;
  } else {
    console.log('RENDER_SMOKE=PASS');
  }
}

// Exported so tools/inspector_audit.mjs can reuse the shim rather than keeping
// a second, silently diverging copy of it.
export { installDom, Node, TextNode, makeApi, rawRequest, outline, TABS, DEEP_LINKS, DIST };

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  run().catch((err) => { console.error(err); process.exitCode = 1; });
}
