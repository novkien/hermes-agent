// Threads tab — per-thread agent policy for the Telegram forum Hermes runs on.
//
// This is the surface that decides what each agent thread can actually do:
// which toolsets it may call, which skills it preloads, and which other threads
// it is allowed to message (the cross-thread allowlist). It lived only in
// config.yaml, which is why the dashboard could show a fleet of agents but not
// explain — let alone change — what any of them were permitted to do.
//
// Config location is resolved, not assumed. Hermes' `toolset_policy._topic_extra`
// prefers `platforms.telegram.extra` when *that* dict already carries
// `group_topics`, and otherwise reads the legacy top-level `telegram.extra`.
// Writing to the wrong one creates a second copy that silently shadows the live
// config, so this tab reads the same precedence rule and writes back in place.

import { el, clear, statusChip, segmented, iconButton, fmtAge, fmtTime } from '../ui.js';
import { createTable } from '../components/table.js';
import { createDetail } from '../components/detail.js';
import { createForm } from '../components/form.js';
import { createStatRow } from '../components/stat.js';
import { recordFrom } from '../pure/data-shape.js';
import {
  loadEnvelope, tabToolbar, runMutation, sideHint, paint,
} from './_kit.js';

export const ROUTE = 'threads';
export const LABEL = 'Threads';
export const GROUP = 'GOVERN';

export const SOURCE_ENDPOINTS = Object.freeze(['/api/config', '/api/adapter/thread-sessions']);

/** Which config branch the gateway is actually reading `group_topics` from. */
export function resolveTopicExtraPath(config) {
  const typed = config?.platforms?.telegram?.extra;
  if (typed && typeof typed === 'object' && 'group_topics' in typed) {
    return ['platforms', 'telegram', 'extra'];
  }
  return ['telegram', 'extra'];
}

function atPath(root, path) {
  return path.reduce((node, key) => (node && typeof node === 'object' ? node[key] : undefined), root);
}

/** Build a minimal nested body `{a:{b:{key: value}}}` for the config PUT. */
export function nestBody(path, key, value) {
  const body = {};
  let cursor = body;
  for (const segment of path) {
    cursor[segment] = {};
    cursor = cursor[segment];
  }
  cursor[key] = value;
  return body;
}

/**
 * Pure: flatten `group_topics` into one row per thread, joined with the
 * per-thread channel override and the room slot it belongs to.
 */
export function threadRows(config) {
  const extra = atPath(config, resolveTopicExtraPath(config)) || {};
  const groups = Array.isArray(extra.group_topics) ? extra.group_topics : [];
  const overrides = config?.platforms?.telegram?.channel_overrides || {};
  const slots = Array.isArray(extra.room_slots) ? extra.room_slots : [];

  // thread id -> {slot, role}, so a thread can say which room seat it fills.
  const seatByThread = new Map();
  for (const slot of slots) {
    for (const [key, value] of Object.entries(slot)) {
      if (!key.endsWith('_thread_id')) continue;
      seatByThread.set(String(value), { slot: slot.slot, role: key.replace('_thread_id', '') });
    }
  }

  const rows = [];
  for (const group of groups) {
    const chatId = String(group?.chat_id ?? '');
    for (const topic of Array.isArray(group?.topics) ? group.topics : []) {
      const threadId = String(topic?.thread_id ?? '');
      const override = overrides[threadId] || null;
      const seat = seatByThread.get(threadId) || null;
      rows.push({
        id: threadId,
        chat_id: chatId,
        thread_id: threadId,
        name: topic?.name || `thread ${threadId}`,
        skills: Array.isArray(topic?.skills) ? topic.skills : [],
        enabled_skills: Array.isArray(topic?.enabled_skills) ? topic.enabled_skills : [],
        enabled_toolsets: Array.isArray(topic?.enabled_toolsets) ? topic.enabled_toolsets : [],
        cross_thread: Array.isArray(topic?.cross_thread) ? topic.cross_thread.map(String) : [],
        system_prompt: override?.system_prompt || '',
        model: override?.model || null,
        provider: override?.provider || null,
        locked: override?.locked === true,
        slot: seat?.slot ?? null,
        role: seat?.role ?? null,
        // Kept so an edit can rewrite exactly this entry and leave the rest of
        // the array byte-identical — group_topics is a list, and Hermes'
        // deep-merge replaces lists wholesale rather than merging them.
        raw: topic,
      });
    }
  }
  return rows;
}

/**
 * Pure: rebuild the whole `group_topics` array with one topic replaced.
 *
 * The full array always goes back because `_deep_merge` only recurses into
 * dicts — a partial list would truncate every thread that was left out.
 */
export function replaceTopic(groups, chatId, threadId, patch) {
  return (Array.isArray(groups) ? groups : []).map((group) => {
    if (String(group?.chat_id ?? '') !== String(chatId)) return group;
    return {
      ...group,
      topics: (Array.isArray(group.topics) ? group.topics : []).map((topic) => (
        String(topic?.thread_id ?? '') === String(threadId) ? { ...topic, ...patch } : topic
      )),
    };
  });
}

const ROLE_TONE = { ceo: 'info', coder: 'ok', research: 'warn', system: 'idle' };

const VIEWS = [
  { value: 'all', label: 'All' },
  { value: 'room', label: 'Room slots' },
  { value: 'restricted', label: 'Restricted' },
];

export function createThreads({ api, profile, toolbar, onNavigate: navigate }) {
  const root = el('div', { class: 'tab tab-threads' });
  const stats = el('div', { class: 'stat-row-host' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(stats, main);

  let config = null;
  let meta = null;
  let rows = [];
  // thread_id -> live sessions currently bound to it (adapter, best-effort).
  // Config says what a thread is ALLOWED to do; this says what is actually
  // running on it right now, so the two only make sense read together.
  let liveByThread = new Map();
  let selected = null;
  let view = 'all';
  let search = '';

  const table = createTable({
    rowId: (row) => row.id,
    emptyTitle: 'No threads configured',
    emptyNote: 'Telegram forum topics with a thread binding appear here.',
    sort: { key: 'name', dir: 'asc' },
    columns: [
      {
        key: 'name',
        label: 'Thread',
        sortable: true,
        render: (row) => el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: row.name }),
          el('span', { class: 'cell-dim mono', text: `thread ${row.thread_id}` }),
        ]),
      },
      {
        key: 'role',
        label: 'Room seat',
        width: '120px',
        sortable: true,
        render: (row) => (row.role
          ? statusChip(ROLE_TONE[row.role] || 'idle', `${row.role} · slot ${row.slot}`)
          : el('span', { class: 'cell-dim', text: '—' })),
      },
      {
        key: 'live',
        label: 'Live sessions',
        width: '110px',
        sortable: true,
        sortValue: (row) => (row.live || []).length,
        render: (row) => liveCell(row.live || []),
      },
      {
        key: 'enabled_toolsets',
        label: 'Toolsets',
        width: '90px',
        align: 'right',
        sortable: true,
        sortValue: (row) => row.enabled_toolsets.length,
        render: (row) => (row.enabled_toolsets.length
          ? el('span', { class: 'mono', text: String(row.enabled_toolsets.length) })
          : el('span', { class: 'cell-dim', text: 'all' })),
      },
      {
        key: 'skills',
        label: 'Skills',
        width: '80px',
        align: 'right',
        sortable: true,
        sortValue: (row) => row.enabled_skills.length || row.skills.length,
        render: (row) => el('span', { class: 'mono', text: String(row.enabled_skills.length || row.skills.length) }),
      },
      {
        key: 'cross_thread',
        label: 'Can message',
        width: '110px',
        align: 'right',
        sortable: true,
        sortValue: (row) => row.cross_thread.length,
        render: (row) => (row.cross_thread.length
          ? el('span', { class: 'mono', text: `${row.cross_thread.length} thread${row.cross_thread.length === 1 ? '' : 's'}` })
          : el('span', { class: 'cell-dim', text: 'none' })),
      },
      {
        key: 'system_prompt',
        label: 'Prompt',
        width: '80px',
        render: (row) => (row.system_prompt
          ? statusChip('ok', 'custom')
          : el('span', { class: 'cell-dim', text: 'default' })),
      },
    ],
    onSelect: (row) => { selected = row; renderSide(); },
  });
  main.append(table.node);

  function visibleRows() {
    const term = search.trim().toLowerCase();
    return rows.filter((row) => {
      if (view === 'room' && !row.role) return false;
      if (view === 'restricted' && !row.enabled_toolsets.length) return false;
      if (term) {
        const hay = `${row.name} ${row.thread_id} ${row.skills.join(' ')} ${row.enabled_toolsets.join(' ')}`.toLowerCase();
        if (!hay.includes(term)) return false;
      }
      return true;
    });
  }

  async function load() {
    table.setLoading();
    const result = await loadEnvelope(api, '/api/config', { profile, allowEmpty: false });
    meta = result.meta;
    if (result.state !== 'ready') {
      config = null;
      rows = [];
      if (result.state === 'unsupported') table.setUnsupported({ title: 'Config not exposed', reason: result.reason });
      else table.setUnavailable({ reason: result.reason || 'Config unavailable' });
      renderToolbar(toolbar);
      renderSide();
      return;
    }
    config = result.data || {};
    rows = threadRows(config);
    for (const row of rows) row.live = liveByThread.get(row.thread_id) || [];
    const wanted = selected?.id ?? null;
    selected = (wanted && rows.find((r) => r.id === wanted)) || null;
    table.setRows(visibleRows());
    table.setSelected(selected?.id ?? null);
    renderStats();
    renderToolbar(toolbar);
    renderSide();

    await loadLiveSessions();
  }

  /** Best-effort: a thread's live-session column is an overlay, not the source
   * of truth, so an adapter miss just leaves it empty rather than failing the
   * whole tab. */
  async function loadLiveSessions() {
    const byChatId = new Map();
    for (const row of rows) {
      if (!row.chat_id || !row.thread_id) continue;
      if (!byChatId.has(row.chat_id)) byChatId.set(row.chat_id, []);
      byChatId.get(row.chat_id).push(row.thread_id);
    }
    if (!byChatId.size) return;

    const next = new Map();
    await Promise.all([...byChatId.entries()].map(async ([chatId, threadIds]) => {
      try {
        const response = await api.get(
          `/api/adapter/thread-sessions?chat_id=${encodeURIComponent(chatId)}&thread_ids=${encodeURIComponent(threadIds.join(','))}`,
          { profile },
        );
        const payload = recordFrom(response?.data) || {};
        const byThread = recordFrom(payload.sessions_by_thread) || {};
        for (const [threadId, sessions] of Object.entries(byThread)) {
          next.set(threadId, Array.isArray(sessions) ? sessions : []);
        }
      } catch {
        // Leave this chat's threads without a live-session overlay.
      }
    }));

    liveByThread = next;
    for (const row of rows) row.live = liveByThread.get(row.thread_id) || [];
    table.setRows(visibleRows());
    table.setSelected(selected?.id ?? null);
    renderSide();
  }

  function refilter() {
    table.setRows(visibleRows());
    table.setSelected(selected?.id ?? null);
    renderToolbar(toolbar);
  }

  function renderStats() {
    clear(stats);
    const withToolsets = rows.filter((r) => r.enabled_toolsets.length).length;
    const withCross = rows.filter((r) => r.cross_thread.length).length;
    stats.append(createStatRow([
      { label: 'Threads', value: String(rows.length), iconName: 'chat', seriesIndex: 1 },
      { label: 'Room seats', value: String(rows.filter((r) => r.role).length), iconName: 'room-binding', seriesIndex: 2 },
      {
        label: 'Toolset-scoped',
        value: String(withToolsets),
        iconName: 'tools',
        seriesIndex: 3,
        foot: `${rows.length - withToolsets} inherit everything`,
      },
      { label: 'Cross-thread', value: String(withCross), iconName: 'link', seriesIndex: 4, foot: 'may message another thread' },
      { label: 'Custom prompt', value: String(rows.filter((r) => r.system_prompt).length), iconName: 'pencil', seriesIndex: 5 },
    ]));
  }

  function renderToolbar(host) {
    if (!host) return;
    const input = el('input', {
      class: 'input input-sm',
      type: 'search',
      placeholder: 'Search threads, skills, toolsets…',
      value: search,
    });
    input.addEventListener('input', () => { search = input.value; refilter(); });

    const location = config ? resolveTopicExtraPath(config).join('.') : '';
    paint(host, tabToolbar({
      title: 'Threads',
      subtitle: location ? `policy from ${location}.group_topics` : '',
      meta,
      onRefresh: () => load(),
      filters: [
        segmented(VIEWS.map((v) => ({
          ...v,
          count: v.value === 'all' ? rows.length
            : v.value === 'room' ? rows.filter((r) => r.role).length
              : rows.filter((r) => r.enabled_toolsets.length).length,
        })), {
          value: view,
          ariaLabel: 'Filter threads',
          onChange: (next) => { view = next; refilter(); },
        }),
        input,
      ],
    }));
  }

  /** Save a patch to one topic entry, rewriting the full group_topics array. */
  async function saveTopic(row, patch) {
    const path = resolveTopicExtraPath(config);
    const extra = atPath(config, path) || {};
    const next = replaceTopic(extra.group_topics, row.chat_id, row.thread_id, patch);
    return runMutation(
      () => api.put('/api/config', nestBody(path, 'group_topics', next), { profile }),
      { pending: 'Save thread policy', ok: `${row.name} updated`, onDone: () => load() },
    );
  }

  function policyForm(row) {
    const knownToolsets = Array.isArray(config?.platform_toolsets?.telegram)
      ? config.platform_toolsets.telegram
      : [];
    const form = createForm({
      submitLabel: 'Save policy',
      note: 'Toolset and skill policy re-resolves on the thread’s next message — no restart needed. '
        + 'Leaving toolsets empty lets the thread inherit every toolset the platform allows.',
      values: {
        enabled_toolsets: row.enabled_toolsets,
        enabled_skills: row.enabled_skills,
        skills: row.skills,
        cross_thread: row.cross_thread,
      },
      fields: [
        {
          key: 'enabled_toolsets',
          label: 'Enabled toolsets',
          type: 'tags',
          span: 2,
          hint: knownToolsets.length ? `Known for telegram: ${knownToolsets.slice(0, 8).join(', ')}…` : '',
        },
        { key: 'skills', label: 'Preloaded skills', type: 'tags', span: 2, hint: 'Loaded into the prompt on every message.' },
        { key: 'enabled_skills', label: 'Available skills', type: 'tags', span: 2, hint: 'Loadable on demand; not preloaded.' },
        {
          key: 'cross_thread',
          label: 'Cross-thread allowlist',
          type: 'tags',
          span: 2,
          hint: 'Thread ids this agent may send messages to. Empty means it may not message anyone.',
        },
      ],
      onSubmit: async (diff) => {
        if (!Object.keys(diff).length) return;
        const patch = {};
        for (const key of ['enabled_toolsets', 'enabled_skills', 'skills']) {
          if (key in diff) patch[key] = diff[key];
        }
        // cross_thread is numeric upstream; the tag editor hands back strings.
        if ('cross_thread' in diff) {
          patch.cross_thread = diff.cross_thread
            .map((value) => Number(String(value).trim()))
            .filter((value) => Number.isFinite(value));
        }
        form.setBusy(true);
        await saveTopic(row, patch);
        form.setBusy(false);
      },
    });
    return form;
  }

  function promptForm(row) {
    const form = createForm({
      submitLabel: 'Save prompt',
      note: 'The per-thread system prompt is bound into the running gateway, so it applies after a gateway restart.',
      values: { system_prompt: row.system_prompt },
      fields: [
        {
          key: 'system_prompt',
          label: 'System prompt',
          type: 'textarea',
          span: 2,
          hint: 'Empty falls back to the gateway-wide prompt.',
        },
      ],
      onSubmit: async (diff) => {
        if (!('system_prompt' in diff)) return;
        form.setBusy(true);
        // channel_overrides is a dict keyed by thread id, and Hermes recurses
        // into dicts, so this touches exactly one thread's override.
        await runMutation(
          () => api.put('/api/config', {
            platforms: {
              telegram: {
                channel_overrides: { [row.thread_id]: { system_prompt: diff.system_prompt } },
              },
            },
          }, { profile }),
          { pending: 'Save prompt', ok: `${row.name} prompt saved — restart the gateway to apply`, onDone: () => load() },
        );
        form.setBusy(false);
      },
    });
    return form;
  }

  function crossThreadSection(row) {
    if (!row.cross_thread.length) {
      return el('div', { class: 'field-hint', text: 'This thread may not message any other thread.' });
    }
    const byId = new Map(rows.map((r) => [r.id, r]));
    return el('div', { class: 'stack-sm' }, row.cross_thread.map((id) => {
      const target = byId.get(id);
      return el('div', { class: 'choice-row' }, [
        el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: target?.name || `thread ${id}` }),
          el('span', { class: 'cell-dim mono', text: id }),
        ]),
        target
          ? iconButton({
            icon: 'chevron-right',
            label: `Open ${target.name}`,
            onClick: () => { selected = target; table.setSelected(target.id); renderSide(); },
          })
          : statusChip('warn', 'not configured'),
      ]);
    }));
  }

  /** Compact table-cell summary of a thread's live sessions. */
  function liveCell(live) {
    if (!live.length) return el('span', { class: 'cell-dim', text: '—' });
    return statusChip('ok', `${live.length} live`);
  }

  /** Detail-panel list of a thread's live sessions, each opening its chat. */
  function liveSessionsSection(row) {
    const live = row.live || [];
    if (!live.length) {
      return el('div', { class: 'field-hint', text: 'No session is currently open on this thread.' });
    }
    return el('div', { class: 'stack-sm' }, live.map((session) => {
      const activity = session.last_activity_at || session.started_at;
      return el('div', { class: 'choice-row' }, [
        el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: session.title || session.id }),
          el('span', {
            class: 'cell-dim mono',
            text: `${session.id} · ${session.message_count ?? 0} msgs · ${session.source || 'unknown'}`
              + (activity ? ` · active ${fmtAge(new Date(activity * 1000).toISOString())}` : ''),
          }),
        ]),
        navigate
          ? iconButton({
            icon: 'chevron-right',
            label: `Open ${session.id}`,
            onClick: () => navigate('sessions', { session: session.id }),
          })
          : statusChip('ok', 'live'),
      ]);
    }));
  }

  function tagList(values, emptyText) {
    if (!values.length) return el('div', { class: 'field-hint', text: emptyText });
    return el('div', { class: 'taglist-static' }, values.map((value) => el('span', { class: 'chip', text: value })));
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (!selected) {
      paint(inspectorHost, sideHint('Select a thread', [
        'Each Telegram forum topic is one agent thread with its own toolset, skill and messaging policy.',
        'Toolset and skill changes take effect on the thread’s next message; a system-prompt change needs a gateway restart.',
        'The cross-thread allowlist is what lets one agent message another — an empty list means it cannot.',
      ]));
      return;
    }

    const row = selected;
    paint(inspectorHost, createDetail({
      title: row.name,
      meta,
      chips: [
        row.role ? statusChip(ROLE_TONE[row.role] || 'idle', `${row.role} · slot ${row.slot}`) : null,
        row.locked ? statusChip('warn', 'locked') : null,
        row.model ? el('span', { class: 'chip', text: row.model }) : null,
        (row.live || []).length ? statusChip('ok', `${row.live.length} live`) : null,
      ].filter(Boolean),
      fields: [
        { label: 'Thread id', value: row.thread_id, mono: true },
        { label: 'Chat id', value: row.chat_id, mono: true },
        { label: 'Model override', value: row.model },
        { label: 'Provider override', value: row.provider },
      ],
      sections: [
        { title: 'Live sessions', node: liveSessionsSection(row) },
        { title: 'Preloaded skills', node: tagList(row.skills, 'None preloaded.') },
        { title: 'Available skills', node: tagList(row.enabled_skills, 'Inherits the global skill set.') },
        { title: 'Enabled toolsets', node: tagList(row.enabled_toolsets, 'Inherits every toolset the platform allows.') },
        { title: 'Cross-thread targets', node: crossThreadSection(row) },
        { title: 'Edit policy', node: policyForm(row).node },
        { title: 'System prompt', node: promptForm(row).node },
      ],
      raw: row.raw,
      rawLabel: 'group_topics entry',
    }));
  }

  renderSide();

  return {
    mount(container) { clear(container); container.append(root); },
    renderInspector,
    activate(params = {}) {
      const wanted = params.thread || params.id || null;
      return load().then(() => {
        if (!wanted) return;
        const match = rows.find((r) => r.id === String(wanted));
        if (match) { selected = match; table.setSelected(match.id); renderSide(); }
      });
    },
    deactivate() { return { selection: selected?.id ?? null }; },
    refresh: load,
    renderToolbar,
    get data() { return rows; },
  };
}
