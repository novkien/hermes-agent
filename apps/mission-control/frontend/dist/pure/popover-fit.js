// Where a popover goes, and how tall it is allowed to be.
//
// Extracted from `ui.js`'s `openMenu` because the old inline arithmetic put the
// toolsets menu half off-screen, for two reasons that need different answers:
//
//   1. It clamped the popover's *position* but never its *height*. A menu whose
//      CSS `max-height` is 420px opens on a 430px-tall viewport and there is no
//      position that fits it; clamping `top` to 8 just moves the overflow to
//      the bottom edge. So this returns an explicit `maxHeight`.
//   2. It measured once, at open. The toolsets and skills menus fetch their
//      contents, so at measure time they are a four-line skeleton and by the
//      time the data lands they are ten times taller.
//
// (2) is the interesting one. The obvious repair — re-measure whenever the
// content changes — is a trap: the clamp from (1) pins the box a ResizeObserver
// would be watching, and a MutationObserver that re-runs a routine which itself
// writes styles is one step from an render loop (it froze a tab during
// development). The fix is to never depend on the content's height at all:
//
//   * opening upward pins the popover's BOTTOM edge above the anchor, so it
//     grows upward on its own and can never cross the bottom edge;
//   * opening downward pins its TOP edge below the anchor;
//   * `maxHeight` is the room on that side, so it can never cross the far edge.
//
// Growth is then handled by CSS, at the moment it happens, with no observers
// and no second measurement. Only the horizontal placement needs a measured
// width, and width does not change as content loads.

/** Breathing room between the popover and both its anchor and the viewport edge. */
export const POPOVER_GAP = 8;

/** Below this a scrolling popover is unusable; better to overlap than to slice. */
export const POPOVER_MIN_HEIGHT = 140;

/**
 * @param {{top:number,bottom:number,left:number,right:number}} anchor viewport rect
 * @param {{width:number}} size the popover's measured width
 * @param {{width:number,height:number}} viewport
 * @returns {{top:(number|null),bottom:(number|null),left:number,maxHeight:number,maxWidth:number,placement:string}}
 *   Exactly one of `top`/`bottom` is a number; the other is null and the caller
 *   must clear that edge so the popover stays anchored to the one that matters.
 */
export function fitPopover(anchor, size, viewport, { placement = 'above', align = 'end' } = {}) {
  const roomAbove = anchor.top - POPOVER_GAP * 2;
  const roomBelow = viewport.height - anchor.bottom - POPOVER_GAP * 2;

  // Honour the requested side while it can show a usable menu; flip only when
  // the other side is genuinely roomier. This depends on the anchor alone, so
  // the answer does not change as the content loads.
  let side = placement === 'below' ? 'below' : 'above';
  const preferred = side === 'above' ? roomAbove : roomBelow;
  const other = side === 'above' ? roomBelow : roomAbove;
  if (preferred < POPOVER_MIN_HEIGHT && other > preferred) side = side === 'above' ? 'below' : 'above';

  const maxHeight = Math.max(POPOVER_MIN_HEIGHT, side === 'above' ? roomAbove : roomBelow);
  const maxWidth = Math.max(0, viewport.width - POPOVER_GAP * 2);
  const width = Math.min(size.width || 0, maxWidth);

  const rawLeft = align === 'start' ? anchor.left : anchor.right - width;
  const left = clamp(rawLeft, POPOVER_GAP, viewport.width - width - POPOVER_GAP);

  return side === 'above'
    // `bottom` is measured from the viewport's bottom edge, which is what
    // `position: fixed` expects.
    ? { top: null, bottom: Math.max(POPOVER_GAP, viewport.height - anchor.top + POPOVER_GAP), left, maxHeight, maxWidth, placement: side }
    : { top: Math.max(POPOVER_GAP, anchor.bottom + POPOVER_GAP), bottom: null, left, maxHeight, maxWidth, placement: side };
}

function clamp(value, low, high) {
  // A viewport narrower than the popover makes `high < low`; pinning to the
  // near edge keeps the head of the content reachable.
  if (high < low) return low;
  return Math.max(low, Math.min(value, high));
}
