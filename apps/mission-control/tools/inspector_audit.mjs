#!/usr/bin/env node
// Inspector layout audit — what actually lands in the 320px right sidebar.
//
// There is no browser on this host, so this cannot measure pixels. What it can
// do is remove the guesswork about *which* CSS is in play: it mounts every tab,
// drives a selection so the inspector renders its populated state rather than
// its empty hint, and reports every class name that appears in the inspector
// subtree. Those classes are then checked against a hazard list derived from
// styles.css — rules that cannot survive a 320px column.
//
// A class only fails here if it is genuinely reachable inside the inspector, so
// the output is a work list rather than a lint of the whole stylesheet.
//
// Usage:  node tools/inspector_audit.mjs [base-url] [tab ...]

import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';
import {
  installDom, Node, makeApi, rawRequest, TABS, DIST,
} from './render_smoke.mjs';

const ONLY = process.argv.slice(3).filter((a) => !a.startsWith('http'));
const CSS = fs.readFileSync(path.resolve('frontend/dist/styles.css'), 'utf8');

// --------------------------------------------------------------- hazard rules

// Each rule reads the declaration block of a class and decides whether that
// block can hold up in a 320px column. Kept deliberately small and specific —
// a broad heuristic here produces noise that nobody acts on.
const HAZARDS = [
  {
    id: 'fixed-min-width',
    test: (block) => {
      const m = block.match(/min-width:\s*(\d+)px/);
      return m && Number(m[1]) > 240 ? `min-width:${m[1]}px exceeds the ~296px content box` : null;
    },
  },
  {
    id: 'fixed-width',
    test: (block) => {
      const m = block.match(/(?<!min-|max-)\bwidth:\s*(\d+)px/);
      return m && Number(m[1]) > 296 ? `width:${m[1]}px exceeds the ~296px content box` : null;
    },
  },
  {
    id: 'nowrap-flex-row',
    test: (block) => (
      /display:\s*flex/.test(block)
      && !/flex-wrap:\s*wrap/.test(block)
      && !/flex-direction:\s*column/.test(block)
      && /gap:/.test(block)
        ? 'horizontal flex row without flex-wrap — children cannot reflow'
        : null),
  },
  {
    id: 'rigid-grid-columns',
    test: (block) => {
      const m = block.match(/grid-template-columns:\s*([^;]+)/);
      if (!m) return null;
      const spec = m[1];
      // Sum the fixed px minimums the track list demands.
      const mins = [...spec.matchAll(/(\d+)px/g)].map((x) => Number(x[1]));
      const total = mins.reduce((a, b) => a + b, 0);
      return total > 296 ? `grid tracks demand ≥${total}px` : null;
    },
  },
];

// Classes reviewed and judged safe at 320px, with the reason. The heuristics
// above are deliberately blunt, so this list is what keeps the tool's output at
// zero — a new entry should be added only after actually reading the rule.
const REVIEWED_SAFE = new Map([
  ['rack-more', 'full-width centred button holding a short "show N more" label'],
  ['rack-card-top', 'has min-width:0 and a flex:none dot; the title truncates by design'],
  ['rack-card-actions', 'a few 24px icon buttons, ~100px total, revealed on hover'],
]);

/**
 * Merge every declaration block for a class into one, in source order, so a
 * later rule that fixes an earlier one is respected. Without this the audit
 * reports `.detail-actions` as unwrappable because an early block omits
 * `flex-wrap`, even though a later block adds it and wins the cascade.
 */
function computedFor(cls) {
  const re = new RegExp(`(^|[,}\\s])\\.${cls.replace(/[-/\\^$*+?.()|[\]{}]/g, '\\$&')}(?![\\w-])[^{}]*\\{([^}]*)\\}`, 'g');
  const decls = new Map();
  let m;
  let found = false;
  while ((m = re.exec(CSS))) {
    found = true;
    for (const part of m[2].split(';')) {
      const idx = part.indexOf(':');
      if (idx < 0) continue;
      decls.set(part.slice(0, idx).trim(), part.slice(idx + 1).trim());
    }
  }
  if (!found) return null;
  return [...decls].map(([k, v]) => `${k}: ${v}`).join('; ');
}

function collectClasses(node, acc = new Set()) {
  if (!node || !node.classList) return acc;
  for (const c of node.classList.set) acc.add(c);
  for (const child of node.children || []) collectClasses(child, acc);
  return acc;
}

/**
 * Click the first selectable row so the inspector shows a populated detail
 * rather than its empty hint — the populated state is the one with the
 * interesting layout. Returns whether anything was actually clicked.
 */
function driveSelection(container) {
  // This audit runs against the live Hermes host, so the click set is limited
  // to targets that are provably selection-only. `createTable` binds row clicks
  // on the <tr> itself (table.js:124) and does nothing but call `onSelect`;
  // skills' rack cards are the same. Generic buttons are excluded on purpose —
  // Fire cron, Enable plugin, Approve permit and models' MoA preset button are
  // all real mutations. Dispatch bubbles upward only, so clicking a row can
  // never reach an action button nested inside it.
  const rows = [];
  (function walk(node, inHead) {
    if (!node || !node.children) return;
    const cls = node.classList ? [...node.classList.set] : [];
    // A <thead> row carries no click listener, so taking it would leave the
    // inspector on its empty hint and quietly under-report the real layout.
    if ((node.tagName === 'TR' && !inHead) || cls.includes('rack-card')) rows.push(node);
    for (const child of node.children) walk(child, inHead || node.tagName === 'THEAD');
  }(container, false));

  let clicked = 0;
  for (const node of rows.slice(0, 1)) {
    try { node.click(); clicked += 1; } catch { /* not selectable */ }
  }
  return clicked > 0;
}

async function run() {
  const stray = [];
  process.on('unhandledRejection', (err) => stray.push(err));

  const { body } = installDom();
  const api = makeApi();
  await rawRequest('/api/csrf');
  const sse = { on: () => () => {}, close: () => {} };

  const findings = new Map();
  let audited = 0;
  let withInspector = 0;

  for (const [route, factoryName] of TABS) {
    if (ONLY.length && !ONLY.includes(route)) continue;
    const container = new Node('div');
    const toolbar = new Node('div');
    const inspector = new Node('div');
    inspector.classList.add('inspector');
    body.append(container, toolbar, inspector);

    try {
      const module = await import(pathToFileURL(path.join(DIST, 'tabs', `${route}.js`)).href);
      const factory = module[factoryName];
      if (typeof factory !== 'function') continue;
      const instance = factory({
        api, profile: null, events: null, sse, toolbar,
        onNavigate: () => {}, refreshInspector: () => {},
      });
      instance.mount(container);
      await instance.activate({});
      await new Promise((r) => setTimeout(r, 250));
      audited += 1;

      if (typeof instance.renderInspector !== 'function') {
        console.log(`[ -- ] ${route.padEnd(16)} no renderInspector (uses shell placeholder)`);
        continue;
      }
      withInspector += 1;
      instance.renderInspector(inspector);
      const selected = driveSelection(container);
      instance.renderInspector(inspector);
      await new Promise((r) => setTimeout(r, 80));

      const classes = [...collectClasses(inspector)].sort();
      const state = selected ? 'detail' : 'hint-only';
      const hits = [];
      for (const cls of classes) {
        if (REVIEWED_SAFE.has(cls)) continue;
        const block = computedFor(cls);
        if (!block) continue;
        for (const rule of HAZARDS) {
          const why = rule.test(block);
          if (!why) continue;
          hits.push(`.${cls}: ${why}`);
          const key = `.${cls} — ${why}`;
          if (!findings.has(key)) findings.set(key, new Set());
          findings.get(key).add(route);
        }
      }
      const uniq = [...new Set(hits)];
      const mark = uniq.length ? 'WARN' : ' ok ';
      console.log(`[${mark}] ${route.padEnd(16)} ${String(classes.length).padStart(2)} classes · ${state}${uniq.length ? ` · ${uniq.length} hazard(s)` : ''}`);
      for (const h of uniq) console.log(`         ↳ ${h}`);
      stray.length = 0;
    } catch (err) {
      console.log(`[FAIL] ${route.padEnd(16)} ${err.message}`);
    }
  }

  console.log(`\n${audited} tabs audited · ${withInspector} own the inspector · ${findings.size} distinct hazards`);
  if (findings.size) {
    console.log('\nDistinct hazards, by reach:');
    const ranked = [...findings.entries()].sort((a, b) => b[1].size - a[1].size);
    for (const [key, routes] of ranked) {
      console.log(`  ${key}\n     in: ${[...routes].join(', ')}`);
    }
    console.log('\nINSPECTOR_AUDIT=WARN');
  } else {
    console.log('INSPECTOR_AUDIT=CLEAN');
  }
}

run().catch((err) => { console.error(err); process.exitCode = 1; });
