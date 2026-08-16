import assert from 'node:assert/strict';
import { createLiveResourceStore } from '../frontend/dist/live-store.js';
import { SseClient } from '../frontend/dist/events.js';

const scheduled = [];
const calls = [];
const pending = [];

const api = {
  get(path, options) {
    calls.push({ path, options });
    if (pending.length) return pending.shift()(path, options);
    return Promise.resolve({ data: { profile_id: options.profile, route: 'overview', resources: {} } });
  },
};

const store = createLiveResourceStore({ api, schedule: (fn) => scheduled.push(fn) });
const stream = new SseClient({ profile: 'alpha' });
assert.match(stream.url, /profile=alpha/);
stream.setProfile('beta');
assert.match(stream.url, /profile=beta/);

function bootstrap(profile, revision, id, title) {
  return {
    data: {
      profile_id: profile,
      route: 'overview',
      resources: {
        issues: {
          revision,
          entities: [{ entity_id: id, revision, payload: { id: Number(id), title } }],
          snapshot: { count: 1 },
          provenance: 'live',
        },
      },
    },
  };
}

pending.push(() => Promise.resolve(bootstrap('alpha', 1, '1', 'alpha')));
await store.hydrate('overview', 'alpha');
assert.equal(store.view().entities('issues')[0].title, 'alpha');

// Same route/profile request is single-flight.
let releaseSingle;
pending.push(() => new Promise((resolve) => { releaseSingle = resolve; }));
const singleA = store.hydrate('issues', 'alpha');
const callsBeforeSecond = calls.length;
const singleB = store.hydrate('issues', 'alpha');
assert.equal(calls.length, callsBeforeSecond, 'single-flight issued a duplicate request');
releaseSingle({ data: { profile_id: 'alpha', route: 'issues', resources: {} } });
await singleA;

// An obsolete profile response may complete, but can never overwrite active state.
let releaseOld;
pending.push(() => new Promise((resolve) => { releaseOld = resolve; }));
const old = store.hydrate('overview', 'alpha', { force: true });
pending.push(() => Promise.resolve(bootstrap('beta', 1, '2', 'beta')));
await store.hydrate('overview', 'beta', { force: true });
releaseOld(bootstrap('alpha', 99, '1', 'obsolete'));
await old;
assert.equal(store.profile, 'beta');
assert.equal(store.view().entities('issues')[0].title, 'beta');
for (const frame of scheduled.splice(0)) frame();

let notifications = 0;
store.subscribe(
  (view) => view.resource('issues').revision,
  () => { notifications += 1; },
);
for (let revision = 2; revision <= 101; revision += 1) {
  store.applyEvent({
    event_id: `e-${revision}`,
    resource_key: 'issues', operation: 'upsert', profile_id: 'beta',
    entity_id: '2', revision, payload: { id: 2, title: `beta-${revision}` },
  });
}
assert.equal(scheduled.length > 0, true);
const frames = scheduled.splice(0);
assert.equal(frames.length, 1, '100 events scheduled more than one DOM frame');
frames[0]();
assert.equal(notifications, 1);
assert.equal(store.view().entities('issues')[0].title, 'beta-101');

// Replayed/stale revision for the same entity is idempotent.
assert.equal(store.applyEvent({
  resource_key: 'issues', operation: 'upsert', profile_id: 'beta',
  entity_id: '2', revision: 100, payload: { id: 2, title: 'stale' },
}), false);
assert.equal(store.view().entities('issues')[0].title, 'beta-101');

store.applyEvent({
  resource_key: 'issues', operation: 'delete', profile_id: 'beta',
  entity_id: '2', revision: 102, payload: {},
});
assert.equal(store.view().entities('issues').length, 0);

// A failed force-resync preserves last-known-good state and marks it stale.
store.applyEvent({
  resource_key: 'issues', operation: 'upsert', profile_id: 'beta',
  entity_id: '2', revision: 103, payload: { id: 2, title: 'last-good' },
});
pending.push(() => Promise.reject(new Error('offline')));
await store.forceResync('overview', 'beta');
assert.equal(store.view().entities('issues')[0].title, 'last-good');
assert.equal(store.view().resource('issues').provenance, 'stale');
assert.match(store.view().resource('issues').error, /offline/);

// Profile A data remains isolated in its own map.
store.setContext('alpha', 'overview');
assert.equal(store.view().entities('issues')[0].title, 'alpha');

assert.equal(String(createLiveResourceStore).includes('localStorage'), false);
assert.equal(String(createLiveResourceStore).includes('indexedDB'), false);
store.dispose();
console.log('LIVE_STORE_CONTRACTS=PASS');
