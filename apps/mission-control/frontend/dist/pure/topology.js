// Fleet topology model — pure, DOM-free, so the contract tests can import it.
//
// The old Fleet tab invented its own node shapes (`profiles[].role`,
// `rooms[].slot_id`) that no upstream ever returns, so it rendered a chip list
// of nothing. This module builds the topology out of joins that are actually
// present in the live data:
//
//   room chat  ──> slot (1-5)  ──> seat (ceo|coder|research|system, a thread id)
//                                    └─> session  (chat_id + thread_id match)
//                                          └─> task (tasks.session_id)
//
// Every edge above is a recorded field, not an inference. Anything that cannot
// be joined is reported as detached rather than being attached to a guess.

export const SEAT_ROLES = Object.freeze(['ceo', 'coder', 'research', 'system']);

/** Services the capability registry probes, in control-plane order. */
export const SERVICE_ORDER = Object.freeze([
  'hermes-gateway', 'hermes-dashboard', 'adapter', 'cron',
]);

function num(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** Pure: normalized service nodes from the capability registry payload. */
export function serviceNodes(capabilities) {
  const sources = capabilities && typeof capabilities === 'object' ? capabilities : {};
  const names = [
    ...SERVICE_ORDER.filter((name) => name in sources),
    ...Object.keys(sources).filter((name) => !SERVICE_ORDER.includes(name)),
  ];
  return names.map((name) => {
    const entry = sources[name] || {};
    return {
      id: name,
      label: name.replace(/^hermes-/, ''),
      healthy: entry.healthy === true,
      unknown: entry.healthy === undefined || entry.healthy === null,
      checkedAt: entry.last_checked_at ?? null,
      routes: Array.isArray(entry.routes_checked) ? entry.routes_checked : [],
    };
  });
}

/**
 * Pure: the room → slot → seat → session → task tree.
 *
 * A seat holds at most one session in the model even though the room chat
 * accumulates many over time: the newest one is the occupant, older ones are
 * counted as `history` so the UI can say "4 previous" without drawing four
 * dead edges.
 */
export function buildTopology({ rooms, sessions, tasks, threadSessions, roomCards } = {}) {
  const roomPayload = rooms && typeof rooms === 'object' ? rooms : {};
  const slots = Array.isArray(roomPayload.room_slots) ? roomPayload.room_slots : [];
  const roomChatId = roomPayload.room_chat_id != null ? String(roomPayload.room_chat_id) : null;
  const sessionList = Array.isArray(sessions) ? sessions : [];
  const taskList = Array.isArray(tasks) ? tasks : [];

  // thread id -> seat coordinates, built from the slot table itself.
  const seatByThread = new Map();
  for (const slot of slots) {
    for (const role of SEAT_ROLES) {
      const thread = slot[`${role}_thread_id`];
      if (thread === undefined || thread === null) continue;
      seatByThread.set(String(thread), { slot: slot.slot, role, thread: String(thread) });
    }
  }

  // A card records the session that created it, and a manager thread runs
  // hundreds of sessions across its resets — the coder seat on this deployment
  // has created 205 cards from a thread whose live tip accounts for four of
  // them. Attributing by session id alone therefore loses almost everything a
  // manager ever made. `threadSessions` (the adapter's `room-sessions?history=1`
  // id -> thread_id pairs) is what closes that gap; without it this degrades to
  // the live sessions, which is the old behaviour.
  const threadOfSession = new Map();
  for (const session of sessionList) {
    if (session.id != null && session.thread_id != null) {
      threadOfSession.set(String(session.id), String(session.thread_id));
    }
  }
  for (const row of Array.isArray(threadSessions) ? threadSessions : []) {
    if (row && row.id != null && row.thread_id != null) {
      threadOfSession.set(String(row.id), String(row.thread_id));
    }
  }

  // The adapter's own attribution, when it is available: exact per-thread
  // counts plus the newest few cards, computed where both the kanban boards and
  // state.db live. The client-side grouping below stays as the fallback for an
  // adapter that predates that route.
  const roomCardCounts = roomCards && typeof roomCards.counts === 'object' ? roomCards.counts : null;
  const roomCardRows = roomCards && typeof roomCards.cards === 'object' ? roomCards.cards : null;

  // Cards grouped by the SEAT their creating session sat on, newest first.
  const tasksBySeat = new Map();
  for (const task of taskList) {
    const sessionKey = task.session_id ? String(task.session_id) : null;
    if (!sessionKey) continue;
    const seat = seatByThread.get(threadOfSession.get(sessionKey));
    if (!seat) continue;
    const key = `${seat.slot}:${seat.role}`;
    if (!tasksBySeat.has(key)) tasksBySeat.set(key, []);
    tasksBySeat.get(key).push(task);
  }
  for (const list of tasksBySeat.values()) {
    list.sort((a, b) => num(b.created_at) - num(a.created_at));
  }

  // Sessions grouped onto seats, newest first.
  const sessionsBySeat = new Map();
  const detachedSessions = [];
  for (const session of sessionList) {
    const chatId = session.chat_id != null ? String(session.chat_id) : null;
    const threadId = session.thread_id != null ? String(session.thread_id) : null;
    const seat = chatId && roomChatId && chatId === roomChatId && threadId
      ? seatByThread.get(threadId)
      : null;
    if (!seat) {
      detachedSessions.push(session);
      continue;
    }
    const key = `${seat.slot}:${seat.role}`;
    if (!sessionsBySeat.has(key)) sessionsBySeat.set(key, []);
    sessionsBySeat.get(key).push(session);
  }
  for (const list of sessionsBySeat.values()) {
    list.sort((a, b) => num(b.last_activity_at || b.started_at) - num(a.last_activity_at || a.started_at));
  }

  const slotNodes = slots.map((slot) => {
    const seats = SEAT_ROLES.map((role) => {
      const thread = slot[`${role}_thread_id`];
      const key = `${slot.slot}:${role}`;
      const found = sessionsBySeat.get(key) || [];
      const occupant = found[0] || null;
      const threadKey = thread === undefined || thread === null ? null : String(thread);
      const fromRoom = roomCardRows && threadKey ? roomCardRows[threadKey] : null;
      const seatTasks = Array.isArray(fromRoom) ? fromRoom : (tasksBySeat.get(key) || []);
      // The drawn cards are a window on the newest; the count is the whole
      // stack. Reporting `seatTasks.length` as the total is what made a manager
      // with 214 cards claim it had four.
      const seatTaskTotal = roomCardCounts && threadKey && roomCardCounts[threadKey] != null
        ? num(roomCardCounts[threadKey])
        : seatTasks.length;
      const running = seatTasks.filter((task) => task.status === 'in_progress' || task.status === 'running');
      return {
        id: key,
        slot: slot.slot,
        role,
        thread: thread === undefined || thread === null ? null : String(thread),
        session: occupant,
        history: Math.max(0, found.length - 1),
        tasks: seatTasks,
        taskTotal: seatTaskTotal,
        running: running.length,
        // "Live" means the session is flagged active upstream, not merely that
        // a session row exists — an ended session still occupies the thread.
        live: Boolean(occupant && occupant.is_active),
        lastActivity: occupant ? (occupant.last_activity_at || occupant.started_at || null) : null,
      };
    });
    return {
      id: `slot:${slot.slot}`,
      slot: slot.slot,
      seats,
      occupied: seats.filter((seat) => seat.session).length,
      live: seats.filter((seat) => seat.live).length,
      tasks: seats.reduce((total, seat) => total + seat.taskTotal, 0),
    };
  });

  // An orphan is a card no seat claimed: no creating session recorded, or one
  // that never sat on a room thread (a worker or a delegate created it).
  const claimed = new Set();
  for (const list of tasksBySeat.values()) for (const task of list) claimed.add(task);
  const orphanTasks = taskList.filter((task) => !claimed.has(task));

  return {
    roomChatId,
    slots: slotNodes,
    detachedSessions,
    orphanTasks,
    totals: {
      slots: slotNodes.length,
      seats: slotNodes.length * SEAT_ROLES.length,
      occupied: slotNodes.reduce((total, slot) => total + slot.occupied, 0),
      live: slotNodes.reduce((total, slot) => total + slot.live, 0),
      tasks: slotNodes.reduce((total, slot) => total + slot.tasks, 0),
    },
  };
}

/** The three sub-roles that sit under the CEO in the org-chart view. */
export const MANAGER_ROLES = Object.freeze(['coder', 'research', 'system']);

function normalizedName(topic) {
  return typeof topic?.name === 'string' ? topic.name.trim() : '';
}

function isDeveloperTopic(topic) {
  return /-\s*Developer$/i.test(normalizedName(topic));
}

/** 'lab' | 'comfyui' | null — the handful of singleton branches that hang off
 * the room directly rather than under any slot's CEO. Matched by name because
 * the config carries no explicit "kind" field for these. */
function classifySingletonTopic(topic) {
  const name = normalizedName(topic);
  if (/^skill lab$/i.test(name) || /^system prompt lab$/i.test(name)) return 'lab';
  if (/comfyui/i.test(name) && /manager/i.test(name)) return 'comfyui';
  return null;
}

/** A developer topic's owning manager role, if the config ever states one
 * explicitly; otherwise every developer defaults to 'system', matching how
 * the current room config actually assigns them. */
function developerManagerRole(topic) {
  const explicit = topic?.manager_role || topic?.parent_role || topic?.assigned_manager;
  if (typeof explicit === 'string' && MANAGER_ROLES.includes(explicit.toLowerCase())) {
    return explicit.toLowerCase();
  }
  return 'system';
}

/**
 * Pure: the room → CEO → {coder, research, system} manager → {developers,
 * kanban cards} org-chart, built on top of `buildTopology`'s seat/session/task
 * joins.
 *
 * `topics` is optional and comes from a richer config catalog (name, skills,
 * thread_id, cross_thread) that today's `/room-binding` payload may or may
 * not include. When it's absent every seat still renders — labels just fall
 * back to the plain role name, and there are no developer/lab/comfyui
 * branches to draw. Nothing here throws or leaves a gap when the extra data
 * is missing; it draws less, not incorrectly.
 */
export function buildOrgChart({ rooms, sessions, tasks, topics, threadSessions, roomCards } = {}) {
  const base = buildTopology({ rooms, sessions, tasks, threadSessions, roomCards });
  const topicList = Array.isArray(topics) ? topics : [];
  const roomPayload = rooms && typeof rooms === 'object' ? rooms : {};

  // Room task binding: a root task handed to the CEO entry route takes a slot
  // for its whole lifecycle, so the task id is the slot's correlation key.
  // `live_occupancy` only holds tasks in flight, which is empty between tasks;
  // `recent_bindings` carries the last task each slot ran so a slot still
  // reports what it was doing. Live always wins over recent.
  const bindingBySlot = new Map();
  for (const row of Array.isArray(roomPayload.recent_bindings) ? roomPayload.recent_bindings : []) {
    if (row && row.room_slot !== undefined && row.room_slot !== null) {
      bindingBySlot.set(Number(row.room_slot), { ...row, live: false });
    }
  }
  for (const row of Array.isArray(roomPayload.live_occupancy) ? roomPayload.live_occupancy : []) {
    if (row && row.room_slot !== undefined && row.room_slot !== null) {
      bindingBySlot.set(Number(row.room_slot), { ...row, live: true });
    }
  }

  const topicByThread = new Map();
  for (const topic of topicList) {
    if (topic && topic.thread_id !== undefined && topic.thread_id !== null) {
      topicByThread.set(String(topic.thread_id), topic);
    }
  }

  const seatThreadIds = new Set();
  for (const slot of base.slots) {
    for (const seat of slot.seats) if (seat.thread) seatThreadIds.add(seat.thread);
  }

  const developerTopics = [];
  const singletons = { lab: [], comfyui: [] };
  const unclassified = [];
  for (const topic of topicList) {
    const threadId = topic?.thread_id != null ? String(topic.thread_id) : null;
    if (threadId && seatThreadIds.has(threadId)) continue; // already shown as a CEO/manager seat
    if (isDeveloperTopic(topic)) { developerTopics.push(topic); continue; }
    const kind = classifySingletonTopic(topic);
    if (kind) { singletons[kind].push(topic); continue; }
    unclassified.push(topic);
  }

  const developersByRole = { coder: [], research: [], system: [] };
  for (const topic of developerTopics) {
    developersByRole[developerManagerRole(topic)].push(topic);
  }

  // The "canonical" slot for a manager role is whichever slot's seat topic
  // actually carries `cross_thread` — i.e. the one real entity that oversees
  // the same-role seat in every other slot. Developers attach only there, so
  // they don't get drawn five times over. If no topic data says which slot
  // that is, nothing attaches — no guess.
  const canonicalSlotForRole = {};
  for (const role of MANAGER_ROLES) {
    for (const slot of base.slots) {
      const seat = slot.seats.find((s) => s.role === role);
      const topic = seat?.thread ? topicByThread.get(seat.thread) : null;
      if (topic && Array.isArray(topic.cross_thread) && topic.cross_thread.length) {
        canonicalSlotForRole[role] = slot.slot;
        break;
      }
    }
  }

  const slotTrees = base.slots.map((slot) => {
    const ceo = slot.seats.find((s) => s.role === 'ceo') || null;
    const managers = MANAGER_ROLES.map((role) => {
      const seat = slot.seats.find((s) => s.role === role) || null;
      const topic = seat?.thread ? topicByThread.get(seat.thread) || null : null;
      const developers = canonicalSlotForRole[role] === slot.slot ? developersByRole[role] : [];
      return {
        role,
        seat,
        topic,
        developers,
        // `cards` is the drawable window; `cardTotal` is how many exist.
        cards: seat ? seat.tasks : [],
        cardTotal: seat ? seat.taskTotal : 0,
      };
    });
    const ceoTopic = ceo?.thread ? topicByThread.get(ceo.thread) || null : null;
    const binding = bindingBySlot.get(Number(slot.slot)) || null;
    return { slot: slot.slot, ceo, ceoTopic, binding, managers };
  });

  return {
    roomChatId: base.roomChatId,
    slotTrees,
    singletons,
    unclassified,
    detachedSessions: base.detachedSessions,
    orphanTasks: base.orphanTasks,
    totals: {
      ...base.totals,
      developers: developerTopics.length,
      labs: singletons.lab.length,
      comfyui: singletons.comfyui.length,
      unclassified: unclassified.length,
      boundSlots: [...bindingBySlot.values()].filter((b) => b.live).length,
    },
  };
}

/** Pure: profile rows for the fleet's profile lane. */
export function profileNodes(profiles) {
  const list = Array.isArray(profiles) ? profiles : (profiles?.profiles || []);
  return (Array.isArray(list) ? list : []).map((profile) => ({
    name: profile.name,
    isDefault: profile.is_default === true,
    running: profile.gateway_running === true,
    model: profile.model || null,
    provider: profile.provider || null,
    skills: num(profile.skill_count),
    description: profile.description || '',
  }));
}
