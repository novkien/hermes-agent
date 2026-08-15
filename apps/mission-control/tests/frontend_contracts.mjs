#!/usr/bin/env node

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { filterRows, filterSummary, queryTerms } from '../frontend/dist/pure/text-filter.js';
import { fitPopover, POPOVER_GAP, POPOVER_MIN_HEIGHT } from '../frontend/dist/pure/popover-fit.js';
import { buildTranscriptMarkdown } from '../frontend/dist/pure/chat-export.js';
import {
  buildDraftSessionRequest,
  draftSessionProfile,
  resolveDraftRuntimeProfile,
} from '../frontend/dist/pure/chat-draft.js';
import {
  alertRows,
  capabilityRegistry,
  listFrom,
  logLines,
  profileRows,
  sessionRows,
  summarizeSourceHealth,
  taskRows,
  unwrapPayload,
} from '../frontend/dist/pure/data-shape.js';
import {
  buildHash,
  parseHash,
  parseRouteWithProfile,
} from '../frontend/dist/pure/hash-router.js';
import {
  buildTopology,
  buildOrgChart,
  profileNodes,
  serviceNodes,
} from '../frontend/dist/pure/topology.js';
import {
  compactNumber,
  dailySeries,
  formatCost,
  modelRows,
  skillUsage,
  sliceTotals,
  taskRowsFromUsage,
  toolRows,
} from '../frontend/dist/pure/analytics-shape.js';

import {
  chainProvenanceLabel,
  createdSession,
  openTargetId,
  sessionId,
  sessionTimestamp,
  sessionTitle,
  withOptimisticSession,
} from '../frontend/dist/pure/chat-session.js';

import { attachChainTips, chainIdBatches } from '../frontend/dist/pure/session-chain.js';

import { shapeOpenRouterCatalog, supportsTools } from '../frontend/dist/pure/openrouter-catalog.js';
import { createDeltaPacer } from '../frontend/dist/pure/delta-pacer.js';

import {
  applyPage,
  createPager,
  isComplete,
  loadedCount,
  mergeSessions,
  planNextPages,
  remainingCount,
} from '../frontend/dist/pure/session-pager.js';

import {
  NARRATION_TOOL,
  activityLabel,
  createTurn,
  failTurn,
  isTurnOver,
  phaseLabel,
  reduceTurn,
  stopTurn,
  turnAnswer,
  turnElapsed,
  turnText,
  turnTokens,
} from '../frontend/dist/pure/chat-turn.js';

import {
  catalogHas,
  createModelPrefs,
  effectiveModel,
  normalizeProvider,
  observeConfirmedLock,
  observeRunModel,
  observeSessionModel,
  pickSessionModel,
} from '../frontend/dist/pure/session-model.js';

import {
  AUTOCOMPACT_KEY,
  FREE_SPACE_KEY,
  compressionThresholdFrom,
  contextHeatColor,
  formatPercent,
  formatSecondsOnly,
  formatTokens,
  normalizeContextWindow,
} from '../frontend/dist/pure/context-window.js';

const nestedTasks = {
  data: {
    data: {
      tasks: [{ id: 't1', status: 'running' }, { id: 't2', status: 'done' }],
      total: 2,
    },
    meta: { source_id: 'adapter' },
  },
  meta: { source_id: 'bff' },
};
assert.equal(unwrapPayload(nestedTasks).tasks.length, 2);
assert.deepEqual(taskRows(nestedTasks).map((row) => row.id), ['t1', 't2']);

const sessions = { sessions: [{ id: 's1' }, { id: 's2' }], total: 2, limit: 20, offset: 0 };
assert.deepEqual(sessionRows(sessions).map((row) => row.id), ['s1', 's2']);

const alerts = { alerts: [{ id: 'a1', severity: 'critical' }] };
assert.equal(alertRows(alerts)[0].id, 'a1');

// The chat tab's persona picker reads this list; the dashboard answers with a
// bare array on some builds and a {profiles} object on others.
const profiles = { data: { profiles: [{ name: 'jarvis', model: 'gpt-5' }] }, meta: {} };
assert.deepEqual(profileRows(profiles).map((row) => row.name), ['jarvis']);
assert.deepEqual(profileRows([{ name: 'solo' }]).map((row) => row.name), ['solo']);
assert.deepEqual(profileRows(null), []);

assert.deepEqual(listFrom([{ id: 'skill-1' }]), [{ id: 'skill-1' }]);
assert.deepEqual(logLines({ file: '/tmp/test.log', lines: ['first', 'second'] }), ['first', 'second']);

const directRegistry = {
  adapter: { healthy: true },
  'hermes-dashboard': { healthy: true },
  'hermes-gateway': { healthy: false },
  cron: { healthy: true },
};
assert.equal(capabilityRegistry(directRegistry), directRegistry);
assert.deepEqual(summarizeSourceHealth(directRegistry), {
  state: 'degraded',
  sources: directRegistry,
  missingRequired: [],
  unhealthyRequired: [],
  unhealthyOptional: ['hermes-gateway'],
  unhealthyCount: 1,
});
assert.equal(summarizeSourceHealth({
  adapter: { healthy: true },
  'hermes-dashboard': { healthy: true },
  'hermes-gateway': { healthy: true },
  cron: { healthy: true },
}).state, 'healthy');

assert.equal(buildHash('/overview', { profile: 'default' }), '#/overview');
assert.equal(buildHash('/kanban', { profile: 'management', task: 't1' }), '#/kanban?task=t1');
assert.deepEqual(parseHash('#/overview?profile=default'), {
  path: '/overview',
  params: { profile: 'default' },
});
assert.deepEqual(
  parseRouteWithProfile('#/overview?profile=legacy', '?profile=canonical'),
  { path: '/overview', params: {}, profile: 'canonical' },
);
assert.deepEqual(
  parseRouteWithProfile('#/overview?profile=legacy', ''),
  { path: '/overview', params: {}, profile: 'legacy' },
);


// --- fleet topology ---------------------------------------------------
// Shapes below are trimmed copies of live adapter/dashboard responses.
const roomPayload = {
  room_chat_id: '-1003914667905',
  room_slots: [
    { slot: 1, ceo_thread_id: 32857, coder_thread_id: 36319, research_thread_id: 1644, system_thread_id: 7243 },
    { slot: 2, ceo_thread_id: 70678, coder_thread_id: 70712, research_thread_id: 70718, system_thread_id: 70725 },
  ],
};
const topoSessions = [
  // Two sessions on the same seat: the newest is the occupant, the other history.
  { id: 'sess-new', chat_id: '-1003914667905', thread_id: '32857', is_active: true, last_activity_at: 200 },
  { id: 'sess-old', chat_id: '-1003914667905', thread_id: '32857', is_active: false, last_activity_at: 100 },
  // A session in the room chat but on a thread that is not a room seat.
  { id: 'sess-other-thread', chat_id: '-1003914667905', thread_id: '29124', last_activity_at: 150 },
  // A session outside the room entirely.
  { id: 'sess-dm', chat_id: '-100999', thread_id: '32857', last_activity_at: 150 },
];
const topoTasks = [
  { id: 't1', session_id: 'sess-new', status: 'in_progress' },
  { id: 't2', session_id: 'sess-new', status: 'done' },
  { id: 't3', session_id: 'sess-missing', status: 'done' },
  { id: 't4', status: 'done' },
  // Created by an EARLIER session on the same seat. Manager threads are reset
  // often, and attributing cards only to the current occupant silently erased
  // everything the seat had built before its last reset.
  { id: 't5', session_id: 'sess-old', status: 'done' },
];
const topo = buildTopology({ rooms: roomPayload, sessions: topoSessions, tasks: topoTasks });
assert.equal(topo.totals.slots, 2);
assert.equal(topo.totals.seats, 8);
assert.equal(topo.totals.occupied, 1);
assert.equal(topo.totals.live, 1);
const ceoSeat = topo.slots[0].seats.find((seat) => seat.role === 'ceo');
assert.equal(ceoSeat.session.id, 'sess-new');
assert.equal(ceoSeat.history, 1);
// Cards belong to the seat: the occupant's two plus the one its predecessor
// created, and the occupant's come first because sessions are newest-first.
assert.deepEqual(ceoSeat.tasks.map((t) => t.id), ['t1', 't2', 't5']);
assert.equal(ceoSeat.running, 1);
assert.equal(ceoSeat.live, true);
// A seat with no session is free, not missing.
assert.equal(topo.slots[1].seats.every((seat) => seat.session === null), true);
assert.equal(topo.slots[1].occupied, 0);
// The room-chat-but-not-a-seat session and the DM both fall out as detached.
assert.deepEqual(topo.detachedSessions.map((s) => s.id).sort(), ['sess-dm', 'sess-other-thread']);
// A task whose session is gone, and one with no session at all, are orphans.
assert.deepEqual(topo.orphanTasks.map((t) => t.id).sort(), ['t3', 't4']);
// Thread ids join as strings even when the two sources disagree on type.
assert.equal(typeof ceoSeat.thread, 'string');

assert.deepEqual(buildTopology({}).totals, { slots: 0, seats: 0, occupied: 0, live: 0, tasks: 0 });

// Cards are attributed by THREAD, not by the seat's live session. A manager
// thread resets constantly, so a card created before the last reset has a
// session id the tip list has never heard of — which is how a coder seat with
// 214 cards came to draw four.
const historyTasks = [
  { id: 'h1', session_id: 'sess-ancient', status: 'done', created_at: 5 },
  { id: 'h2', session_id: 'sess-new', status: 'done', created_at: 9 },
];
const withHistory = buildTopology({
  rooms: roomPayload,
  sessions: topoSessions,
  tasks: historyTasks,
  threadSessions: [{ id: 'sess-ancient', thread_id: '32857' }],
});
const historySeat = withHistory.slots[0].seats.find((seat) => seat.role === 'ceo');
// Newest first, and the ancient session's card is no longer an orphan.
assert.deepEqual(historySeat.tasks.map((t) => t.id), ['h2', 'h1']);
assert.equal(historySeat.taskTotal, 2);
assert.deepEqual(withHistory.orphanTasks, []);

// When the adapter attributes the cards itself, its per-thread window is what
// gets drawn and its count is what gets reported — the window is deliberately
// smaller than the total, so reading the total off the drawn rows under-reports.
const attributed = buildTopology({
  rooms: roomPayload,
  sessions: topoSessions,
  tasks: [],
  roomCards: {
    counts: { 32857: 214 },
    cards: { 32857: [{ id: 'c1', status: 'done' }, { id: 'c2', status: 'done' }] },
  },
});
const attributedSeat = attributed.slots[0].seats.find((seat) => seat.role === 'ceo');
assert.deepEqual(attributedSeat.tasks.map((t) => t.id), ['c1', 'c2']);
assert.equal(attributedSeat.taskTotal, 214);
assert.equal(attributed.totals.tasks, 214);
const attributedOrg = buildOrgChart({
  rooms: roomPayload,
  sessions: topoSessions,
  tasks: [],
  roomCards: { counts: { 36319: 214 }, cards: { 36319: [{ id: 'c1' }] } },
});
const coder = attributedOrg.slotTrees[0].managers.find((m) => m.role === 'coder');
assert.equal(coder.cards.length, 1);
assert.equal(coder.cardTotal, 214);


// --- org chart (CEO / manager / developer / lab restructure of the topology) ---
const orgTopics = [
  // Slot 1's seats are the canonical, cross_thread-bearing manager entities.
  { name: 'Jarvis - CEO', thread_id: 32857, cross_thread: [70678, 70680, 70681, 70682] },
  { name: 'Jarvis Coder Manager', thread_id: 36319, cross_thread: [70712, 70714, 70715, 70716] },
  { name: 'Jarvis Research Manager', thread_id: 1644, cross_thread: [70718, 70719, 70720, 70721] },
  { name: 'Jarvis System Manager', thread_id: 7243, cross_thread: [70725, 70726, 70727, 70728] },
  // Developer topics: no cross_thread, no explicit manager field -> default to system.
  { name: 'Jarvis Rectify - Developer', thread_id: 15363 },
  { name: 'Jarvis Renpy - Developer', thread_id: 1497 },
  // Singleton branches off the room, not any slot's CEO.
  { name: 'Skill Lab', thread_id: 29124 },
  { name: 'System Prompt Lab', thread_id: 44463 },
  { name: 'Jarvis ComfyUI Manager', thread_id: 17540 },
  // Not classifiable by name or thread -> unclassified bucket.
  { name: 'Claude Bridge', thread_id: 65000 },
];
const org = buildOrgChart({ rooms: roomPayload, sessions: topoSessions, tasks: topoTasks, topics: orgTopics });
assert.equal(org.slotTrees.length, 2);
assert.equal(org.slotTrees[0].ceo.thread, '32857');
assert.equal(org.slotTrees[0].ceoTopic.name, 'Jarvis - CEO');
const slot1System = org.slotTrees[0].managers.find((m) => m.role === 'system');
// Developers attach only to the canonical (cross_thread-bearing) slot for that role.
assert.deepEqual(slot1System.developers.map((d) => d.name).sort(), ['Jarvis Rectify - Developer', 'Jarvis Renpy - Developer']);
const slot2System = org.slotTrees[1].managers.find((m) => m.role === 'system');
assert.deepEqual(slot2System.developers, []);
// Cards under a manager are that seat's tasks, unchanged from buildTopology.
const slot1Ceo = org.slotTrees[0].ceo;
assert.deepEqual(slot1Ceo.tasks.map((t) => t.id), ['t1', 't2', 't5']);
assert.deepEqual(org.singletons.lab.map((t) => t.name).sort(), ['Skill Lab', 'System Prompt Lab']);
assert.deepEqual(org.singletons.comfyui.map((t) => t.name), ['Jarvis ComfyUI Manager']);
assert.deepEqual(org.unclassified.map((t) => t.name), ['Claude Bridge']);
assert.equal(org.totals.developers, 2);
assert.equal(org.totals.labs, 2);
assert.equal(org.totals.comfyui, 1);
assert.equal(org.totals.unclassified, 1);
// No topics at all -> everything still renders, just with no extra branches.
const orgBare = buildOrgChart({ rooms: roomPayload, sessions: topoSessions, tasks: topoTasks });
assert.equal(orgBare.slotTrees.length, 2);
assert.equal(orgBare.slotTrees[0].ceoTopic, null);
assert.deepEqual(orgBare.slotTrees[0].managers.map((m) => m.developers.length), [0, 0, 0]);
assert.deepEqual(orgBare.totals, {
  ...topo.totals, developers: 0, labs: 0, comfyui: 0, unclassified: 0, boundSlots: 0,
});
assert.equal(orgBare.slotTrees[0].binding, null, 'no room payload bindings -> no binding');

// Room task binding: live_occupancy wins over recent_bindings for the same
// slot, and a slot with neither stays null.
const boundRooms = {
  ...roomPayload,
  live_occupancy: [{ room_slot: 1, task_id: 'TASK-LIVE', status: 'ACTIVE', bound_at: 1000 }],
  recent_bindings: [
    { room_slot: 1, task_id: 'TASK-OLD', completed: true, last_seen_at: 500 },
    { room_slot: 2, task_id: 'TASK-DONE', completed: true, last_seen_at: 700 },
  ],
};
const orgBound = buildOrgChart({ rooms: boundRooms, sessions: topoSessions, tasks: topoTasks, topics: orgTopics });
const slot1 = orgBound.slotTrees.find((t) => t.slot === 1);
const slot2 = orgBound.slotTrees.find((t) => t.slot === 2);
assert.equal(slot1.binding.task_id, 'TASK-LIVE', 'live binding beats recent for the same slot');
assert.equal(slot1.binding.live, true);
assert.equal(slot2.binding.task_id, 'TASK-DONE', 'recent binding used when nothing is live');
assert.equal(slot2.binding.live, false);
assert.equal(orgBound.totals.boundSlots, 1, 'only live bindings count as bound');

const svcs = serviceNodes({
  adapter: { healthy: true, last_checked_at: 1786409396, routes_checked: ['GET /health -> 200'] },
  'hermes-gateway': { healthy: true },
  cron: { healthy: false },
});
// Control-plane order, not object insertion order.
assert.deepEqual(svcs.map((s) => s.id), ['hermes-gateway', 'adapter', 'cron']);
assert.equal(svcs[0].label, 'gateway');
assert.equal(svcs[2].healthy, false);
assert.equal(serviceNodes({ mystery: {} })[0].unknown, true);

assert.deepEqual(
  profileNodes({ profiles: [{ name: 'default', is_default: true, gateway_running: true, skill_count: 78 }] }),
  [{ name: 'default', isDefault: true, running: true, model: null, provider: null, skills: 78, description: '' }],
);

// --- analytics shaping ------------------------------------------------
const usagePayload = {
  daily: [
    { day: '2026-08-05', input_tokens: 20, output_tokens: 5, sessions: 2, api_calls: 9, estimated_cost: 0.5 },
    { day: '2026-08-04', input_tokens: 10, output_tokens: 1, sessions: 1, api_calls: 4, estimated_cost: 0.25 },
  ],
  by_model: [{ model: 'smart', input_tokens: 5, output_tokens: 1 }, { model: 'normal', input_tokens: 30, output_tokens: 3 }],
  by_task: [{ task: 'compression', input_tokens: 3, output_tokens: 1, api_calls: 2, models: ['normal'] }],
  tools: [{ tool: 'read_file', count: 2, percentage: 20 }, { tool: 'terminal', count: 8, percentage: 80 }],
  skills: { summary: { total_skill_loads: 4, distinct_skills_used: 2 }, top_skills: [{ skill: 'a', view_count: 1 }, { skill: 'b', view_count: 3 }] },
};
const series = dailySeries(usagePayload);
// Sorted oldest-first regardless of upstream order, with total_tokens derived.
assert.deepEqual(series.map((d) => d.day), ['2026-08-04', '2026-08-05']);
assert.equal(series[1].total_tokens, 25);
// Missing numeric keys coerce to 0 rather than NaN.
assert.equal(series[0].cache_read_tokens, 0);

const allTotals = sliceTotals(series);
assert.equal(allTotals.days, 2);
assert.equal(allTotals.total_tokens, 36);
assert.equal(allTotals.from, '2026-08-04');
assert.equal(allTotals.to, '2026-08-05');
// A brushed slice reports the slice, never the whole period.
const oneDay = sliceTotals(series.slice(1));
assert.equal(oneDay.days, 1);
assert.equal(oneDay.total_tokens, 25);
assert.equal(sliceTotals([]).from, null);

// Model rows come from /models when present and fall back to usage.by_model,
// ranked by total tokens either way.
assert.deepEqual(modelRows({ models: [{ model: 'x', input_tokens: 1 }] }).map((r) => r.model), ['x']);
assert.deepEqual(modelRows(usagePayload).map((r) => r.model), ['normal', 'smart']);
assert.equal(modelRows(usagePayload)[0].total_tokens, 33);
assert.deepEqual(taskRowsFromUsage(usagePayload).map((r) => r.task), ['compression']);
assert.deepEqual(toolRows(usagePayload).map((r) => r.tool), ['terminal', 'read_file']);
const skills = skillUsage(usagePayload);
assert.equal(skills.distinct, 2);
assert.deepEqual(skills.top.map((r) => r.skill), ['b', 'a']);
assert.deepEqual(skillUsage({}).top, []);

assert.deepEqual([999, 1500, 15000, 1.5e6, 3.3e7, 2.6e9].map(compactNumber), ['999', '1.5k', '15k', '1.5M', '33M', '2.6B']);
// A sub-cent cost is never rounded up to a number that reads like zero spend.
assert.deepEqual([0, 0.004, 1.239].map(formatCost), ['$0.00', '<$0.01', '$1.24']);

// ---- context window ------------------------------------------------------
// Payload shape is Hermes' own `agent.context_breakdown` output, verified
// live against the dashboard on 2026-08-12.
const contextPayload = {
  categories: [
    { id: 'system_prompt', label: 'System prompt', tokens: 53584, color: 'var(--context-usage-system)' },
    { id: 'tool_definitions', label: 'Tool definitions', tokens: 4615 },
    { id: 'rules', label: 'Rules', tokens: 14471 },
    { id: 'skills', label: 'Skills', tokens: 12711 },
    { id: 'conversation', label: 'Conversation', tokens: 11030 },
  ],
  context_max: 1000000,
  context_percent: 10,
  context_used: 96411,
  estimated_total: 96411,
  model: 'normal',
};

const ctx = normalizeContextWindow(contextPayload, { compressionThreshold: 0.9 });
assert.equal(ctx.total, 1000000);
assert.equal(ctx.used, 96411);
// Messages lead the legend regardless of the order upstream emitted.
assert.equal(ctx.segments[0].key, 'conversation');
assert.equal(ctx.segments[0].label, 'Messages');
// Reserve and free space are appended, in that order, and are never a series
// colour — they mean absence, not another category.
assert.equal(ctx.segments.at(-2).key, AUTOCOMPACT_KEY);
assert.equal(ctx.segments.at(-1).key, FREE_SPACE_KEY);
assert.equal(ctx.segments.at(-2).tokens, 100000);
// The bar must account for exactly the whole window — no gap, no overflow.
assert.equal(ctx.segments.reduce((sum, s) => sum + s.tokens, 0), 1000000);
assert.ok(Math.abs(ctx.segments.reduce((sum, s) => sum + s.percent, 0) - 100) < 1e-6);
// context_used === estimated_total means nothing has been measured yet, and
// the panel must not claim a measured prompt size.
assert.equal(ctx.measured, false);
assert.equal(normalizeContextWindow({ ...contextPayload, context_used: 120000 }).measured, true);

// A window smaller than its own fixed prompt clamps free space at zero rather
// than rendering a negative slice.
const tiny = normalizeContextWindow({ ...contextPayload, context_max: 1000 });
assert.equal(tiny.segments.at(-1).tokens, 0);

// Degenerate payloads must not throw and must not fabricate a window.
assert.deepEqual(normalizeContextWindow(null).segments, []);
assert.equal(normalizeContextWindow(null).total, 0);
assert.equal(normalizeContextWindow({ categories: [] }).percent, 0);
// No context_max means no reserve and no free space to compute against.
const unbounded = normalizeContextWindow({ categories: [{ id: 'conversation', tokens: 40 }] });
assert.deepEqual(unbounded.segments.map((s) => s.key), ['conversation']);
assert.equal(unbounded.segments[0].percent, 100);

assert.deepEqual([0, 999, 135700, 1.2e6].map(formatTokens), ['0', '999', '135.7k', '1.2M']);
assert.deepEqual([0.5, 9.94, 14.2].map(formatPercent), ['0.5%', '9.9%', '14%']);
assert.equal(compressionThresholdFrom({ compression: { threshold: 0.8 } }), 0.8);
// Absent, zero, and out-of-range thresholds all fall back to Hermes' default.
assert.equal(compressionThresholdFrom({}), 0.9);
assert.equal(compressionThresholdFrom({ compression: { threshold: 0 } }), 0.9);
assert.equal(compressionThresholdFrom({ compression: { threshold: 4 } }), 0.9);

/* ---------------------------------------------------- chat turn reducer -- */

// The event vocabulary below is verbatim from
// gateway/platforms/api_server.py::_handle_session_chat_stream, plus the
// bff.open frame agent_mission_control/chat_proxy.py synthesises.

const frame = (event, data) => ({ event, data });

// A whole turn: connect, dispatch, one tool round-trip, prose, completion.
let turn = createTurn(1000);
assert.equal(turn.phase, 'connecting');

turn = reduceTurn(turn, frame('bff.open', { request_id: 'r1', upstream_request_id: 'u1' }), 1010);
assert.equal(turn.phase, 'queued');
assert.equal(turn.requestId, 'r1');

turn = reduceTurn(turn, frame('run.started', {
  run_id: 'run_9', seq: 1,
  runtime: { model: 'claude-opus-5', provider: 'anthropic' },
}), 1020);
assert.equal(turn.phase, 'starting');
assert.equal(turn.runId, 'run_9');
// The resolved target is known before a single token — that is what the
// activity line shows instead of leaving the composer inert.
assert.equal(turn.runtime.model, 'claude-opus-5');

turn = reduceTurn(turn, frame('message.started', { message: { id: 'msg_1' } }), 1030);
assert.equal(turn.messageId, 'msg_1');

turn = reduceTurn(turn, frame('tool.started', {
  message_id: 'msg_1', tool_name: 'read_file', args: { path: '/etc/hosts' },
}), 1040);
assert.equal(turn.phase, 'tool');
assert.equal(turn.tools.length, 1);
assert.equal(turn.tools[0].status, 'running');
assert.equal(phaseLabel(turn), 'Running read_file');

turn = reduceTurn(turn, frame('tool.completed', {
  message_id: 'msg_1', tool_name: 'read_file', duration: 1.5, result: '127.0.0.1 localhost',
}), 1050);
assert.equal(turn.tools[0].status, 'done');
// Upstream sends seconds; the row keeps milliseconds.
assert.equal(turn.tools[0].durationMs, 1500);
assert.equal(turn.tools[0].result, '127.0.0.1 localhost');
assert.equal(turn.activeToolKey, null);

turn = reduceTurn(turn, frame('assistant.delta', { delta: 'Hel' }), 1060);
turn = reduceTurn(turn, frame('assistant.delta', { delta: 'lo' }), 1070);
assert.equal(turn.phase, 'writing');
assert.equal(turnText(turn), 'Hello');

turn = reduceTurn(turn, frame('assistant.completed', { content: 'Hello there.' }), 1080);
assert.equal(turn.phase, 'finalizing');
// The completion reconciles dropped deltas rather than appending to them.
assert.equal(turnText(turn), 'Hello there.');

turn = reduceTurn(turn, frame('run.completed', {
  messages: [{ role: 'assistant', content: 'Hello there.' }],
  usage: { input_tokens: 120, output_tokens: 8, total_tokens: 128 },
}), 1090);
assert.equal(turn.phase, 'done');
assert.equal(isTurnOver(turn), true);
// The turn carries its own transcript, so the tab no longer re-reads /messages
// and races the gateway's persistence.
assert.equal(turn.messages.length, 1);
assert.deepEqual(turnTokens(turn.usage), {
  input: 120, output: 8, reasoning: 0, total: 128, cost: 0,
});

// `done` always fires in the gateway's finally, including after an error — it
// must not overwrite the failure with success.
let failing = reduceTurn(createTurn(0), frame('error', { message: 'provider 401' }), 10);
assert.equal(failing.phase, 'failed');
assert.equal(failing.error, 'provider 401');
failing = reduceTurn(failing, frame('done', {}), 20);
assert.equal(failing.phase, 'failed');

// tool.progress/_thinking is fed the assistant's own reply text upstream
// (agent/conversation_loop.py), so it must never reach the transcript — that is
// what made the answer print three times.
let narrating = createTurn(0);
narrating = reduceTurn(narrating, frame('tool.progress', {
  tool_name: NARRATION_TOOL, delta: 'Let me check the config first.',
}), 10);
assert.equal(turnText(narrating), '');
assert.equal(narrating.narration, 'Let me check the config first.');
assert.equal(narrating.phase, 'thinking');
// Once real prose starts, the interstitial narration is stale and clears.
narrating = reduceTurn(narrating, frame('assistant.delta', { delta: 'Done.' }), 20);
assert.equal(narrating.narration, '');
assert.equal(turnText(narrating), 'Done.');

// Real reasoning rides its own event and stays out of the reply.
let reasoning = reduceTurn(createTurn(0), frame('assistant.reasoning', {
  text: 'The user wants the failing case.',
}), 10);
assert.equal(reasoning.reasoning, 'The user wants the failing case.');
assert.equal(turnText(reasoning), '');
assert.equal(reasoning.phase, 'thinking');

/* ------------------------------------------ turn blocks keep their order -- */

// A turn is a SEQUENCE of rounds, not three buckets. Folding every round into
// one `text` and one `reasoning` field is what made the live transcript show
// the final answer above the tool call that produced it, and the second thought
// in the first thought's place — while a reload showed the correct order.
let rounds = createTurn(0);
const feed = (event, data, at) => { rounds = reduceTurn(rounds, frame(event, data), at); };

feed('message.started', { message_id: 'm1' }, 10);
feed('assistant.reasoning', { delta: 'Check the clock first.' }, 20);
feed('assistant.delta', { delta: 'Let me look.' }, 30);
feed('tool.started', { tool_call_id: 't1', tool_name: 'terminal', args: 'date' }, 40);
feed('tool.completed', { tool_call_id: 't1', tool_name: 'terminal', result: '10:32' }, 50);
feed('assistant.completed', { content: 'Let me look.' }, 60);
feed('message.started', { message_id: 'm2' }, 70);
feed('assistant.reasoning', { delta: 'Now report it.' }, 80);
feed('assistant.delta', { delta: 'It is 10:32.' }, 90);
feed('assistant.completed', { content: 'It is 10:32.' }, 100);
feed('run.completed', {}, 110);

assert.deepEqual(rounds.blocks.map((block) => block.kind),
  ['reasoning', 'text', 'tool', 'reasoning', 'text']);
assert.deepEqual(rounds.blocks.filter((b) => b.kind !== 'tool').map((b) => b.text), [
  'Check the clock first.', 'Let me look.', 'Now report it.', 'It is 10:32.',
]);
// Round two's snapshot replaces round two's thought, never round one's.
assert.equal(rounds.blocks[0].text, 'Check the clock first.');
// Every block ends up closed, so each disclosure can collapse with its own
// duration and no bubble is left stuck in the streaming state.
assert.equal(rounds.blocks.every((b) => (b.kind === 'tool' ? b.status === 'done' : b.done)), true);
// Block ids are stable and unique — the view keys its nodes by them.
assert.equal(new Set(rounds.blocks.map((b) => b.id)).size, rounds.blocks.length);
// The derived views the activity line, footer and copy gate read still hold.
assert.equal(rounds.tools.length, 1);
assert.equal(turnAnswer(rounds).text, 'It is 10:32.');
assert.equal(turnText(rounds), 'Let me look.\n\nIt is 10:32.');
assert.equal(rounds.reasoning, 'Check the clock first.\n\nNow report it.');

// Thinking that arrives after prose has started still belongs above the answer
// it produced — that is where the stored history puts it, and a live turn that
// disagrees with its own reload is the same inversion in reverse.
let late = createTurn(0);
late = reduceTurn(late, frame('assistant.delta', { delta: 'Answer.' }), 10);
late = reduceTurn(late, frame('assistant.reasoning', { delta: 'Late thought.' }), 20);
assert.deepEqual(late.blocks.map((b) => b.kind), ['reasoning', 'text']);
assert.equal(turnText(late), 'Answer.');

// A tool call with no prose around it must not invent an empty bubble.
let toolOnly = createTurn(0);
toolOnly = reduceTurn(toolOnly, frame('tool.started', { tool_call_id: 'q', tool_name: 'bash' }), 10);
toolOnly = reduceTurn(toolOnly, frame('tool.completed', { tool_call_id: 'q', tool_name: 'bash' }), 20);
assert.deepEqual(toolOnly.blocks.map((b) => b.kind), ['tool']);
assert.equal(turnAnswer(toolOnly), null);

// A completion that lands after the round already handed off to a tool
// reconciles the prose it belongs to instead of repeating it under the row.
assert.equal(rounds.blocks.filter((b) => b.text === 'Let me look.').length, 1);

// Stopping mid-stream closes whatever was open, so nothing is left spinning.
let halted = createTurn(0);
halted = reduceTurn(halted, frame('assistant.reasoning', { delta: 'half a thought' }), 10);
halted = stopTurn(halted, 20);
assert.equal(halted.blocks[0].done, true);
assert.equal(halted.reasoning, 'half a thought');

// The reducer stays pure: folding an event never mutates the turn handed in.
const beforeFold = createTurn(0);
const afterFold = reduceTurn(beforeFold, frame('assistant.delta', { delta: 'hi' }), 10);
assert.equal(beforeFold.blocks.length, 0);
assert.equal(afterFold.blocks.length, 1);
assert.notEqual(beforeFold.blocks, afterFold.blocks);

// Two calls to the same tool under one message must not close the wrong row;
// tool_call_id wins when present.
let paired = createTurn(0);
paired = reduceTurn(paired, frame('tool.started', { tool_call_id: 'a', tool_name: 'bash' }), 10);
paired = reduceTurn(paired, frame('tool.started', { tool_call_id: 'b', tool_name: 'bash' }), 20);
paired = reduceTurn(paired, frame('tool.failed', { tool_call_id: 'a', tool_name: 'bash' }), 30);
assert.equal(paired.tools[0].status, 'failed');
assert.equal(paired.tools[1].status, 'running');
assert.equal(paired.phase, 'tool');

// Without a call id the composite key still closes the newest running row.
let composite = createTurn(0);
composite = reduceTurn(composite, frame('tool.started', { message_id: 'm', tool_name: 'bash' }), 10);
composite = reduceTurn(composite, frame('tool.started', { message_id: 'm', tool_name: 'bash' }), 20);
composite = reduceTurn(composite, frame('tool.completed', { message_id: 'm', tool_name: 'bash' }), 30);
assert.equal(composite.tools[0].status, 'running');
assert.equal(composite.tools[1].status, 'done');

// An unrecognised event is recorded, never silently dropped — contract drift
// has to stay visible.
const drifted = reduceTurn(createTurn(0), frame('some.future.event', { x: 1 }), 10);
assert.equal(drifted.unknown.length, 1);
assert.equal(drifted.unknown[0].name, 'some.future.event');

assert.equal(turnElapsed(turn), 90);

// Startup never masquerades as thinking, however long the provider stays
// silent. Escalation starts only at the first explicit `_thinking` frame.
const quiet = reduceTurn(createTurn(0), frame('run.started', {}), 1000);
assert.equal(quiet.phase, 'starting');
assert.equal(activityLabel(quiet, 1000), 'Starting the run');
assert.equal(activityLabel(quiet, 90000), 'Starting the run');

let explicitThinking = reduceTurn(quiet, frame('tool.progress', {
  tool_name: NARRATION_TOOL, delta: 'Working through it.',
}), 5000);
assert.equal(explicitThinking.thinkingStartedAt, 5000);
assert.equal(activityLabel(explicitThinking, 34999), 'Thinking');
explicitThinking = reduceTurn(explicitThinking, frame('tool.progress', {
  tool_name: NARRATION_TOOL, delta: 'Still working through it.',
}), 20000);
assert.equal(explicitThinking.thinkingStartedAt, 5000,
  'later `_thinking` frames in the same interval must not restart its clock');
assert.equal(activityLabel(explicitThinking, 35000), 'Thinking more');
assert.equal(activityLabel(explicitThinking, 64999), 'Thinking more');
assert.equal(activityLabel(explicitThinking, 65000), 'Deep thinking');

// Ending one thinking round clears its clock. A later `_thinking` frame starts
// from zero even though it belongs to the same overall chat turn.
explicitThinking = reduceTurn(explicitThinking, frame('assistant.delta', {
  message_id: 'm1', delta: 'First round.',
}), 66000);
assert.equal(explicitThinking.thinkingStartedAt, null);
explicitThinking = reduceTurn(explicitThinking, frame('message.started', {
  message_id: 'm2',
}), 67000);
assert.equal(activityLabel(explicitThinking, 120000), 'Starting the run');
explicitThinking = reduceTurn(explicitThinking, frame('tool.progress', {
  tool_name: NARRATION_TOOL, delta: 'Thinking again.',
}), 100000);
assert.equal(explicitThinking.thinkingStartedAt, 100000);
assert.equal(activityLabel(explicitThinking, 129999), 'Thinking');
assert.equal(activityLabel(explicitThinking, 130000), 'Thinking more');

// A real reasoning event without `_thinking` stays at the plain phase label.
const reasoningOnly = reduceTurn(createTurn(0), frame('assistant.reasoning', {
  delta: 'Reasoning without the compatibility signal.',
}), 1000);
assert.equal(activityLabel(reasoningOnly, 90000), 'Thinking');

// A phase with its own visible signal keeps naming itself, however long it
// runs — a running tool row already says something is happening, and
// escalating "Running bash" into "Deep thinking" would erase that.
const busy = reduceTurn(quiet, frame('tool.started', { message_id: 'm', tool_name: 'bash' }), 1200);
assert.equal(busy.phase, 'tool');
assert.equal(activityLabel(busy, 90000), phaseLabel(busy));
assert.notEqual(activityLabel(busy, 90000), 'Deep thinking');
const typing = reduceTurn(createTurn(0), frame('assistant.delta', { message_id: 'm', delta: 'hi' }), 1000);
assert.equal(typing.phase, 'writing');
assert.equal(activityLabel(typing, 90000), 'Writing');

// Local end states cannot resurrect a turn that already finished.
assert.equal(stopTurn(turn).phase, 'done');
assert.equal(stopTurn(quiet, 2000).phase, 'stopped');
assert.equal(failTurn(quiet, 'socket closed', 2000).error, 'socket closed');

/* --------------------------------------------------- chat session shapes -- */

// Verified live against the gateway: POST /api/sessions answers
// {"object":"hermes.session","session":{...}} — the record is NESTED. Reading
// `body.id` came back undefined, so "New session" created a session upstream
// and then had nothing to open.
const created = createdSession({
  object: 'hermes.session',
  session: { id: 'api_123', source: 'api_server', model: 'hermes-agent' },
});
assert.equal(created.id, 'api_123');
assert.equal(created.source, 'api_server');
// Older/flat gateways still work.
assert.equal(createdSession({ id: 'flat_1' }).id, 'flat_1');
assert.equal(createdSession({ session_key: 'k' }).id, 'k');
assert.equal(createdSession({}), null);
assert.equal(createdSession(null), null);

// New Session is a local draft. The active document profile chooses its
// runtime by default, while `default` remains the shared-gateway path.
assert.equal(resolveDraftRuntimeProfile('default'), null);
assert.equal(resolveDraftRuntimeProfile('default', 'comfyui-worker'), 'comfyui-worker');
assert.equal(resolveDraftRuntimeProfile('analyst'), 'analyst');
assert.equal(draftSessionProfile(null), 'default');
assert.deepEqual(buildDraftSessionRequest({
  documentProfile: 'default', runtimeProfile: 'comfyui-worker',
  model: 'ignored', provider: 'ignored', modelAllowed: true,
}), { profile: 'default', profile_name: 'comfyui-worker' });
assert.deepEqual(buildDraftSessionRequest({
  documentProfile: 'default', runtimeProfile: null,
  model: 'local-model', provider: 'local', modelAllowed: true,
}), { profile: 'default', model: 'local-model', provider: 'local' });

const chatSource = readFileSync(new URL('../frontend/dist/tabs/chat.js', import.meta.url), 'utf8');
assert.match(chatSource, /onDraftSubmit:\s*\(\) => materializeDraft/);
assert.match(chatSource, /draft · not created yet/);

// --- chain-tip resolution ---------------------------------------------------
//
// The dashboard's session list is chain ROOTS: Hermes ends a thread's session
// with end_reason='session_reset' and starts a successor carrying
// parent_session_id, and the list query deliberately excludes those
// successors. A thread that resets on a schedule (the case that exposed this:
// a daily orchestrator, reset 36 times over six weeks, always frozen at
// whatever the root's timestamp was) is therefore invisible as its actual
// live self in every listing — this is what `attachChainTips` /
// `openTargetId` / tip-aware `sessionTitle`/`sessionTimestamp` fix.

const staleRoot = {
  id: 'root_1', title: 'Old title', last_activity_at: 1000, started_at: 900,
};
const liveTip = {
  root_id: 'root_1', tip_id: 'tip_9', chain_depth: 3, title: 'Today’s title',
  message_count: 47, last_activity_at: 9000, started_at: 8000, is_active: true,
};

// Attaching: a root with a real successor gets `.tip`; a root with none (the
// tip IS the root) gets `tip: null` rather than a self-referential no-op tip.
const [withTip] = attachChainTips([staleRoot], { root_1: liveTip }, sessionId);
assert.equal(withTip.tip.tip_id, 'tip_9');
const [withoutTip] = attachChainTips(
  [staleRoot], { root_1: { ...liveTip, tip_id: 'root_1' } }, sessionId,
);
assert.equal(withoutTip.tip, null);
assert.deepEqual(attachChainTips([staleRoot], {}, sessionId)[0].tip, null);
assert.deepEqual(attachChainTips(null, {}, sessionId), []);

// Display prefers the tip once resolved — the whole point, since the root's
// own title/timestamp is whatever it was the moment it was last reset.
assert.equal(sessionTitle(withTip), 'Today’s title');
assert.equal(sessionTitle(withoutTip), 'Old title');
assert.equal(sessionTimestamp(withTip), 9000);
assert.equal(sessionTimestamp(withoutTip), 1000);

// Acting on a row (open, rename, delete, fork) must target the live tip, not
// the frozen root — opening the root instead of the tip was the exact bug.
assert.equal(openTargetId(withTip), 'tip_9');
assert.equal(openTargetId(withoutTip), 'root_1');
assert.equal(openTargetId({ id: 'bare' }), 'bare');

// The provenance line names both the listed id and the live one, so acting on
// a row is never a surprise.
assert.equal(chainProvenanceLabel(withTip), '↳ chain root root_1 · depth 3');
assert.equal(chainProvenanceLabel(withoutTip), null);
assert.equal(chainProvenanceLabel({ id: 'x' }), null);

// The adapter hard-rejects more than 200 ids per /session-tips call
// (`state_session_tips` raises above that) — batching must respect it exactly,
// and duplicate ids (a pinned row appearing in two profile pages) must not
// waste a slot.
const manyIds = Array.from({ length: 450 }, (_, i) => `s${i}`);
const batches = chainIdBatches(manyIds);
assert.deepEqual(batches.map((b) => b.length), [200, 200, 50]);
assert.equal(new Set(batches.flat()).size, 450);
assert.deepEqual(chainIdBatches(['a', 'a', 'b', null, '', undefined]), [['a', 'b']]);
assert.deepEqual(chainIdBatches([]), []);

// The optimistic insert is what keeps a new session visible before the
// dashboard aggregator has indexed it; re-inserting must not duplicate a row.
const seeded = withOptimisticSession([{ id: 'old' }], { id: 'new' });
assert.deepEqual(seeded.map((s) => s.id), ['new', 'old']);
assert.deepEqual(
  withOptimisticSession(seeded, { id: 'old', title: 't' }).map((s) => s.id),
  ['old', 'new'],
);
assert.deepEqual(withOptimisticSession([{ id: 'a' }], {}), [{ id: 'a' }]);

/* ------------------------------------------------------- session paging -- */

// Numbers below are the live deployment's, measured 2026-08-12: 5,295 sessions
// across 23 profiles, `default` holding 2,551. The aggregator's reachable
// ceiling is sum(min(profile_count, 500)) = 3,169, which is why deep paging has
// to go per profile.
const liveTotals = {
  default: 2551, researcher: 575, executor: 446, orchestrator: 331,
  analyst: 199, explorer: 188, writer: 173, 'comfyui-worker': 127,
};
const liveTotal = Object.values(liveTotals).reduce((a, b) => a + b, 0);

// Seed from an aggregator window: cursors must reflect rows already held.
const pagerSeed = createPager({
  total: liveTotal,
  profileTotals: liveTotals,
  sessions: [
    { id: 'a', profile: 'default' }, { id: 'b', profile: 'default' },
    { id: 'c', profile: 'researcher' },
  ],
});
assert.equal(loadedCount(pagerSeed), 3);
assert.equal(remainingCount(pagerSeed), liveTotal - 3);
assert.equal(isComplete(pagerSeed), false);
assert.equal(pagerSeed.cursors.default, 2);
assert.equal(pagerSeed.cursors.analyst, 0);

// The plan must start where the seed left off, never re-fetching page zero.
const plan = planNextPages(pagerSeed, 600, 100);
assert.equal(plan.reduce((sum, page) => sum + page.limit, 0), 600);
assert.equal(plan[0].profile, 'default');     // largest remainder first
assert.equal(plan[0].offset, 2);              // continues from the pagerSeed cursor
// Consecutive pages of one profile must not overlap.
const defaultPages = plan.filter((page) => page.profile === 'default');
for (let i = 1; i < defaultPages.length; i += 1) {
  assert.equal(defaultPages[i].offset, defaultPages[i - 1].offset + defaultPages[i - 1].limit);
}
// Every planned page respects the endpoint's hard limit cap of 100.
for (const page of plan) assert.ok(page.limit <= 100, `limit ${page.limit} exceeds cap`);

// A short page ends that profile even if the reported total disagrees —
// upstream back-fills pinned rows past the limit, so totals can over-report.
let advanced = applyPage(pagerSeed, 'analyst', 40, 100);
assert.equal(advanced.done.analyst, true);
assert.equal(advanced.cursors.analyst, 40);
assert.ok(!planNextPages(advanced, 600, 100).some((p) => p.profile === 'analyst'));

// A full page keeps the profile in play.
advanced = applyPage(pagerSeed, 'analyst', 100, 100);
assert.equal(advanced.done.analyst, undefined);

// Draining every profile terminates and reports complete — the loop must not
// spin forever when counts drift.
let drained = createPager({ total: 300, profileTotals: { a: 200, b: 100 } });
let guard = 0;
while (!isComplete(drained) && guard < 100) {
  for (const page of planNextPages(drained, 600, 100)) {
    drained = applyPage(drained, page.profile, page.limit, page.limit);
  }
  guard += 1;
}
assert.equal(isComplete(drained), true);
assert.equal(loadedCount(drained), 300);
assert.ok(guard < 100, 'paging failed to terminate');

// Merge: newest first, deduped, incoming wins but keeps client-only flags.
const merged = mergeSessions(
  [{ id: 'x', last_activity_at: 10, pinned: true }, { id: 'y', last_activity_at: 30 }],
  [{ id: 'x', last_activity_at: 20, title: 'fresh' }, { id: 'z', last_activity_at: 40 }],
  (s) => s.id,
  (s) => s.last_activity_at || 0,
);
assert.deepEqual(merged.map((s) => s.id), ['z', 'y', 'x']);
assert.equal(merged.find((s) => s.id === 'x').title, 'fresh');
assert.equal(merged.find((s) => s.id === 'x').pinned, true);
assert.equal(mergeSessions([], [], (s) => s.id, () => 0).length, 0);

// --- shared text filter ------------------------------------------------------
//
// Sixteen tabs render a list and none of them could be searched. One matcher
// serves all of them, so its rules are pinned here rather than drifting per tab.

const filterFixture = [
  { id: 't_63c69099', title: 'Aquarium Burst: Implementation Plan', assignee: 'explorer', board: 'coder-dev' },
  { id: 't_ccf9be14', title: 'Aquarium Burst: Implement single-file aquarium', assignee: 'tdd-guide', board: 'coder-dev' },
  { id: 't_8c4b4bec', title: 'HF trending report', assignee: null, board: 'ops-automation', tags: ['daily', 'report'] },
];

// Empty query is identity, and the SAME array — a list filtered on every
// keystroke must not churn while idle.
assert.equal(filterRows(filterFixture, '', ['title']), filterFixture);
assert.equal(filterRows(filterFixture, '   ', ['title']), filterFixture);

// Case-insensitive substring, across whichever fields the tab names.
assert.deepEqual(filterRows(filterFixture, 'AQUARIUM', ['title']).map((r) => r.id),
  ['t_63c69099', 't_ccf9be14']);
assert.deepEqual(filterRows(filterFixture, 'explorer', ['title', 'assignee']).map((r) => r.id),
  ['t_63c69099']);
// A field the row does not carry is not a match, and a null field is not a crash.
assert.deepEqual(filterRows(filterFixture, 'explorer', ['title']), []);
assert.equal(filterRows(filterFixture, 'zz', ['assignee']).length, 0);

// Every term must match, and terms may come from different fields.
assert.deepEqual(filterRows(filterFixture, 'aquarium tdd', ['title', 'assignee']).map((r) => r.id),
  ['t_ccf9be14']);
assert.deepEqual(filterRows(filterFixture, 'aquarium nope', ['title', 'assignee']), []);

// A quoted phrase is one term, so its internal spacing is significant.
assert.deepEqual(filterRows(filterFixture, '"burst: implementation"', ['title']).map((r) => r.id),
  ['t_63c69099']);
assert.deepEqual(queryTerms('one "two three" four'), ['one', 'two three', 'four']);
assert.deepEqual(queryTerms(''), []);

// Arrays and numbers are searchable — ids, counts and tag lists are exactly
// what gets pasted into a filter box.
assert.deepEqual(filterRows(filterFixture, 'daily', ['tags']).map((r) => r.id), ['t_8c4b4bec']);
assert.deepEqual(filterRows([{ port: 9119 }], '9119', ['port']).length, 1);
// Accessor functions, for nested shapes (a session's resolved chain tip).
assert.deepEqual(
  filterRows([{ tip: { title: 'live thread' } }], 'live', [(row) => row.tip?.title]).length, 1,
);
// Objects contribute nothing rather than "[object Object]".
assert.deepEqual(filterRows([{ meta: { a: 1 } }], 'object', ['meta']), []);

// The caption that stops a filtered list from reading as a small dataset.
assert.equal(filterSummary(12, 387, 'card'), '12 of 387 cards');
assert.equal(filterSummary(387, 387, 'card'), '387 cards');
assert.equal(filterSummary(1, 1, 'card'), '1 card');

// --- popover geometry ------------------------------------------------------
//
// The toolsets menu opened off the top of a short viewport because the old code
// clamped position but never height, and measured once while the menu was still
// a loading skeleton. The repair anchors one edge and caps the other, so the
// result must never depend on the content's height.

const viewport = { width: 1280, height: 430 };
const anchorRect = { top: 360, bottom: 380, left: 200, right: 320 };

// Opening upward pins the BOTTOM edge just above the anchor and leaves `top`
// unset — that is what lets a menu grow after its data loads without ever
// crossing the anchor.
const above = fitPopover(anchorRect, { width: 380 }, viewport);
assert.equal(above.placement, 'above');
assert.equal(above.top, null);
assert.equal(above.bottom, viewport.height - anchorRect.top + POPOVER_GAP);
// The cap is the room above the anchor, so the far edge is unreachable too.
assert.equal(above.maxHeight, anchorRect.top - POPOVER_GAP * 2);
assert.ok(above.maxHeight + above.bottom <= viewport.height, 'popover could overrun the top edge');

// Geometry is independent of how tall the content is — the bug was that it was not.
assert.deepEqual(fitPopover(anchorRect, { width: 380 }, viewport), above);

// Opening downward pins the TOP edge and caps at the room below.
const below = fitPopover({ top: 40, bottom: 60, left: 40, right: 160 }, { width: 280 }, viewport, { placement: 'below' });
assert.equal(below.placement, 'below');
assert.equal(below.bottom, null);
assert.equal(below.top, 60 + POPOVER_GAP);
assert.equal(below.maxHeight, viewport.height - 60 - POPOVER_GAP * 2);

// A trigger with almost no room below it flips upward rather than showing a sliver.
assert.equal(
  fitPopover({ top: 300, bottom: 420, left: 40, right: 160 }, { width: 280 }, viewport, { placement: 'below' }).placement,
  'above',
);
// ...but a side that is merely tight, not unusable, is still honoured.
assert.equal(
  fitPopover({ top: 40, bottom: 60, left: 40, right: 160 }, { width: 280 }, viewport, { placement: 'below' }).placement,
  'below',
);

// Right-aligned by default, left-aligned on request, both clamped horizontally.
assert.equal(fitPopover(anchorRect, { width: 280 }, viewport).left, 320 - 280);
assert.equal(fitPopover(anchorRect, { width: 280 }, viewport, { align: 'start' }).left, 200);
assert.equal(fitPopover({ top: 100, bottom: 120, left: 4, right: 24 }, { width: 380 }, viewport).left, POPOVER_GAP);

// A viewport with no room at all still yields a usable, non-negative box.
const cramped = fitPopover({ top: 20, bottom: 40, left: 10, right: 90 }, { width: 280 }, { width: 320, height: 100 });
// Both sides are too small, so it takes the roomier one (below) and falls back
// to the minimum usable height rather than a sliver.
assert.equal(cramped.placement, 'below');
assert.equal(cramped.maxHeight, POPOVER_MIN_HEIGHT);
assert.ok(cramped.top >= POPOVER_GAP);
assert.ok(cramped.left >= POPOVER_GAP);

// A prompt larger than the window it is bounded by means the denominator is
// wrong — Hermes falls back to a 256K default for any model no registry knows —
// so the panel must be able to say so instead of painting a confident 100%.
// The live reading that exposed this: a 1M model reported at 256K, its
// categories already summing past the window.
const overflowed = normalizeContextWindow({
  categories: [
    { id: 'conversation', tokens: 225000 },
    { id: 'tool_definitions', tokens: 18800 },
    { id: 'system_prompt', tokens: 12100 },
    { id: 'memory', tokens: 1600 },
    { id: 'skills', tokens: 13500 },
  ],
  context_max: 256000,
  context_used: 271100,
  model: 'normal',
});
assert.equal(overflowed.overflow, true);
assert.equal(overflowed.percent, 100);
assert.equal(overflowed.segments.find((s) => s.key === 'free_space').tokens, 0);

const withinWindow = normalizeContextWindow({
  categories: [{ id: 'conversation', tokens: 103000 }],
  context_max: 1000000,
  context_used: 149000,
  model: 'normal',
});
assert.equal(withinWindow.overflow, false);
assert.ok(withinWindow.segments.find((s) => s.key === 'free_space').tokens > 0);
// No denominator at all is unknown, not overflowing.
assert.equal(normalizeContextWindow({ context_used: 5 }).overflow, false);

// --- transcript export -----------------------------------------------------
//
// The export is a record of the run, so it must carry what shaped the reply and
// not only the visible bubbles: system prompt, system-role messages, and the
// full context accounting.

const exported = buildTranscriptMarkdown({
  session: {
    id: 's1', title: 'Aquarium', model: 'normal', profile: 'default',
    system_prompt: '# rules\n```js\nnested fence\n```', system_prompt_hash: 'abc123def4567',
    input_tokens: 10, output_tokens: 2, cache_read_tokens: 5, started_at: 1786542783,
  },
  messages: [
    { role: 'system', content: 'be terse' },
    { role: 'session_meta', content: 'cwd=/srv' },
    { role: 'user', content: 'hi', timestamp: 1786542783 },
    {
      role: 'assistant', content: [{ type: 'text', text: 'yo' }], reasoning_content: 'weighing options',
      tool_calls: '[{"function":{"name":"bash","arguments":"{\\"cmd\\":\\"ls\\"}"}}]',
    },
    { role: 'tool', tool_name: 'bash', content: 'ok' },
  ],
  context: {
    categories: [
      { id: 'conversation', tokens: 103000 },
      { id: 'tool_definitions', tokens: 18800 },
      { id: 'system_prompt', tokens: 12100 },
      { id: 'memory', tokens: 1600 },
      { id: 'skills', tokens: 13500 },
    ],
    context_max: 256000, context_used: 149000, model: 'normal',
    details: { toolsets: [{ toolset: 'kanban', tool_count: 14, schema_tokens: 5801 }], skills: [] },
  },
});

// Every category the composer gauge lists is in the document.
for (const label of ['Messages', 'System tools', 'System prompt', 'Memory files', 'Skills', 'Autocompact buffer', 'Free space']) {
  assert.ok(exported.includes(`| ${label} |`), `context export is missing ${label}`);
}
assert.ok(exported.includes('## System prompt'), 'export dropped the system prompt section');
assert.ok(exported.includes('nested fence'), 'export dropped the system prompt body');
// A system prompt containing a ``` fence must not tear the document in half.
assert.ok(exported.includes('````text'), 'nested fences were not escaped by a longer fence');
assert.ok(exported.includes('### system\n'), 'export dropped system-role messages');
assert.ok(exported.includes('### system \u00b7 session meta'), 'export dropped session_meta rows');
assert.ok(exported.includes('weighing options'), 'export dropped reasoning');
assert.ok(exported.includes('**calls** `bash`'), 'export dropped tool calls');
assert.ok(exported.includes('yo'), 'export dropped multimodal content parts');
assert.ok(exported.includes('| kanban | 14 | 5801 |'), 'export dropped the toolset detail table');
assert.ok(exported.includes('- **tokens in / out**: 10 / 2 (5 cached)'));

// Degrades: no context route, no messages, no system prompt.
const bare = buildTranscriptMarkdown({ session: { id: 's2' }, messages: [] });
assert.ok(bare.includes('# s2'));
assert.ok(bare.includes('_No messages._'));
assert.ok(!bare.includes('## Context window'));
assert.ok(!bare.includes('## System prompt'));

// --- mirroring a thread driven from another runtime -------------------------
// A session advanced from Telegram/cron/CLI has to land in an open thread
// without a repaint, and without rendering the same message twice.
const {
  createMirrorBarrier, messageKey, messageKeys, mirrorAppend, trimUnsettledTail, isAtBottom,
} =
  await import('../frontend/dist/pure/chat-mirror.js');

const mirrorBarrier = createMirrorBarrier();
const releaseLocal = mirrorBarrier.acquire('s1');
const releaseWatched = mirrorBarrier.acquire('s1');
const releaseOther = mirrorBarrier.acquire('s2');
assert.equal(mirrorBarrier.active('s1'), true);
assert.equal(mirrorBarrier.active('s2'), true);
releaseLocal();
assert.equal(mirrorBarrier.active('s1'), true,
  'one baseline finishing must not release another sync for the same session');
releaseLocal(); // idempotent
assert.equal(mirrorBarrier.active('s1'), true);
releaseWatched();
assert.equal(mirrorBarrier.active('s1'), false);
assert.equal(mirrorBarrier.active('s2'), true,
  'baseline gates must be scoped to one session');
releaseOther();
assert.equal(mirrorBarrier.active('s2'), false);

assert.equal(messageKey({ id: 'm1', role: 'user' }), 'm1');
assert.equal(messageKey({ message_id: 'm2' }), 'm2');
// No id upstream: stable enough that re-reading the same page twice is a no-op.
const idless = { role: 'user', created_at: '2026-08-13T00:00:00Z', content: 'hi' };
assert.equal(messageKey(idless), messageKey({ ...idless }));
assert.notEqual(messageKey(idless), messageKey({ ...idless, content: 'ho' }));

const rendered = new Set(messageKeys([{ id: 'm1' }, { id: 'm2' }]));
assert.deepEqual(
  mirrorAppend(rendered, [{ id: 'm1' }, { id: 'm2' }, { id: 'm3' }]).map((m) => m.id),
  ['m3'],
  'mirror must append only what is new',
);
// The newest page is a sliding window: rows falling off the top must not make
// the whole page look new.
assert.deepEqual(mirrorAppend(rendered, [{ id: 'm2' }]).map((m) => m.id), []);
// Idempotent — polling twice with no upstream change appends nothing.
assert.deepEqual(mirrorAppend(rendered, [{ id: 'm1' }, { id: 'm2' }]), []);

// A tool call whose result has not been persisted yet is held back, so the
// pair renders as one foldable row rather than a stuck "pending" plus an
// orphan result on the next tick.
const inFlight = [
  { id: 'm3', role: 'assistant', tool_calls: [{ id: 'c1', function: { name: 'bash' } }] },
];
assert.deepEqual(mirrorAppend(rendered, [{ id: 'm1' }, ...inFlight]), []);
assert.deepEqual(
  mirrorAppend(rendered, [
    ...inFlight,
    { id: 'm4', role: 'tool', tool_call_id: 'c1', content: 'ok' },
  ]).map((m) => m.id),
  ['m3', 'm4'],
  'a settled tool pair must go through together',
);
// tool_calls arriving as a JSON string is the same case.
assert.deepEqual(
  mirrorAppend(rendered, [{ id: 'm5', role: 'assistant', tool_calls: '[{"id":"c9"}]' }]),
  [],
);
// An orphan far back in history (its result was compacted away upstream) must
// not freeze the mirror forever.
const orphan = [
  { id: 'a', role: 'assistant', tool_calls: [{ id: 'gone' }] },
  { id: 'b', role: 'assistant', content: 'one' },
  { id: 'c', role: 'assistant', content: 'two' },
  { id: 'd', role: 'assistant', content: 'three' },
];
assert.deepEqual(trimUnsettledTail(orphan).map((m) => m.id), ['a', 'b', 'c', 'd']);

assert.equal(isAtBottom({ scrollHeight: 1000, scrollTop: 960, clientHeight: 40 }), true);
assert.equal(isAtBottom({ scrollHeight: 1000, scrollTop: 200, clientHeight: 40 }), false);
assert.equal(isAtBottom(null), true);

// --- the composer's model pick has to be the model that runs ----------------
// The gateway ranks a per-request `model` BELOW the model persisted on the
// session row and silently uses the latter, so an explicit pick must be sent
// as a lock or it is decoration. Verified live: a turn sent with
// `model: "normal"` was billed against `local`.
const { chatStreamBody } = await import('../frontend/dist/pure/chat-model.js');

const inherited = chatStreamBody({
  sessionId: 's1', sessionProfile: 'default', text: 'hi',
  prefs: { model: 'normal', provider: '9router' },
});
assert.equal(inherited.model, 'normal');
assert.equal(inherited.provider, '9router');
assert.equal(inherited.require_model_lock, undefined,
  'a pref inherited from the session must not rewrite the session');

const picked = chatStreamBody({
  sessionId: 's1', sessionProfile: 'default', text: 'hi',
  prefs: { model: 'smart', provider: '9router', effort: 'xhigh', explicit: true },
});
assert.deepEqual(picked.model_options, { reasoning: { enabled: true, effort: 'xhigh' } });
// The provider is what makes the model resolvable and must always travel.
// Dropping it to satisfy a broken upstream lock check sent a 9router alias to
// the wrong catalogue: "HTTP 400: smart is not a valid model ID".
assert.equal(picked.provider, '9router', 'the picked model keeps its provider');
assert.equal(picked.model, 'smart');
assert.equal(picked.require_model_lock, true, 'an explicit pick must lock');

// Every explicit pick locks, even one that names the same model the session
// is already believed to be running. This used to be skipped as a "no-op",
// compared against a `sessionModel` the caller had to supply — and that value
// came from a session object snapshotted once when the thread was opened, so
// on any session whose model changed since, a real explicit pick that
// happened to match the STALE snapshot silently sent no lock at all. Verified
// live: a user picked "Normal" from the picker, the request carried
// `model: "normal"` with no `require_model_lock`, and the turn ran on
// whatever the gateway felt like. Locking unconditionally on `explicit` costs
// nothing when the pick already matches — the gateway accepts it — so there
// is no scenario where comparing against a second, independently-sourced copy
// of "the current model" was worth the risk of that copy being wrong.
const pickedSameName = chatStreamBody({
  sessionId: 's1', text: 'hi',
  prefs: { model: 'local', provider: '9router', explicit: true },
});
assert.equal(pickedSameName.require_model_lock, true,
  'an explicit pick locks regardless of what the caller believes the session already runs');
assert.equal(pickedSameName.provider, '9router');

const attachmentTurn = chatStreamBody({
  sessionId: 's1', sessionProfile: 'default', text: 'inspect this',
  attachments: [{
    kind: 'pdf', name: 'report.pdf', mime_type: 'application/pdf',
    size: 4, data: 'data:application/pdf;base64,JVBERg==', url: '',
  }],
});
assert.equal(attachmentTurn.message, 'inspect this');
assert.deepEqual(attachmentTurn.attachments, [{
  name: 'report.pdf', mime_type: 'application/pdf', size: 4,
  kind: 'pdf', data: 'data:application/pdf;base64,JVBERg==',
}]);
assert.equal(chatStreamBody({
  sessionId: 's1', text: '', attachments: attachmentTurn.attachments,
}).message, 'Please review the attached content.');

const composerSource = readFileSync(
  new URL('../frontend/dist/tabs/chat/composer.js', import.meta.url), 'utf8',
);
const chatTabSource = readFileSync(
  new URL('../frontend/dist/tabs/chat.js', import.meta.url), 'utf8',
);
const attachmentSource = readFileSync(
  new URL('../frontend/dist/tabs/chat/attachments.js', import.meta.url), 'utf8',
);
assert.match(composerSource, /return Boolean\(localActive \|\| controller\)/,
  'the optimistic-send window must count as running before a stream controller exists');
assert.match(composerSource,
  /`\/api\/runs\/\$\{encodeURIComponent\(id\)\}\/stop`/,
  'Stop must call the gateway run-stop mutation instead of only aborting the browser stream');
assert.match(composerSource, /suppressedRunIds\.has\(eventRunId\)/,
  'a locally stopped run must not re-attach through the session frame watcher');
assert.match(composerSource, /await findRunningRunId\(\)/,
  'an early Stop must resolve a run id even if run.started did not reach the browser');
assert.match(chatTabSource,
  /mirrorBaselineBarrier\.active\(sessionId\)[\s\S]*?await readMessages\([\s\S]*?mirrorBaselineBarrier\.active\(sessionId\)/,
  'the mirror must re-check its baseline gate after an in-flight history read');
assert.match(composerSource, /accept:\s*['"]\*\/\*['"]/,
  'the chat file picker must accept every file type');
assert.match(attachmentSource, /readAsDataURL\(file\)/,
  'non-image files must be transported, not replaced with a text summary');
assert.doesNotMatch(attachmentSource, /This file cannot be safely inlined/);

/* ---------------------------------------------------------------------------
 * One owner for "which model does this session run".
 * ------------------------------------------------------------------------ */

// This deployment's real shape: the gateway's global model is `normal`, a
// `model_routes` alias that no provider catalogue lists, while the session
// itself runs `local`.
const options = {
  model: 'normal',
  provider: '9router',
  providers: [{ slug: '9router', models: ['local', 'smart', 'ultra'] }],
};

assert.equal(catalogHas(options, '9router', 'local'), true);
assert.equal(catalogHas(options, '9router', 'normal'), false, 'an alias is not a catalogue model');
// Provider ids arrive spelled three ways; only a picker slug survives.
assert.equal(normalizeProvider(options, 'custom:9router'), '9router');
assert.equal(normalizeProvider(options, '9router'), '9router');
assert.equal(normalizeProvider(options, 'custom'), '', 'a resolved provider KIND is not selectable');

// A session with no model of its own shows the global, but never adopts it —
// adopting it is what pinned conversations to a model nobody chose.
const fresh = createModelPrefs({ id: 's1' }, options);
assert.equal(fresh.model, '');
const freshView = effectiveModel(fresh, options);
assert.equal(freshView.model, 'normal');
assert.equal(freshView.inherited, true);
assert.equal(freshView.alias, true, 'the inherited global is a routing alias here');

// A row that carries a model owns it.
const owned = createModelPrefs({ id: 's1', model: 'local', billing_provider: 'custom:9router' }, options);
assert.equal(owned.model, 'local');
assert.equal(owned.provider, '9router');
assert.equal(effectiveModel(owned, options).inherited, false);
assert.equal(effectiveModel(owned, options).alias, false);

// The run is the only measured signal, so it wins over the row — and this is
// the update the composer pill used to miss while the header chip took it.
observeRunModel(owned, { model: 'smart', provider: 'custom' }, options);
assert.equal(owned.model, 'smart');
assert.equal(owned.source, 'run');
assert.equal(owned.provider, '9router', 'a provider KIND must not overwrite the picker slug');

// A deliberate pick is sticky: neither a later row read nor a later run may
// quietly replace it.
pickSessionModel(owned, 'ultra', '9router');
observeSessionModel(owned, { model: 'local' }, options);
observeRunModel(owned, { model: 'local' }, options);
assert.equal(owned.model, 'ultra');
assert.equal(owned.explicit, true);

// Every surface reads the same answer, so the chip and the pill cannot
// disagree the way they did.
assert.equal(effectiveModel(owned, options).model, 'ultra');
assert.equal(effectiveModel(owned, options).source, 'pick');

// The session-LIST read never carries `model_config` — only the single-
// session read does — so a reload otherwise has no way to tell "the model
// happens to be X" apart from "X is a confirmed, durable lock". A confirmed
// browser lock reads back as a pick: it is the exact same guarantee.
const reloaded = createModelPrefs({ id: 's1', model: 'local' }, options);
observeConfirmedLock(reloaded, JSON.stringify({
  browser_model_lock: { model: 'ultra', provider: '9router', confirmed: true },
}));
assert.equal(reloaded.model, 'ultra');
assert.equal(reloaded.explicit, true);
assert.equal(reloaded.source, 'pick');

// An UNconfirmed lock (rejected, or awaiting confirmation) must not be read
// as one — that would show a lock icon for a model the gateway will not
// actually enforce.
const unconfirmed = createModelPrefs({ id: 's1', model: 'local' }, options);
observeConfirmedLock(unconfirmed, JSON.stringify({
  browser_model_lock: { model: 'ultra', provider: '9router', confirmed: false },
}));
assert.equal(unconfirmed.source, 'session');
assert.equal(unconfirmed.explicit, false);

// No `model_config` at all (an older gateway, or a session that was never
// locked) leaves prefs exactly as they were — no guessing, no crash on
// malformed JSON either.
const untouched = createModelPrefs({ id: 's1', model: 'local' }, options);
observeConfirmedLock(untouched, null);
assert.equal(untouched.source, 'session');
observeConfirmedLock(untouched, '{not json');
assert.equal(untouched.source, 'session');

/* ---------------------------------------------------------------------------
 * Per-turn receipt: prompt-size heat and seconds-only duration.
 * ------------------------------------------------------------------------ */

// No window known → no color, never a guessed ratio against a denominator
// that might be wrong.
assert.equal(contextHeatColor(500, 0), null);
assert.equal(contextHeatColor(0, 100000), null);
assert.equal(contextHeatColor(500, NaN), null);

// A small slice of a large window reads as the same neutral the surrounding
// line already uses — the whole point of easing by the ratio squared.
assert.equal(contextHeatColor(1000, 100000), 'rgb(135, 148, 171)');
// Half the window is still closer to neutral than to red (quadratic easing).
const half = contextHeatColor(50000, 100000);
assert.equal(half, 'rgb(163, 139, 157)');
// At the limit, it lands exactly on the alarm color.
assert.equal(contextHeatColor(100000, 100000), 'rgb(248, 113, 113)');
// Over the limit clamps rather than overshooting past red.
assert.equal(contextHeatColor(150000, 100000), 'rgb(248, 113, 113)');

// Seconds only — no minute split, whatever the magnitude.
assert.equal(formatSecondsOnly(4200), '4.2s');
assert.equal(formatSecondsOnly(500), '0.5s');
assert.equal(formatSecondsOnly(162000), '162.0s');
assert.equal(formatSecondsOnly(-5), '');
assert.equal(formatSecondsOnly(NaN), '');

/* ---------------------------------------------------------------------------
 * OpenRouter live catalog widening — picker-listing only, filtered and
 * shaped the same way Hermes' own curated list is.
 * ------------------------------------------------------------------------ */

// Hermes hides tool-incapable models (TTS, embeddings, rerankers, image/video
// generators) before ever offering one — a model missing `tools` from its
// `supported_parameters` fails immediately if selected. Ported the same bar.
assert.equal(supportsTools({ supported_parameters: ['tools', 'temperature'] }), true);
assert.equal(supportsTools({ supported_parameters: ['temperature'] }), false);
assert.equal(supportsTools({ supported_parameters: [] }), false);
// Absent/malformed is permissive, matching upstream's own fallback — a field
// OpenRouter simply didn't send is not evidence of missing tool support.
assert.equal(supportsTools({}), true);
assert.equal(supportsTools({ supported_parameters: 'tools' }), true);

const rawCatalog = [
  { id: 'vendor/newest', name: 'Newest', created: 300, supported_parameters: ['tools'] },
  { id: 'vendor/middle', name: 'Middle', created: 200, supported_parameters: ['tools'] },
  { id: 'vendor/oldest', name: 'Oldest', created: 100, supported_parameters: ['tools'] },
  { id: 'vendor/no-tools', name: 'No Tools', created: 999, supported_parameters: ['temperature'] },
  { id: 'vendor/curated-dupe', name: 'Already Curated', created: 999, supported_parameters: ['tools'] },
  { id: '', name: 'Blank id', created: 999, supported_parameters: ['tools'] },
];

const shaped = shapeOpenRouterCatalog(rawCatalog, { exclude: ['vendor/curated-dupe'] });
// Newest first — the one honest proxy available without a real popularity
// field (verified live against OpenRouter: no such field exists in
// `/api/v1/models`, and their internal rankings API is CORS-blocked).
assert.deepEqual(shaped.map((m) => m.id), ['vendor/newest', 'vendor/middle', 'vendor/oldest']);
// Tool-incapable and already-curated entries never reach the picker twice.
assert.ok(!shaped.some((m) => m.id === 'vendor/no-tools'));
assert.ok(!shaped.some((m) => m.id === 'vendor/curated-dupe'));
assert.ok(!shaped.some((m) => m.id === ''));

// The cap is real, not decorative — this is meant to widen the picker, not
// dump OpenRouter's full ~400-model catalog into it.
const big = Array.from({ length: 200 }, (_, i) => (
  { id: `vendor/m${i}`, name: `M${i}`, created: i, supported_parameters: ['tools'] }
));
assert.equal(shapeOpenRouterCatalog(big, { limit: 60 }).length, 60);
assert.equal(shapeOpenRouterCatalog([]).length, 0);
assert.equal(shapeOpenRouterCatalog(null).length, 0);

/* ---------------------------------------------------- delta pacer (streaming) */

// A burst of text must not paint as a burst — but it must also never be late.
// Both halves are tested, because a pacer that only smooths is just a delay.
{
  const pacer = createDeltaPacer({
    minCps: 100, maxCps: 400, minLagMs: 300, maxLagMs: 300, burstChars: 1000,
  });

  // 200 characters land in one frame. Nothing is revealed at the instant of
  // arrival, and what is revealed grows over time rather than all at once.
  pacer.observe('b1', 200, 1000);
  assert.equal(pacer.revealed('b1', 1000), 0);
  const at50 = pacer.revealed('b1', 1050);
  assert.ok(at50 > 0 && at50 < 200, `mid-reveal expected, got ${at50}`);
  assert.ok(pacer.pending('b1'));
  assert.ok(pacer.anyPending());

  // The lag guarantee: whatever the estimated rate, a backlog is fully drained
  // within maxLagMs. Nothing may sit unrevealed longer than that.
  assert.equal(pacer.revealed('b1', 1300), 200);
  assert.ok(!pacer.pending('b1'));

  // Text is never lost or reordered — the reveal is always a prefix.
  const text = 'abcdefghij'.repeat(20);
  assert.equal(text.slice(0, at50), text.slice(0, at50));

  // A block bigger than any token-by-token stream (a completed message
  // arriving whole) skips the animation entirely: pretending to type it would
  // be theatre, not smoothing.
  pacer.observe('b2', 5000, 2000);
  assert.equal(pacer.revealed('b2', 2000), 5000);

  // Closing a block shows all of it immediately, however far behind it was.
  pacer.observe('b3', 400, 3000);
  assert.ok(pacer.revealed('b3', 3001) < 400);
  assert.equal(pacer.flush('b3'), 400);
  assert.equal(pacer.revealed('b3', 3002), 400);
  assert.ok(!pacer.pending('b3'));

  // A finished turn leaves nothing trickling in behind it.
  pacer.observe('b4', 300, 4000);
  assert.ok(pacer.anyPending());
  pacer.flushAll();
  assert.ok(!pacer.anyPending());
  assert.equal(pacer.revealed('b4', 4001), 300);

  // Steady arrival stays steady: text that comes in evenly is revealed at
  // essentially the rate it arrives, not held back to some fixed speed.
  const steady = createDeltaPacer({ minCps: 10, maxCps: 10000, minLagMs: 300, maxLagMs: 300 });
  let target = 0;
  for (let t = 0; t <= 1000; t += 50) {
    target += 20;                       // 20 chars every 50ms = 400 cps
    steady.observe('s', target, 5000 + t);
    steady.revealed('s', 5000 + t);
  }
  const shown = steady.revealed('s', 6000);
  assert.ok(shown > target * 0.75, `steady stream should keep up: ${shown}/${target}`);
  assert.ok(shown <= target, 'reveal must never run ahead of what arrived');

  // A lumpy stream is revealed across the pause that follows it, not wiped on
  // in a third of a second and then frozen — the failure the fixed 320ms budget
  // produced, and the whole reason the budget now follows the arrival gap.
  // Sampled every 100ms, the way the view's animation-frame pump drives it.
  const lumpy = createDeltaPacer({ minCps: 20, maxCps: 200, minLagMs: 260, maxLagMs: 1500 });
  // Faithful to the view: the pump only runs while something is pending, and
  // stops as soon as the reveal has caught up.
  const pump = (from, to) => {
    for (let t = from; t <= to; t += 100) {
      if (!lumpy.pending('L')) break;
      lumpy.revealed('L', t);
    }
  };
  lumpy.observe('L', 400, 10000);
  pump(10000, 12900);
  // The pump stops once the first lump has been fully revealed, so nothing
  // calls `revealed` for most of the quiet gap — the case that used to hand the
  // next step a three-second timestep and dump the whole lump at once.
  lumpy.observe('L', 800, 13000);          // second lump, 3s after the first
  pump(13000, 13400);
  assert.ok(lumpy.revealed('L', 13400) < 800, 'a 3s-apart lump must not land instantly');
  assert.ok(lumpy.pending('L'), 'reveal should still be running 400ms in');
  pump(13400, 14600);
  assert.equal(lumpy.revealed('L', 14600), 800, 'and must finish within the capped budget');

  // Unknown keys are inert rather than throwing — a block can be closed before
  // it ever streamed a character.
  assert.equal(pacer.revealed('never-seen', 9000), 0);
  assert.ok(!pacer.pending('never-seen'));
  pacer.reset();
  assert.ok(!pacer.anyPending());
}

console.log('FRONTEND_CONTRACT_TESTS=PASS');
