// Chat — Hermes Desktop-style conversation surface. The session browser lives
// in the shell's right inspector column: sessions from every profile are
// merged, grouped by platform (source), sorted by recency within each group,
// and searchable. The workspace holds the thread + composer, with a hero
// state before selection.
//
// This module is the orchestrator only. The pieces it wires together live
// alongside it:
//
//   pure/chat-turn.js      one turn's SSE stream, as a testable reducer
//   pure/chat-session.js   session naming, ordering, thread identity
//   pure/chat-model.js     model/effort controls and the request body
//   tabs/chat/transcript   messages, reasoning disclosures, tool rows
//   tabs/chat/turn-view    the reducer bound to the DOM (activity, stop)
//   tabs/chat/composer     input, attachments, and the turn controller
//   tabs/chat/sider        the session browser
//   tabs/chat/context-panel  the context-window gauge
//   tabs/chat/palette      slash/skill menu, toolsets, command palette, find
//   tabs/chat/run-trace    provider requests, from the adapter timeline

import {
  el, clear, closeMenu, emptyState, openMenu, skeleton, unavailableState,
} from '../ui.js';
import { icon } from '../icons.js';
import {
  listFrom, profileRows, recordFrom, sessionRows, summarizeSourceHealth,
} from '../pure/data-shape.js';
import { compressionThresholdFrom } from '../pure/context-window.js';
import { buildTranscriptMarkdown } from '../pure/chat-export.js';
import { buildHash } from '../pure/hash-router.js';
import { copyText } from '../markdown-render.js';
import { toast } from '../components/toast.js';
import {
  createdSession, openTargetId, relativeTime, sessionId as idOf, sessionTimestamp,
  sessionTitle, threadIdentity, withOptimisticSession,
} from '../pure/chat-session.js';
import { attachChainTips } from '../pure/session-chain.js';
import {
  createMirrorBarrier, isAtBottom, messageKeys, mirrorAppend,
} from '../pure/chat-mirror.js';
import { displayModelName, unwrapModelOptions } from '../pure/chat-model.js';
import {
  catalogHas, createModelPrefs, effectiveModel, normalizeProvider, observeConfirmedLock,
  observeRunModel, observeSessionModel, pickSessionModel,
} from '../pure/session-model.js';
import {
  LOAD_MORE_BUDGET, applyPage, createPager, isComplete, loadedCount,
  mergeSessions, planNextPages,
} from '../pure/session-pager.js';
import { turnTokens } from '../pure/chat-turn.js';
import { createModal } from './session-detail.js';
import { renderHistory } from './chat/transcript.js';
import { createComposer } from './chat/composer.js';
import { renderSider } from './chat/sider.js';
import { buildProfileMenu } from './chat/profile-picker.js';
import { createRunTrace } from './chat/run-trace.js';
import { createPermitBanner } from './chat/permits.js';
import { createThreadSearch, openCommandPalette } from './chat/palette.js';
import { fetchChainTips } from './_kit.js';

export const GATEWAY_UNAVAILABLE_REASON =
  'Hermes Gateway is not reachable from the Pi BFF. Session history remains readable; create/send actions are disabled.';

// One page of history. The gateway caps /messages at 500 and the SPA used to
// ask for everything, which made a long-running Telegram thread take seconds to
// open and then rendered thousands of nodes nobody scrolled to.
const PAGE_SIZE = 80;

// Re-reading the thread is now the fallback path, not the happy path, so it can
// afford to wait for the gateway's own persistence instead of racing it.
const RELOAD_BACKOFF_MS = [400, 1200, 2500];

// How often an open thread checks whether someone else advanced it.
//
// The `session.changed` event is the fast path, but it cannot be the only one:
// the BFF's poller reads the dashboard's session list, and that list is chain
// ROOTS — a thread that resets on a schedule is being driven on a successor
// session the poller never sees, so its event never fires. This interval is the
// backstop that makes mirroring true for every session rather than most of
// them. It costs one bounded read per open thread, only while the tab is
// visible and the local composer is idle.
const MIRROR_INTERVAL_MS = 6000;
const MIRROR_HIDDEN_INTERVAL_MS = 30000;

export function createChat({ api, profile, sse, refreshInspector, onNavigate }) {
  const root = el('div', { class: 'tab tab-chat' });
  const threadPane = el('div', { class: 'chat-main' });
  root.append(threadPane);

  let allSessions = null; // null = source unavailable; [] = loaded, empty
  let capabilitiesEnvelope = null;
  let modelOptions = null;
  // Every profile the dashboard knows, so a new session can be started from
  // another profile's persona without leaving this tab's profile scope.
  let profilesList = null;
  // session id -> the profile whose SOUL.md started it. Read from the BFF,
  // which is the only place that fact survives: the gateway keeps the resolved
  // system_prompt but not where it came from.
  const personaBySession = new Map();
  let gatewayAvailable = false;
  let selectedSessionId = null;
  // Which sessions have a turn in flight right now. Two sources, because
  // neither is complete on its own: the gateway's `session.running` topic knows
  // about every session but only as fast as its poll, while the composer knows
  // first-hand — with no delay at all — about the one it is painting. The
  // session list Hermes serves carries no in-flight field whatsoever, which is
  // why the chip this replaces ("active", really just "this record has not
  // ended yet") was lit on practically every row and told the reader nothing.
  let runningSessionId = null;
  let runningIds = new Set();
  let unsubscribeRunning = null;
  let unsubscribeChatFrame = null;

  function runningSet() {
    if (!runningSessionId) return runningIds;
    const union = new Set(runningIds);
    union.add(runningSessionId);
    return union;
  }
  let selectedSession = null;
  let loaded = false;
  let unsubscribeSessionChanged = null;
  let sessionQuery = '';
  let inspectorHost = null;
  let composerHandle = null;
  let threadList = null;
  let threadSearch = null;
  let runTrace = null;
  let historyOffset = 0;
  let historyExhausted = false;
  let permitBanner = null;
  let pendingMessageAnchor = null;
  // Identity of every message currently rendered in the open thread. This is
  // what makes mirroring append-only instead of a repaint.
  let renderedKeys = new Set();
  let mirrorTimer = null;
  let mirrorVisibility = null;
  let mirroring = false;
  const mirrorBaselineBarrier = createMirrorBarrier();
  // Paging state for the full session list. The aggregator's first page is only
  // the newest 500 of (here) 5,295; `pager` knows how to reach the rest.
  let pager = createPager({});
  let loadingMore = false;
  let loadAllRequested = false;
  // What THIS gateway build supports, from its own /v1/capabilities. Affordances
  // are gated on it rather than on the SPA's assumptions, so a dashboard pointed
  // at an older gateway hides the controls that would 404 instead of offering
  // them and failing silently.
  let gatewayFeatures = null;
  // platform key -> how many of that group's rows are revealed. A Map (not a
  // Set) because groups now expand a chunk at a time: with the full list
  // loaded, "expand everything" would build tens of thousands of nodes.
  const expandedPlatforms = new Map();
  const pinnedSessions = new Set();

  // Per-session composer overrides: {model, provider, effort}. Sent per turn on
  // chat/stream rather than persisted as a session model lock (that is a
  // separate, explicit action on the thread header).
  const composerPrefs = new Map();
  // The header chip's model node and which thread it belongs to, so a pick or
  // a run observed after the header was built can repaint it in place instead
  // of leaving it frozen at whatever the thread looked like when it was
  // opened — the header chip, the sidebar row and the composer pill used to
  // each read "the session's model" at a different moment and never converge.
  let modelChipNode = null;
  let modelChipSessionId = null;
  // Per-session extra instructions and unsent drafts. Memory-only: tab state
  // never goes to Web Storage.
  const instructions = new Map();
  const drafts = new Map();

  // Hermes' compaction threshold, read once with the rest of the tab's config.
  const state = { compressionThreshold: null, contextExpanded: false };

  const instructionsStore = {
    get: (id) => instructions.get(id || '') || '',
    set: (id, value) => instructions.set(id || '', String(value || '')),
  };
  const draftStore = {
    get: (id) => drafts.get(id || '') || '',
    set: (id, value) => drafts.set(id || '', String(value || '')),
  };

  function notifyInspector() {
    if (typeof refreshInspector === 'function') refreshInspector();
    else if (inspectorHost) renderInspector(inspectorHost);
  }

  async function load() {
    clear(threadPane);
    threadPane.append(skeleton({ lines: 6 }));

    const [
      capabilityResult, sessionResult, modelResult, configResult, gatewayResult,
      profilesResult,
    ] = await Promise.allSettled([
      api.get('/api/capabilities', { profile }),
      // The dashboard's cross-profile aggregator: one read that already tags
      // every row with its owning `profile` and `source`.
      // 500 is this endpoint's hard cap (`le=500` on its `limit`), so this is
      // the largest first screenful it can give. Everything past it is paged
      // per profile — see loadMoreSessions().
      api.get('/api/upstream/api/profiles/sessions?profile=all&limit=500&offset=0&order=recent', { profile }),
      api.get('/api/upstream/api/model/options', { profile }),
      api.get('/api/upstream/api/config', { profile }),
      api.get('/api/gateway/v1/capabilities', { profile }),
      api.get('/api/upstream/api/profiles', { profile }),
    ]);
    profilesList = profilesResult.status === 'fulfilled'
      ? profileRows(profilesResult.value?.data)
      : null;
    gatewayFeatures = gatewayResult.status === 'fulfilled'
      ? (recordFrom(gatewayResult.value?.data)?.features || null)
      : null;
    capabilitiesEnvelope = capabilityResult.status === 'fulfilled' ? capabilityResult.value : null;
    modelOptions = modelResult.status === 'fulfilled' ? unwrapModelOptions(modelResult.value?.data) : null;
    state.compressionThreshold = configResult.status === 'fulfilled'
      ? compressionThresholdFrom(configResult.value?.data)
      : null;

    const health = summarizeSourceHealth(capabilitiesEnvelope?.data);
    gatewayAvailable = health.sources['hermes-gateway']?.healthy === true;

    if (sessionResult.status === 'fulfilled') {
      const envelope = recordFrom(sessionResult.value?.data) || {};
      allSessions = sessionRows(sessionResult.value?.data);
      // `total` and `profile_totals` are what make the rest of the list
      // reachable: the aggregator cannot page past sum(min(count, 500)) rows,
      // so the pager uses the per-profile counts to continue on its own.
      pager = createPager({
        total: envelope.total || allSessions.length,
        profileTotals: envelope.profile_totals || {},
        sessions: allSessions,
      });
    } else {
      // Aggregator unavailable (older dashboard) — fall back to the active
      // profile's own list so the sidebar still renders.
      const fallback = await api.get('/api/upstream/api/sessions?limit=100&offset=0', { profile })
        .catch(() => null);
      allSessions = fallback ? sessionRows(fallback.data).map((s) => ({ profile, ...s })) : null;
      const total = recordFrom(fallback?.data)?.total || (allSessions ? allSessions.length : 0);
      pager = createPager({
        total,
        profileTotals: { [profile || 'default']: total },
        sessions: allSessions || [],
      });
    }
    await resolveChainTips();
    applyPins();

    loaded = true;
    notifyInspector();
    if (selectedSessionId) await openThread(selectedSessionId);
    else renderHero();
  }

  /**
   * Pull the next slice of the full session list.
   *
   * Runs the pager's plan with bounded concurrency: each entry is one
   * `/api/sessions?profile=…&limit=100&offset=…` read, which is the only
   * endpoint that pages a profile all the way down. Rows merge into the
   * existing list newest-first and deduped, so the ordering the sider renders
   * never depends on the order the pages happen to land in.
   */
  async function loadMoreSessions(budget = LOAD_MORE_BUDGET) {
    if (loadingMore || isComplete(pager)) return;
    loadingMore = true;
    notifyInspector();
    try {
      const plan = planNextPages(pager, budget);
      const CONCURRENCY = 4;
      for (let i = 0; i < plan.length; i += CONCURRENCY) {
        if (!loadAllRequested && i > 0 && !loadingMore) break;
        const batch = plan.slice(i, i + CONCURRENCY);
        const results = await Promise.all(batch.map((page) => api
          .get(
            `/api/upstream/api/sessions?profile=${encodeURIComponent(page.profile)}`
            + `&limit=${page.limit}&offset=${page.offset}&order=recent`,
            { profile: page.profile },
          )
          .then((response) => ({ page, rows: sessionRows(response?.data) }))
          .catch(() => ({ page, rows: [] }))));

        for (const { page, rows } of results) {
          // The single-profile endpoint does not tag rows with their profile;
          // the sider groups and deep-links on it, so tag them here.
          const tagged = rows.map((row) => ({ profile: page.profile, ...row }));
          allSessions = mergeSessions(allSessions || [], tagged, idOf, sessionTimestamp);
          pager = applyPage(pager, page.profile, rows.length, page.limit);
        }
        // Resolves only the rows this batch just merged in — `resolveChainTips`
        // skips anything already resolved, so paging in another 600 sessions
        // costs one more round of tip lookups, not a re-resolve of everything
        // loaded so far.
        await resolveChainTips();
        applyPins();
        notifyInspector();
      }
    } finally {
      loadingMore = false;
      notifyInspector();
    }
    // "Load all" keeps going until the pager says every profile is drained.
    if (loadAllRequested && !isComplete(pager)) await loadMoreSessions(budget);
  }

  function loadAllSessions() {
    loadAllRequested = true;
    return loadMoreSessions().catch(() => null);
  }

  function stopLoadingSessions() {
    loadAllRequested = false;
  }

  /**
   * Does this gateway advertise the feature? An absent capability document
   * (older gateway, or the read failed) means "assume yes" — the affordance
   * then fails loudly against upstream rather than being invisibly withheld.
   */
  function supports(feature) {
    if (!gatewayFeatures) return true;
    return gatewayFeatures[feature] !== false;
  }

  /**
   * Resolve every currently-loaded row that has not been resolved yet, and
   * attach `.tip`.
   *
   * The dashboard's own session list is chain ROOTS — it deliberately hides
   * the successor Hermes starts every time a thread resets or compresses —
   * so a session that resets on a schedule (a daily orchestrator is the case
   * that exposed this: 36 resets over six weeks, always frozen at whatever
   * the root's timestamp was) is invisible as its actual live self anywhere
   * in this sider. `sessionTitle`/`sessionTimestamp`/`openTargetId` all read
   * `.tip` once it is here, so this one call is what makes sorting, the
   * displayed title, and the click target all correct.
   *
   * Only rows without a resolved id are sent — `loadMoreSessions` calls this
   * again after every batch, and re-resolving the first 500 rows on every
   * "Load more" would be pure waste.
   */
  async function resolveChainTips() {
    if (!allSessions || !allSessions.length) return;
    const pending = allSessions.filter((row) => row.tip === undefined);
    if (!pending.length) return;
    const tips = await fetchChainTips({ api, profile, ids: pending.map(idOf) }).catch(() => ({}));
    const resolved = attachChainTips(pending, tips, idOf);
    const byId = new Map(resolved.map((row) => [idOf(row), row]));
    allSessions = allSessions.map((row) => byId.get(idOf(row)) || row);
  }

  function applyPins() {
    if (!allSessions) return;
    allSessions = allSessions.map((session) => (
      pinnedSessions.has(idOf(session)) ? { ...session, pinned: true } : session
    ));
  }

  /* ---------------------------------------------------------------- hero -- */

  function renderHero() {
    clear(threadPane);
    threadList = null;
    composerHandle = null;
    if (!allSessions) {
      threadPane.append(unavailableState({ reason: 'Session source unavailable' }));
      return;
    }
    const hero = el('div', { class: 'chat-hero' });
    hero.append(
      el('div', { class: 'chat-hero-mark', text: 'HERMES AGENT' }),
      el('div', { class: 'chat-hero-sub', text: 'Select a session on the right, or start a new one.' }),
    );
    const rows = visibleSessions();
    const profileCount = new Set(rows.map((item) => item.profile).filter(Boolean)).size || 1;
    hero.append(el('div', {
      class: 'chat-hero-meta',
      text: `${rows.length} session${rows.length === 1 ? '' : 's'} across ${profileCount} profile${profileCount === 1 ? '' : 's'}`,
    }));
    if (!gatewayAvailable) {
      hero.append(el('div', { class: 'state-warning chat-hero-warning', role: 'status' }, [
        el('div', { class: 'state-warning-title', text: 'Chat write path degraded' }),
        el('div', { class: 'state-warning-note', text: GATEWAY_UNAVAILABLE_REASON }),
      ]));
    }
    hero.append(el('button', {
      class: 'btn btn-sm chat-hero-cta', type: 'button', onclick: () => createSession(),
      disabled: gatewayAvailable ? null : 'disabled',
    }, [icon('spark', { size: 13 }), ' New session']));
    threadPane.append(hero);
  }

  /* -------------------------------------------------------------- thread -- */

  async function openThread(sessionId, { navigate = true } = {}) {
    if (!sessionId) return;
    selectedSessionId = sessionId;
    stopStream();
    stopMirror();
    notifyInspector();
    clear(threadPane);
    threadPane.append(skeleton({ lines: 6 }));

    // The sider list is a bounded window of recent sessions, so a session
    // opened from outside it (a deep link, or the Fleet/Topology chat popup)
    // will not be in `allSessions`. Read the record instead so every entry
    // point gets the same header.
    let session = visibleSessions().find((item) => idOf(item) === sessionId);
    if (!session) {
      const fetched = await api
        .get(`/api/upstream/api/sessions/${encodeURIComponent(sessionId)}`, { profile })
        .catch(() => null);
      session = recordFrom(fetched?.data, ['session']) || { id: sessionId };
      if (!session.id) session.id = sessionId;
    }
    selectedSession = session;
    const sessionProfile = session.profile || profile;

    historyOffset = 0;
    historyExhausted = false;
    const [messages] = await Promise.all([
      readMessages(sessionId, sessionProfile, 0),
      loadPersona(sessionId),
    ]);

    clear(threadPane);
    threadPane.append(threadHeader(session, sessionProfile));

    const list = el('div', { class: 'thread-list' });
    threadList = list;
    threadSearch = createThreadSearch(list, {
      api, profile, onOpenSession: (id) => openThread(id),
    });
    threadPane.append(threadSearch.node);

    // Permits are the agent's other blocking state. The ledger is not
    // session-scoped, so this is a banner rather than an inline row — but it is
    // decidable here instead of only from the Permits tab.
    permitBanner = createPermitBanner({ api, profile, onNavigate });
    threadPane.append(permitBanner.node);
    permitBanner.refresh().catch(() => null);

    const earlier = el('button', {
      class: 'chat-load-earlier', type: 'button',
      onclick: () => loadEarlier(sessionId, sessionProfile, earlier),
    }, ['Load earlier messages']);
    earlier.hidden = messages.length < PAGE_SIZE;
    list.append(earlier);

    renderHistory(list, messages, {
      onFork: () => forkSession(session),
      onRegenerate: (message) => composerHandle && composerHandle.setText(message.content || ''),
      onPermalink: permalink,
    });
    renderedKeys = new Set(messageKeys(messages));
    if (!messages.length) {
      list.append(emptyState({ title: 'No messages yet', note: 'Send the first message to start this conversation.' }));
    }
    threadPane.append(list);
    list.scrollTop = list.scrollHeight;

    composerHandle = createComposer({
      api,
      profile,
      disabled: !gatewayAvailable,
      placeholder: gatewayAvailable ? 'Give Hermes a task…' : GATEWAY_UNAVAILABLE_REASON,
      unavailableReason: GATEWAY_UNAVAILABLE_REASON,
      list,
      sessionId,
      sessionProfile,
      session,
      prefs: composerPrefsFor(sessionId, session),
      contextState: { get expanded() { return state.contextExpanded; }, set expanded(v) { state.contextExpanded = v; } },
      compressionThreshold: state.compressionThreshold,
      modelOptions,
      instructionsStore,
      draftStore,
      onFork: () => forkSession(session),
      onTurnSettled: (turn) => onTurnSettled(
        turn, session, sessionId, sessionProfile,
      ),
      // A turn watched from elsewhere was painted from frames, not from
      // persisted rows, so the mirror has to be re-anchored exactly as it is
      // after a local turn — otherwise the next poll appends the whole thing
      // again as plain history.
      onRemoteSettled: () => {
        syncMirrorBaseline(sessionId, sessionProfile).catch(() => null);
        patchLocalSession(session, { last_activity_at: Math.floor(Date.now() / 1000) });
        refreshSessionList().catch(() => null);
      },
      // Only the tab actually painting a turn knows one is running: the session
      // list Hermes serves carries no in-flight field. The gateway's own
      // `session.running` topic covers every OTHER session; this covers the one
      // open here without waiting for that poll to come round.
      onRunningChange: (isRunning) => {
        runningSessionId = isRunning ? sessionId : null;
        notifyInspector();
      },
      onNeedsReload: () => reloadThread(list, sessionId, sessionProfile),
      // A pick is a fact the header chip needs the instant it happens, not
      // only once some future turn confirms it — the whole complaint this
      // fixes is a chip that kept naming an old model long after the operator
      // had already changed the pick.
      onModelChanged: () => refreshModelChip(sessionId, session),
      // A pick from the pill has to mean the same thing a `/model` switch
      // means in the TUI: the session is now this model, full stop, for
      // every future turn and every client — not "this tab, until reload".
      onPickModel: (model, provider) => persistModelLock(session, model, provider),
    });
    threadPane.append(composerHandle.node);
    // The list read that built `session` above never carries `model_config` —
    // only the single-session read does — so the lock badge starts blank and
    // upgrades once this resolves, rather than blocking the thread on it.
    syncModelLockStatus(sessionId, sessionProfile, session).catch(() => null);

    subscribeToSession(sessionId, sessionProfile, list);
    // Point the app's one SSE connection at this session so its live turn
    // frames ride the stream that is already open, instead of this tab opening
    // a second permanent connection of its own.
    if (sse) sse.watch(sessionId);
    startMirror(sessionId, sessionProfile, list);

    // Chat was the only major tab with no deep link, so a refresh always
    // dropped you back on the hero. `profile` stays in the document query —
    // never in the hash — exactly as the router contract requires.
    // Written directly to the URL bar rather than through onNavigate/app.js's
    // navigate(): that function re-mounts and re-activates the CACHED chat
    // instance on every call (tabInstances keys by route, not by params), so
    // routing an in-place selection through it turned every openThread into
    // another activate() → load() → openThread() → onNavigate() cycle — an
    // infinite loop of the same 8 reads, confirmed live (176 requests and
    // climbing on one deep link). Matches the router's own contract: `profile`
    // stays in the document query, only route params go in the hash.
    if (navigate) {
      const url = new URL(window.location.href);
      url.hash = buildHash('/chat', { s: sessionId });
      window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
    }

    if (pendingMessageAnchor) {
      focusMessage(pendingMessageAnchor);
      pendingMessageAnchor = null;
    }
  }

  /**
   * Scroll to and mark a specific message, for `#/chat?s=<id>&m=<message_id>`.
   * A message that is not in the loaded page keeps paging back until it is,
   * because a permalink that lands on "somewhere in this thread" is not one.
   */
  async function focusMessage(messageId, attempts = 6) {
    for (let i = 0; i < attempts; i += 1) {
      const node = threadList?.querySelector(`[data-message-id="${CSS.escape(messageId)}"]`);
      if (node) {
        node.scrollIntoView({ block: 'center' });
        node.classList.add('is-anchored');
        setTimeout(() => node.classList.remove('is-anchored'), 2600);
        return true;
      }
      const button = threadList?.querySelector('.chat-load-earlier');
      if (!button || button.hidden) return false;
      await loadEarlier(selectedSessionId, selectedSession?.profile || profile, button);
    }
    return false;
  }

  async function readMessages(sessionId, sessionProfile, offset) {
    const response = await api.get(
      `/api/upstream/api/sessions/${encodeURIComponent(sessionId)}/messages?limit=${PAGE_SIZE}&offset=${offset}&order=latest`,
      { profile: sessionProfile },
    ).catch(() => null);
    // Verified against the live dashboard: `order=latest` selects the NEWEST
    // page but returns it in chronological order, and `offset` walks backwards
    // a page at a time. So the rows are already oldest-first — reversing them
    // here would print every page upside down.
    return listFrom(response?.data, ['messages']);
  }

  async function loadEarlier(sessionId, sessionProfile, button) {
    if (historyExhausted) return;
    historyOffset += PAGE_SIZE;
    button.disabled = true;
    const older = await readMessages(sessionId, sessionProfile, historyOffset);
    button.disabled = false;
    if (!older.length) {
      historyExhausted = true;
      button.hidden = true;
      return;
    }
    const anchor = threadList.scrollHeight;
    const fragment = document.createDocumentFragment();
    renderHistory(fragment, older, {});
    for (const key of messageKeys(older)) renderedKeys.add(key);
    button.after(fragment);
    // Keep the reader where they were rather than jumping to the new top.
    threadList.scrollTop += threadList.scrollHeight - anchor;
    if (older.length < PAGE_SIZE) { historyExhausted = true; button.hidden = true; }
  }

  /**
   * Fallback re-read. Only runs when a turn produced nothing observable, and
   * retries with backoff instead of blanking a live transcript — the previous
   * version cleared the thread the instant the stream closed and repainted
   * whatever the gateway had persisted so far, which is what made replies
   * vanish until a manual refresh.
   */
  async function reloadThread(list, sessionId, sessionProfile) {
    for (const delay of RELOAD_BACKOFF_MS) {
      await new Promise((resolve) => setTimeout(resolve, delay));
      const messages = await readMessages(sessionId, sessionProfile, 0);
      if (!messages.length) continue;
      if (list !== threadList) return; // the reader moved on
      clear(list);
      renderHistory(list, messages, {});
      renderedKeys = new Set(messageKeys(messages));
      list.scrollTop = list.scrollHeight;
      return;
    }
  }

  /* ------------------------------------------------------------- mirroring -- */

  /**
   * Append whatever someone else added to this thread since the last look.
   *
   * "Someone else" is the whole point: a Hermes session belongs to the
   * conversation, not to the client that happens to have it open, and a turn
   * driven from Telegram, cron, the CLI or a kanban seat used to be invisible
   * here until a manual refresh. Append-only, so a reader mid-scroll with a
   * tool result open keeps both.
   */
  async function mirrorThread(sessionId, sessionProfile, list) {
    if (mirroring) return;
    if (list !== threadList || selectedSessionId !== sessionId) return;
    // A live turn-view owns the transcript while it is streaming — whether this
    // tab started that turn or is watching one started from the CLI, Telegram
    // or cron. Either way the very rows this would re-fetch are already being
    // painted frame by frame, and appending the persisted copy on top would
    // duplicate the whole turn.
    if (composerHandle && (composerHandle.isRunning() || composerHandle.isWatching())) return;
    // A completed streamed turn is visible already, but its persisted message
    // ids arrive slightly later. Until baseline sync records those ids, a
    // mirror read would append the same turn as ordinary history.
    if (mirrorBaselineBarrier.active(sessionId)) return;
    mirroring = true;
    try {
      const messages = await readMessages(sessionId, sessionProfile, 0);
      if (!messages.length) return;
      if (list !== threadList || selectedSessionId !== sessionId) return;
      // The read may have started while idle and returned after a local send
      // claimed the transcript or a baseline sync began. Re-check immediately
      // before touching the DOM to close that in-flight request race.
      if (composerHandle && (composerHandle.isRunning() || composerHandle.isWatching())) return;
      if (mirrorBaselineBarrier.active(sessionId)) return;
      const fresh = mirrorAppend(renderedKeys, messages);
      if (!fresh.length) return;

      const follow = isAtBottom(list);
      const fragment = document.createDocumentFragment();
      renderHistory(fragment, fresh, { onPermalink: permalink });
      list.append(fragment);
      for (const key of messageKeys(fresh)) renderedKeys.add(key);
      if (follow) list.scrollTop = list.scrollHeight;

      // The sider orders by recency, so a thread advancing elsewhere has to
      // move in the list too — otherwise the transcript is live while the row
      // above it still claims the session went quiet an hour ago.
      patchRootByTarget(sessionId, { last_activity_at: Math.floor(Date.now() / 1000) });
      notifyInspector();
      if (runTrace) runTrace.refresh().catch(() => null);
    } catch (_err) {
      // A failed poll is not worth surfacing: the next tick retries, and the
      // transcript on screen is still whatever the last good read produced.
    } finally {
      mirroring = false;
    }
  }

  /**
   * Re-anchor the mirror on the server's copy without repainting.
   *
   * After a locally streamed turn the transcript on screen was drawn from SSE
   * frames, not from persisted rows, so none of it is in `renderedKeys` — and
   * the next mirror poll would faithfully append the turn a second time, as a
   * plain history row with no `msg-turn-foot` (that receipt is a turn-view-only
   * feature; the mirror path never had it). This marks the server's current
   * page as already-seen instead, retrying because the gateway finishes
   * persisting shortly after the stream closes.
   *
   * It has to retry the WHOLE backoff schedule, not stop at the first non-empty
   * read: any session with prior history is non-empty on the very first
   * attempt regardless of whether the turn that just finished has been
   * persisted yet, so treating "got some page back" as "found the turn I'm
   * waiting for" made this return immediately on turn one of any established
   * thread — exactly backwards from what the retry loop exists for. Folding
   * keys from every attempt is safe (a `Set` add is idempotent), so there is no
   * reason to stop early even once something comes back.
   */
  async function syncMirrorBaseline(sessionId, sessionProfile) {
    // Acquired synchronously, before the first backoff await, so a session
    // event or timer cannot squeeze a mirror append into the persistence gap.
    const release = mirrorBaselineBarrier.acquire(sessionId);
    try {
      for (const delay of RELOAD_BACKOFF_MS) {
        await new Promise((resolve) => setTimeout(resolve, delay));
        if (selectedSessionId !== sessionId) return;
        const messages = await readMessages(sessionId, sessionProfile, 0).catch(() => []);
        for (const key of messageKeys(messages)) renderedKeys.add(key);
      }
    } finally {
      release();
    }
  }

  /**
   * Re-read the newest page of the session list and merge it in place.
   *
   * Deliberately not `load()`: that rebuilds the whole tab, re-opens the
   * thread and throws away paging progress. This only refreshes recency,
   * titles and message counts for the rows the aggregator considers newest,
   * which is exactly what a session advancing elsewhere changes. Throttled,
   * because `session.changed` is a fleet-wide topic and a busy fleet emits it
   * far more often than a sidebar needs redrawing.
   */
  let lastListRefresh = 0;
  async function refreshSessionList() {
    const now = Date.now();
    if (now - lastListRefresh < 10000) return;
    lastListRefresh = now;
    const response = await api
      .get('/api/upstream/api/profiles/sessions?profile=all&limit=200&offset=0&order=recent', { profile })
      .catch(() => null);
    const rows = sessionRows(response?.data);
    if (!rows.length) return;
    allSessions = mergeSessions(allSessions || [], rows, idOf, sessionTimestamp);
    await resolveChainTips();
    applyPins();
    notifyInspector();
  }

  function stopMirror() {
    if (mirrorTimer) clearInterval(mirrorTimer);
    mirrorTimer = null;
    if (mirrorVisibility) document.removeEventListener('visibilitychange', mirrorVisibility);
    mirrorVisibility = null;
  }

  function startMirror(sessionId, sessionProfile, list) {
    stopMirror();
    let hidden = document.hidden;
    const arm = () => {
      if (mirrorTimer) clearInterval(mirrorTimer);
      hidden = document.hidden;
      // A backgrounded tab still mirrors, just slowly. Chrome throttles timers
      // in hidden tabs well past whatever interval is asked for anyway, which
      // is exactly why coming back cannot be left to the timer — see below.
      mirrorTimer = setInterval(
        () => { mirrorThread(sessionId, sessionProfile, list).catch(() => null); },
        hidden ? MIRROR_HIDDEN_INTERVAL_MS : MIRROR_INTERVAL_MS,
      );
    };

    // Returning to the tab has to catch up NOW, not on the next tick.
    //
    // Without this the whole feature still felt like it needed a refresh, for a
    // reason that is invisible from the code alone: the interval a hidden tab
    // was armed with is not the interval it gets. Chrome throttles background
    // timers hard — measured here at roughly 45s against a 30s interval — so
    // switching back to a thread that had advanced meant staring at a stale
    // transcript for the better part of a minute. Refreshing was simply faster,
    // which is precisely the behaviour this was meant to remove. The visible
    // path also re-arms at the fast interval instead of inheriting the slow one.
    mirrorVisibility = () => {
      arm();
      if (!document.hidden) mirrorThread(sessionId, sessionProfile, list).catch(() => null);
    };
    document.addEventListener('visibilitychange', mirrorVisibility);
    arm();
  }

  function onTurnSettled(turn, session, sessionId, sessionProfile) {
    // Refresh the sider's recency ordering without a full reload. The runtime
    // the gateway REPORTS having used also lands here, so the header chip
    // shows what actually answered rather than what the row was created with —
    // the two diverging without saying so is what made a turn look like it had
    // honoured a model choice it had not.
    const ran = turn?.runtime || null;
    const targetId = openTargetId(session);
    // Through `patchLocalSession`, not a direct `patchRootByTarget` call: that
    // also mutates the live `selectedSession` object in place, which is the
    // exact same object the open thread's composer was handed at creation. Any
    // future reader of `session.model` on that reference — not only the ones
    // this turn already fixed — sees the current answer instead of whatever
    // the thread looked like the moment it was opened.
    patchLocalSession(session, {
      last_activity_at: Math.floor(Date.now() / 1000),
      ...(ran?.model ? { model: ran.model } : {}),
      ...(ran?.provider ? { provider: ran.provider } : {}),
    });
    // Every surface that names this session's model hears the same news.
    // Patching only the row is what used to let the pill and the header chip
    // go on naming a model that had already been superseded by the one the
    // gateway reported running — each read "the model" at its own moment and
    // none of them heard about this one.
    if (ran?.model && composerPrefs.has(targetId)) {
      const prefs = composerPrefs.get(targetId);
      // Captured BEFORE `observeRunModel` touches it: that call is a no-op
      // once a session is already locked (`prefs.explicit`), so this is the
      // one moment that can tell "never locked" apart from "locked, still
      // running the same thing".
      const wasLocked = prefs.explicit;
      observeRunModel(prefs, ran, modelOptions);
      if (composerHandle) composerHandle.refreshModel();
      // A session nobody ever visited the picker for was left to drift on
      // whatever the gateway's precedence chain resolved turn to turn — the
      // exact failure mode this whole feature exists to close, just for the
      // common case where the operator never clicked anything. The moment a
      // turn actually runs, what it ran on is no longer a guess; lock it then,
      // quietly, the same way a deliberate pick would, rather than leaving a
      // manual "Pin" click as the only way to stop the drift.
      if (!wasLocked) {
        // `ran.provider` is the gateway's resolved KIND (e.g. `custom:9router`
        // for every custom provider), not the picker's own slug — passed
        // through raw, it fails `catalogHas`'s slug match and the pill would
        // call a real, catalog-listed model a "routing alias" it is not.
        const provider = normalizeProvider(modelOptions, ran.provider) || prefs.provider;
        pickSessionModel(prefs, ran.model, provider);
        persistModelLock(session, ran.model, provider, { silent: true }).catch(() => null);
      }
    }
    refreshModelChip(targetId, session);
    if (runTrace) runTrace.refresh().catch(() => null);
    // The turn just drawn on screen came from SSE frames, not persisted rows.
    // Teach the mirror that those rows are already on screen, or its next poll
    // would append the whole turn again.
    syncMirrorBaseline(sessionId, sessionProfile).catch(() => null);
    // A long turn that finishes while the operator is elsewhere should say so.
    if (document.hidden && turn && !turn.error) {
      const tokens = turnTokens(turn.usage);
      notifyDone(sessionTitle(session), tokens ? `${tokens.total.toLocaleString()} tokens` : '');
    }
  }

  function notifyDone(title, detail) {
    try {
      if (typeof Notification === 'undefined') return;
      if (Notification.permission === 'granted') {
        new Notification(`Hermes finished · ${title}`, { body: detail });
      } else if (Notification.permission === 'default') {
        Notification.requestPermission().catch(() => null);
      }
    } catch (_err) { /* notifications are a nicety, never a failure path */ }
  }

  /** Which profile's persona started this session, cached for the tab's life. */
  async function loadPersona(sessionId) {
    if (!sessionId || personaBySession.has(sessionId)) return;
    const response = await api
      .get(`/api/sessions/${encodeURIComponent(sessionId)}/persona`, { profile })
      .catch(() => null);
    const body = recordFrom(response?.data) || {};
    personaBySession.set(sessionId, body.profile_name || null);
  }

  /** Paint the header's model chip from the current resolver state, or hide it. */
  function paintModelChip(id, session) {
    if (!modelChipNode) return;
    const view = effectiveModel(composerPrefsFor(id, session), modelOptions);
    modelChipNode.hidden = !view.model;
    if (!view.model) return;
    modelChipNode.textContent = view.inherited ? `${view.model} (default)` : view.model;
    modelChipNode.title = view.note;
  }

  /** Repaint the header chip after something changed the resolved model —
   * a no-op once the reader has navigated to a different thread. */
  function refreshModelChip(id, session) {
    if (modelChipSessionId !== id) return;
    paintModelChip(id, session);
  }

  function threadHeader(session, sessionProfile) {
    const head = el('div', { class: 'chat-thread-head' });
    const titleRow = el('div', { class: 'chat-thread-title-row' });
    titleRow.append(el('div', { class: 'chat-thread-title', text: sessionTitle(session) }));

    const actions = el('div', { class: 'chat-thread-actions' });
    actions.append(headAction('search', 'Find in conversation', () => threadSearch.toggle()));
    actions.append(headAction('activity', 'Run trace — what reached the provider', () => toggleRunTrace(session)));
    actions.append(headAction('download', 'Export transcript', () => exportTranscript(session)));
    // Gated on what this gateway build actually advertises, not on hope.
    if (gatewayAvailable && supports('session_resources')) {
      actions.append(headAction('branch', 'Fork this conversation', () => forkSession(session)));
      actions.append(headAction('lock', 'Pin the model for this session', () => lockModel(session)));
    }
    actions.append(headAction('pencil', 'Rename session', () => renameSession(session)));
    titleRow.append(actions);
    head.append(titleRow);

    const meta = el('div', { class: 'chat-thread-meta' });
    const bits = [];
    if (session.profile) bits.push(['chip', session.profile]);
    // A runner-backed session's own profile IS session.profile (it runs on
    // that profile's own isolated gateway), so this only ever shows for
    // legacy sessions created on the shared default gateway with another
    // profile's SOUL.md borrowed in — the one case where the two diverge.
    const persona = personaBySession.get(openTargetId(session));
    if (persona && persona !== session.profile) bits.push(['chip', `persona: ${persona}`]);
    if (session.source) bits.push(['chip', session.source]);
    // Placeholder (`null` text) so the model chip keeps its position next to
    // profile/source instead of trailing after every plain-text stat — it is
    // built as its own node below (so `refreshModelChip` has a stable element
    // to repaint), but the ORDER it reads in is still decided here.
    bits.push(['chip', null]);
    if (session.message_count !== undefined && session.message_count !== null) {
      bits.push(['plain', `${session.message_count} msgs`]);
    }
    if (session.tool_call_count) bits.push(['plain', `${session.tool_call_count} tools`]);
    const tokens = (session.input_tokens || 0) + (session.output_tokens || 0);
    if (tokens) bits.push(['plain', `${tokens.toLocaleString()} tok`]);
    if (session.last_activity_at) bits.push(['plain', `active ${relativeTime(session.last_activity_at)}`]);

    // Built as its own node rather than a plain string, so `refreshModelChip`
    // has a stable element to repaint in place after a pick or a run — the
    // same resolver the composer pill uses, so the two surfaces can no longer
    // name two different models for one session.
    const targetId = openTargetId(session);
    modelChipNode = el('span', { class: 'chip chip-info' });
    modelChipSessionId = targetId;
    paintModelChip(targetId, session);

    for (const [kind, text] of bits) {
      if (kind === 'chip' && text === null) { meta.append(modelChipNode); continue; }
      meta.append(kind === 'chip'
        ? el('span', { class: 'chip chip-info', text })
        : el('span', { class: 'chat-thread-meta-item', text }));
    }

    // Room/thread identity for chat platforms — telegram & friends bind a
    // session to one chat+thread, so these ids are what distinguish it.
    const thread = threadIdentity(session);
    if (thread) {
      if (thread.threadId) {
        meta.append(el('span', { class: 'chat-thread-origin mono', title: 'Thread id' }, [
          el('span', { class: 'chat-thread-origin-key', text: 'thread' }),
          thread.threadId,
        ]));
      }
      if (thread.chatId) {
        meta.append(el('span', {
          class: 'chat-thread-origin mono',
          title: thread.chatName ? `Chat id — ${thread.chatName}` : 'Chat id',
        }, [
          el('span', { class: 'chat-thread-origin-key', text: thread.chatType || 'chat' }),
          thread.chatId,
        ]));
      }
    }

    meta.append(el('span', { class: 'chat-thread-id mono', text: session.id || '' }));
    head.append(meta);
    head.dataset.sessionProfile = sessionProfile || '';
    return head;
  }

  function headAction(iconName, label, onClick) {
    const button = el('button', {
      class: 'chat-thread-action', type: 'button', title: label, 'aria-label': label,
    }, [icon(iconName, { size: 13 })]);
    button.addEventListener('click', onClick);
    return button;
  }

  function toggleRunTrace(session) {
    if (runTrace && runTrace.node.isConnected) {
      runTrace.node.remove();
      runTrace = null;
      return;
    }
    runTrace = createRunTrace({ api, sessionId: openTargetId(session) });
    threadPane.insertBefore(runTrace.node, threadList);
    runTrace.refresh().catch(() => null);
  }

  /**
   * A shareable link to one message. `profile` stays in the document query and
   * only the route params go in the hash — the canonical URL contract, built
   * with the router's own helper rather than by string concatenation.
   */
  function permalink(messageId, button) {
    const url = new URL(window.location.href);
    url.hash = buildHash('/chat', { s: selectedSessionId, m: messageId });
    copyText(url.toString(), button);
  }

  /**
   * A record of the run, not a screenshot of the thread. The visible bubbles
   * are the smallest part of what determined the reply: the system prompt, the
   * tool schemas and the context accounting are the rest, and none of them are
   * in the DOM. So all three are fetched and handed to the pure builder.
   *
   * Each read is independently optional — a session whose context route is
   * missing still exports its transcript rather than nothing at all.
   */
  async function exportTranscript(session) {
    const id = openTargetId(session);
    if (!id) return;
    const sessionProfile = session?.profile || profile;
    const [detail, messages, context] = await Promise.all([
      api.get(`/api/sessions/${encodeURIComponent(id)}`, { profile: sessionProfile })
        .then((response) => recordFrom(response?.data)).catch(() => null),
      readMessages(id, sessionProfile, 0),
      api.get(
        `/api/upstream/api/sessions/${encodeURIComponent(id)}/context?details=1`,
        { profile: sessionProfile },
      ).then((response) => recordFrom(response?.data)).catch(() => null),
    ]);

    const markdown = buildTranscriptMarkdown({
      session: { ...(session || {}), ...(detail?.session || detail || {}) },
      messages,
      context,
      compressionThreshold: state.compressionThreshold,
    });

    const blob = new Blob([markdown], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const link = el('a', { href: url, download: `${id}.md` });
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  /* ---------------------------------------------------- session mutations -- */

  /** Ask which profile to start on, then create. With nothing else to offer,
   * skip the menu and behave exactly like every other entry point. */
  function startSession(anchor) {
    if (!gatewayAvailable) return;
    const others = (profilesList || []).filter((row) => (row?.name ?? row?.id) !== profile);
    if (!anchor || !others.length) { createSession(); return; }
    openMenu(anchor, buildProfileMenu(profilesList, profile, (picked) => createSession(picked)), {
      placement: 'below', align: 'start',
    });
  }

  async function createSession(runtimeProfile = null) {
    if (!gatewayAvailable) return;
    try {
      // Say which model this session is for.
      //
      // `POST /api/sessions` falls back to the gateway's own internal default
      // when the body omits `model`, and that value is not the one
      // `/api/model/options` advertises as the default — a session created
      // from here was born on a model the dashboard never showed anywhere.
      // Since the session row's model then outranks every per-turn selection,
      // that silent divergence decided the model for the whole conversation.
      //
      // Only a model the picker can actually offer is worth persisting on the
      // row. A global default that no provider catalogue lists is a routing
      // alias or a stale config value: the session row would then pin the
      // conversation to a name that resolves to something else, and every
      // surface would go on disagreeing about which model it runs. In that
      // case say nothing and let the gateway resolve its own default — the
      // first turn reports what it truly ran on, and that is what sticks.
      const created0 = { profile };
      // A picked profile runs on its OWN isolated gateway (runner_manager,
      // BFF-side) — its own SOUL.md, model, credentials, memory and
      // state.db, not text borrowed into a session still running on this
      // tab's default gateway. The BFF resolves model/provider from that
      // profile itself; nothing to inject here beyond the name.
      if (runtimeProfile) {
        created0.profile_name = runtimeProfile;
      } else if (catalogHas(modelOptions, modelOptions?.provider, modelOptions?.model)) {
        created0.model = modelOptions.model;
        created0.provider = modelOptions.provider;
      }
      const response = await api.post('/api/chat/sessions', created0, { profile });
      const created = createdSession(response.data || response);
      if (!created) throw new Error('gateway returned no session id');
      if (runtimeProfile) {
        personaBySession.set(created.id, runtimeProfile);
      }
      // Insert optimistically: the sider reads from the dashboard aggregator,
      // which has not indexed this row yet. Waiting for it is what used to
      // leave a brand-new session showing a bare id and no composer state.
      allSessions = withOptimisticSession(allSessions, {
        ...created, profile, source: created.source || 'api_server',
        last_activity_at: created.started_at || Math.floor(Date.now() / 1000),
      });
      notifyInspector();
      await openThread(created.id);
      if (composerHandle) composerHandle.focus();
    } catch (err) {
      clear(threadPane);
      threadPane.append(unavailableState({ reason: `Create session failed: ${err.message}`, requestId: err.request_id }));
    }
  }

  async function renameSession(session) {
    const id = openTargetId(session);
    const next = window.prompt('Rename session', sessionTitle(session));
    if (next === null) return;
    await api.patch(`/api/sessions/${encodeURIComponent(id)}`, { title: next }, { profile })
      .catch(() => null);
    patchLocalSession(session, { title: next });
    if (selectedSessionId === id) await openThread(id, { navigate: false });
  }

  async function deleteSession(session) {
    const id = openTargetId(session);
    if (!window.confirm(`Delete session ${sessionTitle(session)}?`)) return;
    await api.del(`/api/sessions/${encodeURIComponent(id)}?confirm=1`, { profile }).catch(() => null);
    allSessions = (allSessions || []).filter((row) => idOf(row) !== id && row.tip?.tip_id !== id);
    if (selectedSessionId === id) { selectedSessionId = null; renderHero(); }
    notifyInspector();
  }

  async function forkSession(session) {
    const id = openTargetId(session);
    const response = await api.post(`/api/sessions/${encodeURIComponent(id)}/fork`, {}, { profile })
      .catch(() => null);
    // Fork answers with the same nested `{object, session:{…}}` envelope as
    // create, so it goes through the same unwrapper.
    const fork = createdSession(recordFrom(response?.data));
    if (!fork) return;
    allSessions = withOptimisticSession(allSessions, {
      ...session, ...fork,
      title: fork.title || `${sessionTitle(session)} (fork)`,
      last_activity_at: Math.floor(Date.now() / 1000),
    });
    notifyInspector();
    await openThread(fork.id);
  }

  /**
   * The durable, gateway-side model lock — upstream's own name for it is
   * "backend-ack a Browser model lock" (`POST /api/sessions/{id}/model`,
   * confirmed). This is not the per-request `require_model_lock` flag the
   * composer already sends on every turn after a pick — that only reaches as
   * far as the tab that sent it, dies on reload, and is invisible to a turn
   * driven from Telegram, cron or another tab open on the same session. This
   * endpoint instead writes the pick onto the session row itself
   * (`model_config.browser_model_lock`), and the gateway re-applies it on
   * EVERY future turn for this session, from ANY client, without that turn
   * needing to repeat anything. It is the same durability Hermes' own
   * `/model` TUI command relies on — the gateway in fact ranks a confirmed
   * browser lock even ABOVE a TUI `/model` session override, so this is not a
   * weaker substitute, it is the API-native equivalent.
   */
  async function persistModelLock(session, model, provider, { silent = false } = {}) {
    const id = openTargetId(session);
    const res = await api.post(`/api/sessions/${encodeURIComponent(id)}/model`, {
      model, provider,
    }, { profile }).catch((err) => {
      // Silent only ever suppresses the SUCCESS toast (an autolock is meant to
      // be invisible when it works) — a failure always surfaces. An autolock
      // that silently failed would leave a session drifting with no visible
      // sign anything was ever supposed to prevent it.
      toast(`Could not lock the model: ${err.message || 'request failed'}`, { tone: 'danger' });
      return null;
    });
    if (!res) return false;
    patchLocalSession(session, { model });
    refreshModelChip(id, session);
    if (composerHandle) composerHandle.refreshModel();
    // The button this backs used to do nothing visible on success — clicking
    // it and a pick already changing the pill's own text looked identical to
    // "nothing happened", which is what made the control feel pointless.
    if (!silent) toast(`Locked to ${displayModelName(model)}`, { tone: 'ok' });
    return true;
  }

  /**
   * Pick up whatever confirmed lock the gateway already has for this session.
   * The session-list read never carries `model_config`, so without this a
   * reload leaves the pill unable to tell "the model happens to be X" apart
   * from "X is locked and every future turn enforces it" — exactly the gap a
   * durable lock exists to close.
   */
  async function syncModelLockStatus(id, sessionProfile, session) {
    const res = await api.get(
      `/api/upstream/api/sessions/${encodeURIComponent(id)}`,
      { profile: sessionProfile },
    ).catch(() => null);
    if (!res || selectedSessionId !== id || !composerPrefs.has(id)) return;
    const row = recordFrom(res.data)?.session || recordFrom(res.data) || {};
    observeConfirmedLock(composerPrefs.get(id), row.model_config);
    refreshModelChip(id, session);
    if (composerHandle) composerHandle.refreshModel();
  }

  async function lockModel(session) {
    const id = openTargetId(session);
    const prefs = composerPrefsFor(id, session);
    // Pinning an INHERITED model is the whole point of this control: the
    // session has been following the gateway default and the operator wants
    // that to stop being a default. So it locks the effective model, not only
    // an explicit pick — a test requiring an explicit pick silently did
    // nothing in exactly the case the button exists for.
    const view = effectiveModel(prefs, modelOptions);
    if (!view.model) return;
    pickSessionModel(prefs, view.model, view.provider);
    await persistModelLock(session, view.model, view.provider);
  }

  function togglePin(session) {
    const id = idOf(session);
    if (pinnedSessions.has(id)) pinnedSessions.delete(id);
    else pinnedSessions.add(id);
    applyPins();
    notifyInspector();
  }

  /**
   * Apply a field patch (rename, model lock) to whichever row in `allSessions`
   * represents `targetId` — the row itself if it has no chain tip, or the
   * `.tip` sub-object if it does. A rename/lock always targets the live tip
   * (see `openTargetId`), so patching only the row's own fields would edit
   * data nothing displays: `sessionTitle`/`sessionTimestamp` read `.tip`
   * first once one is resolved.
   */
  function patchRootByTarget(targetId, patch) {
    if (!allSessions || !targetId) return;
    allSessions = allSessions.map((row) => {
      if (idOf(row) === targetId) return { ...row, ...patch };
      if (row.tip?.tip_id === targetId) return { ...row, tip: { ...row.tip, ...patch } };
      return row;
    });
  }

  function patchLocalSession(session, patch) {
    const targetId = openTargetId(session);
    patchRootByTarget(targetId, patch);
    // Mutated in place, not reassigned. The composer and `onTurnSettled` are
    // handed this exact object once, when the thread is opened, and hold onto
    // it for the thread's whole lifetime — a reassignment here would only ever
    // reach `selectedSession`, leaving every earlier holder of the reference
    // (the model-lock decision in particular) reading a value frozen at open
    // time no matter how many turns had patched it since. `allSessions` stays
    // immutable through `patchRootByTarget` above, which is what the sider's
    // re-paint relies on; this object has exactly one purpose — being the
    // live answer to "what does the open thread's session look like now" —
    // and every reader of it wants the mutation, not a snapshot.
    if (selectedSession && openTargetId(selectedSession) === targetId) {
      Object.assign(selectedSession, patch);
    }
    notifyInspector();
  }

  /* ------------------------------------------------------- live mirroring -- */

  function subscribeToSession(sessionId, sessionProfile, list) {
    if (unsubscribeSessionChanged) unsubscribeSessionChanged();
    unsubscribeSessionChanged = null;
    if (!sse) return;
    unsubscribeSessionChanged = sse.on('session.changed', (event) => {
      // Two shapes arrive on this topic and both matter. The BFF's session
      // poller publishes one list-level event with an EMPTY entity_id meaning
      // "sessions moved, somebody's did"; the chat write handlers publish a
      // targeted one carrying the session id. The old filter compared
      // `entity_id` against the open session and returned on anything else,
      // which silently discarded every list-level event — so a turn driven from
      // Telegram or cron never reached the tab at all and the subscription only
      // ever fired for turns this tab had itself just sent.
      const id = event.entity_id || '';
      if (id && id !== selectedSessionId && id !== openTargetId(selectedSession)) return;
      mirrorThread(sessionId, sessionProfile, list).catch(() => null);
      // A list-level event also means the sider's ordering and counts moved.
      refreshSessionList().catch(() => null);
    });
  }

  /**
   * Track which sessions the gateway currently has a turn in flight for.
   *
   * Tab-scoped rather than thread-scoped: this drives an indicator on every row
   * in the sider, including sessions nobody has opened, so it has to outlive
   * whichever thread happens to be on screen.
   */
  /**
   * Hand live turn frames to whichever composer is currently on screen.
   *
   * The BFF folds the watched session's frames into the shared event stream as
   * `chat.frame`; the session filter here is what keeps a frame from a stream
   * that has not finished switching sessions out of the wrong transcript.
   */
  function subscribeToChatFrames() {
    if (unsubscribeChatFrame || !sse) return;
    unsubscribeChatFrame = sse.on('chat.frame', (payload) => {
      if (!payload || payload.session_id !== selectedSessionId) return;
      if (!composerHandle) return;
      composerHandle.feedRemoteFrame({ event: payload.event, data: payload.data });
    });
  }

  function subscribeToRunning() {
    if (unsubscribeRunning || !sse) return;
    unsubscribeRunning = sse.on('session.running', (event) => {
      const rows = event?.payload?.running;
      if (!Array.isArray(rows)) return;
      const next = new Set(rows.map((row) => row && row.session_id).filter(Boolean));
      // Repainting the sider on every tick would fight the reader's scroll for
      // no reason — the set is usually unchanged.
      if (next.size === runningIds.size && [...next].every((id) => runningIds.has(id))) return;
      runningIds = next;
      notifyInspector();
    });
  }

  /* ----------------------------------------------------- inspector column -- */

  function visibleSessions() {
    return allSessions || [];
  }

  function composerPrefsFor(sessionId, session) {
    const key = sessionId || '__draft__';
    if (!composerPrefs.has(key)) {
      // Seeded from the SESSION's own model and from nothing else. Falling back
      // to the gateway's global selection here is what used to write a model
      // the session does not run into the state that then asks for it; the
      // global is still shown, but as an inherited value (`effectiveModel`),
      // not as this session's answer.
      composerPrefs.set(key, createModelPrefs(session, modelOptions));
    } else {
      // A row re-read from the aggregator is fresh evidence, not noise.
      observeSessionModel(composerPrefs.get(key), session, modelOptions);
    }
    return composerPrefs.get(key);
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSider(container, {
      api, profile,
      sessions: allSessions,
      selectedId: selectedSessionId,
      runningIds: runningSet(),
      query: sessionQuery,
      expandedPlatforms,
      gatewayAvailable,
      gatewayUnavailableReason: GATEWAY_UNAVAILABLE_REASON,
      onOpen: (id) => openThread(id),
      onCreate: startSession,
      onSearch: (value) => { sessionQuery = value; renderInspector(container); },
      onRename: renameSession,
      onDelete: deleteSession,
      onFork: forkSession,
      onTogglePin: togglePin,
      // Paging: the sider shows how much of the real total is held and can
      // pull the rest in, instead of silently capping at the first page.
      loadedTotal: loadedCount(pager),
      sessionTotal: pager.total,
      pagingComplete: isComplete(pager),
      loadingMore,
      loadAllRequested,
      onLoadMore: () => loadMoreSessions(),
      onLoadAll: loadAllSessions,
      onStopLoading: stopLoadingSessions,
    });
  }

  /* ------------------------------------------------------------- palette -- */

  function commands() {
    const rows = [
      { group: 'Chat', icon: 'spark', label: 'New session', hint: '⌘⇧O', run: () => createSession() },
      { group: 'Chat', icon: 'search', label: 'Find in conversation', hint: '⌘F', run: () => threadSearch && threadSearch.show() },
      { group: 'Chat', icon: 'stop', label: 'Stop the running turn', hint: 'Esc', run: () => composerHandle && composerHandle.stop() },
      { group: 'Chat', icon: 'refresh', label: 'Reload sessions', run: () => load() },
    ];
    if (selectedSession) {
      rows.push(
        { group: 'Session', icon: 'branch', label: 'Fork this conversation', run: () => forkSession(selectedSession) },
        { group: 'Session', icon: 'pencil', label: 'Rename session', run: () => renameSession(selectedSession) },
        { group: 'Session', icon: 'lock', label: 'Pin the model for this session', run: () => lockModel(selectedSession) },
        { group: 'Session', icon: 'activity', label: 'Toggle run trace', run: () => toggleRunTrace(selectedSession) },
        { group: 'Session', icon: 'download', label: 'Export transcript', run: () => exportTranscript(selectedSession) },
        { group: 'Session', icon: 'trash', label: 'Delete session', run: () => deleteSession(selectedSession) },
      );
    }
    for (const session of visibleSessions().slice(0, 25)) {
      rows.push({
        group: 'Switch to', icon: 'chat', label: sessionTitle(session),
        hint: session.profile || '', run: () => openThread(openTargetId(session)),
      });
    }
    return rows;
  }

  function onShortcut(event) {
    if (!root.isConnected) return;
    const meta = event.metaKey || event.ctrlKey;
    if (meta && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      openCommandPalette(commands());
      return;
    }
    if (meta && event.shiftKey && event.key.toLowerCase() === 'o') {
      event.preventDefault();
      createSession();
      return;
    }
    if (meta && event.key.toLowerCase() === 'f' && threadSearch) {
      event.preventDefault();
      threadSearch.toggle();
    }
  }

  /* ------------------------------------------------------------ lifecycle -- */

  function stopStream() {
    if (composerHandle) composerHandle.destroy();
    composerHandle = null;
  }

  function setCapability(envelope) {
    capabilitiesEnvelope = envelope;
    const health = summarizeSourceHealth(capabilitiesEnvelope?.data);
    const next = health.sources['hermes-gateway']?.healthy === true;
    const changed = next !== gatewayAvailable;
    gatewayAvailable = next;
    if (!root.isConnected) return;
    notifyInspector();
    // The composer is built with its enabled/disabled state baked in, so a
    // gateway that comes back has to rebuild it — otherwise the thread stays
    // read-only until the operator switches sessions.
    if (changed && selectedSessionId) openThread(selectedSessionId, { navigate: false });
  }

  return {
    mount(container) {
      clear(container);
      container.append(root);
    },
    activate(params = {}) {
      document.addEventListener('keydown', onShortcut, true);
      subscribeToRunning();
      subscribeToChatFrames();
      const deepLinked = params.s || params.session || null;
      if (deepLinked) selectedSessionId = deepLinked;
      pendingMessageAnchor = params.m || null;
      return load().catch((err) => {
        clear(threadPane);
        threadPane.append(unavailableState({ reason: `Chat unavailable: ${err.message}`, requestId: err.request_id }));
      });
    },
    deactivate() {
      document.removeEventListener('keydown', onShortcut, true);
      stopStream();
      stopMirror();
      closeMenu();
      if (unsubscribeSessionChanged) unsubscribeSessionChanged();
      unsubscribeSessionChanged = null;
      if (unsubscribeRunning) unsubscribeRunning();
      unsubscribeRunning = null;
      if (unsubscribeChatFrame) unsubscribeChatFrame();
      unsubscribeChatFrame = null;
      // Stop the shared stream carrying a session this tab is no longer showing.
      if (sse) sse.watch(null);
      return { s: selectedSessionId || undefined };
    },
    renderInspector,
    setCapability,
    refresh: load,
    // Select a session from outside the tab (the Fleet/Topology chat popup
    // does this). Deliberately thin: the thread + composer are built by
    // openThread, the one implementation both the tab and the popup use.
    async openSession(id) {
      if (!id) return;
      selectedSessionId = id;
      if (!loaded) await load();
      else await openThread(id);
    },
  };
}

/**
 * Chat as a popup over another tab. Hosts the real `createChat` surface, so a
 * change to the thread, composer, or streaming behaviour reaches the popup for
 * free. The session browser is not mounted here — the caller already picked
 * the session.
 */
export function createChatModal({ api, profile, sse } = {}) {
  let chat = null;
  const modal = createModal({
    title: 'Chat',
    size: 'wide',
    onClose: () => { if (chat) chat.deactivate(); },
  });

  async function openSession(id) {
    modal.show();
    if (!chat) chat = createChat({ api, profile, sse, refreshInspector: () => {} });
    // `close()` clears `modal.body`, which detaches chat's own root from the
    // document (the `chat` instance itself is kept, not recreated, so its
    // session/composer state survives). Re-mounting on every open re-attaches
    // it — without this, the second open left the chat rendering into a
    // detached node: an empty modal with nothing visibly wrong in the code
    // that ran, since it ran against a real, just no-longer-attached, tree.
    chat.mount(modal.body);
    await chat.openSession(id);
  }

  return { open: openSession, close: modal.close, get isOpen() { return modal.isOpen; } };
}

// Re-exported so existing importers (and the Fleet/Kanban popups) keep working
// after the split. The implementations live in the pure modules.
export {
  relativeTime, sessionTimestamp, threadIdentity,
} from '../pure/chat-session.js';
export {
  DEFAULT_REASONING_EFFORT, REASONING_EFFORTS, chatStreamBody, displayModelName,
  modelPillLabel, modelSupportsReasoning, providerRows,
} from '../pure/chat-model.js';
export { toImageAttachment } from './chat/attachments.js';
export { parseToolCalls, toolArgsPreview, toolResultText } from './chat/transcript.js';
export { closeMenu } from '../ui.js';
