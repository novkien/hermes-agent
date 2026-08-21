// Hand-authored SVG charts. No CDN, no bundler, no chart library.
//
// Scope is deliberately small — one bar chart with an optional second stacked
// series, a hover layer, and a drag-to-select brush. That is every chart this
// dashboard actually needs, and a small honest implementation beats a generic
// one nobody can read.
//
// Rules this follows (from the data-viz pass):
//   * one y axis, never two — two measures of different scale get two charts;
//   * series colour comes from the validated `--series-*` ramp, never from the
//     semantic ok/warn/danger tokens, so a colour never implies health;
//   * a legend is present whenever there are two series, and a single series
//     gets none because the title already names it;
//   * a 2px surface gap separates stacked segments and adjacent bars;
//   * values are never printed on every bar — the tooltip and the table carry
//     the exact numbers.

import { el } from '../ui.js';

const SVG = 'http://www.w3.org/2000/svg';

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    node.setAttribute(key, String(value));
  }
  return node;
}

/** Pure: "nice" axis maximum, so gridlines land on round numbers. */
export function niceMax(value) {
  const n = Number(value) || 0;
  if (n <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(n));
  const scaled = n / magnitude;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return step * magnitude;
}

/** Pure: index of the bar under an x coordinate, or -1. */
export function barIndexAt(x, { padLeft, plotWidth, count }) {
  if (!count || plotWidth <= 0) return -1;
  const rel = x - padLeft;
  if (rel < 0 || rel > plotWidth) return -1;
  return Math.min(count - 1, Math.max(0, Math.floor((rel / plotWidth) * count)));
}

/**
 * Stacked/single bar chart over discrete buckets.
 *
 * @param {object}   opts
 * @param {string[]} opts.labels        one per bucket (x axis)
 * @param {Array}    opts.series        [{key, label, values, seriesIndex}]
 * @param {Function} [opts.formatValue] number -> string for axis + tooltip
 * @param {Function} [opts.onBrush]     (fromIndex, toIndex|null) after a drag
 * @param {Function} [opts.onSelect]    (index) on a plain click
 * @param {number[]} [opts.brush]       [from, to] to render as already selected
 */
export function barChart({
  labels = [], series = [], height = 180, formatValue = (v) => String(v),
  onBrush = null, onSelect = null, brush = null, ariaLabel = 'chart',
} = {}) {
  const count = labels.length;
  let live = series.filter((s) => Array.isArray(s.values) && s.values.length);
  const root = el('div', { class: 'chart' });
  if (!count || !live.length) return root;

  const padLeft = 46;
  const padRight = 8;
  const padTop = 10;
  const padBottom = 22;
  const width = 640;
  const plotWidth = width - padLeft - padRight;
  const plotHeight = height - padTop - padBottom;

  let stackTotals = labels.map((_, i) => live.reduce((total, s) => total + (Number(s.values[i]) || 0), 0));
  let max = niceMax(Math.max(...stackTotals, 0));
  const slot = plotWidth / count;
  const barWidth = Math.max(2, slot - 2); // the 2px gap between adjacent bars

  const svg = svgEl('svg', {
    class: 'chart-svg', viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: 'none', role: 'img', 'aria-label': ariaLabel,
  });

  // Gridlines + y labels. Recessive: hairline, muted text, behind the marks.
  const gridNodes = [];
  for (let step = 0; step <= 2; step += 1) {
    const value = (max / 2) * step;
    const y = padTop + plotHeight - (value / max) * plotHeight;
    const line = svgEl('line', { class: 'chart-grid', x1: padLeft, y1: y, x2: width - padRight, y2: y });
    svg.append(line);
    const text = svgEl('text', { class: 'chart-axis', x: padLeft - 6, y: y + 3, 'text-anchor': 'end' });
    text.textContent = formatValue(value);
    svg.append(text);
    gridNodes.push({ step, line, text });
  }

  const brushLayer = svgEl('rect', { class: 'chart-brush', x: 0, y: padTop, width: 0, height: plotHeight, rx: 3 });
  svg.append(brushLayer);

  const bars = svgEl('g', { class: 'chart-bars' });
  labels.forEach((label, i) => {
    let cursor = padTop + plotHeight;
    live.forEach((s, sIndex) => {
      const value = Number(s.values[i]) || 0;
      const segment = (value / max) * plotHeight;
      const top = cursor - segment;
      const isTop = live.slice(sIndex + 1).every((other) => !(Number(other.values[i]) > 0));
      bars.append(svgEl('rect', {
        class: `chart-bar chart-series-${s.seriesIndex ?? sIndex + 1}`,
        x: padLeft + i * slot + (slot - barWidth) / 2,
        y: top,
        width: barWidth,
        // Only the top of the stack gets the 4px rounded data-end; inner
        // segments stay square so the stack reads as one bar.
        height: value > 0 ? Math.max(1, segment - (sIndex === 0 ? 0 : 2)) : 0,
        rx: isTop ? 3 : 0,
        'data-bucket': i,
        'data-series': sIndex,
      }));
      cursor = top - (value > 0 ? 2 : 0); // 2px surface gap between stacked segments
    });
  });
  svg.append(bars);

  // X labels: only ends and middle, so they never collide.
  const tickIndexes = count <= 8
    ? labels.map((_, i) => i)
    : [0, Math.floor(count / 2), count - 1];
  for (const i of tickIndexes) {
    const text = svgEl('text', {
      class: 'chart-axis', x: padLeft + i * slot + slot / 2, y: height - 6, 'text-anchor': 'middle',
    });
    text.textContent = labels[i];
    svg.append(text);
  }

  const cursorLine = svgEl('line', {
    class: 'chart-cursor', x1: 0, y1: padTop, x2: 0, y2: padTop + plotHeight, visibility: 'hidden',
  });
  svg.append(cursorLine);

  const tooltip = el('div', { class: 'chart-tooltip', hidden: true });
  root.append(svg, tooltip);

  function paintBrush(range) {
    if (!range) { brushLayer.setAttribute('width', '0'); return; }
    const [from, to] = range;
    const lo = Math.min(from, to);
    const hi = Math.max(from, to);
    brushLayer.setAttribute('x', String(padLeft + lo * slot));
    brushLayer.setAttribute('width', String((hi - lo + 1) * slot));
  }
  paintBrush(brush);

  root.update = ({ labels: nextLabels = labels, series: nextSeries = live, brush: nextBrush = null } = {}) => {
    if (nextLabels.length !== labels.length || nextLabels.some((label, index) => label !== labels[index])) return false;
    const nextLive = nextSeries.filter((item) => Array.isArray(item.values) && item.values.length);
    if (nextLive.length !== live.length) return false;
    live = nextLive;
    stackTotals = labels.map((_, index) => live.reduce(
      (total, item) => total + (Number(item.values[index]) || 0), 0,
    ));
    max = niceMax(Math.max(...stackTotals, 0));
    for (const item of gridNodes) {
      const value = (max / 2) * item.step;
      const y = padTop + plotHeight - (value / max) * plotHeight;
      item.line?.setAttribute('y1', String(y));
      item.line?.setAttribute('y2', String(y));
      item.text.setAttribute('y', String(y + 3));
      item.text.textContent = formatValue(value);
    }
    labels.forEach((_, index) => {
      let cursor = padTop + plotHeight;
      live.forEach((item, seriesIndex) => {
        const value = Number(item.values[index]) || 0;
        const segment = (value / max) * plotHeight;
        const top = cursor - segment;
        const rect = bars.querySelector(`[data-bucket="${index}"][data-series="${seriesIndex}"]`);
        if (!rect) return;
        rect.setAttribute('y', String(top));
        rect.setAttribute('height', String(value > 0 ? Math.max(1, segment - (seriesIndex === 0 ? 0 : 2)) : 0));
        cursor = top - (value > 0 ? 2 : 0);
      });
    });
    paintBrush(nextBrush);
    return true;
  };

  function pointIndex(event) {
    const rect = svg.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * width;
    return barIndexAt(x, { padLeft, plotWidth, count });
  }

  let dragFrom = null;
  let dragTo = null;

  svg.addEventListener('mousemove', (event) => {
    const index = pointIndex(event);
    if (index < 0) { tooltip.hidden = true; cursorLine.setAttribute('visibility', 'hidden'); return; }
    if (dragFrom !== null) { dragTo = index; paintBrush([dragFrom, dragTo]); }

    const x = padLeft + index * slot + slot / 2;
    cursorLine.setAttribute('x1', String(x));
    cursorLine.setAttribute('x2', String(x));
    cursorLine.setAttribute('visibility', 'visible');

    tooltip.replaceChildren(
      el('div', { class: 'chart-tooltip-title', text: labels[index] }),
      ...live.map((s) => el('div', { class: 'chart-tooltip-row' }, [
        el('span', { class: `chart-swatch chart-series-${s.seriesIndex ?? 1}`, 'aria-hidden': 'true' }),
        el('span', { class: 'chart-tooltip-label', text: s.label }),
        el('span', { class: 'chart-tooltip-value mono', text: formatValue(Number(s.values[index]) || 0) }),
      ])),
    );
    tooltip.hidden = false;
    const rect = svg.getBoundingClientRect();
    const left = ((x / width) * rect.width);
    tooltip.style.left = `${Math.min(Math.max(left, 8), rect.width - 8)}px`;
  });

  svg.addEventListener('mouseleave', () => {
    tooltip.hidden = true;
    cursorLine.setAttribute('visibility', 'hidden');
  });

  if (onBrush || onSelect) {
    svg.classList.add('is-interactive');
    svg.addEventListener('mousedown', (event) => {
      const index = pointIndex(event);
      if (index < 0) return;
      dragFrom = index;
      dragTo = index;
      paintBrush([index, index]);
      event.preventDefault();
    });
    // Bound on window so a drag that ends outside the chart still resolves.
    const finish = () => {
      if (dragFrom === null) return;
      const from = dragFrom;
      const to = dragTo;
      dragFrom = null;
      dragTo = null;
      if (from === to) {
        // A click is a click, not a one-bucket brush.
        paintBrush(null);
        if (onSelect) onSelect(from);
        else if (onBrush) onBrush(from, from);
        return;
      }
      paintBrush([from, to]);
      if (onBrush) onBrush(Math.min(from, to), Math.max(from, to));
    };
    window.addEventListener('mouseup', finish);
    root.dispose = () => window.removeEventListener('mouseup', finish);
  }

  return root;
}

/** Legend row. Required whenever a chart carries two or more series. */
export function chartLegend(series = []) {
  return el('div', { class: 'chart-legend' }, series.filter(Boolean).map((s, i) => el('span', {
    class: 'chart-legend-item',
  }, [
    el('span', { class: `chart-swatch chart-series-${s.seriesIndex ?? i + 1}`, 'aria-hidden': 'true' }),
    el('span', { text: s.label }),
  ])));
}
