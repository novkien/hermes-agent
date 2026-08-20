#!/usr/bin/env node

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const DIST = join(HERE, '..', 'frontend', 'dist');
const APP = join(HERE, '..', 'agent_mission_control');
const CONFIG = join(HERE, '..', 'config');

const routeRegistry = readFileSync(join(DIST, 'pure', 'route-registry.js'), 'utf8');
const shell = readFileSync(join(DIST, 'app.js'), 'utf8');
const index = readFileSync(join(DIST, 'index.html'), 'utf8');
const tab = readFileSync(join(DIST, 'tabs', 'repositories.js'), 'utf8');
const css = readFileSync(join(DIST, 'repositories.css'), 'utf8');
const appPy = readFileSync(join(APP, 'app.py'), 'utf8');
const routesPy = readFileSync(join(APP, 'repository_routes.py'), 'utf8');
const servicePy = readFileSync(join(APP, 'repository_sync.py'), 'utf8');
const registry = readFileSync(join(CONFIG, 'repositories.yaml'), 'utf8');

assert.match(routeRegistry, /repositories:\s*route\('repositories', 'Repositories'/);
assert.match(routeRegistry, /group: 'BUILD & INTEGRATE'/);
assert.match(shell, /repositories:\s*\(\) => import\('\.\/tabs\/repositories\.js'\)/);
assert.match(index, /repositories\.css/);
assert.match(appPy, /build_repository_router/);
assert.match(appPy, /include_router\(build_repository_router\(deps\.router\)\)/);

for (const path of [
  '/api/repositories',
  '/api/repositories/{repo}/initialize',
  '/api/repositories/{repo}/pull',
  '/api/repositories/{repo}/pulls/{number}/codex-review',
  '/api/repositories/{repo}/pulls/{number}/ready',
  '/api/repositories/{repo}/pulls/{number}/draft',
  '/api/repositories/{repo}/pulls/{number}/merge-and-pull',
]) {
  assert.ok(routesPy.includes(path), `missing repository API path: ${path}`);
}

for (const name of [
  'hermes-agent', 'hermes-skills', 'hermes-plugins', 'agents',
  'llama-proxy', '9router', 'godot-mcp',
]) {
  assert.match(registry, new RegExp(`^  ${name}:`, 'm'));
}
assert.match(registry, /git_dir: repos\/\{repository\}\.git/);
assert.doesNotMatch(registry, /production_worktree|worktrees\/\{repository\}\/production/);
assert.match(registry, /hermes-skills:[\s\S]*work_tree: \.\n[\s\S]*paths:[\s\S]*- skills[\s\S]*- workspace\/skills-pack/);
assert.match(registry, /hermes-plugins:[\s\S]*work_tree: plugins/);
assert.match(registry, /agents:[\s\S]*work_tree: profiles/);
assert.match(registry, /llama-proxy:[\s\S]*host: jarvis-pi[\s\S]*work_tree: \/home\/pi\/llama-proxy/);
assert.match(registry, /9router:[\s\S]*host: jarvis-pi[\s\S]*work_tree: \/home\/pi\/9router/);
assert.match(registry, /godot-mcp:[\s\S]*host: workstation[\s\S]*work_tree: \/home\/novkien\/godot-mcp/);

assert.match(tab, /Review with Codex/);
assert.match(tab, /Merge & Pull/);
assert.match(tab, /Pull production/);
assert.match(tab, /Initialize repository layout/);
assert.match(tab, /Codex/);
assert.match(tab, /Selected PR evidence/);
assert.match(tab, /Partial success/);
assert.match(tab, /REPOSITORY_CACHE_TTL_MS = 60_000/);
assert.doesNotMatch(tab, /Auto-commit/);
assert.doesNotMatch(tab, /Commit local/);
assert.doesNotMatch(tab, /Sync all/);
assert.doesNotMatch(tab, /Safe sync/);

assert.match(servicePy, /git merge --ff-only/);
assert.match(servicePy, /--git-dir=/);
assert.match(servicePy, /--work-tree=/);
assert.match(servicePy, /production_dirty/);
assert.match(servicePy, /partial_success/);
assert.match(servicePy, /@codex review/);
assert.doesNotMatch(servicePy, /stash push/);
assert.doesNotMatch(servicePy, /git add -A/);
assert.doesNotMatch(servicePy, /shutil\.copy/);
assert.doesNotMatch(servicePy, /deployment_root/);
assert.doesNotMatch(servicePy, /path_candidates/);
assert.doesNotMatch(servicePy, /worktrees\/<repo>|worktrees\/\{repository\}\/production/);

assert.match(css, /\.repo-grid/);
assert.match(css, /\.repo-pr-control/);
assert.match(css, /\.repo-review-thread/);

console.log('repository_surface: OK');
