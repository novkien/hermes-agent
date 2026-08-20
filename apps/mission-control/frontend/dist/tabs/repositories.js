// AgentOS Repositories — owner repository control plane.

import { el, clear, skeleton, unavailableState, statusChip } from '../ui.js';
import { bindLiveResources, liveRows, mergeProjectedRows } from './_live.js';
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
  ahead: 'danger', diverged: 'danger', conflict: 'danger',
  origin_mismatch: 'danger', wrong_branch: 'danger',
  error: 'danger',
});
const CHECK_TONES = Object.freeze({ passed: 'ok', pending: 'warn', failed: 'danger', none: 'idle' });
const CODEX_TONES = Object.freeze({
  reviewed: 'ok', requested: 'info', waiting: 'info', stale: 'warn',
  has_findings: 'danger', not_requested: 'idle',
});

function clone(value) {
  if (typeof structuredClone === 'function') return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
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

function button(text, onclick, { primary = false, danger = false, disabled = false } = {}) {
  return el('button', {
    class: `btn btn-sm${primary ? ' btn-primary' : ''}${danger ? ' btn-danger' : ''}`,
    type: 'button',
    disabled: disabled ? '' : undefined,
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
    if (operation.action === 'merge_and_pull') {
      return `PR #${operation.pull_number} merged and production advanced to ${shortSha(operation.production?.after_sha)}.`;
    }
    if (operation.action === 'pull_production') {
      return operation.production?.changed
        ? `Production advanced to ${shortSha(operation.production?.after_sha)}.`
        : 'Production already matched the remote branch.';
    }
    if (operation.action === 'codex_review') return 'Codex review requested for the current PR head.';
    if (operation.action === 'initialize_layout') return 'Canonical Git directory and production worktree are ready.';
    return 'Repository operation completed.';
  }
  if (operation.partial_success) {
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

  async function load({ refresh = true, background = false } = {}) {
    if (refreshing) return;
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
    repositories = mergeProjectedRows(repositories, live.rows, (repo) => repo.name);
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
    return runAction(
      repo, `initialize:${repo.name}`,
      `/api/repositories/${encodeURIComponent(repo.name)}/initialize`,
    );
  }

  function pullProduction(repo) {
    return runAction(
      repo, `pull:${repo.name}`,
      `/api/repositories/${encodeURIComponent(repo.name)}/pull`,
    );
  }

  function requestCodex(repo, pull) {
    return runAction(
      repo, `review:${repo.name}:${pull.number}`,
      `/api/repositories/${encodeURIComponent(repo.name)}/pulls/${pull.number}/codex-review`,
      { expected_head_sha: pull.head_sha },
    );
  }

  function changeDraft(repo, pull, ready) {
    return runAction(
      repo, `${ready ? 'ready' : 'draft'}:${repo.name}:${pull.number}`,
      `/api/repositories/${encodeURIComponent(repo.name)}/pulls/${pull.number}/${ready ? 'ready' : 'draft'}`,
    );
  }

  function mergeAndPull(repo, pull) {
    const workTree = repo.layout?.work_tree || repo.path || 'canonical production worktree';
    const confirmed = window.confirm(
      `Rebase-merge PR #${pull.number} into ${repo.branch}, then fast-forward production on ${repo.host}:\n\n${workTree}\n\nContinue?`,
    );
    if (!confirmed) return;
    return runAction(
      repo, `merge:${repo.name}:${pull.number}`,
      `/api/repositories/${encodeURIComponent(repo.name)}/pulls/${pull.number}/merge-and-pull`,
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
    const pending = [...active].some((key) => key.includes(`:${repo.name}`));
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
    card.append(
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
          text: repo.layout?.ready
            ? `${repo.ahead || 0} ahead · ${repo.behind || 0} behind`
            : 'Canonical checkout not initialized',
        }),
      ]),
      el('div', { class: 'repo-card-facts' }, [
        el('div', { class: 'repo-fact' }, [
          el('span', { text: 'Production' }),
          el('code', { text: repo.layout?.work_tree || '—' }),
        ]),
        el('div', { class: 'repo-fact' }, [
          el('span', { text: 'Git common dir' }),
          el('code', { text: repo.layout?.git_dir || '—' }),
        ]),
        el('div', { class: 'repo-fact' }, [
          el('span', { text: 'Local / remote' }),
          el('code', { text: `${shortSha(repo.local_sha)} / ${shortSha(repo.remote_sha)}` }),
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
          !repo.layout?.ready
            ? button(pending ? 'Initializing…' : 'Initialize', () => initialize(repo), { primary: true, disabled: pending })
            : button(pending ? 'Pulling…' : 'Pull production', () => pullProduction(repo), { disabled: pending }),
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
          text: 'Owner control for registry repositories: PR checks, Codex review, rebase merge, and direct production fast-forward.',
        }),
      ]),
      el('div', { class: 'repo-monitor-state' }, [
        el('span', { class: `repo-live-dot${stats.attention ? ' is-warn' : ''}` }),
        el('span', { text: `${registry.count || repositories.length} registry repositories` }),
      ]),
    ]));
    metricsHost.append(el('div', { class: 'repo-metrics' }, [
      metric('Repositories', stats.repositories, 'from repositories.yaml'),
      metric('Production ready', stats.ready, 'canonical worktrees'),
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
    const busy = [...active].some((key) => key.endsWith(`:${repo.name}:${pull.number}`));
    const codex = pull.codex || {};
    const checks = pull.checks || {};
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
        button(busy ? 'Working…' : 'Review with Codex', () => requestCodex(repo, pull), { disabled: busy }),
        pull.draft
          ? button('Mark ready', () => changeDraft(repo, pull, true), { disabled: busy })
          : button('Convert to draft', () => changeDraft(repo, pull, false), { disabled: busy }),
        button('Merge & Pull', () => mergeAndPull(repo, pull), {
          primary: true,
          disabled: busy || pull.draft || pull.mergeable === false || !repo.layout?.ready,
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
    inspectorHost.append(
      el('div', { class: 'repo-side-head' }, [
        el('div', { class: 'repo-side-name', text: repo.repo_full_name || repo.name }),
        el('div', { class: 'repo-side-status' }, [
          statusChip(REPO_TONES[repo.state] || 'idle', label(repo.state)),
          statusChip(repo.layout?.ready ? 'ok' : 'warn', repo.layout?.ready ? 'production ready' : 'layout missing'),
        ]),
      ]),
      section('Canonical layout', [
        kv('Host', repo.host),
        kv('Branch', repo.branch, { mono: true }),
        kv('Origin', repo.origin_actual || repo.origin_url, { mono: true }),
        kv('Git dir', repo.layout?.git_dir, { mono: true }),
        kv('Production', repo.layout?.work_tree, { mono: true }),
        kv('Local SHA', shortSha(repo.local_sha), { mono: true }),
        kv('Remote SHA', shortSha(repo.remote_sha), { mono: true }),
        kv('Working tree', repo.working_tree?.dirty ? 'DIRTY' : 'Clean', {
          tone: repo.working_tree?.dirty ? 'is-danger' : 'is-ok',
        }),
        el('div', { class: 'repo-side-footer-actions' }, [
          !repo.layout?.ready
            ? button('Initialize production layout', () => initialize(repo), { primary: true })
            : button('Pull production', () => pullProduction(repo), { primary: true }),
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
      button(refreshing ? 'Refreshing…' : 'Refresh', () => load({ refresh: true }), { disabled: refreshing }),
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
    refresh: () => load({ refresh: true }),
    renderToolbar,
    renderInspector(container) {
      inspectorHost = container;
      renderSide();
    },
    get data() { return repositories; },
    get filters() { return { filter, search }; },
  };
}
