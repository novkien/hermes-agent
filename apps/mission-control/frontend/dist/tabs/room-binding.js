// Room Binding tab — which task is sitting in which room slot, right now.
//
// Two halves that used to be one guess-shaped blob: the *static* slot layout
// comes from config.yaml (`telegram.extra.room_slots` — which ceo/coder/
// research/system thread makes up each of the five slots), and the *live*
// occupancy comes from the session-injector plugin's own SQLite state, read
// read-only by the adapter.
//
// There is deliberately no unbind control. Real unbind runs on the live gateway
// event loop inside that plugin and has no safe control surface, so this tab
// monitors and links out rather than pretending to steer.

import { el, clear, statusChip, iconButton, fmtTime, fmtAge } from '../ui.js';
import { icon } from '../icons.js';
import { createTable } from '../components/table.js';
import { createDetail } from '../components/detail.js';
import { createStatRow } from '../components/stat.js';
import {
  loadEnvelope, tabToolbar, sideHint, paint,
} from './_kit.js';

export const ROUTE = 'room-binding';
export const LABEL = 'Room Binding';
export const GROUP = 'GOVERN';
export const READ_ONLY_NOTE = 'monitoring only — bind/unbind stays with the session-injector plugin';

export const SOURCE_ENDPOINTS = Object.freeze(['/api/adapter/room-binding']);

/** The four agent seats every room slot is made of, in escalation order. */
export const SEAT_ROLES = Object.freeze(['ceo', 'coder', 'research', 'system']);

const STATUS_TONE = { active: 'ok', bound: 'ok', pending: 'warn', releasing: 'warn', released: 'idle', error: 'danger' };

/**
 * Pure: join the static slot layout with live occupancy into one row per slot.
 *
 * A slot with no live binding is *free*, not missing — that distinction is the
 * whole point of the view, so an unoccupied slot still gets a row.
 */
export function slotRows(payload) {
  const data = payload && typeof payload === 'object' ? payload : {};
  const slots = Array.isArray(data.room_slots) ? data.room_slots : [];
  const occupancy = Array.isArray(data.live_occupancy) ? data.live_occupancy : [];
  const reservations = Array.isArray(data.reservations) ? data.reservations : [];

  const bindingBySlot = new Map();
  for (const binding of occupancy) {
    const key = String(binding?.room_slot ?? '');
    if (!bindingBySlot.has(key)) bindingBySlot.set(key, binding);
  }
  const reservationBySlot = new Map();
  for (const reservation of reservations) {
    const key = String(reservation?.room_slot ?? '');
    if (!reservationBySlot.has(key)) reservationBySlot.set(key, reservation);
  }

  return slots.map((slot) => {
    const key = String(slot?.slot ?? '');
    const binding = bindingBySlot.get(key) || null;
    const reservation = reservationBySlot.get(key) || null;
    return {
      id: key,
      slot: slot?.slot ?? null,
      seats: SEAT_ROLES.map((role) => ({
        role,
        thread_id: slot?.[`${role}_thread_id`] ?? null,
      })).filter((seat) => seat.thread_id !== null),
      occupied: Boolean(binding),
      task_id: binding?.task_id ?? null,
      chat_id: binding?.chat_id ?? null,
      status: binding?.status ?? null,
      bound_at: binding?.bound_at ?? null,
      updated_at: binding?.updated_at ?? null,
      origin_session_key: binding?.origin_session_key ?? null,
      origin_thread_id: binding?.origin_thread_id ?? null,
      terminal_request_id: binding?.terminal_request_id ?? null,
      reserved_by: reservation?.requester_session_key ?? null,
      reserved_task: reservation?.task_id ?? null,
      reservation_expires: reservation?.expires_at ?? null,
      raw: { slot, binding, reservation },
    };
  });
}

/** Pure: normalize the room-binding envelope into render shape. */
export function renderRoomBinding(envelope) {
  const meta = (envelope && envelope.meta) || null;
  const raw = (envelope && envelope.data) || null;
  if (!meta) return { rows: [], meta: null, state: 'unavailable', payload: null };
  if (meta.freshness === 'unavailable') return { rows: [], meta, state: 'unavailable', payload: null };
  if (meta.freshness === 'unsupported') return { rows: [], meta, state: 'unsupported', payload: null };
  if (!raw) return { rows: [], meta, state: 'empty', payload: null };
  const rows = slotRows(raw);
  return { rows, meta, state: rows.length ? 'ready' : 'empty', payload: raw };
}

export function createRoomBinding({ api, profile, sse, toolbar, onNavigate: navigate }) {
  const root = el('div', { class: 'tab tab-room-binding' });
  const stats = el('div', { class: 'stat-row-host' });
  const banner = el('div');
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(stats, banner, main);

  let rows = [];
  let payload = null;
  let meta = null;
  let selected = null;
  let unsubscribe = null;

  const table = createTable({
    rowId: (row) => row.id,
    emptyTitle: 'No room slots configured',
    emptyNote: 'Room slots come from telegram.extra.room_slots in config.yaml.',
    sort: { key: 'slot', dir: 'asc' },
    columns: [
      {
        key: 'slot',
        label: 'Slot',
        width: '70px',
        sortable: true,
        sortValue: (row) => Number(row.slot) || 0,
        render: (row) => el('span', { class: 'cell-strong mono', text: `#${row.slot}` }),
      },
      {
        key: 'occupied',
        label: 'State',
        width: '110px',
        sortable: true,
        render: (row) => (row.occupied
          ? statusChip(STATUS_TONE[row.status] || 'ok', row.status || 'bound')
          : row.reserved_task
            ? statusChip('warn', 'reserved')
            : statusChip('idle', 'free')),
      },
      {
        key: 'task_id',
        label: 'Task',
        sortable: true,
        render: (row) => {
          const taskId = row.task_id || row.reserved_task;
          if (!taskId) return el('span', { class: 'cell-dim', text: '—' });
          return el('div', { class: 'cell-stack' }, [
            el('span', { class: 'cell-strong mono', text: String(taskId) }),
            row.origin_session_key
              ? el('span', { class: 'cell-dim mono', text: row.origin_session_key })
              : null,
          ].filter(Boolean));
        },
      },
      {
        key: 'seats',
        label: 'Threads',
        width: '210px',
        render: (row) => el('div', { class: 'inline-chips' }, row.seats.map((seat) => el('span', {
          class: 'chip',
          title: `${seat.role} thread ${seat.thread_id}`,
          text: `${seat.role[0].toUpperCase()}${seat.thread_id}`,
        }))),
      },
      {
        key: 'bound_at',
        label: 'Held for',
        width: '110px',
        sortable: true,
        render: (row) => (row.bound_at
          ? el('span', { text: fmtAge(row.bound_at) })
          : el('span', { class: 'cell-dim', text: '—' })),
      },
    ],
    rowClass: (row) => (row.occupied && row.status === 'error' ? 'row-danger' : ''),
    rowActions: (row) => [
      row.task_id && navigate
        ? iconButton({
          icon: 'link',
          label: `Open task ${row.task_id}`,
          onClick: () => navigate('kanban', { task: row.task_id }),
        })
        : null,
    ].filter(Boolean),
    onSelect: (row) => { selected = row; renderSide(); },
  });
  main.append(table.node);

  async function load() {
    table.setLoading();
    const result = await loadEnvelope(api, '/api/adapter/room-binding', { profile, allowEmpty: false });
    meta = result.meta;
    if (result.state !== 'ready') {
      rows = [];
      payload = null;
      if (result.state === 'unsupported') table.setUnsupported({ title: 'Room binding not exposed', reason: result.reason });
      else table.setUnavailable({ reason: result.reason || 'Adapter unavailable' });
      clear(stats);
      clear(banner);
      renderToolbar(toolbar);
      renderSide();
      return;
    }
    payload = result.data || {};
    rows = slotRows(payload);
    const wanted = selected?.id ?? null;
    selected = (wanted && rows.find((r) => r.id === wanted)) || null;
    table.setRows(rows);
    table.setSelected(selected?.id ?? null);
    renderStats();
    renderBanner();
    renderToolbar(toolbar);
    renderSide();
  }

  function renderStats() {
    clear(stats);
    const occupied = rows.filter((r) => r.occupied).length;
    const oldest = rows
      .filter((r) => r.bound_at)
      .map((r) => r.bound_at)
      .sort()[0];
    stats.append(createStatRow([
      { label: 'Slots', value: String(rows.length), iconName: 'room-binding', seriesIndex: 1 },
      { label: 'Occupied', value: String(occupied), iconName: 'kanban', seriesIndex: 2 },
      { label: 'Free', value: String(rows.length - occupied), iconName: 'check', seriesIndex: 3 },
      { label: 'Reserved', value: String(rows.filter((r) => !r.occupied && r.reserved_task).length), iconName: 'alerts', seriesIndex: 4 },
      {
        label: 'Longest hold',
        value: oldest ? fmtAge(oldest) : '—',
        iconName: 'run-inspector',
        seriesIndex: 5,
        foot: oldest ? fmtTime(oldest) : '',
      },
    ]));
  }

  function renderBanner() {
    clear(banner);
    // `occupancy_available: false` means the plugin's state DB was missing or
    // locked. The slot layout is still correct, but "free" would be a lie.
    if (payload && payload.occupancy_available === false) {
      banner.append(el('div', { class: 'notice notice-warn' }, [
        el('div', { class: 'notice-title', text: 'Live occupancy unavailable' }),
        el('div', {
          class: 'notice-note',
          text: payload.occupancy_note
            || 'The session-injector state database could not be read, so slots below show layout only.',
        }),
      ]));
    }
  }

  function renderToolbar(host) {
    if (!host) return;
    const occupied = rows.filter((r) => r.occupied).length;
    paint(host, tabToolbar({
      title: 'Room Binding',
      subtitle: rows.length ? `${occupied} of ${rows.length} slots in use` : '',
      meta,
      onRefresh: () => load(),
      actions: [el('span', { class: 'chip chip-info', text: 'monitoring only' })],
    }));
  }

  function seatSection(row) {
    if (!row.seats.length) return el('div', { class: 'field-hint', text: 'No threads configured for this slot.' });
    return el('div', { class: 'stack-sm' }, row.seats.map((seat) => el('div', { class: 'choice-row' }, [
      el('div', { class: 'cell-stack' }, [
        el('span', { class: 'cell-strong', text: seat.role }),
        el('span', { class: 'cell-dim mono', text: `thread ${seat.thread_id}` }),
      ]),
      navigate
        ? iconButton({
          icon: 'chevron-right',
          label: `Open thread ${seat.thread_id} policy`,
          onClick: () => navigate('threads', { thread: String(seat.thread_id) }),
        })
        : null,
    ].filter(Boolean))));
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (!selected) {
      paint(inspectorHost, sideHint('Select a slot', [
        'A room slot is a set of four agent threads (ceo, coder, research, system) that a task borrows as a unit.',
        'The layout comes from config.yaml; whether a slot is actually held right now comes from the session-injector plugin.',
        'There is no unbind button on purpose — release runs inside the plugin on the live gateway loop.',
      ]));
      return;
    }

    const row = selected;
    paint(inspectorHost, createDetail({
      title: `Room slot ${row.slot}`,
      meta,
      chips: [
        row.occupied
          ? statusChip(STATUS_TONE[row.status] || 'ok', row.status || 'bound')
          : statusChip('idle', 'free'),
        row.reserved_task ? statusChip('warn', 'reserved') : null,
      ].filter(Boolean),
      fields: [
        { label: 'Task', value: row.task_id, mono: true },
        { label: 'Chat', value: row.chat_id, mono: true },
        { label: 'Origin session', value: row.origin_session_key, mono: true },
        { label: 'Origin thread', value: row.origin_thread_id, mono: true },
        { label: 'Bound at', value: row.bound_at ? fmtTime(row.bound_at) : null, mono: true },
        { label: 'Updated', value: row.updated_at ? fmtTime(row.updated_at) : null, mono: true },
        { label: 'Terminal request', value: row.terminal_request_id, mono: true },
        { label: 'Reserved by', value: row.reserved_by, mono: true },
        { label: 'Reservation expires', value: row.reservation_expires ? fmtTime(row.reservation_expires) : null, mono: true },
      ],
      sections: [{ title: 'Seats', node: seatSection(row) }],
      relations: row.task_id && navigate
        ? [{ label: 'Task', text: String(row.task_id), onClick: () => navigate('kanban', { task: row.task_id }) }]
        : [],
      actions: [
        el('div', { class: 'field-hint' }, [
          icon('lock', { size: 11 }),
          ' Release is owned by the session-injector plugin and is not proxied.',
        ]),
      ],
      raw: row.raw,
    }));
  }

  function bindEvents() {
    if (!sse || unsubscribe) return;
    unsubscribe = sse.on('room_binding.changed', () => {
      if (!root.isConnected) return;
      load().catch(() => null);
    });
  }

  renderSide();

  return {
    mount(container) { clear(container); container.append(root); },
    renderInspector,
    activate() { bindEvents(); return load(); },
    deactivate() {
      if (unsubscribe) { unsubscribe(); unsubscribe = null; }
      return { selection: selected?.id ?? null };
    },
    refresh: load,
    renderToolbar,
    get data() { return rows; },
  };
}
