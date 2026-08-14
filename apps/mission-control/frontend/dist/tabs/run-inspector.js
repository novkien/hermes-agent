// Run Inspector — what actually happened on one task or session run.
//
// The correlation engine was constructed with an empty provider map until
// recently, so this tab could only ever render "no data" and nobody noticed the
// UI was a flat <ul> with a typed-id box. Both halves are fixed here: the graph
// is laid out by node type with its edges drawn and labelled by evidence, and
// the entity picker lists real recent tasks and sessions instead of asking the
// operator to remember an id.
//
// The two run-inspector routes return a bare `{root, tree, trajectory}` — no
// envelope — so `api.get` hands back `{data, meta: null}` and provenance comes
// from the tree's own `coverage` field instead of a meta badge.

import {
  el, clear, skeleton, statusChip, segmented, iconButton, emptyState,
  unavailableState, errorPanel, fmtTime, fmtAge,
} from '../ui.js';
import { sessionRows, taskRows } from '../pure/data-shape.js';
import { createDetail } from '../components/detail.js';
import { sideHint, paint, tabToolbar, loadEnvelope } from './_kit.js';

export const ROUTE = 'run-inspector';
export const LABEL = 'Run Inspector';
export const GROUP = 'OPERATE';

export const SOURCE_ENDPOINTS = Object.freeze([
  '/api/run-inspector/task/{id}',
  '/api/run-inspector/session/{id}',
]);

export const SSE_EVENTS = Object.freeze(['run.changed', 'task.changed']);

/** Layout order for the graph: causes above effects. */
export const NODE_LANES = Object.freeze(['agent', 'task', 'session', 'thread', 'run', 'permit', 'issue']);

const NODE_TONE = {
  task: 'accent', session: 'info', run: 'ok', agent: 'violet',
  thread: 'info', permit: 'warn', issue: 'danger',
};

const KIND_TONE = { native: 'ok', partial: 'warn', inferred: 'idle', unsupported: 'danger' };

const EVENT_TONE = {
  created: 'info', claimed: 'info', spawned: 'info', run: 'accent',
  heartbeat: 'idle', completed: 'ok', failed: 'danger', error: 'danger',
};

/** Pure: node id as it appears on an edge (`"task:t_abc"`). */
export function nodeKey(node) {
  return `${node.type}:${node.id}`;
}

/**
 * Pure: group nodes into type lanes and index the edges by endpoint.
 *
 * Deliberately not a force-directed layout: these graphs are 3-8 nodes of
 * known types, so a fixed lane order reads better than physics and needs no
 * library.
 */
export function buildGraph(tree) {
  const nodes = Array.isArray(tree?.nodes) ? tree.nodes : [];
  const edges = Array.isArray(tree?.edges) ? tree.edges : [];
  const byKey = new Map(nodes.map((node) => [nodeKey(node), node]));

  const lanes = [];
  for (const type of NODE_LANES) {
    const members = nodes.filter((node) => node.type === type);
    if (members.length) lanes.push({ type, nodes: members });
  }
  // Anything with a type the lane list does not know about still gets shown.
  const known = new Set(NODE_LANES);
  const extras = nodes.filter((node) => !known.has(node.type));
  if (extras.length) {
    for (const type of [...new Set(extras.map((n) => n.type))]) {
      lanes.push({ type, nodes: extras.filter((n) => n.type === type) });
    }
  }

  const edgesByNode = new Map();
  for (const edge of edges) {
    for (const key of [edge.source, edge.target]) {
      if (!edgesByNode.has(key)) edgesByNode.set(key, []);
      edgesByNode.get(key).push(edge);
    }
  }

  return { lanes, edges, byKey, edgesByNode };
}

/** Pure: a short human label for a node of any type. */
export function nodeLabel(node) {
  if (!node) return 'node';
  if (node.type === 'task') return node.title || node.id;
  if (node.type === 'agent') return node.name || node.id;
  if (node.type === 'run') return `run ${node.id}${node.outcome ? ` · ${node.outcome}` : ''}`;
  if (node.type === 'thread') return `thread ${node.id}`;
  return node.id;
}

/** Pure: trajectory events sorted oldest-first with a normalized timestamp. */
export function timelineEvents(trajectory) {
  const list = Array.isArray(trajectory) ? trajectory : (trajectory?.events || []);
  return list
    .map((event) => {
      const raw = event.occurred_at ?? event.ts ?? event.timestamp ?? null;
      // The adapter emits epoch *seconds*; Date wants milliseconds, and a
      // string timestamp passes through untouched.
      const iso = typeof raw === 'number'
        ? new Date(raw * 1000).toISOString()
        : (raw || null);
      return {
        ...event,
        kind: event.kind || event.event_type || event.type || 'event',
        iso,
        sort: iso ? Date.parse(iso) : 0,
      };
    })
    .sort((a, b) => a.sort - b.sort);
}

export function createRunInspector({ api, profile, sse, toolbar, onNavigate: navigate }) {
  const root = el('div', { class: 'tab tab-run-inspector' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(main);

  const pickerPane = el('div', { class: 'stack-sm' });
  const graphPane = el('div');
  const timelinePane = el('div');
  main.append(pickerPane, graphPane, timelinePane);

  let kind = 'task';
  let currentId = null;
  let payload = null;
  let selectedNode = null;
  let candidates = { task: [], session: [] };
  let unsubscribe = null;

  // ------------------------------------------------------------------ picker

  async function loadCandidates() {
    const [tasks, sessions] = await Promise.all([
      loadEnvelope(api, '/api/adapter/kanban/tasks?limit=25', { profile, pick: taskRows }),
      loadEnvelope(api, '/api/upstream/api/sessions?limit=25', { profile, pick: sessionRows }),
    ]);
    candidates = {
      task: (Array.isArray(tasks.data) ? tasks.data : []).map((t) => ({
        id: t.id,
        label: t.title || t.id,
        note: [t.status, t.assignee].filter(Boolean).join(' · '),
      })),
      session: (Array.isArray(sessions.data) ? sessions.data : []).map((s) => ({
        id: s.id || s.session_id,
        label: s.name || s.id || s.session_id,
        note: s.last_activity_at || s.last_active || s.created_at || '',
      })).filter((s) => s.id),
    };
    renderPicker();
  }

  function renderPicker() {
    clear(pickerPane);

    const input = el('input', {
      class: 'input',
      type: 'search',
      placeholder: kind === 'task' ? 'Task id, or pick one below…' : 'Session id, or pick one below…',
      value: currentId || '',
      'aria-label': `${kind} id`,
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') open(kind, input.value.trim());
    });

    pickerPane.append(el('div', { class: 'tab-banner' }, [
      segmented([
        { value: 'task', label: 'Task', count: candidates.task.length },
        { value: 'session', label: 'Session', count: candidates.session.length },
      ], {
        value: kind,
        ariaLabel: 'Entity type',
        onChange: (next) => { kind = next; renderPicker(); },
      }),
      input,
      el('button', {
        class: 'btn btn-sm btn-accent',
        type: 'button',
        text: 'Inspect',
        onclick: () => open(kind, input.value.trim()),
      }),
    ]));

    const list = candidates[kind] || [];
    if (!currentId && list.length) {
      const recent = el('div', { class: 'stack-sm' });
      for (const item of list.slice(0, 8)) {
        recent.append(el('div', { class: 'choice-row' }, [
          el('div', { class: 'cell-stack' }, [
            el('span', { class: 'cell-strong', text: item.label, title: item.label }),
            el('span', { class: 'cell-dim mono', text: `${item.id}${item.note ? ` · ${item.note}` : ''}` }),
          ]),
          iconButton({ icon: 'chevron-right', label: `Inspect ${item.id}`, onClick: () => open(kind, item.id) }),
        ]));
      }
      pickerPane.append(recent);
    }
  }

  // ------------------------------------------------------------------- graph

  function nodeChip(node) {
    const key = nodeKey(node);
    const isSelected = selectedNode && nodeKey(selectedNode) === key;
    const isRoot = payload?.root && payload.root.type === node.type && String(payload.root.id) === String(node.id);
    return el('button', {
      class: `graph-node graph-node-${NODE_TONE[node.type] || 'idle'}${isSelected ? ' is-selected' : ''}${isRoot ? ' is-root' : ''}`,
      type: 'button',
      title: nodeLabel(node),
      onclick: () => { selectedNode = node; renderGraph(); renderSide(); },
    }, [
      el('span', { class: 'graph-node-type', text: node.type }),
      el('span', { class: 'graph-node-label', text: nodeLabel(node) }),
    ]);
  }

  function renderGraph() {
    clear(graphPane);
    const tree = payload?.tree;
    if (!tree) return;

    const { lanes, edges } = buildGraph(tree);
    const head = el('div', { class: 'panel-head' }, [
      el('div', { class: 'panel-title', text: 'Correlation graph' }),
      statusChip(tree.coverage === 'complete' ? 'ok' : tree.coverage === 'partial' ? 'warn' : 'idle',
        `coverage: ${tree.coverage || 'unknown'}`),
      el('span', { class: 'chip', text: `${lanes.reduce((n, l) => n + l.nodes.length, 0)} nodes · ${edges.length} edges` }),
    ]);

    const body = el('div', { class: 'panel-body graph-lanes' });
    lanes.forEach((lane, index) => {
      if (index) body.append(el('div', { class: 'graph-lane-sep', 'aria-hidden': 'true' }));
      body.append(el('div', { class: 'graph-lane' }, [
        el('span', { class: 'graph-lane-title', text: lane.type }),
        el('div', { class: 'graph-lane-nodes' }, lane.nodes.map(nodeChip)),
      ]));
    });

    if (edges.length) {
      const edgeList = el('div', { class: 'graph-edges' });
      for (const edge of edges) {
        edgeList.append(el('div', { class: 'graph-edge' }, [
          statusChip(KIND_TONE[edge.kind] || 'idle', edge.kind),
          el('span', { class: 'mono cell-dim', text: `${edge.source} → ${edge.target}` }),
          el('span', { class: 'cell-dim', text: edge.evidence || '' }),
        ]));
      }
      body.append(edgeList);
    }

    if (Array.isArray(tree.unsupported_pairs) && tree.unsupported_pairs.length) {
      // These are relationships the engine has no provider for — naming them is
      // the difference between "no edge" and "we never looked".
      body.append(el('div', { class: 'notice notice-info' }, [
        el('div', { class: 'notice-title', text: 'Relationships with no backing source' }),
        el('div', { class: 'notice-note', text: tree.unsupported_pairs.map((p) => (Array.isArray(p) ? p.join(' → ') : String(p))).join(', ') }),
      ]));
    }

    graphPane.append(el('section', { class: 'panel' }, [head, body]));
  }

  // ---------------------------------------------------------------- timeline

  function renderTimeline() {
    clear(timelinePane);
    const events = timelineEvents(payload?.trajectory);
    const head = el('div', { class: 'panel-head' }, [
      el('div', { class: 'panel-title', text: 'Trajectory' }),
      el('span', { class: 'chip', text: `${events.length} event${events.length === 1 ? '' : 's'}` }),
    ]);
    const body = el('div', { class: 'panel-body' });

    if (!events.length) {
      body.append(emptyState({
        title: 'No trajectory events',
        note: 'This entity has no recorded event stream, or its source has no coverage for one.',
      }));
    } else {
      const track = el('ol', { class: 'timeline' });
      const first = events[0].sort;
      for (const event of events) {
        const offset = event.sort && first ? Math.round((event.sort - first) / 1000) : 0;
        track.append(el('li', { class: 'timeline-item' }, [
          el('div', { class: 'timeline-marker', 'aria-hidden': 'true' }),
          el('div', { class: 'timeline-body' }, [
            el('div', { class: 'timeline-head' }, [
              statusChip(EVENT_TONE[event.kind] || 'idle', event.kind),
              el('span', { class: 'cell-dim mono', text: `${event.entity_type || ''}${event.entity_id ? ` ${event.entity_id}` : ''}` }),
              el('span', { class: 'timeline-when mono', text: event.iso ? `${fmtTime(event.iso)} · +${offset}s` : '' }),
            ]),
            event.payload && Object.keys(event.payload).length
              ? el('div', { class: 'timeline-payload mono', text: JSON.stringify(event.payload).slice(0, 300) })
              : null,
          ].filter(Boolean)),
        ]));
      }
      body.append(track);
    }

    timelinePane.append(el('section', { class: 'panel' }, [head, body]));
  }

  // -------------------------------------------------------------------- load

  async function open(nextKind, id) {
    if (!id) return;
    kind = nextKind;
    currentId = id;
    selectedNode = null;
    clear(graphPane);
    clear(timelinePane);
    graphPane.append(skeleton({ lines: 6 }));

    let response = null;
    try {
      response = await api.get(`/api/run-inspector/${nextKind}/${encodeURIComponent(id)}`, { profile });
    } catch (err) {
      payload = null;
      clear(graphPane);
      graphPane.append(errorPanel({
        message: `No correlated run data for ${id}: ${err.message}`,
        requestId: err.request_id,
        onRetry: () => open(nextKind, id),
      }));
      renderPicker();
      renderToolbar(toolbar);
      return;
    }

    // These two routes answer without the standard envelope, so the tree is
    // either directly on the body or one `data` level down.
    const body = response?.data && response.data.tree ? response.data : response;
    payload = body && body.tree ? body : null;
    clear(graphPane);
    if (!payload) {
      graphPane.append(unavailableState({ reason: `No correlated run data for ${id}` }));
      renderPicker();
      renderToolbar(toolbar);
      return;
    }

    // Root selected by default: the entity the operator actually asked about.
    const { byKey } = buildGraph(payload.tree);
    selectedNode = byKey.get(`${payload.root?.type}:${payload.root?.id}`) || payload.tree.nodes?.[0] || null;

    renderPicker();
    renderGraph();
    renderTimeline();
    renderToolbar(toolbar);
    renderSide();
  }

  function renderToolbar(host) {
    if (!host) return;
    const coverage = payload?.tree?.coverage;
    paint(host, tabToolbar({
      title: 'Run Inspector',
      subtitle: currentId ? `${kind} ${currentId}${coverage ? ` · coverage ${coverage}` : ''}` : 'pick a task or session',
      onRefresh: currentId ? () => open(kind, currentId) : null,
      actions: [
        currentId
          ? el('button', {
            class: 'btn btn-sm',
            type: 'button',
            text: 'Clear',
            onclick: () => {
              currentId = null;
              payload = null;
              selectedNode = null;
              clear(graphPane);
              clear(timelinePane);
              renderPicker();
              renderToolbar(host);
              renderSide();
            },
          })
          : null,
      ].filter(Boolean),
    }));
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (!selectedNode) {
      paint(inspectorHost, sideHint('Pick an entity', [
        'Inspecting a task or session correlates it across the kanban, session and permit sources into one graph.',
        'Each edge is labelled with the evidence that produced it, so an inferred link is never mistaken for a recorded one.',
        'Click any node in the graph to see its full record here.',
      ]));
      return;
    }

    const node = selectedNode;
    const { edgesByNode } = buildGraph(payload.tree);
    const related = edgesByNode.get(nodeKey(node)) || [];

    const relations = [];
    for (const edge of related) {
      const otherKey = edge.source === nodeKey(node) ? edge.target : edge.source;
      const [otherType, ...rest] = otherKey.split(':');
      const otherId = rest.join(':');
      relations.push({
        label: `${edge.evidence || edge.kind} · ${otherType}`,
        text: otherId,
        onClick: () => {
          const { byKey } = buildGraph(payload.tree);
          const target = byKey.get(otherKey);
          if (target) { selectedNode = target; renderGraph(); renderSide(); }
        },
      });
    }
    if (node.type === 'task' && navigate) {
      relations.push({ label: 'Open in', text: 'Kanban', onClick: () => navigate('kanban', { task: node.id }) });
    }
    if (node.type === 'session' && navigate) {
      relations.push({ label: 'Open in', text: 'Sessions', onClick: () => navigate('sessions', { session: node.id }) });
    }

    // Only the fields that mean something at a glance; the rest is in `raw`.
    const fields = [
      { label: 'Type', value: node.type },
      { label: 'Id', value: node.id, mono: true },
      { label: 'Status', value: node.status },
      { label: 'Outcome', value: node.outcome },
      { label: 'Assignee', value: node.assignee },
      { label: 'Board', value: node.board },
      { label: 'Session', value: node.session_id, mono: true },
      { label: 'Started', value: node.started_at ? fmtTime(node.started_at) : null, mono: true },
      { label: 'Completed', value: node.completed_at ? fmtTime(node.completed_at) : null, mono: true },
      { label: 'Last heartbeat', value: node.last_heartbeat_at ? fmtAge(node.last_heartbeat_at) : null },
      { label: 'Model override', value: node.model_override },
      { label: 'Workspace', value: node.workspace_path, mono: true },
    ];

    paint(inspectorHost, createDetail({
      title: nodeLabel(node),
      chips: [
        el('span', { class: 'chip', text: node.type }),
        node.status ? statusChip(node.status === 'done' ? 'ok' : 'info', node.status) : null,
        related.length ? el('span', { class: 'chip', text: `${related.length} edge${related.length === 1 ? '' : 's'}` }) : null,
      ].filter(Boolean),
      fields,
      sections: node.body
        ? [{ title: 'Body', node: el('pre', { class: 'mono pre-wrap file-preview', text: String(node.body).slice(0, 8000) }) }]
        : [],
      relations,
      raw: node,
    }));
  }

  function bindEvents() {
    if (!sse || unsubscribe) return;
    // `task.changed` carries the task id, `run.changed` carries the *run* id —
    // so the run case matches against nodes already in the graph rather than
    // against the entity being inspected.
    const handles = SSE_EVENTS.map((name) => sse.on(name, (event) => {
      if (!root.isConnected || !currentId || !payload) return;
      const id = String(event?.entity_id || '');
      const hit = id === String(currentId)
        || (payload.tree?.nodes || []).some((node) => String(node.id) === id);
      if (!hit) return;
      open(kind, currentId).catch(() => null);
    }));
    unsubscribe = () => { for (const off of handles) off?.(); };
  }

  renderSide();

  return {
    mount(container) { clear(container); container.append(root); },
    renderInspector,
    activate(params = {}) {
      bindEvents();
      renderPicker();
      renderToolbar(toolbar);
      const promise = loadCandidates().catch(() => null);
      const wantedTask = params.task || null;
      const wantedSession = params.session || null;
      if (wantedTask) return open('task', wantedTask);
      if (wantedSession) return open('session', wantedSession);
      return promise;
    },
    deactivate() {
      if (unsubscribe) { unsubscribe(); unsubscribe = null; }
      return { selection: currentId };
    },
    refresh: () => (currentId ? open(kind, currentId) : loadCandidates()),
    renderToolbar,
    open,
    get data() { return payload; },
  };
}
