// AgentOS Repositories — owner repository control plane.

import { el, clear, skeleton, unavailableState, statusChip } from '../ui.js';
import { bindLiveResources, liveRows } from './_live.js';
import { createKeyedReconciler } from '../pure/keyed-dom.js';

export const ROUTE = 'repositories';
export const LABEL = 'Repositories';
export const GROUP = 'BUILD & INTEGRATE';
export const SOURCE_ENDPOINTS = Object.freeze(['/api/repositories']);

const ATTENTION_STATES = new Set([
  'layout_missing', 'behind', 'dirty', 'ahead', 'diverged', 'conflict',
  'origin_mismatch', 'wrong_branch', 'error',
]);
const REPOSITORY_CACHE_TTL_MS = 60_000;
const REPOSITORIES_CACHE = new Map();
const REPO_TONES = Object.freeze({
  synced: 'ok', layout_missing: 'warn', behind: 'warn', dirty: 'warn',
  remote_only: 'info',
  ahead: 'danger', diverged: 'danger', conflict: 'danger',
  origin_mismatch: 'danger', wrong_branch: 'danger',
  error: 'danger',
});
const CHECK_TONES = Object.freeze({ passed: 'ok', pending: 'warn', failed: 'danger', none: 'idle' });
const CODEX_TONES = Object.freeze({
  reviewed: 'ok', requested: 'info', stale: 'warn',
  has_findings: 'danger', not_requested: 'idle',
});
// Mirrors repository_routes.REPOSITORY_MUTATIONS. The read envelope's
// meta.mutations_supported is the honest capability signal; the tab gates its
// action buttons on it instead of guessing from the route table.
const REPOSITORY_MUTATIONS = Object.freeze([
  'initialize_layout', 'sync', 'codex_review',
  'mark_ready', 'mark_draft', 'merge_pr',
]);

function clone(value) {
  if (typeof structuredClone === 'function') return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}

function appendPresent(parent, ...children) {
  parent.append(...children.filter((child) => child !== null && child !== undefined));
}

// Busy keys are `action:repo` or `action:repo:number`. Match whole segments:
// a substring test would let `sync:hermes-agent` freeze a future `agent`
// card, since ':agent' occurs inside ':hermes-agent'.
function keyMatches(key, repoName, pullNumber = null) {
  const parts = String(key).split(':');
  if (parts.length < 2 || parts[1] !== repoName) return false;
  if (pullNumber === null) return true;
  return parts.length >= 3 && Number(parts[2]) === Number(pullNumber);
}

// Live projections are deliberately narrow (no layout, no pull requests), so
// they overlay the full rows field-by-field — but a repo missing from the
// projection must keep its full row instead of vanishing until the next load.
function unionProjectedRows(current, projected) {
  const merged = new Map();
  for (const row of current || []) merged.set(String(row.name), row);
  for (const row of projected || []) {
    merged.set(String(row.name), { ...(merged.get(String(row.name)) || {}), ...row });
  }
  return [...merged.values()];
}

function cacheKey(profile) {
  return `repository-control:${String(profile || 'default')}`;
}

function readCache(profile) {
  const value = REPOSITORIES_CACHE.get(cacheKey(profile));
  if (!value || !value.loaded_at || Date.now() - value.loaded_at > REPOSITORY_CACHE_TTL_MS) return null;
  return value;
}

function writeCache(profile, value) {
  REPOSITORIES_CACHE.set(cacheKey(profile), { ...value, loaded_at: Date.now() });
}

function shortSha(value) {
  return value ? String(value).slice(0, 9) : '—';
}

function fmtWhen(value) {
  if (!value) return '—';
  const stamp = new Date(value);
  if (Number.isNaN(stamp.getTime())) return String(value);
  const seconds = Math.max(0, Math.round((Date.now() - stamp.getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return stamp.toLocaleString();
}

function label(value) {
  return String(value || 'unknown').replaceAll('_', ' ');
}

function button(text, onclick, { primary = false, danger = false, disabled = false, title = '' } = {}) {
  return el('button', {
    class: `btn btn-sm${primary ? ' btn-primary' : ''}${danger ? ' btn-danger' : ''}`,
    type: 'button',
    disabled: disabled ? '' : undefined,
    title: title || undefined,
    onclick,
  }, text);
}

function repoFailure(repo) {
  return repo?.error || repo?.github_error || null;
}

function operationFailure(operation) {
  return operation?.error || null;
}

function operationMessage(operation) {
  if (!operation) return null;
  if (operation.ok) {
    if (operation.action === 'merge_pr') {
      const pin = operation.superproject_pin || {};
      if (operation.repo === 'hermes') {
        return `Hermes PR #${operation.pull_number} merged. Use Sync Hermes to converge the local superproject.`;
      }
      if (pin.managed) {
        const pull = pin.pull_number ? ` PR #${pin.pull_number}` : ' PR';
        return `PR #${operation.pull_number} merged on GitHub. Hermes gitlink${pull} ${pin.state === 'created' ? 'created' : 'updated'} at ${shortSha(pin.target_sha)}; merge it on the Hermes card, then use Sync Hermes.`;
      }
      return `PR #${operation.pull_number} merged on GitHub. This repository has no Hermes local projection.`;
    }
    if (operation.action === 'sync') {
      return `Hermes superproject sync complete at ${shortSha(operation.after?.local_sha)}; submodule pins and layout verified.`;
    }
    if (operation.action === 'prepare_superproject_pin') {
      const pin = operation.superproject_pin || {};
      if (pin.state === 'already_pinned') {
        return `${pin.repository || 'Repository'} is already pinned at ${shortSha(pin.target_sha)} on Hermes master.`;
      }
      const pull = pin.pull_number ? ` #${pin.pull_number}` : '';
      return `Hermes gitlink PR${pull} ${pin.state === 'created' ? 'created' : 'updated'} for ${pin.repository || 'repository'} at ${shortSha(pin.target_sha)}.`;
    }
    if (operation.action === 'codex_review') return 'Codex review requested for the current PR head.';
    if (operation.action === 'initialize_layout') return 'Canonical Git directory and live source are ready.';
    return 'Repository operation completed.';
  }
  if (operation.partial_success) {
    if (operation.action === 'merge_pr' && operation.completed_phase === 'github_merge') {
      return `PR #${operation.pull_number} merged on GitHub, but the Hermes gitlink PR was not prepared: ${operation.error?.message || 'unknown error'}`;
    }
    // The service emits two distinct partial shapes: the GitHub merge landed
    // but the production pull failed, or both landed and only the stashed
    // local work failed to restore. They need different guidance.
    if (operation.completed_phase === 'production_pull') {
      return `Merge and production pull succeeded, but restoring stashed local work failed: ${operation.error?.message || 'unknown error'}`;
    }
    return `GitHub merge succeeded, but production pull failed: ${operation.error?.message || 'unknown error'}`;
  }
  return operation.error?.message || 'Repository operation failed.';
}

function checkSummary(pull) {
  const checks = pull?.checks || {};
  if (checks.state === 'passed') return `${checks.passed || checks.total || 0} passed`;
  if (checks.state === 'failed') return `${checks.failed || 0} failed`;
  if (checks.state === 'pending') return `${checks.pending || 0} pending`;
  return 'No checks';
}

function codexSummary(pull) {
  const codex = pull?.codex || {};
  if (codex.state === 'reviewed') return `Reviewed ${shortSha(codex.reviewed_sha)}`;
  if (codex.state === 'has_findings') return `${codex.unresolved_threads || 0} unresolved`;
  if (codex.state === 'stale') return `Stale · ${shortSha(codex.reviewed_sha)}`;
  if (codex.state === 'requested') return `Requested ${fmtWhen(codex.requested_at)}`;
  return 'Not requested';
}

export function createRepositories({ api, profile, toolbar, liveStore }) {
  const root = el('div', { class: 'tab tab-repositories' });
  const page = el('div', { class: 'repo-page' });
  root.append(page);

  let repositories = [];
  let operations = [];
  let registry = {};
  let selectedName = null;
  let selectedPullNumber = null;
  let filter = 'all';
  let search = '';
  let loading = false;
  let refreshing = false;
  let loadedFromSource = false;
  let loadError = null;
  let lastOperation = null;
  let inspectorHost = null;
  let unsubscribe = null;
  // null = capability signal not seen yet (cache-only hydration); the tab
  // stays permissive until the first meta arrives, then gates on the list.
  let mutationsSupported = null;
  let pendingLoadQueued = false;
  const active = new Set();

  const heroHost = el('div');
  const bannerHost = el('div');
  const metricsHost = el('div');
  const stateHost = el('div');
  const grid = el('div', { class: 'repo-grid' });
  page.append(heroHost, bannerHost, metricsHost, stateHost, grid);

  function selectedRepo() {
    return repositories.find((repo) => repo.name === selectedName) || repositories[0] || null;
  }

  function selectedPull() {
    const repo = selectedRepo();
    return (repo?.pull_requests || []).find((pull) => pull.number === selectedPullNumber)
      || repo?.pull_requests?.[0] || null;
  }

  function visibleRepos() {
    const term = search.trim().toLowerCase();
    return repositories.filter((repo) => {
      if (filter === 'attention' && !ATTENTION_STATES.has(repo.state)) return false;
      if (filter === 'pulls' && !(repo.pull_requests || []).length) return false;
      if (filter === 'remote' && repo.transport !== 'ssh') return false;
      if (!term) return true;
      return [repo.name, repo.repo_full_name, repo.branch, repo.host, repo.path, repo.git_dir]
        .filter(Boolean).join(' ').toLowerCase().includes(term);
    });
  }

  function summary() {
    const pulls = repositories.reduce((sum, repo) => sum + (repo.pull_requests || []).length, 0);
    return {
      repositories: repositories.length,
      pulls,
      attention: repositories.filter((repo) => ATTENTION_STATES.has(repo.state)).length,
      ready: repositories.filter((repo) => repo.layout?.ready).length,
    };
  }

  function canMutate(action) {
    return !Array.isArray(mutationsSupported) || mutationsSupported.includes(action);
  }

  function mutationGateTitle(action, label) {
    return canMutate(action) ? label : `${label} — not advertised by meta.mutations_supported`;
  }

  async function performLoad({ refresh = true, background = false } = {}) {
    refreshing = true;
    if (!background) {
      loading = true;
      loadError = null;
      render();
    }
    try {
      const response = await api.get(
        `/api/repositories?refresh=${refresh ? '1' : '0'}&github=1`, { profile },
      );
      const payload = response.data || {};
      if (Array.isArray(response.meta?.mutations_supported)) {
        mutationsSupported = response.meta.mutations_supported;
      }
      loadError = null;
      repositories = Array.isArray(payload.repositories) ? payload.repositories : [];
      operations = Array.isArray(payload.recent_operations) ? payload.recent_operations : [];
      registry = payload.registry || {};
      loadedFromSource = true;
      if (!selectedName || !repositories.some((repo) => repo.name === selectedName)) {
        selectedName = repositories[0]?.name || null;
      }
      const repo = selectedRepo();
      if (!repo?.pull_requests?.some((pull) => pull.number === selectedPullNumber)) {
        selectedPullNumber = repo?.pull_requests?.[0]?.number || null;
      }
      writeCache(profile, {
        repositories: clone(repositories), operations: clone(operations),
        registry: clone(registry), selected_name: selectedName,
        selected_pull_number: selectedPullNumber,
      });
    } catch (error) {
      loadError = error;
    } finally {
      refreshing = false;
      loading = false;
      renderToolbar(toolbar);
      render();
      renderSide();
    }
  }

  async function load({ refresh = true, background = false } = {}) {
    // A request arriving while one is in flight must not be dropped: the
    // in-flight response predates whatever just changed. Queue exactly one
    // follow-up pass (as a background refresh) instead of silently no-oping.
    if (refreshing) {
      pendingLoadQueued = true;
      return;
    }
    do {
      pendingLoadQueued = false;
      await performLoad({ refresh, background });
      background = true; // queued follow-ups never flash the loading skeleton
    } while (pendingLoadQueued);
  }

  async function revalidate() {
    // Manual refresh follows the live-route contract: force the shared
    // resource resync first (cheap projected rows), then reload the full
    // source of truth for layout and pull-request detail.
    if (liveStore) {
      try {
        await liveStore.resyncResource('repositories', profile, { force: true });
        applyLive();
      } catch (_error) { /* the full load below remains the authority */ }
    }
    await load({ refresh: true });
  }

  function hydrate() {
    const cached = readCache(profile);
    if (!cached) return false;
    repositories = clone(cached.repositories || []);
    operations = clone(cached.operations || []);
    registry = clone(cached.registry || {});
    selectedName = cached.selected_name || repositories[0]?.name || null;
    selectedPullNumber = cached.selected_pull_number || selectedRepo()?.pull_requests?.[0]?.number || null;
    renderToolbar(toolbar);
    render();
    renderSide();
    return repositories.length > 0;
  }

  function applyLive() {
    const live = liveRows(liveStore, 'repositories', profile);
    if (!live) return false;
    repositories = unionProjectedRows(repositories, live.rows);
    if (!selectedName || !repositories.some((repo) => repo.name === selectedName)) {
      selectedName = repositories[0]?.name || null;
    }
    renderToolbar(toolbar);
    render();
    renderSide();
    return true;
  }

  function bindLive() {
    if (unsubscribe || !liveStore) return;
    unsubscribe = bindLiveResources(liveStore, ['repositories'], profile, () => {
      if (root.isConnected) applyLive();
    });
  }

  async function runAction(repo, key, path, body = {}) {
    if (!repo || active.has(key)) return;
    selectedName = repo.name;
    active.add(key);
    lastOperation = null;
    render();
    renderSide();
    try {
      const response = await api.post(path, body, { profile });
      lastOperation = response.data || {};
      await load({ refresh: true, background: true });
    } catch (error) {
      lastOperation = {
        ok: false,
        action: key,
        request_id: error?.request_id || error?.payload?.request_id || null,
        error: {
          code: error?.payload?.error?.code || 'request_failed',
          message: error?.message || 'repository action failed',
          details: error?.payload?.error?.details || null,
        },
      };
    } finally {
      active.delete(key);
      render();
      renderSide();
    }
  }

  function initialize(repo) {
    if (!canMutate('initialize_layout')) return;
    return runAction(
      repo, `initialize:${repo.name}`,
      `/api/repositories/${encodeURIComponent(repo.name)}/initialize`,
    );
  }

  function syncRepository(repo) {
    if (!canMutate('sync')) return;
    if (!repo.capabilities?.sync_local) return;
    return runAction(
      repo, `sync:${repo.name}`,
      `/api/repositories/${encodeURIComponent(repo.name)}/sync`,
    );
  }

  function prepareSuperprojectPin(repo) {
    if (!canMutate('prepare_superproject_pin')) return;
    if (!repo.capabilities?.project_to_superproject) return;
    const confirmed = window.confirm(
      `Create or update the Hermes gitlink PR so ${repo.name}:${repo.branch} is projected at ${repo.superproject?.path}.\n\nThis changes GitHub only. Merge the resulting PR on the Hermes card, then use Sync Hermes.\n\nContinue?`,
    );
    if (!confirmed) return;
    return runAction(
      repo, `pin:${repo.name}`,
      `/api/repositories/${encodeURIComponent(repo.name)}/prepare-superproject-pin`,
    );
  }

  function requestCodex(repo, pull) {
    if (!canMutate('codex_review')) return;
    return runAction(
      repo, `review:${repo.name}:${pull.number}`,
      `/api/repositories/${encodeURIComponent(repo.name)}/pulls/${pull.number}/codex-review`,
      { expected_head_sha: pull.head_sha },
    );
  }

  function changeDraft(repo, pull, ready) {
    if (!canMutate(ready ? 'mark_ready' : 'mark_draft')) return;
    return runAction(
      repo, `${ready ? 'ready' : 'draft'}:${repo.name}:${pull.number}`,
      `/api/repositories/${encodeURIComponent(repo.name)}/pulls/${pull.number}/${ready ? 'ready' : 'draft'}`,
    );
  }

  function mergePr(repo, pull) {
    if (!canMutate('merge_pr')) return;
    // CI and Codex state sit right next to this button; an owner can still
    // override them, but the confirmation must show what is being overridden.
    const evidence = [
      `CI: ${checkSummary(pull)}`,
      `Codex: ${codexSummary(pull)}`,
    ];
    if (pull.mergeable !== true) {
      evidence.push('GitHub has not confirmed the PR as mergeable yet.');
    }
    let convergence;
    if (repo.local_mode === 'superproject') {
      convergence = 'After merge, use Sync Hermes to converge the local superproject.';
    } else if (repo.superproject?.managed) {
      convergence = `After merge, Mission Control will create or update a Hermes gitlink PR for ${repo.superproject.path}. Merge that PR on the Hermes card, then use Sync Hermes.`;
    } else {
      convergence = 'This repository has no Hermes local projection; no local checkout will be changed.';
    }
    const confirmed = window.confirm(
      `Rebase-merge PR #${pull.number} into ${repo.repo_full_name || repo.name}:${repo.branch}.\n\n${evidence.join('\n')}\n\n${convergence}\n\nContinue?`,
    );
    if (!confirmed) return;
    return runAction(
      repo, `merge:${repo.name}:${pull.number}`,
      `/api/repositories/${encodeURIComponent(repo.name)}/pulls/${pull.number}/merge`,
      { expected_head_sha: pull.head_sha },
    );
  }

  function metric(title, value, note) {
    return el('div', { class: 'repo-metric' }, [
      el('span', { class: 'repo-metric-label', text: title }),
      el('strong', { class: 'repo-metric-value', text: String(value) }),
      el('span', { class: 'repo-metric-note', text: note }),
    ]);
  }

  function repoCard(repo) {
    const selected = repo.name === selectedName;
    const pulls = repo.pull_requests || [];
    const pending = [...active].some((key) => keyMatches(key, repo.name));
    const card = el('article', {
      class: `repo-card${selected ? ' is-selected' : ''}${ATTENTION_STATES.has(repo.state) ? ' has-attention' : ''}`,
      tabindex: '0',
      onclick: (event) => {
        if (event.target.closest('button,a,input')) return;
        selectedName = repo.name;
        selectedPullNumber = pulls[0]?.number || null;
        render();
        renderSide();
      },
    });
    appendPresent(card,
      el('div', { class: 'repo-card-head' }, [
        el('div', { class: 'repo-card-title-wrap' }, [
          el('div', { class: 'repo-card-title', text: repo.repo_full_name || repo.name }),
          el('div', {
            class: 'repo-card-sub mono',
            text: `${repo.host || '—'} · ${repo.branch || '—'}`,
          }),
        ]),
        el('div', { class: 'repo-badges' }, [
          repo.transport === 'ssh' ? el('span', { class: 'repo-badge repo-badge-ssh', text: 'SSH' }) : null,
          repo.private ? el('span', { class: 'repo-badge', text: 'PRIVATE' }) : null,
          repo.fork ? el('span', { class: 'repo-badge repo-badge-accent', text: 'FORK' }) : null,
        ].filter(Boolean)),
      ]),
      el('div', { class: 'repo-card-state' }, [
        statusChip(REPO_TONES[repo.state] || 'idle', label(repo.state)),
        el('span', {
          class: 'repo-card-state-note',
          text: repo.local_mode === 'remote_only'
            ? 'GitHub PR control only · no local Git retained'
            : repo.layout?.ready
            ? `${repo.ahead || 0} ahead · ${repo.behind || 0} behind`
            : 'Canonical live source not initialized',
        }),
      ]),
      el('div', { class: 'repo-card-facts' }, [
        el('div', { class: 'repo-fact' }, [
          el('span', { text: 'Local behavior' }),
          el('code', { text: repo.local_mode === 'superproject' ? 'Hermes superproject sync' : repo.superproject?.managed ? `Via Hermes gitlink · ${repo.superproject.path}` : 'Remote only' }),
        ]),
        el('div', { class: 'repo-fact' }, [
          el('span', { text: 'Local source' }),
          el('code', { text: repo.layout?.work_tree || 'Not retained locally' }),
        ]),
        el('div', { class: 'repo-fact' }, [
          el('span', { text: 'Git action' }),
          el('code', { text: repo.local_mode === 'remote_only' ? repo.superproject?.managed ? 'PR merge + Hermes pin PR' : 'PR merge on GitHub' : `${shortSha(repo.local_sha)} / ${shortSha(repo.remote_sha)}` }),
        ]),
        el('div', { class: 'repo-fact' }, [
          el('span', { text: 'Pull requests' }),
          el('strong', { text: String(pulls.length) }),
        ]),
      ]),
      repoFailure(repo) ? el('div', { class: 'repo-notice repo-notice-danger' }, [
        el('strong', { text: repoFailure(repo).code || 'Repository error' }),
        el('span', { text: repoFailure(repo).message || 'Open inspector for details' }),
      ]) : null,
      el('div', { class: 'repo-card-foot' }, [
        el('span', { class: 'repo-pr-summary', text: `${fmtWhen(repo.last_commit_at)} · ${repo.last_commit_subject || 'No commit data'}` }),
        el('div', { class: 'repo-card-actions' }, [
          repo.capabilities?.initialize_local && !repo.layout?.ready
            ? button(pending ? 'Initializing…' : 'Initialize', () => initialize(repo), {
              primary: true, disabled: pending || !canMutate('initialize_layout'),
              title: mutationGateTitle('initialize_layout', 'Initialize'),
            })
            : repo.capabilities?.sync_local ? button(pending ? 'Syncing…' : 'Sync Hermes', () => syncRepository(repo), {
              disabled: pending || !canMutate('sync'),
              title: mutationGateTitle('sync', 'Sync Hermes superproject'),
            }) : repo.capabilities?.project_to_superproject
              ? button(pending ? 'Preparing…' : 'Prepare Hermes pin', () => prepareSuperprojectPin(repo), {
                disabled: pending || !canMutate('prepare_superproject_pin'),
                title: mutationGateTitle('prepare_superproject_pin', 'Prepare Hermes gitlink PR'),
              })
              : el('span', { class: 'repo-pr-summary', text: 'No local projection' }),
        ]),
      ]),
    );
    return card;
  }

  const cards = createKeyedReconciler({
    container: grid,
    key: (repo) => repo.name,
    create: repoCard,
    update(node, repo) {
      const next = repoCard(repo);
      node.className = next.className;
      node.onclick = next.onclick;
      clear(node);
      while (next.firstChild) node.append(next.firstChild);
    },
  });

  function render() {
    clear(heroHost);
    clear(bannerHost);
    clear(metricsHost);
    clear(stateHost);
    if (loading && !repositories.length) {
      grid.hidden = true;
      cards.reconcile([]);
      stateHost.append(el('div', { class: 'repo-loading' }, [skeleton({ lines: 8 })]));
      return;
    }
    if (loadError && !repositories.length) {
      grid.hidden = true;
      cards.reconcile([]);
      stateHost.append(unavailableState({
        reason: loadError.message || 'Repository control unavailable',
        requestId: loadError.request_id,
      }));
      return;
    }
    const stats = summary();
    heroHost.append(el('div', { class: 'repo-hero' }, [
      el('div', {}, [
        el('h1', { class: 'repo-title', text: 'Repository Control' }),
        el('p', {
          class: 'repo-subtitle',
          text: 'GitHub PR control for every repo; local convergence only through the Hermes superproject.',
        }),
      ]),
      el('div', { class: 'repo-monitor-state' }, [
        el('span', { class: `repo-live-dot${stats.attention ? ' is-warn' : ''}` }),
        el('span', { text: `${registry.count || repositories.length} registry repositories` }),
      ]),
    ]));
    metricsHost.append(el('div', { class: 'repo-metrics' }, [
      metric('Repositories', stats.repositories, 'from repositories.yaml'),
      metric('Live source ready', stats.ready, 'canonical live trees'),
      metric('Open PRs', stats.pulls, 'GitHub live state'),
      metric('Attention', stats.attention, 'layout / drift / conflict'),
    ]));
    if (loadError) {
      bannerHost.append(el('div', { class: 'repo-banner repo-banner-danger' }, [
        el('strong', { text: 'Refresh degraded' }),
        el('span', { text: loadError.message || 'Showing retained repository state.' }),
      ]));
    }
    if (lastOperation) {
      bannerHost.append(el('div', {
        class: `repo-banner repo-banner-${lastOperation.ok ? 'ok' : lastOperation.partial_success ? 'warn' : 'danger'}`,
      }, [
        el('strong', { text: lastOperation.partial_success ? 'Partial success' : lastOperation.ok ? 'Completed' : 'Failed' }),
        el('span', { text: operationMessage(lastOperation) }),
        // Correlation id for the audit trail — a failed owner action is only
        // traceable in journalctl/audit through its request id.
        !lastOperation.ok && lastOperation.request_id
          ? el('code', { class: 'mono repo-banner-rid', text: `request ${lastOperation.request_id}` })
          : null,
      ]));
    }
    grid.hidden = false;
    cards.reconcile(visibleRepos());
  }

  function kv(key, value, { mono = false, tone = '' } = {}) {
    return el('div', { class: 'repo-side-kv' }, [
      el('span', { class: 'repo-side-k', text: key }),
      el('span', { class: `repo-side-v${mono ? ' mono' : ''}${tone ? ` ${tone}` : ''}`, text: value ?? '—' }),
    ]);
  }

  function section(title, children, className = '') {
    return el('section', { class: `repo-side-section ${className}`.trim() }, [
      el('div', { class: 'repo-side-section-title', text: title }),
      ...children.filter(Boolean),
    ]);
  }

  function pullRow(repo, pull) {
    const selected = pull.number === selectedPullNumber;
    const busy = [...active].some((key) => keyMatches(key, repo.name, pull.number));
    const codex = pull.codex || {};
    const checks = pull.checks || {};
    // A request already posted for this head does not need another comment;
    // re-requesting is what spams "@codex review" threads on GitHub.
    const reviewPending = codex.state === 'requested';
    return el('div', {
      class: `repo-pr-control${selected ? ' is-selected' : ''}`,
      onclick: (event) => {
        if (event.target.closest('button,a')) return;
        selectedPullNumber = pull.number;
        renderSide();
      },
    }, [
      el('div', { class: 'repo-pr-control-head' }, [
        el('div', { class: 'repo-pr-control-title' }, [
          el('strong', { text: `#${pull.number} ${pull.title || ''}` }),
          el('span', { class: 'mono', text: `${pull.head || '—'} → ${pull.base || repo.branch}` }),
        ]),
        el('a', { href: pull.html_url, target: '_blank', rel: 'noreferrer', class: 'repo-open-link', text: 'Open ↗' }),
      ]),
      el('div', { class: 'repo-pr-status-grid' }, [
        el('div', {}, [el('span', { text: 'CI' }), statusChip(CHECK_TONES[checks.state] || 'idle', checkSummary(pull))]),
        el('div', {}, [el('span', { text: 'Codex' }), statusChip(CODEX_TONES[codex.state] || 'idle', codexSummary(pull))]),
        el('div', {}, [el('span', { text: 'Merge' }), statusChip(pull.mergeable === false ? 'danger' : pull.mergeable === true ? 'ok' : 'warn', pull.mergeable === false ? 'Conflicted' : pull.mergeable === true ? 'Mergeable' : 'Checking')]),
        el('div', {}, [el('span', { text: 'Head' }), el('code', { text: shortSha(pull.head_sha) })]),
      ]),
      el('div', { class: 'repo-pr-actions' }, [
        button(busy ? 'Working…' : reviewPending ? 'Review requested' : 'Review with Codex',
          () => requestCodex(repo, pull), {
          disabled: busy || reviewPending || !canMutate('codex_review'),
          title: reviewPending
            ? 'Codex review already requested for this head'
            : mutationGateTitle('codex_review', 'Review with Codex'),
        }),
        pull.draft
          ? button('Mark ready', () => changeDraft(repo, pull, true), {
            disabled: busy || !canMutate('mark_ready'),
            title: mutationGateTitle('mark_ready', 'Mark ready'),
          })
          : button('Convert to draft', () => changeDraft(repo, pull, false), {
            disabled: busy || !canMutate('mark_draft'),
            title: mutationGateTitle('mark_draft', 'Convert to draft'),
          }),
        button('Merge PR', () => mergePr(repo, pull), {
          primary: true,
          disabled: busy || pull.draft || pull.mergeable === false
            || !canMutate('merge_pr'),
          title: mutationGateTitle('merge_pr', 'Merge PR on GitHub only'),
        }),
      ]),
    ]);
  }

  function renderSide() {
    if (!inspectorHost) return;
    clear(inspectorHost);
    const repo = selectedRepo();
    inspectorHost.append(el('div', { class: 'inspector-title', text: 'Repository inspector' }));
    if (!repo) {
      inspectorHost.append(el('div', { class: 'inspector-empty', text: 'Select a repository.' }));
      return;
    }
    const pull = selectedPull();
    const repoOps = operations.filter((row) => row.repo === repo.name).slice(0, 12);
    const repoPending = [...active].some((key) => keyMatches(key, repo.name));
    appendPresent(inspectorHost,
      el('div', { class: 'repo-side-head' }, [
        el('div', { class: 'repo-side-name', text: repo.repo_full_name || repo.name }),
        el('div', { class: 'repo-side-status' }, [
          statusChip(REPO_TONES[repo.state] || 'idle', label(repo.state)),
          statusChip(
            repo.local_mode === 'remote_only' ? 'info' : repo.layout?.ready ? 'ok' : 'warn',
            repo.local_mode === 'remote_only'
              ? 'GitHub only'
              : repo.layout?.ready ? 'superproject ready' : 'layout missing',
          ),
        ]),
      ]),
      section(repo.local_mode === 'remote_only' ? 'Repository scope' : 'Hermes superproject', [
        kv('Host', repo.host),
        kv('Branch', repo.branch, { mono: true }),
        kv('Origin', repo.origin_actual || repo.origin_url, { mono: true }),
        kv('Local mode', repo.local_mode === 'remote_only' ? repo.superproject?.managed ? `Projected by Hermes gitlink · ${repo.superproject.path}` : 'No Hermes local projection; PR actions only' : 'Superproject sync', { mono: true }),
        repo.local_mode !== 'remote_only' ? kv('Git dir', repo.layout?.git_dir, { mono: true }) : null,
        repo.local_mode !== 'remote_only' ? kv('Live source', repo.layout?.work_tree, { mono: true }) : null,
        repo.local_mode !== 'remote_only' ? kv('Local SHA', shortSha(repo.local_sha), { mono: true }) : null,
        repo.local_mode !== 'remote_only' ? kv('Remote SHA', shortSha(repo.remote_sha), { mono: true }) : null,
        repo.local_mode !== 'remote_only' ? kv('Working tree', repo.working_tree?.dirty ? 'DIRTY' : 'Clean', {
          tone: repo.working_tree?.dirty ? 'is-danger' : 'is-ok',
        }) : null,
        el('div', { class: 'repo-side-footer-actions' }, [
          repo.capabilities?.initialize_local && !repo.layout?.ready
            ? button('Initialize repository layout', () => initialize(repo), {
              primary: true,
              disabled: !canMutate('initialize_layout'),
              title: mutationGateTitle('initialize_layout', 'Initialize repository layout'),
            })
            : repo.capabilities?.sync_local ? button('Sync Hermes', () => syncRepository(repo), {
              primary: true,
              disabled: !canMutate('sync'),
              title: mutationGateTitle('sync', 'Sync Hermes superproject'),
            }) : repo.capabilities?.project_to_superproject
              ? button(repoPending ? 'Preparing…' : 'Prepare Hermes pin', () => prepareSuperprojectPin(repo), {
                primary: true,
                disabled: repoPending || !canMutate('prepare_superproject_pin'),
                title: mutationGateTitle('prepare_superproject_pin', 'Prepare Hermes gitlink PR'),
              })
              : el('span', { class: 'repo-side-empty', text: 'This repository has no Hermes local projection.' }),
        ]),
      ]),
      section('Pull requests', (repo.pull_requests || []).length
        ? (repo.pull_requests || []).map((item) => pullRow(repo, item))
        : [el('div', { class: 'repo-side-empty', text: 'No open pull requests.' })]),
      pull ? section('Selected PR evidence', [
        kv('PR', `#${pull.number}`),
        kv('Current head', pull.head_sha, { mono: true }),
        kv('Reviewed head', pull.codex?.reviewed_sha, { mono: true }),
        kv('Review state', label(pull.codex?.state)),
        kv('Unresolved', String(pull.codex?.unresolved_threads || 0)),
        kv('Checks', checkSummary(pull)),
        ...(pull.codex?.threads || []).slice(0, 8).map((thread) =>
          el('div', { class: `repo-review-thread${thread.resolved ? ' is-resolved' : ''}` }, [
            el('strong', { text: thread.resolved ? 'Resolved thread' : 'Unresolved Codex thread' }),
            el('span', { text: thread.comments?.[0]?.body || 'No comment body' }),
          ])),
      ]) : null,
      section('Recent owner operations', repoOps.length
        ? repoOps.map((item) => el('div', { class: 'repo-history-row' }, [
          el('span', { class: `repo-history-dot ${item.ok ? 'is-ok' : item.partial_success ? 'is-warn' : 'is-danger'}` }),
          el('div', {}, [
            el('strong', { text: `${item.action || 'operation'}${item.pull_number ? ` · #${item.pull_number}` : ''}` }),
            el('span', { text: `${item.status || (item.ok ? 'ok' : 'failed')} · ${fmtWhen(item.finished_at)}` }),
          ]),
        ]))
        : [el('div', { class: 'repo-side-empty', text: 'No owner operations recorded.' })]),
      repoFailure(repo) ? section('Repository error', [
        el('div', { class: 'repo-side-alert repo-side-alert-danger' }, [
          el('strong', { text: repoFailure(repo).code || 'repository_error' }),
          el('div', { text: repoFailure(repo).message || 'Unknown repository error' }),
        ]),
      ], 'repo-side-section-danger') : null,
    );
  }

  function renderToolbar(host) {
    if (!host) return;
    clear(host);
    const filterSelect = el('select', {
      class: 'select input-sm repo-filter',
      onchange: (event) => { filter = event.target.value; render(); },
    }, [
      el('option', { value: 'all', text: 'All repositories' }),
      el('option', { value: 'attention', text: 'Needs attention' }),
      el('option', { value: 'pulls', text: 'Has open PRs' }),
      el('option', { value: 'remote', text: 'Remote hosts' }),
    ]);
    filterSelect.value = filter;
    const searchInput = el('input', {
      class: 'input input-sm repo-search',
      type: 'search', placeholder: 'Search repository, host or path…',
      value: search, 'data-tab-filter': 'true',
      oninput: (event) => { search = event.target.value; render(); },
    });
    host.append(
      filterSelect,
      searchInput,
      el('span', { class: 'repo-toolbar-note', text: 'Owner control · registry driven' }),
      button(refreshing ? 'Refreshing…' : 'Refresh', () => revalidate(), { disabled: refreshing }),
    );
  }

  return {
    mount(container) {
      clear(container);
      container.append(root);
    },
    async activate() {
      renderToolbar(toolbar);
      bindLive();
      const hydrated = applyLive() || hydrate();
      if (!loadedFromSource) await load({ refresh: true, background: hydrated });
    },
    deactivate() {
      if (unsubscribe) { unsubscribe(); unsubscribe = null; }
      return { selection: selectedName, pull: selectedPullNumber };
    },
    refresh: () => revalidate(),
    renderToolbar,
    renderInspector(container) {
      inspectorHost = container;
      renderSide();
    },
    get data() { return repositories; },
    get filters() { return { filter, search }; },
  };
}
