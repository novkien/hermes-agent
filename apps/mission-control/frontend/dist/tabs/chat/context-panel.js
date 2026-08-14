// Context-window gauge for the composer control bar.
//
// The dashboard computes the breakdown with Hermes' own engine (the same one
// behind the CLI's `/context`), so this panel only shapes and paints — it never
// estimates token counts itself. `details=1` is not requested: the per-skill
// and per-toolset tables are far more than a composer-height panel can show.

import { el, clear } from '../../ui.js';
import { formatPercent, formatTokens, normalizeContextWindow } from '../../pure/context-window.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const RADIUS = 7;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

// A donut whose arc is the fraction of the window in use. Unlike the legend
// slices — which are categories and get the neutral data ramp — this single
// number IS a health signal: crossing the compaction threshold silently
// truncates the conversation, so the ring earns status colours.
function contextDial() {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 18 18');
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
  svg.setAttribute('class', 'chat-context-dial-svg');
  svg.setAttribute('aria-hidden', 'true');

  const ring = (className) => {
    const node = document.createElementNS(SVG_NS, 'circle');
    node.setAttribute('cx', '9');
    node.setAttribute('cy', '9');
    node.setAttribute('r', String(RADIUS));
    node.setAttribute('class', className);
    return node;
  };
  const track = ring('chat-context-dial-track');
  const arc = ring('chat-context-dial-arc');
  // Start the arc at 12 o'clock and run clockwise, the direction a "filling
  // up" reading is read in.
  arc.setAttribute('transform', 'rotate(-90 9 9)');
  arc.setAttribute('stroke-dasharray', String(CIRCUMFERENCE));
  svg.append(track, arc);

  function set(percent) {
    const pct = Math.max(0, Math.min(100, Number(percent) || 0));
    arc.setAttribute('stroke-dashoffset', String(CIRCUMFERENCE * (1 - pct / 100)));
    // Thresholds track what actually happens: Hermes compacts at 90%, and
    // 75% is the point where one long turn can push it over.
    const tone = pct >= 90 ? 'is-danger' : pct >= 75 ? 'is-warn' : '';
    svg.setAttribute('class', `chat-context-dial-svg ${tone}`.trim());
  }
  set(0);

  return { node: svg, set };
}

/**
 * `state` carries the expanded flag across thread switches — having to re-open
 * the panel on every session change is what makes a collapsed-by-default gauge
 * useless. `draftTokens` lets the composer show what the message being typed
 * would add, using Hermes' own chars/4 heuristic.
 */
export function createContextPanel({
  api, sessionId, sessionProfile, compressionThreshold, state,
}) {
  // The gauge lives in the composer's control bar as a ring the size of the
  // model/effort pills — at rest it is one glyph, not a band across the
  // thread. The breakdown floats above the input box on click; it is
  // positioned, not in flow, so opening it never resizes the transcript.
  const dial = contextDial();
  const trigger = el('button', {
    class: 'chat-tool-btn chat-context-dial',
    type: 'button',
    'aria-expanded': 'false',
    title: 'Context window',
    'aria-label': 'Context window usage',
    onclick: () => setExpanded(!state.expanded),
  }, [dial.node]);
  trigger.hidden = true;

  const panel = el('div', { class: 'chat-context-panel is-collapsed' });
  const usage = el('span', { class: 'chat-context-usage', text: '—' });
  const head = el('div', { class: 'chat-context-head' }, [
    el('span', { class: 'chat-context-title', text: 'Context window' }),
    usage,
  ]);
  const bar = el('div', { class: 'chat-context-bar' });
  const body = el('div', { class: 'chat-context-body' });
  panel.append(head, bar, body);
  panel.hidden = true;

  let latest = null;
  let draftTokens = 0;

  // The panel floats over the transcript, so it needs the dismissal a
  // floating surface is expected to have. Bound to the expanded state, and
  // self-removing once the panel leaves the DOM (a thread switch rebuilds
  // the whole composer), so switching threads cannot leak listeners.
  function onOutside(event) {
    if (!panel.isConnected) {
      document.removeEventListener('mousedown', onOutside, true);
      return;
    }
    if (panel.contains(event.target) || trigger.contains(event.target)) return;
    setExpanded(false);
  }

  function setExpanded(next) {
    state.expanded = next;
    panel.classList.toggle('is-collapsed', !next);
    trigger.classList.toggle('is-active', next);
    trigger.setAttribute('aria-expanded', String(next));
    document.removeEventListener('mousedown', onOutside, true);
    if (next) document.addEventListener('mousedown', onOutside, true);
  }
  setExpanded(Boolean(state.expanded));

  function paint(view) {
    clear(bar);
    clear(body);

    const summary = view.total
      ? `${formatTokens(view.used)} / ${formatTokens(view.total)} (${formatPercent(view.percent)})`
      : `${formatTokens(view.used)} tok`;
    usage.classList.toggle('is-overflow', Boolean(view.overflow));
    usage.textContent = summary;
    dial.set(view.percent);
    trigger.title = `Context window — ${summary}`;
    trigger.setAttribute('aria-label', `Context window usage: ${summary}`);

    for (const segment of view.segments) {
      if (segment.percent <= 0) continue;
      bar.append(el('span', {
        class: 'chat-context-bar-seg',
        style: `width:${segment.percent}%;background:${segment.color}`,
        title: `${segment.label} — ${formatTokens(segment.tokens)}`,
      }));
    }

    const legend = el('div', { class: 'chat-context-legend' });
    for (const segment of view.segments) {
      legend.append(el('div', {
        class: `chat-context-row${segment.tokens ? '' : ' is-idle'}`,
      }, [
        el('span', { class: 'chat-context-swatch', style: `background:${segment.color}` }),
        el('span', { class: 'chat-context-label', text: segment.label }),
        el('span', { class: 'chat-context-tokens', text: formatTokens(segment.tokens) }),
        el('span', { class: 'chat-context-share', text: formatPercent(segment.percent) }),
      ]));
    }
    body.append(legend);

    // What the operator is about to add, before they add it. Labelled as an
    // estimate because that is what chars/4 is.
    if (draftTokens > 0) {
      body.append(el('div', { class: 'chat-context-draft' }, [
        el('span', { class: 'chat-context-label', text: 'Draft (estimated)' }),
        el('span', { class: 'chat-context-tokens', text: `+${formatTokens(draftTokens)}` }),
      ]));
    }

    // Provenance is not decoration here. This route rebuilds the prompt
    // offline, so the figures are Hermes' chars/4 estimate, not a prompt
    // size a provider actually billed — the same heuristic compression
    // thresholds use, so it is the right number to watch, but it is an
    // estimate and must say so.
    // A prompt larger than the window it is supposedly bounded by means the
    // denominator is wrong, not that the session is exactly full — say so
    // rather than letting a confident 100% and an empty free-space row stand.
    if (view.overflow) {
      body.append(el('div', {
        class: 'chat-context-note is-warn',
        text: `Prompt exceeds the reported ${formatTokens(view.total)} window`
          + ` — the model's context length is probably not configured for ${view.model || 'this model'}.`,
      }));
    }

    body.append(el('div', {
      class: 'chat-context-note',
      text: `${view.measured ? 'Measured prompt size' : 'Estimated (chars/4)'}${view.model ? ` · ${view.model}` : ''}`,
    }));
  }

  async function refresh() {
    if (!sessionId) return;
    const response = await api.get(
      `/api/upstream/api/sessions/${encodeURIComponent(sessionId)}/context`,
      { profile: sessionProfile },
    ).catch(() => null);
    // An older dashboard has no such route. Hide the panel rather than
    // render an empty gauge that reads as "this session uses no context".
    const view = response ? normalizeContextWindow(response.data, {
      compressionThreshold,
    }) : null;
    if (!view || (!view.total && !view.used)) {
      panel.hidden = true;
      trigger.hidden = true;
      return;
    }
    latest = view;
    panel.hidden = false;
    trigger.hidden = false;
    paint(view);
  }

  return {
    node: panel,
    trigger,
    refresh,
    /** Re-paint the draft estimate without another round trip. */
    setDraftTokens(count) {
      const next = Math.max(0, Number(count) || 0);
      if (next === draftTokens) return;
      draftTokens = next;
      if (latest) paint(latest);
    },
    /** The model's window size, for anything outside this panel that needs
     * to judge a token count against it (the per-turn receipt's heat color).
     * `0` until the first successful `refresh()`. */
    contextTotal: () => (latest ? latest.total : 0),
  };
}
