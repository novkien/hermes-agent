// SSE client — single EventSource with native reconnection (browser sends
// Last-Event-ID automatically), heartbeat comments ignored, dedupe by
// event_id, connection state surfaced. Events dispatch to subscribed tabs.

import { dedupeEvents } from './pure/alert-sort.js';

export const SSE_STATE = Object.freeze({
  CONNECTING: 'connecting',
  OPEN: 'open',
  ERROR: 'error',
  CLOSED: 'closed',
});

export class SseClient {
  constructor({ url = '/api/events/stream', token = null } = {}) {
    this.baseUrl = url;
    this.token = token;
    // The session whose live turn should ride this stream. Folded into the one
    // connection the SPA already has rather than opening a second: a browser
    // allows six HTTP/1.1 connections per host, and a permanent extra stream
    // per open tab starved every ordinary request behind it.
    this.watchId = null;
    this.es = null;
    this.state = SSE_STATE.CLOSED;
    this.listeners = new Map(); // eventType -> Set<fn>
    this.seenIds = new Set();
    this.maxSeen = 1000;
    this.onStateChange = null;
  }

  get url() {
    const params = new URLSearchParams();
    if (this.token) params.set('token', this.token);
    if (this.watchId) params.set('watch', this.watchId);
    const query = params.toString();
    return query ? `${this.baseUrl}?${query}` : this.baseUrl;
  }

  /**
   * Point the stream's live-turn overlay at a session (or `null` for none).
   *
   * Reconnecting is what applies it, because the watched session is part of the
   * request. That costs a moment of fleet events, which the browser's own
   * `Last-Event-ID` replay covers — a far better trade than a second permanent
   * connection per tab.
   */
  watch(sessionId) {
    const next = sessionId || null;
    if (next === this.watchId) return;
    this.watchId = next;
    if (!this.es) return;
    this.es.close();
    this.es = null;
    this.connect();
  }

  connect() {
    if (this.es) return;
    this.setState(SSE_STATE.CONNECTING);
    const es = new EventSource(this.url);
    this.es = es;

    es.onopen = () => this.setState(SSE_STATE.OPEN);
    es.onerror = () => {
      // EventSource reconnects natively; surface the state without restarting.
      this.setState(this.es && this.es.readyState === EventSource.CONNECTING
        ? SSE_STATE.CONNECTING
        : SSE_STATE.ERROR);
    };
    es.onmessage = (ev) => this.dispatch('message', ev.data);
    // Named events: task.changed, permit.changed, issue.changed, cron.changed,
    // source.health, alert.changed, cache.invalidated, session.changed, ...
    for (const type of SseClient.EVENT_TYPES) {
      es.addEventListener(type, (ev) => this.dispatch(type, ev.data));
    }
    // Live turn frames are a stream, not a log: they carry no `event_id`, and
    // there is nothing to replay or de-duplicate about a token that has already
    // been painted. So they take their own path around `dispatch`, which exists
    // to enforce exactly those two properties on bus events.
    es.addEventListener('chat.frame', (ev) => this.dispatchRaw('chat.frame', ev.data));
    // Heartbeat comments are ignored by EventSource; nothing to do.
  }

  dispatchRaw(type, data) {
    if (!data) return;
    let payload;
    try {
      payload = JSON.parse(data);
    } catch {
      return;
    }
    for (const fn of this.listeners.get(type) || new Set()) {
      try {
        fn(payload);
      } catch (err) {
        console.error('SSE listener error', err);
      }
    }
  }

  dispatch(type, data) {
    if (!data) return;
    let event;
    try {
      event = JSON.parse(data);
    } catch {
      return; // malformed payload — ignore, never fabricate
    }
    if (!event || !event.event_id) return;
    if (this.seenIds.has(event.event_id)) return; // dedupe
    this.seenIds.add(event.event_id);
    if (this.seenIds.size > this.maxSeen) {
      const it = this.seenIds.values().next();
      if (it.value !== undefined) this.seenIds.delete(it.value);
    }
    const set = this.listeners.get(event.event_type || type) || new Set();
    for (const fn of set) {
      try {
        fn(event);
      } catch (err) {
        console.error('SSE listener error', err);
      }
    }
    // Also notify generic listeners with the raw event.
    const any = this.listeners.get('*') || new Set();
    for (const fn of any) {
      try {
        fn(event);
      } catch (err) {
        console.error('SSE listener error', err);
      }
    }
  }

  on(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(fn);
    return () => this.off(type, fn);
  }

  off(type, fn) {
    this.listeners.get(type)?.delete(fn);
  }

  setState(state) {
    this.state = state;
    if (this.onStateChange) this.onStateChange(state);
  }

  close() {
    if (this.es) {
      this.es.close();
      this.es = null;
    }
    this.setState(SSE_STATE.CLOSED);
  }

  static get EVENT_TYPES() {
    return [
      // This list is an allowlist, not documentation: an event type missing
      // from it is dropped silently by EventSource's named-listener dispatch.
      'task.changed', 'run.changed', 'session.changed', 'session.running',
      'permit.changed',
      'issue.changed', 'cron.changed', 'log.appended', 'alert.changed',
      'source.health', 'cache.invalidated',
      // Note: `chat.frame` is deliberately NOT here — it is registered
      // separately in `connect()` because it bypasses `dispatch`.
    ];
  }
}

export { dedupeEvents };
