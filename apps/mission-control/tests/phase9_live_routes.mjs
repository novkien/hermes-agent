#!/usr/bin/env node

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const LIVE_ROUTES = [
  'room-binding.js', 'threads.js', 'action-audit.js', 'skills.js', 'memory.js',
  'profiles.js', 'models.js', 'tools.js', 'mcp.js', 'plugins.js', 'webhooks.js',
  'channels.js', 'artifacts.js', 'files.js', 'repositories.js', 'system-manager.js',
  'logs.js', 'command-center.js', 'settings.js', 'llama-proxy.js', '9router.js',
];

for (const file of LIVE_ROUTES) {
  const source = await readFile(new URL(`../frontend/dist/tabs/${file}`, import.meta.url), 'utf8');
  assert.doesNotMatch(source, /setInterval\s*\(/, `${file} still owns an operational polling timer`);
  assert.match(source, /liveStore/, `${file} does not accept the shared live store`);
  assert.match(source, /bindLiveResources/, `${file} does not subscribe to revisioned resources`);
}

for (const file of ['repositories.js', 'system-manager.js']) {
  const source = await readFile(new URL(`../frontend/dist/tabs/${file}`, import.meta.url), 'utf8');
  assert.match(source, /resyncResource/, `${file} manual refresh does not use the live resync path`);
}

const appSource = await readFile(new URL('../frontend/dist/app.js', import.meta.url), 'utf8');
assert.match(appSource, /instanceToolbarHost/, 'retained routes do not own isolated toolbar hosts');
assert.match(appSource, /instanceInspectorHost/, 'retained routes do not own isolated inspector hosts');
assert.match(appSource, /instance\.__toolbarHost/, 'active route does not mount its own toolbar host');
assert.match(appSource, /owner\.__inspectorHost/, 'active route does not mount its own inspector host');

console.log('PHASE9_LIVE_ROUTES=PASS');
