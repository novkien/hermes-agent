// AgentOS Repositories — production Git state, safe sync, fork and PR controls.

import { el, clear, skeleton, unavailableState, statusChip } from '../ui.js';
import { bindLiveResources, liveRows, mergeProjectedRows } from './_live.js';
import { createKeyedReconciler } from '../pure/keyed-dom.js';

export const ROUTE = 'repositories';
export const LABEL = 'Repositories';
export const GROUP = 'BUILD & INTEGRATE';
export const SOURCE_ENDPOINTS = Object.freeze(['/api/repositories']);

const ATTENTION_STATES = new Set([
  'behind', 'dirty', 'diverged', 'conflict', 'wrong_branch', 'error',
]);
const TONES = Object.freeze({
  synced: 'ok',
  ahead: 'info',
  behind: 'warn',
  dirty: 'warn',
  diverged: 'danger',
  conflict: 'danger',
  wrong_branch: 'danger',
  error: 'danger',
});
const REPOSITORY_CACHE_TTL_MS = 60_000;
const REPOSITORIES_CACHE = new Map();

function safeClone(value) {
  if (typeof structuredClone === 'function') return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}

function cacheKeyForProfile(profile) {
  return `repositories:${String(profile || 'default')}`;
}

function readRepositoryCache(profile) {
  const key = cacheKeyForProfile(profile);
  const cached = REPOSITORIES_CACHE.get(key);
  if (!cached) return null;
  if (!cached.loaded_at || (Date.now() - cached.loaded_at) > REPOSITORY_CACHE_TTL_MS) return null;
  return cached;
}

function writeRepositoryCache(profile, payload) {
  REPOSITORIES_CACHE.set(cacheKeyForProfile(profile), {
    ...payload,
    loaded_at: Date.now(),
  });
}

function shortSha(value) {
  return value ? String(value).slice(0, 9) : '—';
}

function fmtWhen(value) {
  if (!value) return '—';
  const stamp = new Date(value);
  if (Number.isNaN(stamp.getTime())) return String(value);
  const delta = Math.max(0, Date.now() - stamp.getTime());
  const seconds = Math.round(delta / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return stamp.toLocaleString();
}

function repoError(repo) {
  // A repository can be healthy after a previous operation failed. Keep that
  // failure in Latest operation, but do not turn a synced card red.
  return repo?.error || null;
}

function conflictFiles(repo) {
  return Array.isArray(repo?.conflict_files) ? repo.conflict_files : [];
}

function operationFailure(operation) {
  if (!operation || operation.ok !== false) return null;
  return operation.error || operation.production_sync?.error || null;
}

function operationAfter(operation) {
  return operation?.after || operation?.production_sync?.after || null;
}

function operationPushedSha(operation) {
  return operation?.pushed_sha || operation?.production_sync?.pushed_sha || null;
}

function operationNotice(action, operation) {
  if (!operation || operation.ok === false) return null;
  const after = operationAfter(operation);
  const ahead = Number(after?.ahead || 0);
  if (action === 'sync' && ahead > 0 && !operationPushedSha(operation)) {
    return {
      tone: 'warn',
      message: `Sync completed, but ${ahead} local commit${ahead === 1 ? '' : 's'} remain unpushed. Enable Auto-commit and run Safe sync again.`,
    };
  }
  if (action === 'sync' && operationPushedSha(operation)) {
    const merged = Array.isArray(operation.merged_pulls) ? operation.merged_pulls : [];
    const detail = merged.length
      ? ` Rebase-merged PR${merged.length === 1 ? '' : 's'} ${merged.map((pull) => `#${pull.number}`).join(', ')} first.`
      : '';
    return { tone: 'ok', message: `Safe sync completed and local commits were pushed to origin.${detail}` };
  }
  if (action === 'sync' && Array.isArray(operation.merged_pulls) && operation.merged_pulls.length) {
    return {
      tone: 'ok',
      message: `Safe sync rebase-merged PR${operation.merged_pulls.length === 1 ? '' : 's'} ${operation.merged_pulls.map((pull) => `#${pull.number}`).join(', ')} and updated the local checkout.`,
    };
  }
  return { tone: 'ok', message: 'Repository operation completed successfully.' };
}

export function createRepositories({ api, profile, toolbar, liveStore }) {
  const root = el('div', { class: 'tab tab-repositories' });
  const page = el('div', { class: 'repo-page' });
  root.append(page);

  let repositories = [];
  let operations = [];
  let automation = {};
  let selectedName = null;
  let filter = 'all';
  let search = '';
  let autoCommit = true;
  let loading = false;
  let loadError = null;
  let actionError = null;
  let actionNotice = null;
  let inspectorHost = null;
  let unsubscribe = null;
  let refreshing = false;
  let loadedFromSource = false;
  let syncingAll = false;
  const activeRepos = new Set();
  const heroHost = el('div');
  const metricsHost = el('div');
  const bannerHost = el('div');
  const stateHost = el('div');
  const grid = el('div', { class: 'repo-grid' });
  page.append(heroHost, metricsHost, bannerHost, stateHost, grid);

  const cards = createKeyedReconciler({
    container: grid,
    key: (repo) => repo.name,
    create: (repo) => repoCard(repo),
    update: (node, repo, previous) => {
      const fingerprint = JSON.stringify([
        repo, selectedName, activeRepos.has(repo.name), syncingAll,
      ]);
      if (previous === undefined) {
        node.__repoFingerprint = fingerprint;
        return;
      }
      if (node.__repoFingerprint === fingerprint) return;
      const next = repoCard(repo);
      node.className = next.className;
      node.tabIndex = next.tabIndex;
      node.onclick = next.onclick;
      node.onkeydown = next.onkeydown;
      clear(node);
      while (next.firstChild) node.append(next.firstChild);
      node.__repoFingerprint = fingerprint;
    },
  });

  function selectedRepo() {
    return repositories.find((repo) => repo.name === selectedName) || repositories[0] || null;
  }

  function visibleRepos() {
    const term = search.trim().toLowerCase();
    return repositories.filter((repo) => {
      if (filter === 'attention' && !ATTENTION_STATES.has(repo.state)) return false;
      if (filter === 'forks' && !repo.fork) return false;
      if (!term) return true;
      const haystack = [
        repo.name, repo.repo_full_name, repo.branch, repo.path, repo.host,
        repo.state, repo.origin_url,
      ].filter(Boolean).join(' ').toLowerCase();
      return haystack.includes(term);
    });
  }

  function summary() {
    const attention = repositories.filter((repo) => ATTENTION_STATES.has(repo.state)).length;
    const dirty = repositories.filter((repo) => repo.working_tree?.dirty).length;
    const openPrs = repositories.reduce(
      (sum, repo) => sum + (Array.isArray(repo.pull_requests) ? repo.pull_requests.length : 0), 0,
    );
    const remoteHosts = new Set(
      repositories.filter((repo) => repo.transport === 'ssh').map((repo) => repo.host),
    ).size;
    return { attention, dirty, openPrs, remoteHosts };
  }

  async function load({ refresh = true, background = false } = {}) {
    if (refreshing) return;
    const hadData = repositories.length > 0;
    if (!background) {
      loadError = null;
      loading = true;
      renderMain();
    }
    refreshing = true;
    try {
      const response = await api.get(
        `/api/repositories?refresh=${refresh ? '1' : '0'}&github=1`, { profile },
      );
      const payload = response.data || {};
      repositories = Array.isArray(payload.repositories) ? payload.repositories : [];
      operations = Array.isArray(payload.recent_operations) ? payload.recent_operations : [];
      automation = payload.automation || {};
      loadedFromSource = true;
      selectedName = selectedName || null;
      if (!selectedName || !repositories.some((repo) => repo.name === selectedName)) {
        selectedName = repositories[0]?.name || null;
      }
      writeRepositoryCache(profile, {
        repositories: safeClone(repositories),
        operations: safeClone(operations),
        automation: safeClone(automation),
        selected_name: selectedName,
      });
      actionError = null;
    } catch (err) {
      if (!hadData) loadError = err;
    } finally {
      refreshing = false;
      if (!background) loading = false;
      renderToolbar(toolbar);
      renderMain();
      renderSide();
    }
  }

  function applyLive() {
    const live = liveRows(liveStore, 'repositories', profile);
    if (!live) return false;
    repositories = mergeProjectedRows(repositories, live.rows, (repo) => repo.name);
    if (!selectedName || !repositories.some((repo) => repo.name === selectedName)) {
      selectedName = repositories[0]?.name || null;
    }
    loadError = live.meta.last_error
      ? { message: live.meta.last_error }
      : null;
    renderToolbar(toolbar);
    renderMain();
    renderSide();
    return true;
  }

  async function revalidate({ includeDetails = false } = {}) {
    if (liveStore) {
      await liveStore.resyncResource('repositories', profile, { force: true });
      applyLive();
    }
    if (includeDetails || !loadedFromSource) {
      await load({ refresh: true, background: repositories.length > 0 });
    }
  }

  function bindLive() {
    if (unsubscribe || !liveStore) return;
    unsubscribe = bindLiveResources(liveStore, ['repositories'], profile, () => {
      if (root.isConnected) applyLive();
    });
  }

  function hydrateFromCache() {
    const cached = readRepositoryCache(profile);
    if (!cached) return false;
    if (!Array.isArray(cached.repositories) || !cached.repositories.length) return false;
    repositories = safeClone(cached.repositories);
    operations = safeClone(cached.operations || []);
    automation = safeClone(cached.automation || {});
    selectedName = cached.selected_name || null;
    if (!selectedName || !repositories.some((repo) => repo.name === selectedName)) {
      selectedName = repositories[0]?.name || null;
    }
    loadError = null;
    actionError = null;
    renderToolbar(toolbar);
    renderMain();
    renderSide();
    return true;
  }

  async function runRepoAction(repo, action, extra = {}) {
    if (!repo || activeRepos.has(repo.name) || syncingAll) return;
    selectedName = repo.name;
    actionError = null;
    actionNotice = null;
    activeRepos.add(repo.name);
    renderMain();
    renderSide();
    try {
      let path;
      let body = { auto_commit: autoCommit, ...extra };
      if (action === 'sync') path = `/api/repositories/${encodeURIComponent(repo.name)}/sync`;
      else if (action === 'commit') path = `/api/repositories/${encodeURIComponent(repo.name)}/commit`;
      else if (action === 'upstream') path = `/api/repositories/${encodeURIComponent(repo.name)}/upstream-sync`;
      else throw new Error(`unsupported repository action: ${action}`);
      const response = await api.post(path, body, { profile });
      const operation = response.data || {};
      const failed = operationFailure(operation);
      if (failed) actionError = failed;
      const notice = operationNotice(action, operation);
      await revalidate({ includeDetails: true });
      if (failed) actionError = failed;
      else actionNotice = notice;
    } catch (err) {
      actionError = {
        code: err?.payload?.error?.code || 'request_failed',
        message: err.message || 'repository action failed',
        details: err?.payload?.error?.details || null,
      };
      renderSide();
    } finally {
      activeRepos.delete(repo.name);
      renderMain();
      renderSide();
    }
  }

  async function rebaseMerge(repo, pull) {
    if (!repo || !pull || activeRepos.has(repo.name) || syncingAll) return;
    const confirmed = window.confirm(
      `Rebase and merge PR #${pull.number} into ${repo.repo_full_name}:${repo.branch}?`,
    );
    if (!confirmed) return;
    selectedName = repo.name;
    actionError = null;
    actionNotice = null;
    activeRepos.add(repo.name);
    renderMain();
    renderSide();
    try {
      const response = await api.post(
        `/api/repositories/${encodeURIComponent(repo.name)}/pulls/${pull.number}/rebase-merge`,
        { expected_head_sha: pull.head_sha, auto_commit: autoCommit },
        { profile },
      );
      const operation = response.data || {};
      const failed = operationFailure(operation);
      if (failed) actionError = failed;
      await revalidate({ includeDetails: true });
      if (failed) actionError = failed;
    } catch (err) {
      actionError = {
        code: err?.payload?.error?.code || 'request_failed',
        message: err.message || 'pull request merge failed',
        details: err?.payload?.error?.details || null,
      };
    } finally {
      activeRepos.delete(repo.name);
      renderMain();
      renderSide();
    }
  }

  async function syncAll() {
    if (syncingAll || activeRepos.size) return;
    syncingAll = true;
    actionError = null;
    actionNotice = null;
    renderToolbar(toolbar);
    renderMain();
    renderSide();
    try {
      const response = await api.post(
        '/api/repositories/sync-all', { auto_commit: autoCommit }, { profile },
      );
      const payload = response.data || {};
      const failed = Array.isArray(payload.results)
        ? payload.results.find((item) => item.ok === false)
        : null;
      if (failed) {
        selectedName = failed.repo || selectedName;
        actionError = failed.error || { code: 'sync_all_failed', message: 'one or more repositories failed' };
      }
      await revalidate({ includeDetails: true });
      if (failed) actionError = failed.error || actionError;
      else actionNotice = { tone: 'ok', message: 'Sync all completed successfully.' };
    } catch (err) {
      actionError = { code: 'request_failed', message: err.message || 'sync all failed' };
    } finally {
      syncingAll = false;
      renderToolbar(toolbar);
      renderSide();
      renderMain();
    }
  }

  function metric(label, value, note) {
    return el('div', { class: 'repo-metric' }, [
      el('span', { class: 'repo-metric-label', text: label }),
      el('strong', { class: 'repo-metric-value', text: String(value) }),
      el('span', { class: 'repo-metric-note', text: note }),
    ]);
  }

  function repoCard(repo) {
    const selected = repo.name === selectedName;
    const pending = activeRepos.has(repo.name) || syncingAll;
    const dirty = repo.working_tree || {};
    const failures = conflictFiles(repo);
    const error = repoError(repo);
    const prs = Array.isArray(repo.pull_requests) ? repo.pull_requests : [];
    const card = el('article', {
      class: `repo-card${selected ? ' is-selected' : ''}${ATTENTION_STATES.has(repo.state) ? ' has-attention' : ''}`,
      tabindex: '0',
      onclick: (event) => {
        if (event.target.closest('button,input,a')) return;
        selectedName = repo.name;
        renderMain();
        renderSide();
      },
      onkeydown: (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        selectedName = repo.name;
        renderMain();
        renderSide();
      },
    });

    const badges = [
      repo.fork ? el('span', { class: 'repo-badge repo-badge-accent', text: 'FORK' }) : null,
      repo.private ? el('span', { class: 'repo-badge', text: 'PRIVATE' }) : null,
      repo.transport === 'ssh' ? el('span', { class: 'repo-badge repo-badge-ssh', text: 'SSH' }) : null,
      el('span', { class: 'repo-badge mono', text: repo.branch || '—' }),
    ].filter(Boolean);

    const head = el('div', { class: 'repo-card-head' }, [
      el('div', { class: 'repo-card-title-wrap' }, [
        el('div', { class: 'repo-card-title', text: repo.repo_full_name || repo.name }),
        el('div', {
          class: 'repo-card-sub mono',
          text: `${repo.transport === 'ssh' ? repo.host || 'remote' : 'Hermes'} · ${repo.path || 'checkout unavailable'}`,
        }),
      ]),
      el('div', { class: 'repo-badges' }, badges),
    ]);

    const state = el('div', { class: 'repo-card-state' }, [
      statusChip(TONES[repo.state] || 'idle', String(repo.state || 'unknown').replace('_', ' ')),
      el('span', {
        class: 'repo-card-state-note',
        text: repo.ok === false
          ? (error?.message || 'repository unavailable')
          : `${repo.ahead || 0} ahead · ${repo.behind || 0} behind`,
      }),
    ]);

    const facts = el('div', { class: 'repo-card-facts' }, [
      el('div', { class: 'repo-fact' }, [
        el('span', { text: 'Local / origin' }),
        el('code', { text: `${shortSha(repo.local_sha)} / ${shortSha(repo.remote_sha)}` }),
      ]),
      el('div', { class: 'repo-fact' }, [
        el('span', { text: 'Working tree' }),
        el('strong', {
          text: dirty.dirty
            ? `${dirty.modified || 0} M · ${dirty.staged || 0} staged · ${dirty.untracked || 0} untracked`
            : 'Clean',
        }),
      ]),
      el('div', { class: 'repo-fact' }, [
        el('span', { text: 'Last commit' }),
        el('span', { text: `${fmtWhen(repo.last_commit_at)} · ${repo.last_commit_subject || '—'}` }),
      ]),
      el('div', { class: 'repo-fact' }, [
        el('span', { text: 'Last operation' }),
        el('span', {
          text: repo.last_operation
            ? `${repo.last_operation.action || 'operation'} · ${repo.last_operation.ok ? 'OK' : 'FAILED'} · ${fmtWhen(repo.last_operation.finished_at)}`
            : 'No recorded run',
        }),
      ]),
    ]);

    const notices = [];
    if (failures.length) {
      notices.push(el('button', {
        class: 'repo-notice repo-notice-danger', type: 'button',
        onclick: () => { selectedName = repo.name; renderSide(); },
      }, [
        el('strong', { text: `Conflict · ${failures.length} file${failures.length === 1 ? '' : 's'}` }),
        el('span', { text: failures.slice(0, 2).join(', ') }),
      ]));
    } else if (error) {
      notices.push(el('button', {
        class: 'repo-notice repo-notice-danger', type: 'button',
        onclick: () => { selectedName = repo.name; renderSide(); },
      }, [
        el('strong', { text: error.code || 'Repository error' }),
        el('span', { text: error.message || 'Open inspector for details' }),
      ]));
    }

    const foot = el('div', { class: 'repo-card-foot' }, [
      el('div', { class: 'repo-pr-summary' }, [
        el('span', { class: 'repo-pr-count', text: String(prs.length) }),
        el('span', { text: ` open PR${prs.length === 1 ? '' : 's'}` }),
      ]),
      el('div', { class: 'repo-card-actions' }, [
        el('button', {
          class: 'btn btn-sm btn-primary', type: 'button', disabled: pending ? '' : undefined,
          'aria-busy': pending ? 'true' : undefined,
          onclick: () => runRepoAction(repo, 'sync'),
        }, pending && activeRepos.has(repo.name) ? 'Syncing…' : 'Safe sync'),
        dirty.dirty ? el('button', {
          class: 'btn btn-sm', type: 'button', disabled: pending ? '' : undefined,
          onclick: () => runRepoAction(repo, 'commit', {}),
        }, 'Commit local') : null,
      ].filter(Boolean)),
    ]);

    card.append(head, state, facts, ...notices, foot);
    return card;
  }

  function renderMain() {
    clear(heroHost);
    clear(metricsHost);
    clear(bannerHost);
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
        reason: loadError.message || 'Repository monitor unavailable',
        requestId: loadError.request_id,
      }));
      return;
    }

    const stats = summary();
    heroHost.append(
      el('div', { class: 'repo-hero' }, [
        el('div', {}, [
          el('h1', { class: 'repo-title', text: 'Repositories' }),
          el('p', {
            class: 'repo-subtitle',
            text: 'Production Git state, local work preservation, fork updates and pull-request operations.',
          }),
        ]),
        el('div', { class: 'repo-monitor-state' }, [
          el('span', { class: `repo-live-dot${stats.attention ? ' is-warn' : ''}` }),
          el('span', { text: stats.attention ? `${stats.attention} need attention` : 'All repositories healthy' }),
        ]),
      ]),
    );
    metricsHost.append(
      el('div', { class: 'repo-metrics' }, [
        metric('Repositories', repositories.length, 'owner allowlist'),
        metric('Attention', stats.attention, 'conflict / dirty / behind'),
        metric('Local changes', stats.dirty, 'working trees'),
        metric('Open PRs', stats.openPrs, `${stats.remoteHosts} remote host${stats.remoteHosts === 1 ? '' : 's'}`),
      ]),
    );

    if (loadError) {
      bannerHost.append(el('div', { class: 'repo-banner repo-banner-danger' }, [
        el('strong', { text: 'Refresh degraded' }),
        el('span', { text: loadError.message || 'The last refresh failed; showing retained state.' }),
      ]));
    }
    if (actionNotice) {
      bannerHost.append(el('div', { class: `repo-banner repo-banner-${actionNotice.tone || 'info'}` }, [
        el('strong', { text: actionNotice.tone === 'warn' ? 'Action needs attention' : 'Repository action' }),
        el('span', { text: actionNotice.message }),
      ]));
    }

    const visible = visibleRepos();
    if (!visible.length) {
      grid.hidden = true;
      cards.reconcile([]);
      stateHost.append(el('div', { class: 'repo-empty', text: 'No repositories match the current filter.' }));
      return;
    }
    grid.hidden = false;
    cards.reconcile(visible);
  }

  function toolbarSelect(label, checked, onChange) {
    const input = el('input', {
      type: 'checkbox',
      onchange: (event) => onChange(Boolean(event.target.checked)),
    });
    // `checked="false"` is still a checked HTML boolean attribute. Set the
    // property so the visual control cannot drift from the closure state.
    input.checked = Boolean(checked);
    return el('label', { class: 'repo-toolbar-toggle' }, [input, el('span', { text: label })]);
  }

  function renderToolbar(host) {
    if (!host) return;
    clear(host);
    const searchInput = el('input', {
      class: 'input input-sm repo-search', type: 'search', placeholder: 'Search repositories…',
      value: search, 'data-tab-filter': 'true',
      oninput: (event) => { search = event.target.value; renderMain(); },
    });
    const filterSelect = el('select', {
      class: 'select input-sm repo-filter', value: filter,
      onchange: (event) => { filter = event.target.value; renderMain(); },
    }, [
      el('option', { value: 'all', text: 'All repositories' }),
      el('option', { value: 'attention', text: 'Needs attention' }),
      el('option', { value: 'forks', text: 'Forks' }),
    ]);
    filterSelect.value = filter;
    host.append(
      filterSelect,
      searchInput,
      toolbarSelect('Auto-commit', autoCommit, (value) => { autoCommit = value; renderSide(); }),
      el('span', { class: 'repo-toolbar-toggle', text: 'Live · server managed' }),
      el('button', {
        class: 'btn btn-sm btn-primary', type: 'button',
        disabled: syncingAll || activeRepos.size ? '' : undefined,
        'aria-busy': syncingAll ? 'true' : undefined,
        onclick: () => syncAll(),
      }, syncingAll ? 'Syncing…' : 'Sync all'),
    );
  }

  function kv(label, value, { mono = false, tone = '' } = {}) {
    return el('div', { class: 'repo-side-kv' }, [
      el('span', { class: 'repo-side-k', text: label }),
      el('span', { class: `repo-side-v${mono ? ' mono' : ''}${tone ? ` ${tone}` : ''}`, text: value ?? '—' }),
    ]);
  }

  function sideSection(title, children, extraClass = '') {
    return el('section', { class: `repo-side-section ${extraClass}`.trim() }, [
      el('div', { class: 'repo-side-section-title', text: title }),
      ...children,
    ]);
  }

  function conflictPanel(repo) {
    const files = conflictFiles(repo);
    const err = actionError || repo.error;
    const details = err?.details || {};
    if (!files.length && !err) return null;
    return sideSection('Failure / conflict', [
      el('div', { class: 'repo-side-alert repo-side-alert-danger' }, [
        el('strong', { text: err?.code || (files.length ? 'git_conflict' : 'repository_error') }),
        el('div', { text: err?.message || 'Repository contains unresolved conflicts.' }),
      ]),
      files.length ? el('div', { class: 'repo-conflict-files' }, [
        el('span', { class: 'repo-side-label', text: 'Conflicting files' }),
        ...files.map((path) => el('code', { text: path })),
      ]) : null,
      details.stash_sha ? kv('Preserved stash', details.stash_sha, { mono: true }) : null,
      details.backup_branch ? kv('Backup branch', details.backup_branch, { mono: true }) : null,
      details.recovery_note
        ? el('div', { class: 'repo-recovery-note', text: details.recovery_note }) : null,
    ].filter(Boolean), 'repo-side-section-danger');
  }

  function pullRequestSection(repo) {
    const pulls = Array.isArray(repo.pull_requests) ? repo.pull_requests : [];
    const rows = pulls.length ? pulls.map((pull) => el('div', { class: 'repo-pr-row' }, [
      el('div', { class: 'repo-pr-row-main' }, [
        el('strong', { text: `#${pull.number} ${pull.title || ''}` }),
        el('span', { class: 'mono', text: `${pull.head || '—'} → ${pull.base || repo.branch}` }),
        el('span', { text: `${pull.draft ? 'Draft · ' : ''}${fmtWhen(pull.updated_at)}` }),
      ]),
      el('button', {
        class: 'btn btn-sm btn-accent', type: 'button',
        onclick: () => rebaseMerge(repo, pull),
      }, 'Rebase & merge'),
    ])) : [el('div', { class: 'repo-side-empty', text: 'No open pull requests.' })];
    return sideSection('Pull requests', rows);
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
    const dirty = repo.working_tree || {};
    const op = repo.last_operation || null;
    const historicalFailure = operationFailure(op);
    const pending = activeRepos.has(repo.name) || syncingAll;

    inspectorHost.append(
      el('div', { class: 'repo-side-head' }, [
        el('div', { class: 'repo-side-name', text: repo.repo_full_name }),
        el('div', { class: 'repo-side-status' }, [
          statusChip(TONES[repo.state] || 'idle', repo.state || 'unknown'),
          repo.transport === 'ssh' ? el('span', { class: 'repo-badge repo-badge-ssh', text: 'SSH' }) : null,
          repo.fork ? el('span', { class: 'repo-badge repo-badge-accent', text: 'FORK' }) : null,
        ].filter(Boolean)),
      ]),
      sideSection('Production checkout', [
        kv('Host', repo.transport === 'ssh' ? repo.host : 'Hermes · local'),
        kv('Path', repo.path, { mono: true }),
        kv('Branch', `${repo.current_branch || '—'} / expected ${repo.branch}`, { mono: true }),
        kv('Local SHA', shortSha(repo.local_sha), { mono: true }),
        kv('Origin SHA', shortSha(repo.remote_sha), { mono: true }),
        kv('Ahead / behind', `${repo.ahead || 0} / ${repo.behind || 0}`),
        kv('Working tree', dirty.dirty
          ? `${dirty.entries || 0} entries · ${dirty.modified || 0} modified · ${dirty.untracked || 0} untracked`
          : 'Clean'),
      ]),
      conflictPanel(repo),
      pullRequestSection(repo),
      pending ? sideSection('Operation in progress', [
        el('div', { class: 'repo-side-alert repo-side-alert-info' }, [
          el('strong', { text: 'Working…' }),
          el('div', { text: 'Safe sync is running. Duplicate clicks are disabled until it finishes.' }),
        ]),
      ]) : null,
      sideSection('Latest operation', op ? [
        kv('Action', op.action),
        kv('Trigger', op.trigger),
        kv('Result', op.ok ? 'OK' : 'FAILED', { tone: op.ok ? 'is-ok' : 'is-danger' }),
        kv('Finished', fmtWhen(op.finished_at)),
        kv('Duration', op.duration_ms != null ? `${op.duration_ms} ms` : '—'),
        historicalFailure ? el('div', {
          class: 'repo-side-empty',
          text: `Historical failure: ${historicalFailure.message || historicalFailure.code || 'Unknown error'}`,
        }) : null,
      ] : [el('div', { class: 'repo-side-empty', text: 'No repository operation recorded yet.' })]),
      sideSection('Automation', [
        el('div', { class: 'repo-automation-row' }, [
          el('span', { class: 'repo-side-label', text: 'Cron-safe command' }),
          el('code', { text: automation.cron || 'Unavailable until backend loads.' }),
        ]),
        el('div', { class: 'repo-automation-row' }, [
          el('span', { class: 'repo-side-label', text: 'Future hook trigger' }),
          el('code', { text: (automation.hook_template || '').replace('<repo>', repo.name) || 'Unavailable' }),
        ]),
        el('div', { class: 'repo-side-toggle-row' }, [
          toolbarSelect('Auto-commit local M after clean sync', autoCommit, (value) => {
            autoCommit = value;
            renderToolbar(toolbar);
            renderSide();
          }),
        ]),
      ]),
      sideSection('Recent activity', (
        operations.filter((item) => item.repo === repo.name).slice(0, 6).map((item) =>
          el('div', { class: 'repo-history-row' }, [
            el('span', { class: `repo-history-dot ${item.ok ? 'is-ok' : 'is-danger'}` }),
            el('div', {}, [
              el('strong', { text: item.action || 'operation' }),
              el('span', { text: `${item.trigger || 'unknown'} · ${fmtWhen(item.finished_at)}` }),
            ]),
          ]))
      ).length ? operations.filter((item) => item.repo === repo.name).slice(0, 6).map((item) =>
        el('div', { class: 'repo-history-row' }, [
          el('span', { class: `repo-history-dot ${item.ok ? 'is-ok' : 'is-danger'}` }),
          el('div', {}, [
            el('strong', { text: item.action || 'operation' }),
            el('span', { text: `${item.trigger || 'unknown'} · ${fmtWhen(item.finished_at)}` }),
          ]),
        ])) : [el('div', { class: 'repo-side-empty', text: 'No recent activity.' })]),
      el('div', { class: 'repo-side-footer-actions' }, [
        el('button', {
          class: 'btn btn-sm btn-primary', type: 'button',
          disabled: pending ? '' : undefined,
          'aria-busy': pending ? 'true' : undefined,
          onclick: () => runRepoAction(repo, 'sync'),
        }, pending && activeRepos.has(repo.name) ? 'Syncing…' : 'Safe sync now'),
        dirty.dirty ? el('button', {
          class: 'btn btn-sm', type: 'button', disabled: pending ? '' : undefined,
          onclick: () => runRepoAction(repo, 'commit'),
        }, 'Commit local changes') : null,
      ].filter(Boolean)),
    );
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  return {
    mount(container) {
      clear(container);
      container.append(root);
    },
    async activate() {
      renderToolbar(toolbar);
      bindLive();
      const live = applyLive();
      const hydrated = live || hydrateFromCache();
      if (!loadedFromSource) await load({ refresh: true, background: hydrated });
    },
    deactivate() {
      if (unsubscribe) { unsubscribe(); unsubscribe = null; }
      return { selection: selectedName };
    },
    refresh: () => revalidate({ includeDetails: true }),
    renderToolbar,
    renderInspector,
    get data() { return repositories; },
    get filters() { return { filter, search, autoCommit }; },
  };
}
