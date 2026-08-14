// The composer: input, attachments, model/effort/instructions controls, the
// context gauge, and the turn controller that drives one send from first byte
// to settled transcript.
//
// The turn controller is the important part. It owns the AbortController and
// the explicit run-stop request, folds every frame through the pure reducer,
// and settles the turn from `run.completed` instead of re-reading the thread
// and racing the gateway's own persistence.

import { el, clear, closeMenu, openMenu } from '../../ui.js';
import { icon } from '../../icons.js';
import {
  EFFORT_LABELS, REASONING_EFFORTS, chatStreamBody, displayModelName, estimateTokens,
  modelPillLabel, modelSupportsReasoning, providerRows,
} from '../../pure/chat-model.js';
import { effectiveModel, pickSessionModel } from '../../pure/session-model.js';
import { fetchOpenRouterCatalog, shapeOpenRouterCatalog } from '../../pure/openrouter-catalog.js';
import {
  createTurn, failTurn, isTurnOver, reduceTurn, stopTurn,
} from '../../pure/chat-turn.js';
import { appendMessage, scrollToLatest } from './transcript.js';
import { createTurnView, jumpToLatest } from './turn-view.js';
import { createContextPanel } from './context-panel.js';
import {
  ATTACHMENT_MAX_COUNT, ATTACHMENT_TOTAL_BYTES, filesFromTransfer, toChatAttachment,
} from './attachments.js';
import { openSlashMenu, openToolsetMenu } from './palette.js';

export function createComposer(ctx) {
  const {
    api, profile, disabled, placeholder, list, sessionId, sessionProfile, session,
    prefs, contextState, compressionThreshold, modelOptions, unavailableReason,
    instructionsStore, draftStore, onTurnSettled, onNeedsReload, onModelChanged, onPickModel,
    onRunningChange, onRemoteSettled,
  } = ctx;

  const box = el('div', { class: 'chat-composer' });
  const attachRow = el('div', { class: 'chat-attach-row' });
  const contextPanel = sessionId ? createContextPanel({
    api, sessionId, sessionProfile, compressionThreshold, state: contextState,
  }) : null;

  let attachments = [];
  let attachErrors = [];
  // A message typed while a turn is running is queued, not swallowed. The old
  // composer returned early and did nothing visible, so Enter looked broken.
  const queue = [];

  const input = el('textarea', {
    class: 'chat-input',
    rows: '1',
    placeholder: placeholder || 'Give Hermes a task…',
    'aria-label': 'Message',
  });
  if (disabled) input.setAttribute('disabled', 'disabled');
  if (draftStore && draftStore.get(sessionId)) input.value = draftStore.get(sessionId);

  const sendButton = el('button', {
    class: 'chat-send',
    title: disabled ? unavailableReason : 'Send message',
    'aria-label': 'Send message',
    onclick: () => submit(),
  }, [icon('chevron-right', { size: 16 })]);
  if (disabled) sendButton.setAttribute('disabled', 'disabled');

  const queueRow = el('div', { class: 'chat-queue' });
  queueRow.hidden = true;

  /* ---- turn controller ---- */

  let controller = null;      // AbortController once the stream has opened
  let localActive = false;    // claimed before CSRF/fetch begins
  let stopRequested = false;
  let activeStopRunId = null;
  let activeStopPromise = null;
  const suppressedRunIds = new Set();
  let turn = null;
  let view = null;
  let lastUserText = '';
  let lastAttachments = [];

  const latestPill = jumpToLatest(() => view && view.scrollToBottom());

  function running() {
    // `controller` does not exist until CSRF and the streaming POST have
    // opened. The optimistic user bubble already exists during that window,
    // so callers (especially the history mirror) must treat the earlier
    // `localActive` claim as running too or they can append the persisted copy.
    return Boolean(localActive || controller);
  }

  /* ---- watching a turn this tab did not start ---- */

  // A Hermes session belongs to the conversation, not to the client that
  // happens to have it open. A turn driven from the CLI, Telegram or cron used
  // to be invisible here until it finished: the gateway only persists messages
  // at the END of a turn, so the mirror poll — however fast — had nothing to
  // find, and the whole turn (thinking, tool calls, answer) landed in one lump
  // at the end. The gateway now broadcasts a session's frames to anyone
  // watching, and this paints that broadcast with the same reducer, the same
  // view and the same pacing a locally streamed turn gets. The only difference
  // is that no Stop button is offered: this client did not start the turn, and
  // letting go of a watcher interrupts nothing upstream.
  //
  // Frames arrive from the tab, over the SPA's one existing SSE connection —
  // deliberately not a connection of this module's own. A browser allows six
  // HTTP/1.1 connections per host, and a permanent extra stream per open thread
  // starved every ordinary request behind it until the app looked frozen.
  let remoteTurn = null;
  let remoteView = null;

  // Frames that carry no turn state: they say something about the CONNECTION,
  // which the reducer has no opinion about.
  const CONNECTION_EVENTS = new Set(['session.attached', 'bff.open']);
  const TERMINAL_EVENTS = new Set(['run.completed', 'done', 'complete', 'error']);
  // Only these justify opening a view for a turn already in progress. Anything
  // unrecognised is left alone: a view opened by a frame that carries no turn
  // content has nothing to show and no way to end, which is exactly how a
  // stray activity bar ends up counting upward forever.
  const TURN_EVENTS = new Set([
    'run.started', 'message.started', 'assistant.delta', 'message.delta',
    'assistant.reasoning', 'reasoning.delta', 'tool.progress', 'tool.started',
    'tool.completed', 'tool.failed', 'tool.error', 'assistant.completed',
    'message.completed',
  ]);

  const remote = {
    get turn() { return remoteTurn; },
    discard: discardRemote,
  };

  function discardRemote() {
    if (remoteView) { remoteView.destroy(); remoteView = null; }
    remoteTurn = null;
  }

  function beginRemote() {
    if (remoteView) remoteView.destroy();
    remoteView = createTurnView({
      list,
      onStop: null,
      getContextTotal: () => (contextPanel ? contextPanel.contextTotal() : 0),
    });
    remoteView.onPinChange((pinned) => { latestPill.hidden = pinned; });
    activityHost.replaceChildren(remoteView.node);
    remoteTurn = createTurn();
    remoteView.begin(remoteTurn);
  }

  function settleRemote() {
    const settled = remoteTurn;
    remoteTurn = null;
    if (contextPanel) contextPanel.refresh().catch(() => null);
    // The caller re-anchors the mirror on the server's copy, exactly as it does
    // after a local turn: this transcript was drawn from frames, not from
    // persisted rows, so without that the next poll would append the whole turn
    // a second time.
    if (typeof onRemoteSettled === 'function') onRemoteSettled(settled);
  }

  function onRemoteFrame(event) {
    // The local turn owns the transcript while it runs — and its frames come
    // back over this broadcast too, so without this guard every locally sent
    // message would paint twice.
    if (localActive) return;
    const name = event && event.event;
    const data = event && event.data;
    const eventRunId = data && data.run_id ? String(data.run_id) : null;

    // Aborting the local fetch is immediate UI feedback, but the explicit
    // gateway stop takes a moment to land. Do not re-attach this same composer
    // to the broadcast copy of the run it has just stopped. Its terminal frame
    // releases the suppression; a failed stop request releases it explicitly
    // so continued work remains visible rather than burning tokens in secret.
    if (eventRunId && suppressedRunIds.has(eventRunId)) {
      if (TERMINAL_EVENTS.has(name)) suppressedRunIds.delete(eventRunId);
      return;
    }

    if (CONNECTION_EVENTS.has(name)) {
      // Attaching to a session with nothing in flight clears whatever the last
      // turn left on screen, rather than leaving a dead activity bar up.
      if (name === 'session.attached' && data && data.running === false) discardRemote();
      return;
    }

    if (name === 'run.started') {
      // The prompt is persisted upstream, but the mirror poll stands down while
      // this stream is attached, so it has to be drawn from the frame that
      // announced it — otherwise an answer appears with nothing above it saying
      // what was asked.
      const asked = data && data.user_message;
      const askedText = asked && (typeof asked === 'string' ? asked : asked.content);
      if (askedText) {
        appendMessage(list, 'user', String(askedText), {});
        scrollToLatest(list);
      }
      beginRemote();
    } else if (!remoteTurn) {
      // Attached mid-turn and `run.started` had already aged out of the
      // gateway's replay buffer. Paint what is left rather than nothing — but
      // only for a frame that actually carries turn content, never for one
      // whose job is to close a turn or to say something about the connection.
      if (!TURN_EVENTS.has(name)) return;
      beginRemote();
    }

    remoteTurn = reduceTurn(remoteTurn, event);
    remoteView.apply(remoteTurn);
    if (isTurnOver(remoteTurn)) settleRemote();
  }


  async function runTurn(text, outgoing) {
    lastUserText = text;
    lastAttachments = outgoing;
    stopRequested = false;
    activeStopRunId = null;
    activeStopPromise = null;
    // Claimed before the POST, not after it resolves, so `running()` and the
    // broadcast guard both cover the CSRF/fetch handshake as well as the open
    // stream.
    localActive = true;
    if (remote.turn) remote.discard();

    appendMessage(list, 'user', text, {
      attachments: outgoing,
      onRegenerate: () => regenerate(),
      onFork: ctx.onFork ? () => ctx.onFork() : undefined,
    });
    // Sending is a deliberate act, so it always brings the reader back to the
    // end — including when they had scrolled up to re-read something. Leaving
    // the view where it was put the operator's own message below the fold,
    // which reads as the message never having been sent at all. Everything
    // after this point defers to where the reader chooses to be.
    scrollToLatest(list);

    if (view) view.destroy();
    view = createTurnView({
      list,
      onStop: () => stop(),
      getContextTotal: () => (contextPanel ? contextPanel.contextTotal() : 0),
    });
    view.onPinChange((pinned) => { latestPill.hidden = pinned; });
    activityHost.replaceChildren(view.node);
    // Mounting the activity line shortens the transcript by its own height, so
    // the bottom the previous line reached is no longer the bottom.
    scrollToLatest(list);

    turn = createTurn();
    view.begin(turn);
    setRunningUi(true);

    try {
      const handle = await api.streamPost('/api/chat/stream', chatStreamBody({
        sessionId,
        sessionProfile: sessionProfile || profile,
        text,
        attachments: outgoing,
        prefs,
        instructions: instructionsStore ? instructionsStore.get(sessionId) : '',
      }), {
        profile: sessionProfile || profile,
        onEvent: (event) => {
          if (stopRequested) {
            // Do not paint buffered deltas after the click. We only still care
            // about a run id that arrived late enough to make the explicit
            // stop addressable.
            const runId = turn.runId || event?.data?.run_id;
            if (runId) {
              if (!turn.runId) turn = { ...turn, runId: String(runId) };
              requestRunStop(runId).catch(() => null);
            }
            if (controller) controller.abort();
            return;
          }
          turn = reduceTurn(turn, event);
          view.apply(turn);
        },
      });
      controller = handle.controller;
      // Stop may have been clicked while CSRF/fetch was still opening and no
      // AbortController was available to the composer yet.
      if (stopRequested) {
        if (turn.runId) requestRunStop(turn.runId).catch(() => null);
        controller.abort();
      }
      await handle.done;
      // A stream that closed without `run.completed` still has to settle —
      // otherwise the composer stays disabled with no explanation.
      if (!isTurnOver(turn)) {
        // Fetch abort can resolve the reader cleanly instead of throwing an
        // AbortError when it lands between chunks. The operator's explicit
        // intent still wins: this is stopped, not a successful `done` turn.
        turn = stopRequested
          ? stopTurn(turn)
          : reduceTurn(turn, { event: 'done', data: {} });
        view.apply(turn);
      }
    } catch (err) {
      if (err?.name === 'AbortError') {
        turn = stopTurn(turn);
      } else {
        turn = failTurn(turn, err?.message || 'send failed');
        turn.onRetry = () => retry();
      }
      view.finish(turn);
    } finally {
      // A very early stop can close the stream before `run.started` reaches
      // this tab. Resolve the run from the gateway's registry and stop it
      // explicitly; disconnect alone is only a fallback and is not reliable
      // enough to promise that the session stopped.
      if (stopRequested) {
        const runId = turn?.runId || await findRunningRunId();
        if (runId) await requestRunStop(runId);
      }
      controller = null;
      localActive = false;
      setRunningUi(false);
      settle();
    }
  }

  /**
   * Settle the turn from what the stream already delivered.
   *
   * `run.completed` carries the whole turn's transcript, so the thread is
   * authoritative the moment it lands. Only a turn that produced nothing —
   * a transport failure, or a stop before the first token — falls back to a
   * re-read, and even then the caller retries rather than blanking the live
   * transcript on an empty response.
   */
  function settle() {
    const settled = turn && (turn.messages || turn.text || turn.tools.length);
    if (contextPanel) contextPanel.refresh().catch(() => null);
    if (typeof onTurnSettled === 'function') onTurnSettled(turn);
    if (!settled && typeof onNeedsReload === 'function') onNeedsReload();
    drainQueue();
  }

  async function findRunningRunId() {
    for (const delay of [0, 200, 600]) {
      if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
      const response = await api.get('/api/chat/running', {
        profile: sessionProfile || profile,
      }).catch(() => null);
      const rows = response?.data?.running;
      if (!Array.isArray(rows)) continue;
      const match = rows.find((row) => String(row?.session_id || '') === String(sessionId));
      if (match?.run_id) return String(match.run_id);
    }
    return null;
  }

  function requestRunStop(runId) {
    const id = String(runId || '');
    if (!id) return Promise.resolve(false);
    if (activeStopPromise && activeStopRunId === id) return activeStopPromise;

    activeStopRunId = id;
    suppressedRunIds.add(id);
    activeStopPromise = api.post(
      `/api/runs/${encodeURIComponent(id)}/stop`, {},
      { profile: sessionProfile || profile },
    ).then(() => true).catch((err) => {
      suppressedRunIds.delete(id);
      appendMessage(
        list, 'error',
        `Could not stop the upstream run: ${err?.message || 'request failed'}`,
      );
      scrollToLatest(list);
      return false;
    });
    return activeStopPromise;
  }

  function stop() {
    if (!running() || stopRequested) return;
    stopRequested = true;
    // Stop the local painter synchronously; explicit gateway cancellation can
    // finish in the background without another buffered delta appearing.
    turn = stopTurn(turn);
    if (view) view.apply(turn);
    if (turn?.runId) requestRunStop(turn.runId).catch(() => null);
    // Keep the explicit stop request independent from this AbortController:
    // the latter closes only the SSE response and gives immediate UI feedback.
    if (controller) controller.abort();
  }

  function retry() {
    if (running() || !lastUserText) return;
    runTurn(lastUserText, lastAttachments).catch(() => null);
  }

  function regenerate() {
    if (running() || !lastUserText) return;
    // Re-run the same prompt with whatever the pills say now — the point is to
    // compare models, so the current selection is deliberately used.
    runTurn(lastUserText, lastAttachments).catch(() => null);
  }

  function drainQueue() {
    if (running() || !queue.length) return;
    const next = queue.shift();
    renderQueue();
    runTurn(next.text, next.attachments).catch(() => null);
  }

  function renderQueue() {
    clear(queueRow);
    queueRow.hidden = !queue.length;
    for (const item of queue) {
      queueRow.append(el('span', { class: 'chat-queue-chip', title: item.text }, [
        el('span', { class: 'chat-queue-text', text: item.text.slice(0, 60) }),
        el('button', {
          class: 'chat-queue-drop', type: 'button', title: 'Remove from queue',
          onclick: () => {
            const index = queue.indexOf(item);
            if (index !== -1) queue.splice(index, 1);
            renderQueue();
          },
        }, [icon('close', { size: 10 })]),
      ]));
    }
  }

  function setRunningUi(isRunning) {
    box.classList.toggle('is-running', isRunning);
    sendButton.classList.toggle('is-running', isRunning);
    sendButton.title = isRunning ? 'A turn is running — new messages queue' : 'Send message';
    // The sider's running indicator has no other source for this: the session
    // list Hermes returns carries no "a turn is in flight" field for any
    // session, open or not — only the tab that is actually streaming one
    // knows. Firing on both transitions is what lets the sider clear the
    // indicator the instant this turn settles, not on its next unrelated
    // repaint.
    if (typeof onRunningChange === 'function') onRunningChange(isRunning);
  }

  /* ---- submit ---- */

  function submit() {
    const text = input.value.trim();
    if ((!text && !attachments.length) || disabled || !list) return;
    const outgoing = attachments;
    input.value = '';
    attachments = [];
    attachErrors = [];
    if (draftStore) draftStore.set(sessionId, '');
    renderAttachments();
    autoGrow();

    if (running()) {
      queue.push({ text, attachments: outgoing });
      renderQueue();
      return;
    }
    runTurn(text, outgoing).catch(() => null);
  }

  /* ---- attachments (+) ---- */

  const fileInput = el('input', {
    type: 'file',
    accept: '*/*',
    multiple: 'multiple',
    class: 'chat-file-input',
    'aria-hidden': 'true',
    tabindex: '-1',
  });

  async function acceptFiles(files) {
    attachErrors = [];
    for (const file of files) {
      if (attachments.length >= ATTACHMENT_MAX_COUNT) {
        attachErrors.push(`Only ${ATTACHMENT_MAX_COUNT} attachments are allowed per message`);
        break;
      }
      try {
        const prepared = await toChatAttachment(file);
        const nextTotal = attachments.reduce((total, item) => total + (item.size || 0), 0)
          + prepared.size;
        if (nextTotal > ATTACHMENT_TOTAL_BYTES) {
          throw new Error('combined attachments exceed the 700 KB chat limit');
        }
        attachments.push(prepared);
      } catch (err) {
        attachErrors.push(`${file.name || 'file'}: ${err.message}`);
      }
    }
    renderAttachments();
  }

  fileInput.addEventListener('change', async () => {
    const files = [...(fileInput.files || [])];
    fileInput.value = '';
    await acceptFiles(files);
  });

  const attachButton = el('button', {
    class: 'chat-tool-btn chat-attach-btn',
    title: 'Attach file',
    'aria-label': 'Attach file',
    onclick: () => fileInput.click(),
  }, [icon('plus', { size: 15 })]);
  if (disabled) attachButton.setAttribute('disabled', 'disabled');

  // Paste and drag-drop, the two ways an operator actually attaches files.
  input.addEventListener('paste', (event) => {
    const files = filesFromTransfer(event.clipboardData);
    if (!files.length) return;
    event.preventDefault();
    acceptFiles(files);
  });
  box.addEventListener('dragover', (event) => {
    if (!event.dataTransfer) return;
    event.preventDefault();
    box.classList.add('is-dropping');
  });
  box.addEventListener('dragleave', () => box.classList.remove('is-dropping'));
  box.addEventListener('drop', (event) => {
    box.classList.remove('is-dropping');
    const files = filesFromTransfer(event.dataTransfer);
    if (!files.length) return;
    event.preventDefault();
    acceptFiles(files);
  });

  function renderAttachments() {
    clear(attachRow);
    for (const item of attachments) {
      const chip = el('span', { class: 'chat-attach-chip', title: item.name });
      if (item?.kind === 'image' && item.url) {
        chip.append(el('img', { class: 'chat-attach-thumb', src: item.url, alt: '' }));
      } else {
        chip.append(el('span', { class: 'chat-attach-doc', text: 'file' }));
      }
      chip.append(el('span', { class: 'chat-attach-name', text: item.name }));
      if (item?.mime) {
        chip.append(el('span', { class: 'chat-attach-meta', text: item.mime }));
      }
      chip.append(el('button', {
        class: 'chat-attach-remove',
        title: 'Remove attachment',
        'aria-label': `Remove ${item.name}`,
        onclick: () => {
          attachments = attachments.filter((other) => other !== item);
          renderAttachments();
        },
      }, [icon('close', { size: 11 })]));
      attachRow.append(chip);
    }
    for (const message of attachErrors) {
      attachRow.append(el('div', { class: 'chat-attach-error', text: message }));
    }
  }

  /* ---- model + reasoning + instructions ---- */

  const modelButton = el('button', {
    class: 'chat-tool-pill', 'aria-haspopup': 'true', title: 'Switch model',
  });
  const effortButton = el('button', {
    class: 'chat-tool-pill chat-effort-pill', 'aria-haspopup': 'true', title: 'Reasoning effort',
  });
  const instructionsButton = el('button', {
    class: 'chat-tool-btn', 'aria-haspopup': 'true',
    title: 'Per-session instructions', 'aria-label': 'Per-session instructions',
  }, [icon('doc', { size: 14 })]);
  const toolsetButton = el('button', {
    class: 'chat-tool-btn', 'aria-haspopup': 'true',
    title: 'Tools this agent can call', 'aria-label': 'Tools this agent can call',
  }, [icon('tools', { size: 14 })]);
  if (disabled) {
    modelButton.setAttribute('disabled', 'disabled');
    effortButton.setAttribute('disabled', 'disabled');
    instructionsButton.setAttribute('disabled', 'disabled');
  }

  function paintPills() {
    // One resolver decides what this session runs; the pill only renders it.
    const view = effectiveModel(prefs, modelOptions);
    clear(modelButton);
    // A locked pick and an observed run can show the identical model name —
    // "Local" reads the same whether the operator chose it or a turn merely
    // happened to land there — so the lock glyph is the only thing telling
    // them apart. Without it, an observed value looks exactly as confirmed as
    // a real pick, and the next send can drift straight back off it, because
    // only `pickSessionModel` sets `require_model_lock`; a name that merely
    // matches what last ran does not.
    if (view.source === 'pick') modelButton.append(icon('lock', { size: 10, className: 'chat-tool-pill-lock' }));
    modelButton.append(
      el('span', { class: 'chat-tool-pill-text', text: modelPillLabel(view.model) }),
      icon('chevron-down', { size: 11, className: 'chat-tool-pill-caret' }),
    );
    // An inherited or aliased value is shown but not claimed as a selection,
    // so the pill never implies a pick the picker cannot reproduce.
    modelButton.classList.toggle('is-muted', view.inherited);
    modelButton.title = [
      view.model || 'No model',
      view.note,
      view.source === 'pick' ? 'Locked — every turn runs on exactly this model' : 'Not locked — a future turn can land on a different model',
      view.alias ? 'Routing alias — the gateway resolves it to a real model' : '',
    ].filter(Boolean).join(' · ');
    const supported = modelSupportsReasoning(modelOptions, view.provider, view.model);
    clear(effortButton);
    effortButton.append(
      icon('spark', { size: 12 }),
      el('span', { class: 'chat-tool-pill-text', text: EFFORT_LABELS[prefs.effort] || 'Auto' }),
    );
    effortButton.classList.toggle('is-muted', !supported);
    effortButton.title = supported
      ? 'Reasoning effort'
      : `${displayModelName(view.model)} does not report reasoning support`;

    const saved = instructionsStore ? instructionsStore.get(sessionId) : '';
    instructionsButton.classList.toggle('is-active', Boolean(saved));
    instructionsButton.title = saved
      ? 'Per-session instructions (set)' : 'Per-session instructions';

    // Every path that can change what this pill shows — a pick, and the
    // run-observation `refreshModel()` replays — funnels through here, so
    // this is the one place that needs to tell the host tab something else
    // might need repainting too (the header chip).
    if (typeof onModelChanged === 'function') onModelChanged();
  }

  modelButton.addEventListener('click', () => {
    openMenu(modelButton, buildModelMenu(modelOptions, prefs, paintPills, onPickModel));
  });
  effortButton.addEventListener('click', () => {
    openMenu(effortButton, buildEffortMenu(prefs, paintPills));
  });
  instructionsButton.addEventListener('click', () => {
    openMenu(instructionsButton, buildInstructionsPanel({
      value: instructionsStore ? instructionsStore.get(sessionId) : '',
      onSave: (value) => {
        if (instructionsStore) instructionsStore.set(sessionId, value);
        paintPills();
      },
    }));
  });
  toolsetButton.addEventListener('click', () => {
    openToolsetMenu(toolsetButton, { api, profile: sessionProfile || profile });
  });
  paintPills();

  /* ---- input behaviour ---- */

  function autoGrow() {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
  }

  input.addEventListener('input', () => {
    autoGrow();
    if (draftStore) draftStore.set(sessionId, input.value);
    if (contextPanel) contextPanel.setDraftTokens(estimateTokens(input.value));
    // `/` on an otherwise empty composer opens the skill catalogue, the
    // interaction every chat client has trained operators to expect.
    if (input.value === '/') {
      openSlashMenu(input, {
        api,
        profile: sessionProfile || profile,
        onPick: (skill) => {
          input.value = `/${skill.name} `;
          input.focus();
          autoGrow();
        },
      });
    }
  });

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit();
      return;
    }
    if (event.key === 'Escape' && running()) {
      event.preventDefault();
      stop();
    }
  });

  /* ---- assembly ---- */

  const activityHost = el('div', { class: 'chat-activity-host' });

  if (contextPanel) box.append(contextPanel.node);
  box.append(latestPill);
  box.append(queueRow);
  box.append(attachRow);
  box.append(el('div', { class: 'chat-composer-box' }, [input, sendButton]));
  box.append(el('div', { class: 'chat-composer-bar' }, [
    el('div', { class: 'chat-composer-bar-left' }, [attachButton, fileInput, toolsetButton]),
    el('div', { class: 'chat-composer-bar-right' }, [
      instructionsButton,
      modelButton,
      effortButton,
      contextPanel ? contextPanel.trigger : null,
    ]),
  ]));
  // The run status and its Stop control sit at the right edge, directly under
  // the model / reasoning / context controls, so every control that acts on the
  // run lives in one column; the keyboard hint keeps the left.
  box.append(el('div', { class: 'chat-composer-footer' }, [
    el('div', {
      class: 'chat-composer-hint',
      text: disabled
        ? 'Read-only — gateway unavailable'
        : 'Enter to send · Shift+Enter for newline · / for skills · Esc to stop',
    }),
    activityHost,
  ]));
  renderAttachments();
  if (contextPanel) contextPanel.refresh().catch(() => null);
  if (!disabled) setTimeout(() => input.focus(), 0);
  return {
    node: box,
    focus: () => input.focus(),
    stop,
    isRunning: running,
    /** True while a turn started elsewhere is being painted here. */
    isWatching: () => Boolean(remoteTurn),
    /** Feed one frame of a turn started elsewhere. Called by the tab's SSE client. */
    feedRemoteFrame: onRemoteFrame,
    /** Repaint after the tab learned something new about the session's model. */
    refreshModel: () => paintPills(),
    refreshContext: () => (contextPanel ? contextPanel.refresh().catch(() => null) : null),
    /** Pre-fill the composer, e.g. when the palette inserts a command. */
    setText(text) {
      input.value = String(text || '');
      autoGrow();
      input.focus();
    },
    destroy() {
      if (view) view.destroy();
      if (controller) controller.abort();
      discardRemote();
    },
  };
}

/* ------------------------------------------------------------------ menus -- */

// Module-scoped, not per-menu-open: OpenRouter's public catalog does not
// change within a browsing session, so reopening the picker should not repeat
// an outbound request to a third party for the same 400-model listing.
let openRouterExtraCache = null;
let openRouterExtraPromise = null;

function loadOpenRouterExtras(curatedIds, providerRow) {
  if (openRouterExtraCache) return Promise.resolve(openRouterExtraCache);
  if (!openRouterExtraPromise) {
    openRouterExtraPromise = fetchOpenRouterCatalog()
      .then((raw) => {
        openRouterExtraCache = shapeOpenRouterCatalog(raw, { exclude: curatedIds });
        // Folded into `modelOptions` itself, not tracked as a side list: once
        // picked, one of these is indistinguishable from a curated model to
        // everything downstream — `catalogHas`/`effectiveModel`'s "routing
        // alias" flag would otherwise call a perfectly real, resolvable
        // OpenRouter model an alias, purely because it does not know this
        // cache exists.
        if (providerRow && Array.isArray(providerRow.models)) {
          const known = new Set(providerRow.models);
          for (const item of openRouterExtraCache) {
            if (!known.has(item.id)) providerRow.models.push(item.id);
          }
        }
        return openRouterExtraCache;
      })
      .catch((err) => {
        openRouterExtraPromise = null; // let a retry actually retry
        throw err;
      });
  }
  return openRouterExtraPromise;
}

function buildModelMenu(modelOptions, prefs, onPick, onPickModel) {
  const menu = el('div', { class: 'chat-menu' });
  const rows = providerRows(modelOptions);
  if (!rows.length) {
    menu.append(el('div', { class: 'chat-menu-empty', text: 'Model inventory unavailable' }));
    return menu;
  }

  const search = el('input', {
    class: 'input chat-menu-search',
    type: 'search',
    placeholder: 'Search models…',
    'aria-label': 'Search models',
  });
  const body = el('div', { class: 'chat-menu-body' });
  menu.append(search, body);

  // Hermes curates OpenRouter to ~34 hand-picked agentic models (Hermes
  // Desktop's own picker shows the same set — this is not a shortfall to
  // fetch around). Widening it is opt-in and additive: OpenRouter's public,
  // unauthenticated catalog listing is fetched straight from the browser
  // (never through the gateway — this only changes what the PICKER offers,
  // not how a turn runs) and filtered to the same tool-calling requirement
  // Hermes itself enforces. Cached at module scope so reopening the picker
  // does not repeat the request.
  function pickModelItem(model, providerRow) {
    // A deliberate pick, recorded through the one owner of that state: it is
    // sticky, so nothing observed later quietly replaces it, and only a
    // deliberate pick is worth overriding the session's standing model with
    // — see the `require_model_lock` note in chatStreamBody.
    pickSessionModel(prefs, model, providerRow.slug);
    closeMenu();
    onPick();
    // Picking here has to mean what `/model` means in the TUI: this session
    // now runs on this model, not "this tab, until reload". Fired after the
    // optimistic repaint above, not instead of it — the pill reflects the
    // pick immediately either way, this just makes it durable on the gateway.
    if (typeof onPickModel === 'function') onPickModel(model, providerRow.slug);
  }

  function modelItem(model, label, providerRow) {
    const active = model === prefs.model && providerRow.slug === prefs.provider;
    const item = el('button', {
      class: `chat-menu-item${active ? ' active' : ''}`,
      onclick: () => pickModelItem(model, providerRow),
    }, [
      el('span', { class: 'chat-menu-item-label', text: label }),
      active ? icon('check', { size: 12 }) : null,
    ]);
    item.title = model;
    return item;
  }

  let openRouterRevealed = false;

  function paint(query) {
    clear(body);
    const needle = query.trim().toLowerCase();
    let shown = 0;
    for (const providerRow of rows) {
      // Aggregators list dozens of models; mirror the desktop picker and
      // show the curated shortlist until the user searches.
      const featured = Array.isArray(providerRow.featured_models) ? providerRow.featured_models : [];
      const all = Array.isArray(providerRow.models) ? providerRow.models : [];
      const pool = needle ? all : (featured.length ? featured : all.slice(0, 8));
      const models = pool.filter((model) => !needle || String(model).toLowerCase().includes(needle));

      const isOpenRouter = providerRow.slug === 'openrouter';
      // `models` (already searched/matched above) is filtered against once
      // loaded — `loadOpenRouterExtras` folds the fetched ids straight into
      // `providerRow.models`, so after the first successful load they are
      // already inside `all`/`models` on every later paint. Re-adding them
      // here too would just print the same model twice.
      const shownIds = new Set(models);
      const extra = isOpenRouter && openRouterExtraCache
        ? openRouterExtraCache.filter((item) => !shownIds.has(item.id)
          && (!needle || item.id.toLowerCase().includes(needle) || item.name.toLowerCase().includes(needle)))
        : [];
      const showExpand = isOpenRouter && !needle && !openRouterRevealed;
      if (!models.length && !extra.length && !showExpand) continue;

      const label = providerRow.name || providerRow.slug;
      body.append(el('div', { class: 'chat-menu-group' }, [
        el('span', { text: label }),
        providerRow.authenticated === false
          ? el('span', { class: 'chat-menu-group-note', text: 'not configured' })
          : null,
      ]));
      for (const model of models) {
        body.append(modelItem(model, displayModelName(model), providerRow));
        shown += 1;
      }
      // Extras render whenever the search reaches them, or once revealed —
      // never mixed silently into the default 8-item shortlist above.
      if (needle || openRouterRevealed) {
        for (const item of extra) {
          body.append(modelItem(item.id, item.name, providerRow));
          shown += 1;
        }
      }
      if (showExpand) {
        body.append(openRouterExpandButton(providerRow, all));
        shown += 1; // the expand control itself counts as "something to show"
      }
    }
    if (!shown) body.append(el('div', { class: 'chat-menu-empty', text: 'No matching models' }));
  }

  function openRouterExpandButton(providerRow, curatedIds) {
    const btn = el('button', {
      class: 'chat-menu-item chat-menu-item-expand',
      type: 'button',
      onclick: () => {
        btn.disabled = true;
        btn.textContent = 'Loading…';
        loadOpenRouterExtras(curatedIds, providerRow)
          .then(() => { openRouterRevealed = true; paint(search.value); })
          .catch((err) => {
            btn.disabled = false;
            btn.textContent = `Couldn't load more — ${err.message || 'retry'}`;
          });
      },
    }, ['Show more (OpenRouter · newest, live)']);
    return btn;
  }

  search.addEventListener('input', () => paint(search.value));
  paint('');
  setTimeout(() => search.focus(), 0);
  return menu;
}

function buildEffortMenu(prefs, onPick) {
  const menu = el('div', { class: 'chat-menu chat-menu-narrow' });
  const body = el('div', { class: 'chat-menu-body' });
  const choices = ['', ...REASONING_EFFORTS];
  for (const effort of choices) {
    const active = effort === prefs.effort;
    body.append(el('button', {
      class: `chat-menu-item${active ? ' active' : ''}`,
      onclick: () => {
        prefs.effort = effort;
        closeMenu();
        onPick();
      },
    }, [
      el('span', { class: 'chat-menu-item-label', text: EFFORT_LABELS[effort] || effort }),
      active ? icon('check', { size: 12 }) : null,
    ]));
  }
  menu.append(body);
  return menu;
}

/**
 * Per-session instructions. The gateway accepts `instructions` on every
 * chat/stream call and the BFF already forwards the key — the SPA simply never
 * sent it, so there was no way to tell one thread to behave differently.
 */
function buildInstructionsPanel({ value, onSave }) {
  const menu = el('div', { class: 'chat-menu chat-menu-wide' });
  menu.append(el('div', { class: 'chat-menu-group' }, [el('span', { text: 'Instructions for this session' })]));
  const area = el('textarea', {
    class: 'input chat-instructions',
    rows: '6',
    placeholder: 'Extra instructions sent with every turn in this session…',
    'aria-label': 'Session instructions',
  });
  area.value = value || '';
  const foot = el('div', { class: 'chat-menu-foot' });
  foot.append(el('button', {
    class: 'btn btn-sm', type: 'button',
    onclick: () => { onSave(area.value); closeMenu(); },
  }, ['Save']));
  foot.append(el('button', {
    class: 'btn btn-sm', type: 'button',
    onclick: () => { area.value = ''; onSave(''); closeMenu(); },
  }, ['Clear']));
  menu.append(area, foot);
  setTimeout(() => area.focus(), 0);
  return menu;
}
