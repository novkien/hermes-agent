// Fleet / Topology — the room's five slots as org charts: CEO over three
// managers (coder, research, system), each with a developer branch and a
// kanban-card branch stacked downward beneath it, plus the singleton
// branches (labs, ComfyUI) that hang off the room directly.
//
// The layout is hand-authored SVG in the same spirit as `icons.js`: no chart
// library, no CDN, no physics — coordinates are computed once per render from
// the tree shape (`pure/topology.js#buildOrgChart`). Pan and zoom are a plain
// `translate(...) scale(...)` on a wrapper `<g>`, driven by pointer/wheel
// events; there is no SVG viewer dependency either.
//
// Every edge drawn is a recorded join; nothing here is inferred from naming
// or ordering. The one exception is cosmetic: developer/lab/ComfyUI branches
// only appear when the room-binding payload happens to carry a `topics`
// catalog (name/thread_id/cross_thread) alongside `room_slots` — when it
// doesn't, the chart still draws CEO → managers → cards, just without those
// extra branches. See `buildOrgChart`'s doc comment for how that degrades.

import {
  el, clear, skeleton, statusChip, emptyState, unavailableState, fmtAge, fmtTime,
} from '../ui.js';
import { sessionRows, taskRows } from '../pure/data-shape.js';
import { buildOrgChart, serviceNodes, profileNodes } from '../pure/topology.js';
import { createDetail } from '../components/detail.js';
import { loadEnvelope, tabToolbar, sideHint, paint } from './_kit.js';
import { createSessionDetailModal } from './session-detail.js';
import { createChatModal } from './chat.js';

export const ROUTE = 'fleet';
export const LABEL = 'Fleet / Topology';
export const GROUP = 'OPERATE';

export const SOURCE_ENDPOINTS = Object.freeze([
  '/api/capabilities',
  '/api/adapter/room-binding',
  '/api/adapter/room-sessions',
  '/api/adapter/kanban/tasks?board=all',
  '/api/upstream/api/profiles',
]);

const SEAT_LABEL = { ceo: 'CEO', coder: 'Coder', research: 'Research', system: 'System' };

// Diagram geometry. One place to change, in user units; the wrapper `<g>` is
// panned/zoomed, so these are stable content-space coordinates, not pixels.
const GEO = Object.freeze({
  padX: 24, padY: 28,
  ceoW: 176, ceoH: 38,
  mgrW: 168, mgrH: 34,
  mgrGap: 20,
  branchW: 120, branchH: 22,
  branchGap: 12,
  itemW: 120, itemH: 26, itemGap: 6,
  rowGap: 40,
  // Managers must sit far enough apart that their two-column branch lanes
  // below them never collide; see `slotTreeWidth()`.
  mgrLaneGap: 16,
  slotGap: 64,
  singletonW: 176, singletonH: 34, singletonGap: 12,
});

const ZOOM_MIN = 0.2;
const ZOOM_MAX = 2.5;

function svgEl(tag, attrs = {}, children = []) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'text') { node.textContent = String(value); continue; }
    if (key === 'onclick') { node.addEventListener('click', value); continue; }
    node.setAttribute(key, String(value));
  }
  for (const child of [].concat(children)) if (child) node.append(child);
  return node;
}

/** An elbow path from a box bottom to a box top — readable at any zoom. */
function elbow(x1, y1, x2, y2) {
  const mid = y1 + (y2 - y1) / 2;
  return `M ${x1} ${y1} V ${mid} H ${x2} V ${y2}`;
}

/**
 * A single distribution trunk from a hub down to a row of children, instead
 * of one independent elbow per child. N children used to mean N overlapping
 * diagonal-looking wires converging on the same hub point; this draws one
 * vertical drop, one shared horizontal bus, and one short vertical stub per
 * child — reads as a single wiring harness instead of a rat's nest.
 */
function trunkPath(hubX, hubY, spineY, childXs, childY) {
  if (!childXs.length) return '';
  const minX = Math.min(hubX, ...childXs);
  const maxX = Math.max(hubX, ...childXs);
  const parts = [`M ${hubX} ${hubY} V ${spineY}`, `M ${minX} ${spineY} H ${maxX}`];
  for (const x of childXs) parts.push(`M ${x} ${spineY} V ${childY}`);
  return parts.join(' ');
}

export function createFleet({ api, profile, sse, toolbar, onNavigate: navigate }) {
  const root = el('div', { class: 'tab tab-fleet' });

  const servicePane = el('div');
  const graphPane = el('div');
  const listPane = el('div');
  root.append(servicePane, graphPane, listPane);

  let model = null;
  let services = [];
  let profiles = [];
  let selected = null;
  let meta = null;
  let unsubscribe = null;
  let inspectorHost = null;

  // Both popups are created on first use so a tab that never opens one does
  // not append an overlay to the document.
  let sessionModal = null;
  function transcriptModal() {
    if (!sessionModal) sessionModal = createSessionDetailModal({ api, profile, onChanged: load });
    return sessionModal;
  }

  let chatModalInstance = null;
  function chatModal() {
    if (!chatModalInstance) chatModalInstance = createChatModal({ api, profile, sse });
    return chatModalInstance;
  }

  /** Prior-session count for a seat, preferring the adapter's own tally. */
  function priorSessions(seat) {
    const reported = seat?.session?.prior_sessions;
    if (Number.isFinite(Number(reported)) && Number(reported) > 0) return String(reported);
    return seat?.history ? String(seat.history) : null;
  }

  // ---------------------------------------------------------------- services

  function renderServices() {
    clear(servicePane);
    if (!services.length) return;
    const strip = el('div', { class: 'service-strip' });
    for (const service of services) {
      strip.append(el('div', {
        class: `service-node${service.healthy ? ' is-ok' : service.unknown ? '' : ' is-down'}`,
        title: service.routes.join('\n') || service.id,
      }, [
        el('span', { class: 'service-dot', 'aria-hidden': 'true' }),
        el('div', { class: 'cell-stack' }, [
          el('span', { class: 'cell-strong', text: service.label }),
          el('span', {
            class: 'cell-dim mono',
            text: service.checkedAt
              ? fmtAge(new Date(service.checkedAt * 1000).toISOString())
              : 'never probed',
          }),
        ]),
        statusChip(service.healthy ? 'ok' : service.unknown ? 'idle' : 'danger',
          service.healthy ? 'healthy' : service.unknown ? 'unknown' : 'down'),
      ]));
    }
    servicePane.append(el('section', { class: 'panel' }, [
      el('div', { class: 'panel-head' }, [
        el('div', { class: 'panel-title', text: 'Control plane' }),
        el('span', { class: 'chip', text: `${services.filter((s) => s.healthy).length}/${services.length} healthy` }),
      ]),
      el('div', { class: 'panel-body' }, [strip]),
    ]));
  }

  // ------------------------------------------------------------------- graph

  // Pan/zoom state for the world group. Persists across re-renders (a live
  // SSE refresh should not reset where the user was looking).
  let view = { tx: 24, ty: 24, scale: 1, fitted: false };
  let panState = null;
  let suppressNextClick = false;
  let worldEl = null;
  let svgHostEl = null;

  function wrapClick(handler) {
    return () => {
      if (suppressNextClick) { suppressNextClick = false; return; }
      handler();
    };
  }

  function setWorldTransform() {
    if (worldEl) worldEl.setAttribute('transform', `translate(${view.tx} ${view.ty}) scale(${view.scale})`);
  }

  function fitView(contentWidth, contentHeight) {
    if (!svgHostEl) return;
    const rect = svgHostEl.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const scale = Math.min(1, Math.max(ZOOM_MIN, Math.min(
      (rect.width - 40) / contentWidth,
      (rect.height - 40) / contentHeight,
    )));
    view = {
      tx: Math.max(20, (rect.width - contentWidth * scale) / 2),
      ty: 20,
      scale,
      fitted: true,
    };
    setWorldTransform();
  }

  function bindPanZoom(host) {
    host.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 && event.pointerType === 'mouse') return;
      panState = {
        x: event.clientX, y: event.clientY, tx: view.tx, ty: view.ty, moved: false, pointerId: event.pointerId,
      };
      // Pointer capture is deferred to the first real move (see below), not
      // taken here: capturing on every pointerdown — including a plain click
      // with no movement — makes Chrome retarget the resulting 'click' event
      // to `host` instead of the node under the cursor, so the node's own
      // click listener never fires and nothing becomes selectable.
    });
    host.addEventListener('pointermove', (event) => {
      if (!panState) return;
      const dx = event.clientX - panState.x;
      const dy = event.clientY - panState.y;
      if (!panState.moved && Math.hypot(dx, dy) > 4) {
        panState.moved = true;
        host.setPointerCapture(panState.pointerId);
      }
      if (panState.moved) {
        view.tx = panState.tx + dx;
        view.ty = panState.ty + dy;
        setWorldTransform();
      }
    });
    const endPan = () => {
      if (panState && panState.moved) suppressNextClick = true;
      panState = null;
    };
    host.addEventListener('pointerup', endPan);
    host.addEventListener('pointercancel', endPan);
    // A capture-phase listener on the host, not the node's own wrapClick,
    // is what actually clears `suppressNextClick` — a drag that ends over
    // empty space (no node underneath to consume the flag) would otherwise
    // leave every future click permanently swallowed.
    host.addEventListener('click', (event) => {
      if (suppressNextClick) {
        suppressNextClick = false;
        event.stopPropagation();
      }
    }, true);
    host.addEventListener('wheel', (event) => {
      event.preventDefault();
      const rect = host.getBoundingClientRect();
      const mx = event.clientX - rect.left;
      const my = event.clientY - rect.top;
      const worldX = (mx - view.tx) / view.scale;
      const worldY = (my - view.ty) / view.scale;
      const factor = event.deltaY > 0 ? 0.88 : 1 / 0.88;
      const nextScale = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, view.scale * factor));
      view = { tx: mx - worldX * nextScale, ty: my - worldY * nextScale, scale: nextScale, fitted: true };
      setWorldTransform();
    }, { passive: false });
  }

  // ---------------------------------------------------------------- nodes

  function listHeight(count) {
    return count > 0 ? count * GEO.itemH + (count - 1) * GEO.itemGap : 0;
  }

  function managerHeight(manager) {
    // The card column is capped, plus one row for the "+N more" node when the
    // cap bites — otherwise the box would be drawn shorter than its contents.
    const shown = Math.min(manager.cards.length, CARD_DISPLAY_LIMIT);
    const cardRows = shown + (manager.cardTotal > shown ? 1 : 0);
    const rows = Math.max(listHeight(manager.developers.length), listHeight(cardRows));
    return GEO.mgrH + GEO.rowGap + GEO.branchH + (rows > 0 ? GEO.branchGap + rows : 0);
  }

  /** Horizontal step between two adjacent managers in a slot tree. Each
   * manager owns a two-column branch lane beneath it, so the step is driven by
   * the lane width, not the manager box width, or the lanes would overlap. */
  function managerStep() {
    const laneW = GEO.branchW * 2 + GEO.branchGap;
    return Math.max(GEO.mgrW + GEO.mgrGap, laneW + GEO.mgrLaneGap);
  }

  function slotTreeWidth() {
    return GEO.mgrW + managerStep() * 2;
  }

  function slotTreeHeight(tree) {
    const tall = Math.max(0, ...tree.managers.map(managerHeight));
    return GEO.ceoH + GEO.rowGap + tall;
  }

  function box(svg, { x, y, w, h, cls, title, subtitle, corner, isSelected, onClick, ariaLabel }) {
    const interactive = typeof onClick === 'function';
    const group = svgEl('g', {
      class: `topo-node ${cls}${isSelected ? ' is-selected' : ''}`,
      role: interactive ? 'button' : undefined,
      tabindex: interactive ? '0' : undefined,
      'aria-label': ariaLabel,
      onclick: interactive ? wrapClick(onClick) : undefined,
    });
    if (interactive) {
      group.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onClick(); }
      });
    }
    group.append(svgEl('rect', { class: 'topo-box', x, y, width: w, height: h, rx: 7 }));
    if (title) {
      group.append(svgEl('text', {
        class: 'topo-node-title', x: x + 10, y: y + (subtitle ? h / 2 - 3 : h / 2 + 4), text: title,
      }));
    }
    if (subtitle) {
      group.append(svgEl('text', { class: 'topo-node-sub', x: x + 10, y: y + h - 8, text: subtitle }));
    }
    if (corner) {
      group.append(svgEl('text', {
        class: 'topo-node-corner', x: x + w - 8, y: y + 14, 'text-anchor': 'end', text: corner,
      }));
    }
    svg.append(group);
    return group;
  }

  function seatState(seat) {
    return seat && seat.live ? 'live' : seat && seat.session ? 'idle' : 'free';
  }

  // Every developer topic is named "Jarvis <thing> - Developer"; both the
  // prefix and the suffix are constant across the branch, so they carry no
  // information at this zoom and only push the label out of its box.
  function truncateDeveloperName(name) {
    return String(name || '')
      .replace(/\s*-\s*Developer$/i, '')
      .replace(/^Jarvis\s+/i, '');
  }

  /** Clip a label to what actually fits `width` at the node font size, so a
   * long name degrades to an ellipsis instead of spilling past its box. */
  function fitLabel(text, width, { fontPx = 11 } = {}) {
    const value = String(text || '');
    const budget = Math.max(1, Math.floor((width - 20) / (fontPx * 0.56)));
    return value.length > budget ? `${value.slice(0, Math.max(1, budget - 1))}…` : value;
  }

  function renderGraph() {
    clear(graphPane);
    if (!model) return;
    if (!model.slotTrees.length) {
      graphPane.append(el('section', { class: 'panel' }, [
        el('div', { class: 'panel-body' }, [emptyState({
          title: 'No room slots',
          note: 'The adapter reported no room_slots, so there is no topology to draw.',
        })]),
      ]));
      return;
    }

    const treeW = slotTreeWidth();
    const singletonItems = [
      ...model.singletons.lab.map((topic) => ({ kind: 'lab', topic })),
      ...model.singletons.comfyui.map((topic) => ({ kind: 'comfyui', topic })),
      ...model.unclassified.map((topic) => ({ kind: 'unclassified', topic })),
    ];
    const hasSingletons = singletonItems.length > 0;
    const treesWidth = model.slotTrees.length * treeW + (model.slotTrees.length - 1) * GEO.slotGap;
    const singletonColX = GEO.padX + treesWidth + (hasSingletons ? GEO.slotGap : 0);
    const width = GEO.padX * 2 + treesWidth + (hasSingletons ? GEO.slotGap + GEO.singletonW : 0);

    const roomH = 40;
    const roomY = GEO.padY;
    const treesTop = roomY + roomH + GEO.rowGap;
    const maxTreeHeight = Math.max(0, ...model.slotTrees.map(slotTreeHeight));
    const singletonColHeight = singletonItems.length * GEO.singletonH
      + Math.max(0, singletonItems.length - 1) * GEO.singletonGap;
    const height = treesTop + Math.max(maxTreeHeight, singletonColHeight) + GEO.padY;

    const svg = svgEl('svg', {
      class: 'topo', role: 'img',
      'aria-label': `Room org chart: ${model.totals.occupied} of ${model.totals.seats} seats occupied`,
    });
    const world = svgEl('g', { class: 'topo-world' });
    const edges = svgEl('g', { class: 'topo-edges' });
    const nodes = svgEl('g', { class: 'topo-nodes' });
    world.append(edges, nodes);
    svg.append(world);
    worldEl = world;

    // Room node, centered over the whole map.
    const roomW = Math.min(320, width - GEO.padX * 2);
    const roomX = (width - roomW) / 2;
    nodes.append(svgEl('g', { class: 'topo-room' }, [
      svgEl('rect', { class: 'topo-box', x: roomX, y: roomY, width: roomW, height: roomH, rx: 10 }),
      svgEl('text', { class: 'topo-room-label', x: roomX + 14, y: roomY + 17, text: 'Room chat' }),
      svgEl('text', { class: 'topo-room-sub', x: roomX + 14, y: roomY + 31, text: model.roomChatId || 'unknown chat id' }),
      svgEl('text', {
        class: 'topo-room-sub', x: roomX + roomW - 14, y: roomY + 24, 'text-anchor': 'end',
        text: `${model.totals.live} live · ${model.totals.occupied}/${model.totals.seats} seats`,
      }),
    ]));
    const roomCx = roomX + roomW / 2;
    const roomBottom = roomY + roomH;

    // One shared trunk from Room down into every slot's CEO plus the
    // singleton column's entry point, instead of a separate wire per child
    // (which used to overlap into a dense, hard-to-read bundle).
    const ceoCenters = model.slotTrees.map((tree, index) => {
      const treeX = GEO.padX + index * (treeW + GEO.slotGap);
      const ceoX = treeX + (treeW - GEO.ceoW) / 2;
      return ceoX + GEO.ceoW / 2;
    });
    const singletonEntryCx = hasSingletons ? singletonColX + GEO.singletonW / 2 : null;
    const trunkChildXs = hasSingletons ? [...ceoCenters, singletonEntryCx] : ceoCenters;
    const spineY = roomBottom + (treesTop - roomBottom) / 2;
    edges.append(svgEl('path', {
      class: 'topo-edge topo-trunk',
      d: trunkPath(roomCx, roomBottom, spineY, trunkChildXs, treesTop),
    }));

    model.slotTrees.forEach((tree, index) => {
      const treeX = GEO.padX + index * (treeW + GEO.slotGap);
      const ceoX = treeX + (treeW - GEO.ceoW) / 2;
      const ceoY = treesTop;
      const ceoCx = ceoCenters[index];

      const ceoState = seatState(tree.ceo);
      const isCeoSelected = selected && selected.kind === 'ceo' && selected.id === tree.ceo?.id;
      box(nodes, {
        x: ceoX, y: ceoY, w: GEO.ceoW, h: GEO.ceoH,
        cls: `topo-ceo topo-seat-${ceoState}`,
        title: fitLabel(tree.ceoTopic?.name || SEAT_LABEL.ceo, GEO.ceoW),
        subtitle: tree.ceo?.session ? `${tree.ceo.tasks.length} task${tree.ceo.tasks.length === 1 ? '' : 's'}` : 'free',
        corner: `Slot ${tree.slot}`,
        isSelected: isCeoSelected,
        ariaLabel: `Slot ${tree.slot} CEO: ${ceoState}`,
        onClick: () => tree.ceo && select({
          kind: 'ceo', id: tree.ceo.id, slot: tree.slot, seat: tree.ceo, topic: tree.ceoTopic, binding: tree.binding,
        }),
      });

      if (tree.binding?.task_id) {
        const bound = tree.binding;
        nodes.append(svgEl('g', { class: `topo-binding${bound.live ? ' is-live' : ''}` }, [
          svgEl('text', {
            class: 'topo-binding-label',
            x: ceoCx, y: ceoY + GEO.ceoH + 13, 'text-anchor': 'middle',
            text: `${bound.live ? '● ' : ''}${fitLabel(bound.task_id, GEO.ceoW + 40, { fontPx: 9 })}`,
          }),
        ]));
      }

      const mgrY = ceoY + GEO.ceoH + GEO.rowGap;
      tree.managers.forEach((manager, mi) => {
        const mgrX = treeX + mi * managerStep();
        const mgrCx = mgrX + GEO.mgrW / 2;
        edges.append(svgEl('path', { class: 'topo-edge', d: elbow(ceoCx, ceoY + GEO.ceoH, mgrCx, mgrY) }));

        const mgrState = seatState(manager.seat);
        const isMgrSelected = selected && selected.kind === 'manager' && selected.id === manager.seat?.id;
        box(nodes, {
          x: mgrX, y: mgrY, w: GEO.mgrW, h: GEO.mgrH,
          cls: `topo-manager topo-seat-${mgrState}`,
          title: fitLabel(manager.topic?.name || SEAT_LABEL[manager.role], GEO.mgrW),
          subtitle: manager.seat?.session
            ? `${manager.cardTotal} card${manager.cardTotal === 1 ? '' : 's'}${manager.seat.history ? ` · ${manager.seat.history} prior` : ''}`
            : 'free',
          isSelected: isMgrSelected,
          ariaLabel: `Slot ${tree.slot} ${SEAT_LABEL[manager.role]}: ${mgrState}`,
          onClick: () => manager.seat && select({ kind: 'manager', id: manager.seat.id, slot: tree.slot, manager }),
        });

        // Two branches under the manager, each growing straight down so more
        // cards never widen the chart.
        const laneW = GEO.branchW * 2 + GEO.branchGap;
        const laneX = mgrCx - laneW / 2;
        const branchY = mgrY + GEO.mgrH + GEO.rowGap;
        // A busy manager has hundreds of cards; drawing them all would make a
        // column taller than the rest of the chart put together and slower to
        // paint than it is useful. Show the newest, count them all, and let the
        // overflow node hand off to the Kanban tab.
        const branches = [
          { key: 'developers', label: 'Developers', x: laneX, items: manager.developers, total: manager.developers.length },
          {
            key: 'cards',
            label: 'Kanban cards',
            x: laneX + GEO.branchW + GEO.branchGap,
            items: manager.cards.slice(0, CARD_DISPLAY_LIMIT),
            total: manager.cardTotal,
          },
        ];
        branches.forEach((branch) => {
          const branchCx = branch.x + GEO.branchW / 2;
          edges.append(svgEl('path', { class: 'topo-edge', d: elbow(mgrCx, mgrY + GEO.mgrH, branchCx, branchY) }));
          nodes.append(svgEl('g', { class: 'topo-branch' }, [
            svgEl('rect', { class: 'topo-branch-box', x: branch.x, y: branchY, width: GEO.branchW, height: GEO.branchH, rx: 11 }),
            svgEl('text', {
              class: 'topo-branch-label', x: branchCx, y: branchY + GEO.branchH / 2 + 3, 'text-anchor': 'middle',
              text: `${branch.label} (${branch.total})`,
            }),
          ]));
          branch.items.forEach((item, ii) => {
            const itemY = branchY + GEO.branchH + GEO.branchGap + ii * (GEO.itemH + GEO.itemGap);
            const isItem = branch.key === 'developers'
              ? selected && selected.kind === 'developer' && selected.id === `dev:${item.thread_id}`
              : selected && selected.kind === 'card' && selected.id === item.id;
            edges.append(svgEl('path', {
              class: 'topo-edge is-empty',
              d: elbow(branchCx, ii === 0 ? branchY + GEO.branchH : itemY - GEO.itemGap, branchCx, itemY),
            }));
            box(nodes, {
              x: branch.x, y: itemY, w: GEO.branchW, h: GEO.itemH,
              cls: branch.key === 'developers' ? 'topo-item topo-item-developer' : `topo-item topo-item-card topo-card-${item.status || 'unknown'}`,
              title: fitLabel(
                branch.key === 'developers' ? truncateDeveloperName(item.name) : (item.title || item.id),
                GEO.branchW,
              ),
              subtitle: branch.key === 'developers' ? null : (item.status || null),
              isSelected: isItem,
              ariaLabel: branch.key === 'developers' ? `Developer ${item.name}` : `Card ${item.title || item.id}`,
              onClick: () => (branch.key === 'developers'
                ? select({ kind: 'developer', id: `dev:${item.thread_id}`, slot: tree.slot, role: manager.role, topic: item })
                : select({ kind: 'card', id: item.id, slot: tree.slot, role: manager.role, task: item })),
            });
          });

          const hidden = Math.max(0, branch.total - branch.items.length);
          if (hidden > 0) {
            const itemY = branchY + GEO.branchH + GEO.branchGap
              + branch.items.length * (GEO.itemH + GEO.itemGap);
            edges.append(svgEl('path', {
              class: 'topo-edge is-empty',
              d: elbow(branchCx, itemY - GEO.itemGap, branchCx, itemY),
            }));
            box(nodes, {
              x: branch.x, y: itemY, w: GEO.branchW, h: GEO.itemH,
              cls: 'topo-item topo-item-more',
              title: `+${hidden} more`,
              ariaLabel: `${hidden} more ${branch.label.toLowerCase()}`,
              onClick: () => navigate && navigate('kanban'),
            });
          }
        });
      });
    });

    if (hasSingletons) {
      singletonItems.forEach((entry, index) => {
        const y = treesTop + index * (GEO.singletonH + GEO.singletonGap);
        const cx = singletonColX + GEO.singletonW / 2;
        // The trunk already lands on the first item's top edge; later items
        // chain from the one above instead of each drawing its own wire back
        // to Room, so the column reads as one stack, not a fan of lines.
        if (index > 0) {
          edges.append(svgEl('path', { class: 'topo-edge', d: elbow(cx, y - GEO.singletonGap, cx, y) }));
        }
        const id = `${entry.kind}:${entry.topic.thread_id}`;
        const isSel = selected && selected.kind === entry.kind && selected.id === id;
        box(nodes, {
          x: singletonColX, y, w: GEO.singletonW, h: GEO.singletonH,
          cls: `topo-item topo-item-${entry.kind}`,
          title: fitLabel(entry.topic.name, GEO.singletonW),
          subtitle: entry.kind === 'unclassified' ? 'unclassified' : entry.kind,
          isSelected: isSel,
          ariaLabel: entry.topic.name,
          onClick: () => select({ kind: entry.kind, id, topic: entry.topic }),
        });
      });
    }

    svgHostEl = el('div', { class: 'topo-body', tabindex: '0' }, [svg]);
    bindPanZoom(svgHostEl);
    setWorldTransform();

    graphPane.append(el('section', { class: 'panel' }, [
      el('div', { class: 'panel-head' }, [
        el('div', { class: 'panel-title', text: 'Room topology' }),
        el('span', {
          class: 'chip',
          text: `${model.slotTrees.length} slots · CEO → 3 managers → developers/cards`,
        }),
        el('span', { class: 'legend' }, [
          el('span', { class: 'legend-item legend-live', text: 'live' }),
          el('span', { class: 'legend-item legend-idle', text: 'occupied' }),
          el('span', { class: 'legend-item legend-free', text: 'free' }),
        ]),
        el('button', {
          class: 'btn btn-sm', type: 'button', onclick: () => fitView(width, height),
        }, 'Fit'),
      ]),
      el('div', { class: 'panel-body' }, [svgHostEl]),
    ]));

    if (!view.fitted) fitView(width, height);
    else setWorldTransform();
  }

  // -------------------------------------------------------------- side lists

  function renderLists() {
    clear(listPane);
    if (!model) return;

    const detached = model.detachedSessions
      .slice()
      .sort((a, b) => Number(b.last_activity_at || b.started_at || 0) - Number(a.last_activity_at || a.started_at || 0))
      .slice(0, 12);

    const rows = el('div', { class: 'section-list' });
    for (const session of detached) {
      rows.append(el('button', {
        class: 'section-row section-row-flex',
        type: 'button',
        onclick: () => navigate && navigate('sessions', { session: session.id }),
      }, [
        el('span', { class: 'cell-strong', text: session.title || session.display_name || session.id }),
        el('span', { class: 'cell-dim mono', text: session.source || '—' }),
        el('span', { class: 'cell-dim', text: `${session.message_count ?? 0} msgs` }),
        el('span', {
          class: 'cell-dim mono',
          text: session.last_activity_at
            ? fmtAge(new Date(Number(session.last_activity_at) * 1000).toISOString())
            : '—',
        }),
      ]));
    }

    listPane.append(el('section', { class: 'panel' }, [
      el('div', { class: 'panel-head' }, [
        el('div', { class: 'panel-title', text: 'Sessions outside the room' }),
        el('span', { class: 'chip', text: String(model.detachedSessions.length) }),
      ]),
      el('div', { class: 'panel-body' }, [
        detached.length
          ? rows
          : emptyState({ title: 'None', note: 'Every recent session belongs to a room thread.' }),
      ]),
    ]));

    if (profiles.length) {
      const profileRows = el('div', { class: 'section-list' });
      for (const item of profiles) {
        profileRows.append(el('button', {
          class: 'section-row section-row-flex',
          type: 'button',
          onclick: () => navigate && navigate('profiles', { name: item.name }),
        }, [
          el('span', { class: 'cell-strong', text: item.name }),
          statusChip(item.running ? 'ok' : 'idle', item.running ? 'gateway up' : 'stopped'),
          el('span', { class: 'cell-dim', text: `${item.skills} skills` }),
          el('span', { class: 'cell-dim mono', text: [item.provider, item.model].filter(Boolean).join(' / ') || '—' }),
        ]));
      }
      listPane.append(el('section', { class: 'panel' }, [
        el('div', { class: 'panel-head' }, [
          el('div', { class: 'panel-title', text: 'Profiles' }),
          el('span', { class: 'chip', text: `${profiles.filter((p) => p.running).length} running` }),
        ]),
        el('div', { class: 'panel-body' }, [profileRows]),
      ]));
    }
  }

  // ---------------------------------------------------------------- selection

  function select(next) {
    selected = next;
    renderGraph();
    renderSide();
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSeatDetail(title, seat, binding = null) {
    const session = seat.session;
    const relations = [];
    if (seat.thread && navigate) {
      relations.push({ label: 'Thread policy', text: seat.thread, onClick: () => navigate('threads', { thread: seat.thread }) });
    }
    if (session && navigate) {
      relations.push({ label: 'Session', text: session.id, onClick: () => navigate('sessions', { session: session.id }) });
      // Both popups host the real tab surface (Chat tab / Sessions tab detail)
      // in a modal over this tab, so the org chart underneath keeps its
      // selection and pan/zoom while the popup stays bound to those tabs.
      relations.push({ label: 'Chat', text: 'Open chat', onClick: () => chatModal().open(session.id) });
      relations.push({ label: 'Transcript', text: 'Open transcript', onClick: () => transcriptModal().open(session.id) });
    }
    if (binding?.task_id && navigate) {
      relations.push({
        label: binding.live ? 'Bound task (live)' : 'Last bound task',
        text: binding.task_id,
        onClick: () => navigate('run-inspector', { task: binding.task_id }),
      });
    }
    for (const task of seat.tasks.slice(0, 6)) {
      relations.push({
        label: task.status || 'task',
        text: task.title || task.id,
        onClick: () => navigate && navigate('run-inspector', { task: task.id }),
      });
    }

    paint(inspectorHost, createDetail({
      title,
      chips: [
        statusChip(seat.live ? 'ok' : session ? 'info' : 'idle', seat.live ? 'live' : session ? 'occupied' : 'free'),
        seat.running ? statusChip('accent', `${seat.running} running`) : null,
        binding?.task_id ? statusChip(binding.live ? 'accent' : 'idle', binding.live ? 'task bound' : 'idle slot') : null,
      ].filter(Boolean),
      fields: [
        { label: 'Thread id', value: seat.thread, mono: true },
        { label: 'Session', value: session ? session.id : 'none', mono: true },
        { label: 'Session title', value: session ? (session.title || session.display_name || null) : null },
        { label: 'Model', value: session ? session.model : null },
        { label: 'Profile', value: session ? (session.profile_name || session.profile) : null },
        { label: 'Messages', value: session ? String(session.message_count ?? 0) : null },
        {
          label: 'Last activity',
          value: seat.lastActivity ? fmtTime(new Date(Number(seat.lastActivity) * 1000).toISOString()) : null,
          mono: true,
        },
        // room-sessions returns one live row per thread and reports the rest as
        // a count, so `history` (derived from rows present) is 0 there.
        { label: 'Prior sessions', value: priorSessions(seat) },
        { label: 'Tasks', value: String(seat.tasks.length) },
        // Room task binding: the root task holding this slot. Written by the
        // session-injector plugin when a handoff is sent to the CEO entry
        // route; the adapter only reads it.
        { label: 'Bound task id', value: binding?.task_id || null, mono: true },
        {
          label: 'Binding status',
          value: binding
            ? (binding.live ? (binding.status || 'ACTIVE') : (binding.completed ? 'completed' : 'not active'))
            : null,
        },
        {
          label: 'Bound at',
          value: binding?.bound_at || binding?.last_seen_at
            ? fmtTime(new Date(Number(binding.bound_at || binding.last_seen_at) * 1000).toISOString())
            : null,
          mono: true,
        },
      ],
      relations,
      raw: session || { thread: seat.thread, occupied: false },
    }));
  }

  function renderTopicDetail(title, topic, extraFields = []) {
    paint(inspectorHost, createDetail({
      title,
      fields: [
        { label: 'Thread id', value: topic?.thread_id != null ? String(topic.thread_id) : null, mono: true },
        { label: 'Skills', value: Array.isArray(topic?.skills) && topic.skills.length ? topic.skills.join(', ') : null },
        ...extraFields,
      ],
      raw: topic || {},
    }));
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (!selected) {
      paint(inspectorHost, sideHint('Room topology', [
        'Each slot is a CEO over three managers (coder, research, system); a manager branches into its developers and the kanban cards it created.',
        'A seat is occupied when a session exists on its thread, and live when that session is still active upstream.',
        'Drag to pan, scroll to zoom. Select any node to see its detail here.',
      ]));
      return;
    }

    if (selected.kind === 'ceo' || selected.kind === 'manager') {
      const seat = selected.seat || selected.manager?.seat;
      const label = selected.kind === 'ceo' ? 'CEO' : SEAT_LABEL[selected.manager.role];
      renderSeatDetail(`Slot ${selected.slot} · ${label}`, seat, selected.binding || null);
      return;
    }

    if (selected.kind === 'developer') {
      renderTopicDetail(
        `Slot ${selected.slot} · ${truncateDeveloperName(selected.topic?.name)}`,
        selected.topic,
        [{ label: 'Manager', value: SEAT_LABEL[selected.role] || selected.role }],
      );
      return;
    }

    if (selected.kind === 'card') {
      const task = selected.task;
      paint(inspectorHost, createDetail({
        title: task.title || task.id,
        chips: [statusChip(task.status === 'done' ? 'ok' : task.status === 'in_progress' || task.status === 'running' ? 'accent' : 'idle', task.status || 'unknown')],
        fields: [
          { label: 'Manager', value: SEAT_LABEL[selected.role] || selected.role },
          { label: 'Priority', value: task.priority || null },
          { label: 'Board', value: task.board || null },
          { label: 'Assignee', value: task.assignee || null },
        ],
        relations: navigate ? [{ label: 'Run', text: 'Open run inspector', onClick: () => navigate('run-inspector', { task: task.id }) }] : [],
        raw: task,
      }));
      return;
    }

    if (selected.kind === 'lab' || selected.kind === 'comfyui' || selected.kind === 'unclassified') {
      renderTopicDetail(selected.topic?.name || selected.kind, selected.topic);
      return;
    }
  }

  // -------------------------------------------------------------------- load

  // How many cards a manager draws before the column hands off to "+N more".
  // Also the window the adapter is asked for, so nothing is fetched unseen.
  const CARD_DISPLAY_LIMIT = 10;

  // Cards attributed to each of the room's threads.
  //
  // This is the adapter's `/room-cards`, and the join behind it is the whole
  // point: Hermes keeps a kanban.db per board under ~/.hermes/kanban/boards/,
  // a card records only the session that created it, and a manager thread runs
  // hundreds of sessions across its resets. Attributing cards to managers
  // therefore needs every board joined against state.db's sessions — 1,928
  // cards and 1,738 sessions on this deployment. Doing that from the browser
  // meant ~14 paged reads per tab load and still under-counted; the adapter
  // answers it exactly in one.
  //
  // Before this, the chart read the adapter's DEFAULT board only, which holds
  // 64 of those 1,928 cards, and attributed them to the seat's live session
  // alone — so a coder manager with 214 cards drew four.
  async function loadRoomCards(roomChatId) {
    if (!roomChatId) return null;
    const envelope = await loadEnvelope(
      api,
      `/api/adapter/room-cards?chat_id=${encodeURIComponent(roomChatId)}`
      + `&per_thread=${CARD_DISPLAY_LIMIT}`,
      { profile },
    );
    const data = envelope.data;
    if (!data || typeof data !== 'object') return null;
    return { counts: data.counts || {}, cards: data.cards || {} };
  }

  async function load() {
    clear(servicePane);
    clear(graphPane);
    clear(listPane);
    graphPane.append(skeleton({ lines: 8 }));

    const [caps, rooms, tasks, profileList] = await Promise.all([
      loadEnvelope(api, '/api/capabilities', { profile, allowEmpty: false }),
      loadEnvelope(api, '/api/adapter/room-binding', { profile, allowEmpty: false }),
      // Fallback attribution for an adapter without /room-cards: one merged
      // page across every board, joined client-side by thread below.
      loadEnvelope(api, '/api/adapter/kanban/tasks?board=all&limit=100', { profile, pick: taskRows }),
      loadEnvelope(api, '/api/upstream/api/profiles', { profile, pick: profileNodes }),
    ]);

    // Seat occupancy comes from the adapter's room-sessions view, not the
    // dashboard's session list. The dashboard hides chain successors and
    // surfaces the dead chain root, so a thread that was reset or compressed
    // reads as frozen at its last reset; room-sessions resolves each thread to
    // the live tip instead. It needs the room's chat id, so it loads second.
    const roomChatId = rooms.data?.room_chat_id;
    //
    // `history=1` additionally returns every session the room chat has ever
    // held as id -> thread_id pairs. Cards record the session that created
    // them, and a manager thread runs hundreds of sessions across its resets,
    // so without this map a card made before the last reset cannot be traced
    // back to the manager that made it.
    const roomEnvelope = roomChatId
      ? await loadEnvelope(
        api,
        `/api/adapter/room-sessions?chat_id=${encodeURIComponent(roomChatId)}&history=1`,
        { profile },
      )
      : { state: 'empty', data: null, meta: null };
    const sessions = { ...roomEnvelope, data: sessionRows(roomEnvelope.data) || [] };
    const threadSessions = Array.isArray(roomEnvelope.data?.thread_sessions)
      ? roomEnvelope.data.thread_sessions
      : [];
    const roomCards = await loadRoomCards(roomChatId);

    meta = rooms.meta || caps.meta || null;
    services = serviceNodes(caps.data);
    profiles = Array.isArray(profileList.data) ? profileList.data : [];

    clear(graphPane);
    if (rooms.state === 'unavailable' || rooms.state === 'unsupported') {
      model = null;
      graphPane.append(unavailableState({ reason: rooms.reason, requestId: rooms.requestId }));
      renderServices();
      renderToolbar(toolbar);
      return;
    }

    model = buildOrgChart({
      rooms: rooms.data,
      sessions: Array.isArray(sessions.data) ? sessions.data : [],
      tasks: Array.isArray(tasks.data) ? tasks.data : [],
      topics: Array.isArray(rooms.data?.topics) ? rooms.data.topics : [],
      threadSessions,
      roomCards,
    });

    // Keep the current selection pointing at the refreshed object, not a stale
    // copy — otherwise a refresh silently freezes the inspector.
    if (selected) {
      const all = [];
      for (const tree of model.slotTrees) {
        if (tree.ceo) all.push({ kind: 'ceo', id: tree.ceo.id, slot: tree.slot, seat: tree.ceo, topic: tree.ceoTopic });
        for (const manager of tree.managers) {
          if (manager.seat) all.push({ kind: 'manager', id: manager.seat.id, slot: tree.slot, manager });
          for (const topic of manager.developers) {
            all.push({ kind: 'developer', id: `dev:${topic.thread_id}`, slot: tree.slot, role: manager.role, topic });
          }
          for (const task of manager.cards) {
            all.push({ kind: 'card', id: task.id, slot: tree.slot, role: manager.role, task });
          }
        }
      }
      for (const kind of ['lab', 'comfyui']) {
        for (const topic of model.singletons[kind]) all.push({ kind, id: `${kind}:${topic.thread_id}`, topic });
      }
      for (const topic of model.unclassified) all.push({ kind: 'unclassified', id: `unclassified:${topic.thread_id}`, topic });
      selected = all.find((item) => item.kind === selected.kind && item.id === selected.id) || null;
    }

    renderServices();
    renderGraph();
    renderLists();
    renderToolbar(toolbar);
    renderSide();
  }

  function renderToolbar(host) {
    if (!host) return;
    paint(host, tabToolbar({
      title: 'Fleet / Topology',
      subtitle: model
        ? `${model.totals.live} live · ${model.totals.occupied}/${model.totals.seats} seats · ${model.totals.tasks} tasks`
        : 'room slots, seats, sessions and their tasks',
      onRefresh: () => load(),
      meta,
    }));
  }

  function bindEvents() {
    if (!sse || unsubscribe) return;
    // Only events in SseClient.EVENT_TYPES are ever delivered — subscribing to
    // anything else is silently dead, so keep this list inside that allowlist.
    const handles = ['session.changed', 'task.changed']
      .map((name) => sse.on(name, () => {
        if (root.isConnected) load().catch(() => null);
      }));
    unsubscribe = () => { for (const off of handles) off?.(); };
  }

  return {
    mount(container) { clear(container); container.append(root); },
    activate() {
      bindEvents();
      renderToolbar(toolbar);
      return load();
    },
    deactivate() {
      if (unsubscribe) { unsubscribe(); unsubscribe = null; }
      return { selected: selected ? selected.id : null };
    },
    refresh: load,
    renderToolbar,
    renderInspector,
    get data() { return model; },
  };
}
