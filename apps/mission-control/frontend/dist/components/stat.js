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
 * @param {string} [opts.tone]      'ok' | 'warn' | 'danger' — colors the value
 */
export function createStat({
  label = '', value = null, iconName = null, delta = null, deltaUnit = '%',
  spark = null, seriesIndex = 1, foot = '', onClick = null, tone = null,
} = {}) {
  const node = el(onClick ? 'button' : 'div', {
    class: `stat${onClick ? ' is-clickable' : ''}${tone ? ` stat-tone-${tone}` : ''}`,
    type: onClick ? 'button' : null,
    onclick: onClick || null,
  });

  const head = el('div', { class: 'stat-head' });
  const iconHost = el('span', { class: 'stat-icon', 'aria-hidden': 'true' });
  const labelNode = el('span', { class: 'stat-label' });
  const spacer = el('span', { style: 'flex:1' });
  const deltaHost = el('span', { class: 'stat-delta-host' });
  head.append(iconHost, labelNode, spacer, deltaHost);
  node.append(head);

  const valueNode = el('div', { class: 'stat-value' });
  const footNode = el('div', { class: 'stat-foot' });
  const sparkHost = el('div', { class: 'stat-spark-host' });
  node.append(valueNode, footNode, sparkHost);

  let previousIcon = null;
  let previousSpark = '';
  node.update = ({
    label: nextLabel = '', value: nextValue = null, iconName: nextIcon = null,
    delta: nextDelta = null, deltaUnit: nextDeltaUnit = '%', spark: nextSpark = null,
    seriesIndex: nextSeriesIndex = 1, foot: nextFoot = '', onClick: nextOnClick = null,
  } = {}) => {
    labelNode.textContent = nextLabel;
    valueNode.textContent = nextValue === null || nextValue === undefined ? '—' : String(nextValue);
    footNode.textContent = nextFoot || '';
    footNode.hidden = !nextFoot;

    if (previousIcon !== nextIcon) {
      iconHost.replaceChildren(...(nextIcon ? [icon(nextIcon, { size: 12 })] : []));
      previousIcon = nextIcon;
    }
    deltaHost.replaceChildren(...(() => {
      const next = deltaNode(nextDelta, { unit: nextDeltaUnit });
      return next ? [next] : [];
    })());
    spacer.hidden = !deltaHost.firstChild;

    const sparkKey = JSON.stringify([nextSpark || [], nextSeriesIndex]);
    if (sparkKey !== previousSpark) {
      const next = statSpark(nextSpark, { seriesIndex: nextSeriesIndex });
      sparkHost.replaceChildren(...(next ? [next] : []));
      sparkHost.hidden = !next;
      previousSpark = sparkKey;
    }

    if (node.tagName === 'BUTTON') {
      node.onclick = nextOnClick || null;
      node.disabled = !nextOnClick;
    }
  };
  node.update({ label, value, iconName, delta, deltaUnit, spark, seriesIndex, foot, onClick });
  return node;
}

/** Row of tiles. Pass the same specs `createStat` takes. */
export function createStatRow(specs = []) {
  const row = el('div', { class: 'stat-row' });
  const nodes = new Map();
  row.setStats = (nextSpecs = []) => {
    const ordered = [];
    const active = new Set();
    nextSpecs.filter(Boolean).forEach((spec, i) => {
      const normalized = { seriesIndex: (i % 8) + 1, ...spec };
      const key = String(spec.key || spec.label || i);
      active.add(key);
      let node = nodes.get(key);
      if (!node || (node.tagName === 'BUTTON') !== Boolean(spec.onClick)) {
        node = createStat(normalized);
        nodes.set(key, node);
      } else {
        node.update(normalized);
      }
      ordered.push(node);
    });
    for (const [key, node] of nodes) {
      if (!active.has(key)) {
        node.remove();
        nodes.delete(key);
      }
    }
    row.append(...ordered);
  };
  row.setStats(specs);
  return row;
}
