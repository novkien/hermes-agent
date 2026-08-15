#!/usr/bin/env node

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const DIST = join(HERE, '..', 'frontend', 'dist');
const APP = join(HERE, '..', 'agent_mission_control');

const routeRegistry = readFileSync(join(DIST, 'pure', 'route-registry.js'), 'utf8');
const shell = readFileSync(join(DIST, 'app.js'), 'utf8');
const index = readFileSync(join(DIST, 'index.html'), 'utf8');
const tab = readFileSync(join(DIST, 'tabs', 'repositories.js'), 'utf8');
const css = readFileSync(join(DIST, 'repositories.css'), 'utf8');
const appPy = readFileSync(join(APP, 'app.py'), 'utf8');
const routesPy = readFileSync(join(APP, 'repository_routes.py'), 'utf8');

assert.match(routeRegistry, /repositories:\s*route\('repositories', 'Repositories'/);
assert.match(routeRegistry, /group: 'BUILD & INTEGRATE'/);
assert.match(shell, /repositories:\s*\(\) => import\('\.\/tabs\/repositories\.js'\)/);
assert.match(index, /repositories\.css/);
assert.match(appPy, /build_repository_router/);
assert.match(appPy, /include_router\(build_repository_router\(deps\.router\)\)/);

for (const path of [
  '/api/repositories',
  '/api/repositories/sync-all',
  '/api/repositories/{repo}/sync',
  '/api/repositories/{repo}/commit',
  '/api/repositories/{repo}/upstream-sync',
  '/api/repositories/{repo}/pulls/{number}/rebase-merge',
]) {
  assert.ok(routesPy.includes(path), `missing repository API path: ${path}`);
}

assert.match(tab, /renderInspector/);
assert.match(tab, /Failure \/ conflict/);
assert.match(tab, /Preserved stash/);
assert.match(tab, /Backup branch/);
assert.match(tab, /Rebase & merge/);
assert.match(tab, /Auto-commit/);
assert.match(tab, /30s refresh/);
assert.match(tab, /Cron-safe command/);
assert.match(tab, /Future hook trigger/);
assert.match(css, /\.repo-grid/);
assert.match(css, /\.repo-side-section/);
assert.match(css, /\.repo-side-alert-danger/);

console.log('repository_surface: OK');
