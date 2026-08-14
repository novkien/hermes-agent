// Binds the pure turn reducer to the DOM.
//
// Nothing here decides *what* a turn is doing — `pure/chat-turn.js` does that.
// This module only paints it: the activity line above the composer, the
// reasoning disclosure, the live tool rows and the reply bubble. Keeping the
// two apart is what makes the turn's behaviour testable in Node while the
// pixels stay in the tab.

import { el, clear } from '../../ui.js';
import { icon } from '../../icons.js';
import { renderMarkdown } from '../../markdown-render.js';
import {
  activeTool, activityLabel, isTurnOver, turnAnswer, turnElapsed, turnTokens,
} from '../../pure/chat-turn.js';
import {
  appendMessage, appendToolCall, formatDuration, insertNode, reasoningDisclosure,
  scrollToLatest, setMessageCopyable,
} from './transcript.js';
import { contextHeatColor, formatSecondsOnly } from '../../pure/context-window.js';
import { createDeltaPacer } from '../../pure/delta-pacer.js';

// Hermes Desktop waits ~200ms before naming what it is waiting on, so a fast
// tool never makes the label strobe. Same idea, same reason.
const LABEL_REVEAL_MS = 200;
// Re-parsing Markdown on every token would fight half-written syntax, so
// deltas are still coalesced rather than painted one at a time. But 120ms was
// set from an assumption never measured against this renderer: profiled live
// against `renderMarkdown` (DOM attached, layout forced to flush the real
// cost), ordinary prose up to the 4000-char truncate limit costs under 1ms and
// even a 10KB syntax-highlighted code block costs ~23ms — both comfortably
// inside a much smaller budget. At 120ms, a reply that streams over several
// seconds paints in visible globs with a dead pause between them, which is
// exactly the "chữ chạy không đều" a reader sees; a two-frame budget still
// coalesces a burst without that stutter.
const MARKDOWN_THROTTLE_MS = 32;
// Ceiling for the adaptive throttle below. Past this the reveal stops looking
// continuous however expensive the block is, so a very costly block gives up
// smoothness rather than the frame rate of the whole tab.
const MARKDOWN_THROTTLE_MAX_MS = 200;

/**
 * createTurnView({ list, onStop }) → the live view for one thread.
 *
 * `list` is the transcript container. `onStop` is invoked by the stop button;
 * the caller aborts the fetch, which is what actually interrupts the agent.
 */
export function createTurnView({ list, onStop = null, getContextTotal = null }) {
  const bar = el('div', { class: 'chat-activity', role: 'status', 'aria-live': 'polite' });
  bar.hidden = true;

  const pulse = el('span', { class: 'chat-activity-pulse', 'aria-hidden': 'true' });
  const label = el('span', { class: 'chat-activity-label' });
  const detail = el('span', { class: 'chat-activity-detail' });
  const timer = el('span', { class: 'chat-activity-timer mono' });
  const stopButton = el('button', {
    class: 'chat-activity-stop', type: 'button',
    title: 'Stop this run', 'aria-label': 'Stop this run',
  }, [icon('stop', { size: 11 }), el('span', { text: 'Stop' })]);
  stopButton.addEventListener('click', () => { if (onStop) onStop(); });

  bar.append(pulse, label, detail, timer, stopButton);

  let current = null;          // the turn state last applied
  let lastBlocks = null;       // the block array last painted, by identity
  let nodes = new Map();       // block id → {kind, node, body?}
  let tick = null;
  let frame = null;            // pending animation frame for the reveal pump
  let pendingText = new Set(); // block ids whose Markdown is due a repaint
  // Arrival is bursty no matter how healthy the connection is; reveal should
  // not be. See pure/delta-pacer.js — this is what keeps a turn relayed from
  // another process reading as smoothly as one this tab streamed itself.
  const pacer = createDeltaPacer();
  // The gateway always emits `done` in its finally, so a healthy turn settles
  // twice: once on `run.completed`, once on `done`. Without this guard the
  // second pass appended a second usage footer under the same reply.
  let finished = false;

  /* ------------------------------------------------------------- blocks -- */

  // The turn owns a growing suffix of `list`, and a block can be spliced in
  // ahead of one already on screen, so a new node goes in front of the first
  // later block that has been drawn — never blindly at the end.
  function anchorFor(blocks, index) {
    for (let i = index + 1; i < blocks.length; i += 1) {
      const entry = nodes.get(blocks[i].id);
      if (entry) return entry.node;
    }
    return null;
  }

  function createNode(block, blocks, index) {
    const before = anchorFor(blocks, index);
    if (block.kind === 'reasoning') {
      // Empty on purpose: the `updateNode` that follows immediately fills it
      // with the paced prefix, so the card never opens with a jump of text the
      // pacer has not released yet.
      const node = reasoningDisclosure('', { done: false });
      insertNode(list, node, before);
      return { kind: 'reasoning', node };
    }
    if (block.kind === 'tool') {
      const node = appendToolCall(list, {
        name: block.name, args: block.args, preview: block.preview,
        status: block.status, result: block.result, durationMs: block.durationMs,
        risk: block.risk, before,
      });
      return { kind: 'tool', node };
    }
    const body = appendMessage(list, 'assistant', '', { before });
    body.classList.add('streaming');
    return { kind: 'text', node: body.closest('.msg'), body };
  }

  // The prefix of a block that may be on screen right now. A closed block is
  // never paced: the text is final, and holding any of it back would only
  // invent latency the server did not have.
  function revealOf(block, now, immediate) {
    if (immediate || block.done) {
      pacer.flush(block.id);
      return block.text;
    }
    pacer.observe(block.id, block.text.length, now);
    const shown = pacer.revealed(block.id, now);
    return shown >= block.text.length ? block.text : block.text.slice(0, shown);
  }

  function renderTextBlock(entry, block, now, immediate) {
    const started = performance.now();
    renderMarkdown(entry.body, revealOf(block, now, immediate), { force: true });
    // Re-parsing Markdown costs what the block costs, and a block grows all
    // turn: ordinary prose is well under a millisecond, but a long reply full of
    // syntax-highlighted code reaches tens. Re-parsing THAT every 32ms spends
    // most of a frame budget on work the reader cannot see, and the reveal it
    // was meant to smooth is what stutters. So each block learns its own budget
    // from what it actually just cost — measured, not assumed, because the
    // fixed 120ms this replaced was an assumption nobody had measured either.
    entry.renderCost = entry.renderCost
      ? entry.renderCost + ((performance.now() - started) - entry.renderCost) * 0.3
      : performance.now() - started;
    entry.body.classList.toggle('streaming', !block.done);
    entry.paintedAt = now;
  }

  function throttleFor(entry) {
    const cost = entry.renderCost || 0;
    return Math.min(MARKDOWN_THROTTLE_MAX_MS, Math.max(MARKDOWN_THROTTLE_MS, cost * 4));
  }

  // One pump drives both jobs, because they are the same job seen from two
  // sides: how much text may show yet (the pacer) and how often it is worth
  // re-parsing Markdown to show it (the throttle). Driven by animation frames
  // rather than a timer so the reveal is synced to what the display can
  // actually draw, and so a backgrounded tab stops burning CPU on text nobody
  // is looking at — it catches up in one step when it comes back.
  function pump() {
    frame = null;
    if (!current) return;
    const now = Date.now();
    const due = pendingText;
    pendingText = new Set();
    let painted = false;

    for (const block of current.blocks) {
      if (block.kind === 'tool') continue;
      const entry = nodes.get(block.id);
      if (!entry) continue;
      const pacing = pacer.pending(block.id);
      if (!due.has(block.id) && !pacing) continue;
      if (entry.kind === 'reasoning') {
        updateNode(entry, block, now, false);
        painted = true;
        continue;
      }
      // A block still being revealed repaints at most every `throttleFor(entry)`
      // ms; the final paint of a block that has caught up is never deferred, so
      // text can't be left one frame short of complete.
      if (!pacing || now - (entry.paintedAt || 0) >= throttleFor(entry)) {
        renderTextBlock(entry, block, now, false);
        painted = true;
      } else {
        pendingText.add(block.id);
      }
    }

    if (painted) scrollIfPinned();
    if (pendingText.size || pacer.anyPending()) schedulePump();
  }

  function schedulePump() {
    if (frame !== null) return;
    frame = typeof requestAnimationFrame === 'function'
      ? requestAnimationFrame(pump)
      : setTimeout(pump, 16);
  }

  function cancelPump() {
    if (frame === null) return;
    if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(frame);
    else clearTimeout(frame);
    frame = null;
  }

  // Structure is applied immediately — a block appears the instant it exists,
  // which is what keeps the order honest — and only its text is paced.
  function scheduleText(block) {
    pendingText.add(block.id);
    schedulePump();
  }

  function updateNode(entry, block, now, immediate) {
    if (entry.kind === 'reasoning') {
      entry.node.update(revealOf(block, now, immediate), {
        done: block.done,
        durationMs: block.done ? Math.max(0, (block.endedAt || now) - block.startedAt) : null,
      });
      if (!immediate && pacer.pending(block.id)) schedulePump();
      return;
    }
    if (entry.kind === 'tool') {
      entry.node.update({
        name: block.name, args: block.args, preview: block.preview,
        status: block.status, result: block.result,
        durationMs: block.durationMs, risk: block.risk,
      });
      return;
    }
    if (immediate) renderTextBlock(entry, block, now, true);
    else scheduleText(block);
  }

  /**
   * Fold the turn's block sequence into the DOM. Order comes from the sequence
   * itself, so a second round of thinking or a reply written after a tool call
   * lands where it happened instead of overwriting the first round in place.
   */
  function syncBlocks(turn, now, { immediate = false } = {}) {
    if (turn.blocks === lastBlocks && !immediate) return;
    lastBlocks = turn.blocks;
    for (let i = 0; i < turn.blocks.length; i += 1) {
      const block = turn.blocks[i];
      let entry = nodes.get(block.id);
      if (!entry) {
        entry = createNode(block, turn.blocks, i);
        nodes.set(block.id, entry);
      }
      updateNode(entry, block, now, immediate);
    }
    // Every sync, not only the ones that added a node. A block that merely
    // GROWS — a reasoning card filling up, a tool row gaining its result —
    // pushes the transcript down just as much as a new one, and following only
    // new nodes left the view frozen wherever the thinking card first appeared.
    // `scrollIfPinned` still defers to a reader who scrolled away.
    scrollIfPinned();
  }

  // Sticking to the bottom is only correct when the reader is already there.
  // The old surface yanked the view down on every token, which made reading
  // back through a long turn impossible while it streamed.
  let pinnedToBottom = true;
  const SCROLL_SLACK = 48;
  const onScroll = () => {
    pinnedToBottom = list.scrollHeight - list.scrollTop - list.clientHeight <= SCROLL_SLACK;
    if (typeof onPinChange === 'function') onPinChange(pinnedToBottom);
  };
  list.addEventListener('scroll', onScroll);
  let onPinChange = null;

  function scrollIfPinned() {
    if (pinnedToBottom) scrollToLatest(list);
  }

  function paintBar(turn, now = Date.now()) {
    const running = !isTurnOver(turn);
    bar.hidden = !running;
    if (!running) return;

    const elapsed = turnElapsed(turn, now);
    // Below the reveal delay the bar shows movement but no claim about what
    // that movement is. Past it, a silent phase (no tool row, no streaming
    // text) escalates its own label the longer it runs — "Thinking more",
    // then "Deep thinking" — instead of the flat phase name sitting there
    // unchanged, which is what used to need a separate "stalled" warning to
    // say "this is still normal, just slow."
    label.textContent = elapsed >= LABEL_REVEAL_MS ? activityLabel(turn, now) : 'Working';

    const bits = [];
    if (turn.runtime?.model) bits.push(turn.runtime.model);
    if (turn.narration) bits.push(turn.narration.split('\n')[0].slice(0, 80));
    const row = activeTool(turn);
    if (row && row.args) {
      const preview = typeof row.args === 'string' ? row.args : JSON.stringify(row.args);
      bits.push(preview.slice(0, 60));
    }
    detail.textContent = bits.join(' · ');

    timer.textContent = formatDuration(elapsed);
    bar.dataset.phase = turn.phase;
    stopButton.hidden = !onStop;
  }

  function startTicking() {
    if (tick) return;
    tick = setInterval(() => {
      if (!current || isTurnOver(current)) { stopTicking(); return; }
      paintBar(current);
    }, 250);
  }

  function stopTicking() {
    if (tick) { clearInterval(tick); tick = null; }
  }

  return {
    node: bar,

    /** Called when a new turn starts, before the first frame. */
    begin(turn) {
      current = turn;
      lastBlocks = null;
      nodes = new Map();
      pendingText = new Set();
      pacer.reset();
      pinnedToBottom = true;
      finished = false;
      paintBar(turn);
      startTicking();
    },

    /** Fold one reduced turn state into the DOM. */
    apply(turn, now = Date.now()) {
      current = turn;
      syncBlocks(turn, now);
      if (isTurnOver(turn)) this.finish(turn, now);
      else paintBar(turn, now);
    },

    /**
     * Settle the turn. Renders the final Markdown, closes the reasoning
     * disclosure with its duration, and reports whether the reply landed —
     * the caller uses that to decide if a fallback re-read is needed at all.
     */
    finish(turn, now = Date.now()) {
      current = turn;
      if (finished) return Boolean(turn.text || turn.tools.length);
      finished = true;
      stopTicking();
      cancelPump();
      // The turn is over, so nothing is still "arriving" — every block shows
      // in full immediately rather than finishing an animation after the run
      // it belonged to has ended.
      pacer.flushAll();
      bar.hidden = true;

      // The reducer closes every open block when the turn ends, so this pass
      // renders the final Markdown, stamps each thought with its own duration
      // and drops the streaming state — all from the same sequence.
      syncBlocks(turn, now, { immediate: true });

      // A stream that died mid-block never got its closing frame, and a bubble
      // left in the streaming state hides its own actions for good.
      for (const entry of nodes.values()) {
        if (entry.kind === 'text') entry.body.classList.remove('streaming');
      }

      for (const row of turn.tools) {
        const entry = nodes.get(row.id);
        // A turn cut short leaves rows mid-flight; say so rather than spinning.
        if (entry && row.status === 'running') {
          entry.node.update({ status: 'failed', result: row.result ?? 'interrupted' });
        }
      }

      // Copy and permalink belong to the turn's answer — the last thing it
      // said — not to the prose it wrote on the way to a tool call.
      const answer = turnAnswer(turn);
      if (answer) {
        const entry = nodes.get(answer.id);
        if (entry) setMessageCopyable(entry.body, true);
      }

      const footer = turnFooter(turn, getContextTotal ? getContextTotal() : 0);
      if (footer) list.append(footer);
      if (turn.error) {
        appendMessage(list, 'error', turn.error, { onRetry: turn.onRetry });
      }
      scrollIfPinned();
      return Boolean(turn.text || turn.tools.length);
    },

    /** Let the composer react to the reader scrolling away from the bottom. */
    onPinChange(fn) { onPinChange = fn; },
    isPinned() { return pinnedToBottom; },
    scrollToBottom() {
      pinnedToBottom = true;
      scrollToLatest(list);
      if (typeof onPinChange === 'function') onPinChange(true);
    },

    destroy() {
      stopTicking();
      cancelPump();
      // The transcript outlives the turn, so a view that forgot to unsubscribe
      // would leave one dead scroll listener per turn on it, each still
      // reporting pin state for a turn that ended long ago.
      list.removeEventListener('scroll', onScroll);
      onPinChange = null;
    },
  };
}

/**
 * The per-turn receipt: `{input}↑ {output}↓ in {seconds}s | {model}`. One
 * line, kept minimal on purpose — this sits under every message in a turn
 * that can span many, so it competes with the transcript for attention.
 *
 * Only the prompt-size figure carries color, and only when it means
 * something: `contextWindowTotal` is the model's window (from the context
 * panel's last successful read), so the number can redden as this turn's
 * prompt actually approaches it. Output tokens are a constant green — cheap
 * to produce is always the good direction, so there is nothing to grade
 * against. Everything else (the elapsed time, the model) stays plain: a
 * color there would claim a health meaning neither one has.
 */
function turnFooter(turn, contextWindowTotal = 0) {
  const tokens = turnTokens(turn.usage);
  const elapsed = turnElapsed(turn);
  const ran = turn.runtime?.model || '';
  const asked = turn.runtime?.requested?.model || '';
  const modelText = ran && asked && asked !== ran ? `${asked} → ${ran}` : ran;
  if (!tokens && !elapsed && !modelText) return null;

  const foot = el('div', { class: 'msg-turn-foot' });
  const line = el('div', { class: 'msg-turn-stat' });

  if (tokens) {
    const heat = contextHeatColor(tokens.input, contextWindowTotal);
    line.append(el('span', {
      class: 'msg-turn-num',
      style: heat ? `color:${heat}` : '',
      text: `${tokens.input.toLocaleString()}↑`,
    }));
    line.append(document.createTextNode(' '));
    line.append(el('span', { class: 'msg-turn-num msg-turn-num-out', text: `${tokens.output.toLocaleString()}↓` }));
  }
  if (elapsed) line.append(document.createTextNode(`${tokens ? ' in ' : ''}${formatSecondsOnly(elapsed)}`));
  if (modelText) line.append(document.createTextNode(`${tokens || elapsed ? ' | ' : ''}${modelText}`));
  if (turn.interrupted) line.append(document.createTextNode(' · interrupted'));
  if (turn.partial) line.append(document.createTextNode(' · partial'));

  foot.append(line);
  // `route_source` is the gateway's own answer to "why this model": a
  // confirmed lock, a session /model override, the request, or the global.
  if (turn.runtime?.route_source) {
    foot.title = `model selected by: ${turn.runtime.route_source}`
      + (turn.runtime.model_lock ? ` · lock ${turn.runtime.model_lock}` : '');
  }
  return foot;
}

/** A "jump to latest" pill for when the reader has scrolled away mid-turn. */
export function jumpToLatest(onClick) {
  const button = el('button', {
    class: 'chat-jump-latest', type: 'button', title: 'Jump to latest',
  }, [icon('chevron-down', { size: 13 }), el('span', { text: 'Latest' })]);
  button.hidden = true;
  button.addEventListener('click', onClick);
  return button;
}

export function clearTranscript(list) {
  clear(list);
}
