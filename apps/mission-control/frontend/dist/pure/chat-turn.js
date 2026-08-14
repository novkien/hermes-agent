// Pure reducer for one chat turn's SSE stream.
//
// The chat tab used to read the stream by poking at the DOM from inside the
// event handler, which meant the turn had no state anyone could inspect: no
// phase, no elapsed time, no way to tell "the agent is thinking" from "the
// transport died". Everything the composer wants to show — the activity
// indicator, the stop button, the tool rows, the token chip — is derived here
// instead, from a plain object with no DOM in sight, so it can be tested in
// Node against the real event vocabulary.
//
// The vocabulary is not guessed. It is what `_handle_session_chat_stream` in
// gateway/platforms/api_server.py actually enqueues, plus the `bff.open` frame
// the BFF synthesises before the first upstream byte:
//
//   bff.open · run.started · message.started · assistant.delta ·
//   assistant.reasoning · tool.progress · tool.started · tool.completed ·
//   tool.failed · tool.output_risk · assistant.completed · run.completed ·
//   error · done
//
// Every payload carries session_id / run_id / seq / ts because the gateway's
// `_event_payload` injects them into all of them.

/** Phases, in the order a healthy turn walks through them. */
export const PHASES = [
  'connecting', 'queued', 'starting', 'thinking', 'tool', 'writing',
  'finalizing', 'done', 'failed', 'stopped',
];

const TERMINAL_PHASES = new Set(['done', 'failed', 'stopped']);

// Escalation belongs to an explicit `_thinking` interval, not to the whole
// request. A slow connection, queue or run startup is not evidence that the
// model is thinking, and a later thinking round must get a fresh clock.
const THINKING_MORE_MS = 30000;
const DEEP_THINKING_MS = 60000;

// The reasoning channel that predates a real one. `tool.progress` with
// tool_name `_thinking` is fed `assistant_message.content` by
// agent/conversation_loop.py — the model's own reply text, not its reasoning —
// so it must never reach the transcript, or the answer prints twice. It is
// still a genuine liveness signal, so it drives the status line only.
export const NARRATION_TOOL = '_thinking';

// How the several text blocks of one turn read as a single string. Only
// derived surfaces use it (the copy gate, `settle`, the footer); the transcript
// always paints block by block.
const BLOCK_JOIN = '\n\n';

/**
 * A turn is an ORDERED SEQUENCE, not three buckets.
 *
 * An agent turn walks rounds: think, say something, call a tool, read the
 * result, think again, answer. Modelling that as one `text` field, one
 * `reasoning` field and a `tools` array loses the interleaving — every round
 * after the first overwrote the one before it and painted into a node anchored
 * at the first round's position, which is how the live transcript ended up
 * showing the final answer above the tool call that produced it, and the second
 * thought in the first thought's place.
 *
 * `blocks` is that sequence, in arrival order, and it is the only thing the
 * view needs to lay the turn out. `text`, `reasoning` and `tools` remain as
 * derived views so the activity line, the footer and the tests keep reading the
 * turn the way they always have.
 *
 *   {kind:'reasoning', id, text, done, startedAt, endedAt}
 *   {kind:'text',      id, text, done, startedAt, endedAt}
 *   {kind:'tool',      id, key, name, args, preview, status, result, isError,
 *                      risk, startedAt, endedAt, durationMs}
 */
export function createTurn(now = Date.now()) {
  return {
    phase: 'connecting',
    startedAt: now,
    lastEventAt: now,
    endedAt: null,

    requestId: null,
    upstreamRequestId: null,
    runId: null,
    messageId: null,
    sessionId: null,
    runtime: null,

    blocks: [],
    blockSeq: 0,

    // Derived from `blocks` on every reduction — never written directly.
    text: '',
    reasoning: '',
    tools: [],

    narration: '',
    thinkingStartedAt: null,
    activeToolKey: null,

    usage: null,
    messages: null,
    error: null,
    interrupted: false,
    partial: false,

    seq: 0,
    unknown: [],
    changed: [],
  };
}

/** Elapsed wall time for the turn, in ms. */
export function turnElapsed(turn, now = Date.now()) {
  return Math.max(0, (turn.endedAt || now) - turn.startedAt);
}

export function isTurnOver(turn) {
  return TERMINAL_PHASES.has(turn.phase);
}

/** The tool row currently executing, if any. */
export function activeTool(turn) {
  return turn.tools.find((row) => row.key === turn.activeToolKey) || null;
}

/**
 * The turn's prose, whichever stage it is at. `assistant.completed` is
 * authoritative — it reconciles any delta the transport dropped — but it only
 * wins when it carried something, so an empty completion cannot blank a reply
 * that streamed fine.
 *
 * A turn that spoke in several rounds reads back as its rounds joined; the
 * transcript still paints each round where it happened.
 */
export function turnText(turn) {
  return turn.text;
}

/** The block a reader would copy: the last prose the turn produced. */
export function turnAnswer(turn) {
  for (let i = turn.blocks.length - 1; i >= 0; i -= 1) {
    if (turn.blocks[i].kind === 'text' && turn.blocks[i].text) return turn.blocks[i];
  }
  return null;
}

/* ------------------------------------------------------------ block model -- */

// `blocks` is the authority on order; these three are projections of it, kept
// in step by `setBlocks` so nothing can write one without the other.
function derive(next) {
  const tools = [];
  const prose = [];
  const thoughts = [];
  for (const block of next.blocks) {
    if (block.kind === 'tool') tools.push(block);
    else if (block.kind === 'text') { if (block.text) prose.push(block.text); }
    else if (block.text) thoughts.push(block.text);
  }
  next.tools = tools;
  next.text = prose.join(BLOCK_JOIN);
  next.reasoning = thoughts.join(BLOCK_JOIN);
  return next;
}

function setBlocks(next, blocks) {
  next.blocks = blocks;
  return derive(next);
}

function nextBlockId(next) {
  next.blockSeq += 1;
  return `b${next.blockSeq}`;
}

function streamBlock(next, kind, text, now, done = false) {
  return {
    kind,
    id: nextBlockId(next),
    // Which assistant message the block came from. `assistant.completed`
    // reconciles by it, so a completion cannot land on an earlier round's prose.
    messageId: next.messageId,
    text,
    done,
    startedAt: now,
    endedAt: done ? now : null,
  };
}

/**
 * Which prose block an `assistant.completed` is the authoritative version of.
 *
 * Usually the round is still writing and the answer is obvious. But the
 * completion can also arrive after the round already handed off to a tool, and
 * then the block it reconciles is closed — matching by message id is what keeps
 * that from appending the same paragraph a second time under the tool row.
 * With no message id to go on, a closed block is left alone rather than risking
 * an overwrite of an earlier round.
 */
function reconcileIndex(blocks, messageId) {
  const open = openIndex(blocks, 'text');
  if (open !== -1) return open;
  if (!messageId) return -1;
  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    if (blocks[i].kind === 'text' && blocks[i].messageId === messageId) return i;
  }
  return -1;
}

/** Index of the still-open block of `kind`, or -1. At most one can be open. */
function openIndex(blocks, kind) {
  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    if (blocks[i].kind === kind && !blocks[i].done) return i;
  }
  return -1;
}

/**
 * Prose and reasoning stay open only while their round does. A new assistant
 * message, a hand-off to a tool, or the completion of the message all end the
 * round — so whatever arrives next opens a block of its own, below everything
 * already on screen rather than on top of it.
 *
 * Tool blocks are not touched here: they close on their own completion event,
 * and a tool can still be running while the round moves on.
 */
function closeOpenBlocks(blocks, now) {
  let touched = false;
  const out = blocks.map((block) => {
    if (block.kind === 'tool' || block.done) return block;
    touched = true;
    return { ...block, done: true, endedAt: now };
  });
  return touched ? out : blocks;
}

function patchBlock(blocks, index, patch) {
  const out = blocks.slice();
  out[index] = { ...out[index], ...patch };
  return out;
}

// Two tools can run under one message id, and a tool can be called twice in a
// turn, so `tool_call_id` is preferred and the composite is only a fallback.
function toolKey(data) {
  const callId = data?.tool_call_id || data?.call_id || data?.id;
  if (callId) return `id:${callId}`;
  return `mt:${data?.message_id || ''}:${data?.tool_name || data?.name || ''}`;
}

function toolName(data) {
  return String(data?.tool_name || data?.name || 'tool');
}

// Match the newest still-running row first: a repeated call to the same tool
// under the composite key would otherwise close the wrong one.
function findToolBlock(blocks, key) {
  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    if (blocks[i].kind === 'tool' && blocks[i].key === key && blocks[i].status === 'running') return i;
  }
  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    if (blocks[i].kind === 'tool' && blocks[i].key === key) return i;
  }
  return -1;
}

function textOf(data, ...fields) {
  if (typeof data === 'string') return data;
  for (const field of fields) {
    const value = data?.[field];
    if (typeof value === 'string' && value) return value;
  }
  return '';
}

/**
 * Fold one SSE frame into the turn. Returns a NEW turn object whose `changed`
 * lists the keys a view needs to repaint; the input is never mutated.
 *
 * `event` is the shape api.js emits: `{event, id, data, raw}`.
 */
export function reduceTurn(turn, event, now = Date.now()) {
  const name = String(event?.event || 'message');
  const data = event?.data;
  const next = { ...turn, tools: turn.tools, lastEventAt: now, changed: [] };
  const touch = (key) => { if (!next.changed.includes(key)) next.changed.push(key); };

  if (data && typeof data === 'object') {
    if (Number.isFinite(Number(data.seq))) next.seq = Number(data.seq);
    if (data.session_id && !next.sessionId) next.sessionId = String(data.session_id);
    if (data.run_id && !next.runId) { next.runId = String(data.run_id); touch('runId'); }
  }

  const setPhase = (phase) => {
    if (next.phase !== phase) { next.phase = phase; touch('phase'); }
    // Leaving an explicit `_thinking` interval closes its clock. If another
    // round starts thinking later, its first `_thinking` frame starts over.
    if (phase !== 'thinking' && next.thinkingStartedAt !== null) {
      next.thinkingStartedAt = null;
      touch('thinkingStartedAt');
    }
  };

  switch (name) {
    case 'bff.open': {
      next.requestId = data?.request_id || null;
      next.upstreamRequestId = data?.upstream_request_id || null;
      setPhase('queued');
      return next;
    }

    case 'run.started': {
      // The earliest point at which the resolved target is known — the model
      // and provider the request will actually go to. Showing it here is what
      // answers "what did the agent initialise as" before any token exists.
      next.runtime = data?.runtime || null;
      touch('runtime');
      setPhase('starting');
      return next;
    }

    case 'message.started': {
      const id = data?.message?.id || data?.message_id;
      if (id) next.messageId = String(id);
      // A new assistant message is a new round: whatever the last one was still
      // writing is finished, and the next token belongs below, not inside it.
      setBlocks(next, closeOpenBlocks(next.blocks, now));
      setPhase('starting');
      return next;
    }

    case 'assistant.delta':
    case 'message.delta': {
      const delta = textOf(data, 'delta', 'text');
      if (delta) {
        const index = openIndex(next.blocks, 'text');
        if (index === -1) {
          // Prose starting also ends the thought that preceded it, so the
          // disclosure collapses with a real duration instead of hanging open.
          setBlocks(next, [
            ...closeOpenBlocks(next.blocks, now),
            streamBlock(next, 'text', delta, now),
          ]);
        } else {
          setBlocks(next, patchBlock(next.blocks, index, {
            text: next.blocks[index].text + delta,
          }));
        }
        touch('text');
        // Prose arriving means the interstitial narration is stale.
        if (next.narration) { next.narration = ''; touch('narration'); }
      }
      setPhase('writing');
      return next;
    }

    // Real reasoning, once the gateway emits it separately from the reply.
    case 'assistant.reasoning':
    case 'reasoning.delta': {
      const delta = textOf(data, 'delta', 'text', 'reasoning');
      if (delta) {
        const index = openIndex(next.blocks, 'reasoning');
        if (index !== -1) {
          // A delta appends; a full-text snapshot replaces — and it replaces
          // only the thought still being written, never an earlier round's.
          setBlocks(next, patchBlock(next.blocks, index, {
            text: name === 'reasoning.delta' ? next.blocks[index].text + delta : delta,
          }));
        } else {
          const block = streamBlock(next, 'reasoning', delta, now);
          const writing = openIndex(next.blocks, 'text');
          setBlocks(next, writing === -1
            ? [...next.blocks, block]
            // Thinking that lands once prose is already flowing still belongs
            // above the answer it produced — that is where the stored history
            // puts it, and a live turn that disagrees with its own reload is
            // exactly the inversion this model exists to prevent.
            : [...next.blocks.slice(0, writing), block, ...next.blocks.slice(writing)]);
        }
        touch('reasoning');
      }
      if (openIndex(next.blocks, 'text') === -1) setPhase('thinking');
      return next;
    }

    case 'tool.progress': {
      // Liveness only. See NARRATION_TOOL: this channel carries the reply text,
      // so it drives the status line and never the transcript.
      if (toolName(data) === NARRATION_TOOL) {
        if (!Number.isFinite(next.thinkingStartedAt)) {
          next.thinkingStartedAt = now;
          touch('thinkingStartedAt');
        }
        const preview = textOf(data, 'delta', 'preview');
        if (preview) { next.narration = preview; touch('narration'); }
        setPhase('thinking');
      }
      return next;
    }

    case 'tool.started': {
      const key = toolKey(data);
      const row = {
        kind: 'tool',
        id: nextBlockId(next),
        key,
        name: toolName(data),
        args: data?.args ?? null,
        preview: data?.preview ?? null,
        status: 'running',
        result: null,
        isError: false,
        risk: null,
        startedAt: now,
        endedAt: null,
        durationMs: null,
      };
      // Handing off to a tool ends the round's prose and thinking; anything the
      // model says after the result is a new block under this row.
      setBlocks(next, [...closeOpenBlocks(next.blocks, now), row]);
      next.activeToolKey = key;
      touch('tools');
      setPhase('tool');
      return next;
    }

    case 'tool.completed':
    case 'tool.failed':
    case 'tool.error': {
      const failed = name !== 'tool.completed' || data?.is_error === true;
      const key = toolKey(data);
      const index = findToolBlock(next.blocks, key);
      // `duration` is seconds upstream; the row keeps milliseconds.
      const durationMs = Number.isFinite(Number(data?.duration))
        ? Math.round(Number(data.duration) * 1000)
        : null;
      const result = data?.result ?? null;

      const patch = {
        status: failed ? 'failed' : 'done',
        isError: Boolean(failed),
        endedAt: now,
        result,
      };
      if (index === -1) {
        // A completion with no matching start — still worth a row rather than
        // a silently dropped tool call.
        setBlocks(next, [...closeOpenBlocks(next.blocks, now), {
          kind: 'tool', id: nextBlockId(next),
          key, name: toolName(data), args: data?.args ?? null,
          preview: data?.preview ?? null, risk: null,
          startedAt: now, durationMs, ...patch,
        }]);
      } else {
        const prev = next.blocks[index];
        setBlocks(next, patchBlock(next.blocks, index, {
          ...patch,
          result: result ?? prev.result,
          durationMs: durationMs ?? (prev.startedAt ? now - prev.startedAt : null),
        }));
      }
      touch('tools');
      if (next.activeToolKey === key) next.activeToolKey = null;
      // A tool finishing does not mean the turn is writing yet; fall back to
      // whichever stage the prose is at.
      if (!next.tools.some((row) => row.status === 'running')) {
        // The round that called the tool has already closed its prose, so
        // "writing" is only true if something is still being written.
        setPhase(openIndex(next.blocks, 'text') === -1 ? 'starting' : 'writing');
      }
      return next;
    }

    case 'tool.output_risk': {
      const index = findToolBlock(next.blocks, toolKey(data));
      if (index !== -1) {
        setBlocks(next, patchBlock(next.blocks, index, {
          risk: data?.risk_metadata || data?.risk || null,
        }));
        touch('tools');
      }
      return next;
    }

    case 'assistant.completed':
    case 'message.completed': {
      const finalText = textOf(data, 'content', 'text');
      if (finalText) {
        // The completion reconciles THIS round's prose — the deltas the
        // transport may have dropped — and must not overwrite what earlier
        // rounds already said.
        const id = data?.message_id || data?.message?.id || next.messageId;
        const index = reconcileIndex(next.blocks, id ? String(id) : null);
        setBlocks(next, index === -1
          ? [...closeOpenBlocks(next.blocks, now), streamBlock(next, 'text', finalText, now, true)]
          : patchBlock(next.blocks, index, { text: finalText }));
        touch('text');
      }
      // The message is over either way: the round's blocks close here.
      setBlocks(next, closeOpenBlocks(next.blocks, now));
      if (data?.interrupted) { next.interrupted = true; touch('interrupted'); }
      if (data?.partial) { next.partial = true; touch('partial'); }
      if (data?.runtime) { next.runtime = data.runtime; touch('runtime'); }
      if (next.narration) { next.narration = ''; touch('narration'); }
      setPhase('finalizing');
      return next;
    }

    case 'run.completed': {
      // The authoritative end of the turn. It carries the whole turn's
      // transcript and its token usage, which is why the tab no longer has to
      // re-read /messages and race the gateway's own persistence.
      if (Array.isArray(data?.messages)) { next.messages = data.messages; touch('messages'); }
      if (data?.usage) { next.usage = data.usage; touch('usage'); }
      if (data?.runtime) { next.runtime = data.runtime; touch('runtime'); }
      setBlocks(next, closeOpenBlocks(next.blocks, now));
      next.endedAt = now;
      setPhase(next.interrupted ? 'stopped' : 'done');
      return next;
    }

    case 'error': {
      next.error = typeof data === 'string'
        ? data
        : (data?.error || data?.message || 'stream error');
      setBlocks(next, closeOpenBlocks(next.blocks, now));
      next.endedAt = now;
      touch('error');
      setPhase('failed');
      return next;
    }

    case 'done':
    case 'complete':
    case 'assistant.done': {
      // The gateway always emits `done` in its finally, including after an
      // error frame — so it must not overwrite a failure with success.
      if (!isTurnOver(next)) {
        setBlocks(next, closeOpenBlocks(next.blocks, now));
        next.endedAt = now;
        setPhase(next.interrupted ? 'stopped' : 'done');
      }
      return next;
    }

    default: {
      // Never silently swallow: an unrecognised event is a contract drift we
      // want visible rather than invisible.
      next.unknown = [...turn.unknown, { name, data }];
      touch('unknown');
      return next;
    }
  }
}

/** Locally-driven end states (the stop button, a transport failure). */
export function stopTurn(turn, now = Date.now()) {
  if (isTurnOver(turn)) return turn;
  return derive({
    ...turn, phase: 'stopped', interrupted: true, endedAt: now,
    blocks: closeOpenBlocks(turn.blocks, now),
    narration: '', thinkingStartedAt: null,
    changed: ['phase', 'interrupted', 'narration', 'thinkingStartedAt', 'blocks'],
  });
}

export function failTurn(turn, message, now = Date.now()) {
  if (isTurnOver(turn)) return turn;
  return derive({
    ...turn, phase: 'failed', error: String(message || 'stream failed'),
    blocks: closeOpenBlocks(turn.blocks, now),
    endedAt: now, narration: '', thinkingStartedAt: null,
    changed: ['phase', 'error', 'narration', 'thinkingStartedAt', 'blocks'],
  });
}

// Labels for the activity line. Hermes Desktop names the wait only once it has
// lasted long enough to be worth naming; the view applies that delay, this map
// only supplies the words.
const PHASE_LABELS = {
  connecting: 'Connecting',
  queued: 'Waiting for the agent',
  starting: 'Starting the run',
  thinking: 'Thinking',
  tool: 'Running',
  writing: 'Writing',
  finalizing: 'Finishing',
  done: 'Done',
  failed: 'Failed',
  stopped: 'Stopped',
};

export function phaseLabel(turn) {
  if (turn.phase === 'tool') {
    const row = activeTool(turn);
    return row ? `Running ${row.name}` : PHASE_LABELS.tool;
  }
  return PHASE_LABELS[turn.phase] || turn.phase;
}

/**
 * The activity line escalates only while an explicit `_thinking` interval is
 * active: past 30s, "Thinking more"; past 60s, "Deep thinking". Startup and
 * queue time never count, and leaving the interval resets the clock before a
 * later thinking round.
 */
export function activityLabel(turn, now = Date.now()) {
  if (turn.phase === 'thinking' && Number.isFinite(turn.thinkingStartedAt)) {
    const elapsed = Math.max(0, now - turn.thinkingStartedAt);
    if (elapsed >= DEEP_THINKING_MS) return 'Deep thinking';
    if (elapsed >= THINKING_MORE_MS) return 'Thinking more';
  }
  return phaseLabel(turn);
}

/**
 * Total tokens for the turn, flattening the several shapes `usage` arrives in
 * (the gateway passes the agent's canonical usage dict straight through).
 */
export function turnTokens(usage) {
  if (!usage || typeof usage !== 'object') return null;
  const input = Number(usage.input_tokens ?? usage.prompt_tokens ?? usage.input ?? 0) || 0;
  const output = Number(usage.output_tokens ?? usage.completion_tokens ?? usage.output ?? 0) || 0;
  const reasoning = Number(usage.reasoning_tokens ?? usage.reasoning ?? 0) || 0;
  const total = Number(usage.total_tokens ?? usage.total ?? 0) || (input + output + reasoning);
  if (!total && !input && !output) return null;
  return { input, output, reasoning, total, cost: Number(usage.cost_usd ?? usage.cost ?? 0) || 0 };
}
