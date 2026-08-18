import assert from 'node:assert/strict';
import fs from 'node:fs';
import { JSDOM } from 'jsdom';

const ROOT = new URL('../frontend/dist/', import.meta.url);
const coreRoutes = [
  'overview', 'sessions', 'chat', 'fleet', 'kanban', 'permits',
  'issues', 'cron', 'activity', 'alerts', 'analytics',
];

for (const route of coreRoutes) {
  const source = fs.readFileSync(new URL(`tabs/${route}.js`, ROOT), 'utf8');
  assert.equal(source.includes('setInterval('), false, `${route} still owns a browser polling timer`);
  assert.match(source, /activate\(.*\)\s*\{/, `${route} has no retained-route activation lifecycle`);
  if (route !== 'activity') {
    assert.match(source, /liveStore/, `${route} is not wired to the shared live resource store`);
  }
}

for (const route of ['overview', 'sessions', 'fleet', 'kanban', 'permits', 'issues', 'cron', 'alerts', 'analytics']) {
  const source = fs.readFileSync(new URL(`tabs/${route}.js`, ROOT), 'utf8');
  assert.match(source, /bindLiveResources\(/, `${route} does not subscribe by resource revision`);
}

const chat = fs.readFileSync(new URL('tabs/chat.js', ROOT), 'utf8');
assert.match(chat, /chat\.frame/, 'chat token frames are no longer handled incrementally');
assert.match(chat, /visibilitychange/, 'chat has no event-driven background-tab catch-up');
assert.match(chat, /setTimeout\(/, 'chat has no scheduled transcript catch-up');
assert.match(chat, /lastChatFrameAt/, 'chat cannot recover when a watched stream goes silent');
assert.match(chat, /WATCH_SILENT_CATCHUP_MS/, 'chat does not promptly catch up a silent remote watcher');
assert.match(
  chat,
  /sse\.watch\(sessionId, workerLink \? null : sessionProfile\)/,
  'Kanban worker chat is not watching the dispatcher observation hub',
);

const activity = fs.readFileSync(new URL('tabs/activity.js', ROOT), 'utf8');
assert.match(activity, /createKeyedReconciler\(/, 'activity feed does not retain keyed event rows');
assert.match(activity, /sse\.on\(/, 'activity feed does not append live SSE events');

const dom = new JSDOM('<!doctype html><body></body>', { pretendToBeVisual: true });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.matchMedia = () => ({ matches: true });

const { createStatRow } = await import('../frontend/dist/components/stat.js');
const { barChart } = await import('../frontend/dist/components/chart.js');

const statRow = createStatRow([
  { label: 'Running tasks', value: 1, foot: 'one', spark: [1, 2] },
  { label: 'Issues', value: 2 },
]);
document.body.append(statRow);
const taskStat = statRow.children[0];
const taskValue = taskStat.querySelector('.stat-value');
statRow.setStats([
  { label: 'Running tasks', value: 7, foot: 'seven', spark: [2, 4] },
  { label: 'Issues', value: 2 },
]);
assert.equal(statRow.children[0], taskStat, 'unchanged KPI key lost its DOM node');
assert.equal(taskStat.querySelector('.stat-value'), taskValue, 'KPI value node was rebuilt');
assert.equal(taskValue.textContent, '7');

const chart = barChart({
  labels: ['A', 'B'],
  series: [{ label: 'Tokens', values: [1, 2], seriesIndex: 1 }],
});
document.body.append(chart);
const firstBar = chart.querySelector('[data-bucket="0"][data-series="0"]');
assert.equal(chart.update({
  labels: ['A', 'B'],
  series: [{ label: 'Tokens', values: [3, 5], seriesIndex: 1 }],
}), true);
assert.equal(chart.querySelector('[data-bucket="0"][data-series="0"]'), firstBar, 'chart rebuilt a stable bar');
assert.notEqual(firstBar.getAttribute('height'), '0');

console.log('LIVE_ROUTE_CONTRACTS=PASS');
