// Same-origin BFF API client.
//
// Contract:
// - credentials remain browser-managed; no upstream secret reaches the SPA;
// - successful {data, meta} responses are unwrapped exactly once;
// - non-2xx responses preserve the BFF status and request id;
// - SSE mutations are consumed incrementally instead of being treated as JSON.

import { errorMessage } from './pure/data-shape.js';

export class ApiClient {
  constructor({ base = '', csrfUrl = '/api/csrf' } = {}) {
    this.base = base;
    this.csrfUrl = csrfUrl;
    this.csrfToken = null;
  }

  async ensureCsrf() {
    if (this.csrfToken) return this.csrfToken;
    const res = await fetch(`${this.base}${this.csrfUrl}`, { credentials: 'same-origin' });
    const payload = await readPayload(res);
    if (!res.ok) throw requestError(res, payload, 'CSRF request failed');
    this.csrfToken = payload && (payload.token || (payload.data && payload.data.token));
    if (!this.csrfToken) throw new Error('csrf: no token in response');
    return this.csrfToken;
  }

  async request(path, {
    method = 'GET', body, headers = {}, profile, signal,
  } = {}) {
    const upperMethod = String(method || 'GET').toUpperCase();
    const url = withProfile(`${this.base}${path}`, profile);

    const opts = {
      method: upperMethod,
      credentials: 'same-origin',
      headers: { ...headers },
      signal,
    };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    if (!['GET', 'HEAD', 'OPTIONS'].includes(upperMethod)) {
      try {
        opts.headers['X-CSRF-Token'] = await this.ensureCsrf();
      } catch (err) {
        throw new Error(`mutation blocked: csrf unavailable (${err.message})`);
      }
    }

    const res = await fetch(url.toString(), opts);
    const payload = await readPayload(res);
    if (!res.ok) throw requestError(res, payload);

    if (payload && typeof payload === 'object'
      && Object.prototype.hasOwnProperty.call(payload, 'data')
      && Object.prototype.hasOwnProperty.call(payload, 'meta')) {
      return { data: payload.data, meta: payload.meta };
    }
    return { data: payload, meta: null };
  }

  async streamPost(path, body, {
    profile, headers = {}, onEvent = () => {}, onOpen = () => {},
  } = {}) {
    const csrf = await this.ensureCsrf();
    return this._stream(path, {
      profile, onEvent, onOpen,
      init: {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, ...headers },
        body: JSON.stringify(body ?? {}),
      },
    });
  }

  async _stream(path, { profile, onEvent, onOpen, init }) {
    const url = withProfile(`${this.base}${path}`, profile);
    const controller = new AbortController();

    const res = await fetch(url.toString(), {
      credentials: 'same-origin',
      signal: controller.signal,
      ...init,
    });
    if (!res.ok) {
      const payload = await readPayload(res);
      throw requestError(res, payload);
    }
    if (!res.body) throw new Error('stream response has no readable body');

    onOpen({ status: res.status, requestId: res.headers.get('x-request-id') });
    const done = consumeEventStream(res.body, onEvent, controller.signal);
    return { controller, done };
  }

  async get(path, opts = {}) {
    return this.request(path, { ...opts, method: 'GET' });
  }

  async post(path, body, opts = {}) {
    return this.request(path, { ...opts, method: 'POST', body });
  }

  async put(path, body, opts = {}) {
    return this.request(path, { ...opts, method: 'PUT', body });
  }

  async patch(path, body, opts = {}) {
    return this.request(path, { ...opts, method: 'PATCH', body });
  }

  async del(path, opts = {}) {
    return this.request(path, { ...opts, method: 'DELETE' });
  }
}

// The active profile is ambient state, so callers pass it as an option rather
// than spelling it into every path. A path that already carries `profile=` is
// asking for a specific scope on purpose — the cross-profile aggregator asks
// for `profile=all` — so the explicit value wins over the ambient one.
function withProfile(path, profile) {
  const url = new URL(path, window.location.origin);
  if (profile && !url.searchParams.has('profile')) {
    url.searchParams.set('profile', profile);
  }
  return url;
}

async function readPayload(res) {
  const contentType = res.headers.get('content-type') || '';
  if (contentType.includes('json')) {
    try {
      return await res.json();
    } catch (_err) {
      return null;
    }
  }
  try {
    const text = await res.text();
    return text || null;
  } catch (_err) {
    return null;
  }
}

function requestError(res, payload, fallback = null) {
  const body = payload && typeof payload === 'object'
    && Object.prototype.hasOwnProperty.call(payload, 'data')
    ? payload.data
    : payload;
  const err = new Error(errorMessage(body, fallback || `HTTP ${res.status}`));
  err.status = res.status;
  err.request_id = payload?.meta?.request_id || payload?.request_id || null;
  err.payload = payload;
  return err;
}

async function consumeEventStream(stream, onEvent, signal) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      if (signal.aborted) break;
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
      let boundary = buffer.indexOf('\n\n');
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        emitFrame(frame, onEvent);
        boundary = buffer.indexOf('\n\n');
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) emitFrame(buffer, onEvent);
  } finally {
    reader.releaseLock();
  }
}

function emitFrame(frame, onEvent) {
  let event = 'message';
  let id = null;
  let hasData = false;
  const dataLines = [];
  for (const line of String(frame || '').split('\n')) {
    if (!line || line.startsWith(':')) continue;
    const index = line.indexOf(':');
    const field = index === -1 ? line : line.slice(0, index);
    let value = index === -1 ? '' : line.slice(index + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') event = value || 'message';
    else if (field === 'id') id = value;
    else if (field === 'data') { dataLines.push(value); hasData = true; }
  }
  // A block with no `data:` field at all is not an event — it is a comment,
  // and comments are how both the gateway and this BFF keep an idle stream
  // alive (`: keepalive` every 20s). Dispatching them as `{event:'message'}`
  // handed every consumer a phantom frame: the watcher below read one as the
  // start of a turn nobody had started and opened an activity bar that then
  // counted up forever, because no real frame was ever coming. The SSE spec
  // says the same thing — dispatch only when the data buffer is non-empty.
  if (!hasData) return;
  const raw = dataLines.join('\n');
  let data = raw;
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch (_err) {
      data = raw;
    }
  }
  onEvent({ event, id, data, raw });
}

export const api = new ApiClient();
