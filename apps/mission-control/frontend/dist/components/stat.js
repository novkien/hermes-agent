// KPI tiles and the sparkline that goes in them. Series colors come from the
// validated data-viz ramp (--series-*), never from the semantic status colors —
// a cyan bar means "series 1", not "healthy".

import { el } from '../ui.js';
import { icon } from '../icons.js';

const SVG = 'http://www.w3.org/2000/svg';

export function statSpark(values = [], { seriesIndex = 1, height = 24 } = {}) {
  const nums = (values || []).filter((v) => typeof v === 'number' && Number.isFinite(v));
  if (nums.length < 2) return null;
  const width = 100;
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min || 1;
  const stepX = width / (nums.length - 1);
  const points = nums.map((v, i) => `${(i * stepX).toFixed(2)},${(height - ((v - min) / span) * height).toFixed(2)}`);
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('class', 'stat-spark');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('aria-hidden', 'true');
  const line = document.createElementNS(SVG, 'polyline');
  line.setAttribute('points', points.join(' '));
  line.setAttribute('stroke', `var(--series-${seriesIndex})`);
  svg.append(line);
  return svg;
}

function deltaNode(delta, { unit = '%' } = {}) {
  if (delta === null || delta === undefined || !Number.isFinite(delta)) return null;
  const dir = delta > 0 ? 'up' : delta < 0 ? 'down' : 'flat';
  const glyph = delta > 0 ? '▲' : delta < 0 ? '▼' : '■';
  const sign = delta > 0 ? '+' : '';
  return el('span', { class: `stat-delta stat-delta-${dir}` }, [
    el('span', { text: glyph, 'aria-hidden': 'true' }),
    `${sign}${delta}${unit}`,
  ]);
}

/**
 * @param {object} opts
 * @param {string} opts.label
 * @param {*}      opts.value        rendered as-is; `null` becomes an em dash
 * @param {number} [opts.delta]      trend vs. previous window
 * @param {Array}  [opts.spark]      real observed series only — never fabricated
 * @param {string} [opts.foot]       small caption under the value
 * @param {Function} [opts.onClick]  makes the tile a drill-down target
 */
export function createStat({
  label = '', value = null, iconName = null, delta = null, deltaUnit = '%',
  spark = null, seriesIndex = 1, foot = '', onClick = null,
} = {}) {
  const node = el(onClick ? 'button' : 'div', {
    class: `stat${onClick ? ' is-clickable' : ''}`,
    type: onClick ? 'button' : null,
    onclick: onClick || null,
  });

  const head = el('div', { class: 'stat-head' });
  if (iconName) head.append(icon(iconName, { size: 12 }));
  head.append(el('span', { class: 'stat-label', text: label }));
  const d = deltaNode(delta, { unit: deltaUnit });
  if (d) { head.append(el('span', { style: 'flex:1' })); head.append(d); }
  node.append(head);

  node.append(el('div', {
    class: 'stat-value',
    text: value === null || value === undefined ? '—' : String(value),
  }));
  if (foot) node.append(el('div', { class: 'stat-foot', text: foot }));
  const sparkNode = statSpark(spark, { seriesIndex });
  if (sparkNode) node.append(sparkNode);
  return node;
}

/** Row of tiles. Pass the same specs `createStat` takes. */
export function createStatRow(specs = []) {
  const row = el('div', { class: 'stat-row' });
  specs.filter(Boolean).forEach((spec, i) => {
    row.append(createStat({ seriesIndex: (i % 8) + 1, ...spec }));
  });
  return row;
}
