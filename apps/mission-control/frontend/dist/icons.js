// Hand-authored inline-SVG icon set — no external CDN/icon font. Line-art,
// 24x24 viewbox, currentColor stroke. One glyph per nav route + a small
// utility set (search/retry/close/chevron/check/warning/inbox/plug).

const SVG_NS = 'http://www.w3.org/2000/svg';

function shape(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

// Each entry is a list of [tag, attrs] child shape specs on a 24x24 grid.
const ICONS = {
  overview: [
    ['rect', { x: 3, y: 3, width: 8, height: 8, rx: 1.5 }],
    ['rect', { x: 13, y: 3, width: 8, height: 5, rx: 1.5 }],
    ['rect', { x: 13, y: 10, width: 8, height: 11, rx: 1.5 }],
    ['rect', { x: 3, y: 13, width: 8, height: 8, rx: 1.5 }],
  ],
  chat: [
    ['path', { d: 'M4 5h16v10H9l-4 4v-4H4z', 'stroke-linejoin': 'round' }],
    ['line', { x1: 8, y1: 9, x2: 16, y2: 9 }],
    ['line', { x1: 8, y1: 12, x2: 13, y2: 12 }],
  ],
  sessions: [
    ['rect', { x: 3.5, y: 4, width: 17, height: 16, rx: 2 }],
    ['line', { x1: 3.5, y1: 9, x2: 20.5, y2: 9 }],
    ['line', { x1: 7, y1: 13, x2: 14, y2: 13 }],
    ['line', { x1: 7, y1: 16.5, x2: 12, y2: 16.5 }],
  ],
  fleet: [
    ['circle', { cx: 12, cy: 5, r: 2.2 }],
    ['circle', { cx: 5, cy: 18, r: 2.2 }],
    ['circle', { cx: 19, cy: 18, r: 2.2 }],
    ['line', { x1: 12, y1: 7.2, x2: 6.3, y2: 16.2 }],
    ['line', { x1: 12, y1: 7.2, x2: 17.7, y2: 16.2 }],
  ],
  kanban: [
    ['rect', { x: 3, y: 4, width: 5, height: 16, rx: 1.3 }],
    ['rect', { x: 9.5, y: 4, width: 5, height: 10, rx: 1.3 }],
    ['rect', { x: 16, y: 4, width: 5, height: 13, rx: 1.3 }],
  ],
  'run-inspector': [
    ['circle', { cx: 10, cy: 10, r: 6.2 }],
    ['line', { x1: 14.4, y1: 14.4, x2: 20.5, y2: 20.5 }],
    ['path', { d: 'M7.6 10l1.7 1.8 3-3.6', 'stroke-linejoin': 'round' }],
  ],
  cron: [
    ['circle', { cx: 12, cy: 12, r: 8.2 }],
    ['line', { x1: 12, y1: 12, x2: 12, y2: 7 }],
    ['line', { x1: 12, y1: 12, x2: 15.5, y2: 14 }],
  ],
  activity: [
    ['path', { d: 'M3 12h4l2 7 4-14 2 7h6', 'stroke-linejoin': 'round' }],
  ],
  alerts: [
    ['path', { d: 'M12 3.5l9.5 16.5H2.5z', 'stroke-linejoin': 'round' }],
    ['line', { x1: 12, y1: 10, x2: 12, y2: 14.2 }],
    ['circle', { cx: 12, cy: 17, r: 0.9, fill: 'currentColor', stroke: 'none' }],
  ],
  analytics: [
    ['line', { x1: 3.5, y1: 20.5, x2: 20.5, y2: 20.5 }],
    ['rect', { x: 5, y: 13, width: 3.4, height: 7.5 }],
    ['rect', { x: 10.3, y: 8, width: 3.4, height: 12.5 }],
    ['rect', { x: 15.6, y: 4, width: 3.4, height: 16.5 }],
  ],
  issues: [
    ['circle', { cx: 12, cy: 12, r: 8.5 }],
    ['line', { x1: 12, y1: 8, x2: 12, y2: 13 }],
    ['circle', { cx: 12, cy: 16, r: 0.9, fill: 'currentColor', stroke: 'none' }],
  ],
  permits: [
    ['path', { d: 'M12 3l7 2.6v6c0 4.6-2.9 8-7 9.4-4.1-1.4-7-4.8-7-9.4v-6z', 'stroke-linejoin': 'round' }],
    ['path', { d: 'M8.7 12l2.3 2.3 4.3-4.6', 'stroke-linejoin': 'round' }],
  ],
  'room-binding': [
    ['rect', { x: 3, y: 6, width: 6, height: 12, rx: 1.5 }],
    ['rect', { x: 15, y: 6, width: 6, height: 12, rx: 1.5 }],
    ['line', { x1: 9, y1: 12, x2: 15, y2: 12 }],
  ],
  // Three lanes with a cross-link — one agent thread messaging another.
  threads: [
    ['line', { x1: 4, y1: 5.5, x2: 20, y2: 5.5 }],
    ['line', { x1: 4, y1: 12, x2: 20, y2: 12 }],
    ['line', { x1: 4, y1: 18.5, x2: 20, y2: 18.5 }],
    ['circle', { cx: 8.5, cy: 5.5, r: 1.8 }],
    ['circle', { cx: 15.5, cy: 12, r: 1.8 }],
    ['circle', { cx: 8.5, cy: 18.5, r: 1.8 }],
  ],
  'action-audit': [
    ['rect', { x: 3.5, y: 3.5, width: 13, height: 17, rx: 1.5 }],
    ['path', { d: 'M6.5 8.5h7M6.5 12h7M6.5 15.5h4' }],
    ['circle', { cx: 17, cy: 17, r: 3.2 }],
    ['line', { x1: 19.3, y1: 19.3, x2: 21.5, y2: 21.5 }],
  ],
  skills: [
    ['path', { d: 'M12 3l2.4 5.8L20.5 9l-4.7 4 1.4 6.3L12 16l-5.2 3.3L8.2 13 3.5 9l6.1-0.2z', 'stroke-linejoin': 'round' }],
  ],
  memory: [
    ['rect', { x: 6, y: 6, width: 12, height: 12, rx: 1.5 }],
    ['line', { x1: 9, y1: 2.3, x2: 9, y2: 6 }],
    ['line', { x1: 15, y1: 2.3, x2: 15, y2: 6 }],
    ['line', { x1: 9, y1: 18, x2: 9, y2: 21.7 }],
    ['line', { x1: 15, y1: 18, x2: 15, y2: 21.7 }],
    ['line', { x1: 2.3, y1: 9, x2: 6, y2: 9 }],
    ['line', { x1: 2.3, y1: 15, x2: 6, y2: 15 }],
    ['line', { x1: 18, y1: 9, x2: 21.7, y2: 9 }],
    ['line', { x1: 18, y1: 15, x2: 21.7, y2: 15 }],
  ],
  profiles: [
    ['circle', { cx: 12, cy: 8.3, r: 3.6 }],
    ['path', { d: 'M4.5 20c1.2-4 4-6 7.5-6s6.3 2 7.5 6' }],
  ],
  models: [
    ['path', { d: 'M12 3.5l7.5 4.3v8.4L12 20.5l-7.5-4.3V7.8z', 'stroke-linejoin': 'round' }],
    ['path', { d: 'M4.5 7.8L12 12l7.5-4.2M12 12v8.5' }],
  ],
  tools: [
    ['path', { d: 'M15.5 3.5a4 4 0 00-5.4 4.9L4 14.5V19h4.5l6-6.1a4 4 0 004.9-5.4l-3 3-2-2z', 'stroke-linejoin': 'round' }],
  ],
  mcp: [
    ['path', { d: 'M9 3.5v5M15 3.5v5M6.5 8.5h11v3.5a5 5 0 01-5 5h-1a5 5 0 01-5-5z', 'stroke-linejoin': 'round' }],
    ['line', { x1: 12, y1: 17, x2: 12, y2: 20.5 }],
  ],
  plugins: [
    ['path', {
      d: 'M9 4h3v2.4a1.6 1.6 0 003.2 0V4H18v3.4h-2.4a1.6 1.6 0 000 3.2H18V14h-3v-2.4a1.6 1.6 0 00-3.2 0V14H9v-3.4h2.4a1.6 1.6 0 000-3.2H9z',
      'stroke-linejoin': 'round',
    }],
  ],
  webhooks: [
    ['path', { d: 'M13 3L5 13.5h5.5L10 21l8-11h-5.5z', 'stroke-linejoin': 'round' }],
  ],
  channels: [
    ['path', { d: 'M4 10v4h3.5L14 18v-12L7.5 10z', 'stroke-linejoin': 'round' }],
    ['path', { d: 'M17 9.5a4 4 0 010 5' }],
    ['path', { d: 'M19.3 7.2a7.4 7.4 0 010 9.6' }],
  ],
  artifacts: [
    ['path', { d: 'M4 8l8-4.5L20 8v8l-8 4.5L4 16z', 'stroke-linejoin': 'round' }],
    ['path', { d: 'M4 8l8 4.5L20 8M12 12.5V21' }],
  ],
  files: [
    ['path', { d: 'M4 6.5a1.5 1.5 0 011.5-1.5H10l2 2.5h6.5A1.5 1.5 0 0120 9V17.5A1.5 1.5 0 0118.5 19h-13A1.5 1.5 0 014 17.5z', 'stroke-linejoin': 'round' }],
  ],
  logs: [
    ['rect', { x: 3.5, y: 4, width: 17, height: 16, rx: 1.5 }],
    ['path', { d: 'M6.5 9l2.5 2-2.5 2' }],
    ['line', { x1: 11, y1: 13, x2: 15, y2: 13 }],
  ],
  'command-center': [
    ['rect', { x: 3, y: 4.5, width: 18, height: 15, rx: 1.8 }],
    ['path', { d: 'M6.5 9.5l3 2.5-3 2.5' }],
    ['line', { x1: 11.5, y1: 14.5, x2: 15.5, y2: 14.5 }],
  ],
  settings: [
    ['circle', { cx: 12, cy: 12, r: 3.1 }],
    ['path', {
      d: 'M12 3.8v2.1M12 18.1v2.1M20.2 12h-2.1M5.9 12H3.8M17.6 6.4l-1.5 1.5M7.9 16.1l-1.5 1.5M17.6 17.6l-1.5-1.5M7.9 7.9L6.4 6.4',
    }],
  ],
  'llama-proxy': [
    ['circle', { cx: 12, cy: 12, r: 2.4 }],
    ['path', { d: 'M15.8 8.2a5 5 0 010 7.6' }],
    ['path', { d: 'M18.3 5.7a8.6 8.6 0 010 12.6' }],
    ['path', { d: 'M8.2 8.2a5 5 0 000 7.6' }],
    ['path', { d: 'M5.7 5.7a8.6 8.6 0 000 12.6' }],
  ],

  // utility glyphs
  search: [
    ['circle', { cx: 10.5, cy: 10.5, r: 6.5 }],
    ['line', { x1: 15.2, y1: 15.2, x2: 21, y2: 21 }],
  ],
  retry: [
    ['path', { d: 'M20 12a8 8 0 10-2.5 5.8', 'stroke-linecap': 'round' }],
    ['path', { d: 'M20 6.5V12h-5.5', 'stroke-linejoin': 'round' }],
  ],
  close: [
    ['line', { x1: 5.5, y1: 5.5, x2: 18.5, y2: 18.5 }],
    ['line', { x1: 18.5, y1: 5.5, x2: 5.5, y2: 18.5 }],
  ],
  'chevron-right': [
    ['path', { d: 'M9 5l7 7-7 7', 'stroke-linejoin': 'round' }],
  ],
  'arrow-left': [
    ['line', { x1: 20, y1: 12, x2: 4.5, y2: 12 }],
    ['path', { d: 'M10.5 5.5L4 12l6.5 6.5', 'stroke-linejoin': 'round' }],
  ],
  'chevron-down': [
    ['path', { d: 'M5 9l7 7 7-7', 'stroke-linejoin': 'round' }],
  ],
  check: [
    ['path', { d: 'M4.5 12.5l5 5L20 6', 'stroke-linejoin': 'round' }],
  ],
  warning: [
    ['path', { d: 'M12 3.5l9.5 16.5H2.5z', 'stroke-linejoin': 'round' }],
    ['line', { x1: 12, y1: 10, x2: 12, y2: 14.2 }],
    ['circle', { cx: 12, cy: 17, r: 0.9, fill: 'currentColor', stroke: 'none' }],
  ],
  inbox: [
    ['path', { d: 'M4 12.5h4.5l1.5 3h4l1.5-3H20', 'stroke-linejoin': 'round' }],
    ['path', { d: 'M4 12.5L6 5h12l2 7.5v6A1.5 1.5 0 0118.5 20h-13A1.5 1.5 0 014 18.5z', 'stroke-linejoin': 'round' }],
  ],
  plug: [
    ['path', { d: 'M9 2.5v5M15 2.5v5M6.5 7.5h11v4a5.5 5.5 0 01-11 0z', 'stroke-linejoin': 'round' }],
    ['line', { x1: 12, y1: 16.5, x2: 12, y2: 21.5 }],
  ],
  'external-link': [
    ['path', { d: 'M9.5 6H5.5a1.5 1.5 0 00-1.5 1.5v11A1.5 1.5 0 005.5 20h11a1.5 1.5 0 001.5-1.5v-4' }],
    ['path', { d: 'M14 4h6v6M20 4l-9.5 9.5', 'stroke-linejoin': 'round' }],
  ],
  spark: [
    ['path', { d: 'M12 3v5M12 16v5M3 12h5M16 12h5M6 6l3.2 3.2M18 18l-3.2-3.2M6 18l3.2-3.2M18 6l-3.2 3.2' }],
  ],
  plus: [
    ['line', { x1: 12, y1: 5, x2: 12, y2: 19 }],
    ['line', { x1: 5, y1: 12, x2: 19, y2: 12 }],
  ],
  power: [
    ['path', { d: 'M7.5 6.5a7.5 7.5 0 109 0' }],
    ['line', { x1: 12, y1: 3, x2: 12, y2: 11.5 }],
  ],
  ban: [
    ['circle', { cx: 12, cy: 12, r: 8.5 }],
    ['line', { x1: 6.2, y1: 6.2, x2: 17.8, y2: 17.8 }],
  ],
  archive: [
    ['rect', { x: 3, y: 4, width: 18, height: 4.2, rx: 1.2 }],
    ['path', { d: 'M5 8.2v10A1.8 1.8 0 006.8 20h10.4a1.8 1.8 0 001.8-1.8v-10' }],
    ['line', { x1: 9.8, y1: 12, x2: 14.2, y2: 12 }],
  ],
  trash: [
    ['path', { d: 'M4.5 6.5h15' }],
    ['path', { d: 'M9.5 6.5V4.6A1.1 1.1 0 0110.6 3.5h2.8a1.1 1.1 0 011.1 1.1v1.9' }],
    ['path', { d: 'M6.8 6.5l.9 12a1.6 1.6 0 001.6 1.5h5.4a1.6 1.6 0 001.6-1.5l.9-12' }],
    ['line', { x1: 10.4, y1: 10, x2: 10.8, y2: 16.5 }],
    ['line', { x1: 13.6, y1: 10, x2: 13.2, y2: 16.5 }],
  ],
  save: [
    ['path', { d: 'M4.5 5.8A1.3 1.3 0 015.8 4.5h9.6L19.5 8.6v9.6a1.3 1.3 0 01-1.3 1.3H5.8a1.3 1.3 0 01-1.3-1.3z', 'stroke-linejoin': 'round' }],
    ['path', { d: 'M8 4.5v5h7v-5' }],
    ['rect', { x: 8, y: 13, width: 8, height: 6.4, rx: 0.8 }],
  ],
  copy: [
    ['rect', { x: 8.5, y: 8.5, width: 11, height: 11, rx: 1.6 }],
    ['path', { d: 'M15.5 5.6V5A1.5 1.5 0 0014 3.5H6A1.5 1.5 0 004.5 5v8A1.5 1.5 0 006 14.5h.6' }],
  ],
  // A filled square is the universal "stop a running thing" glyph; `pause`
  // would promise a resume the gateway cannot offer.
  stop: [
    ['rect', { x: 6.5, y: 6.5, width: 11, height: 11, rx: 1.6, fill: 'currentColor' }],
  ],
  // Fork: one line splitting into two, for branching a conversation.
  branch: [
    ['circle', { cx: 7, cy: 5.8, r: 2.3 }],
    ['circle', { cx: 7, cy: 18.2, r: 2.3 }],
    ['circle', { cx: 17, cy: 9.5, r: 2.3 }],
    ['path', { d: 'M7 8.1v7.8', 'stroke-linecap': 'round' }],
    ['path', { d: 'M17 11.8c0 2.6-2.1 4.1-5.2 4.6-1.4.2-2.5.6-3.2 1.1', 'stroke-linejoin': 'round' }],
  ],
  // Pin, for keeping a session at the top of the browser.
  pin: [
    ['path', { d: 'M9.2 3.8h5.6l-.8 5.2 3.2 3.1H6.8l3.2-3.1z', 'stroke-linejoin': 'round' }],
    ['line', { x1: 12, y1: 12.1, x2: 12, y2: 20.2 }],
  ],
  download: [
    ['line', { x1: 12, y1: 3.5, x2: 12, y2: 14.5 }],
    ['path', { d: 'M7.8 10.6L12 14.8l4.2-4.2', 'stroke-linejoin': 'round' }],
    ['path', { d: 'M4.5 16.5v2A1.8 1.8 0 006.3 20.3h11.4a1.8 1.8 0 001.8-1.8v-2' }],
  ],
  eye: [
    ['path', { d: 'M2.8 12S6.5 5.8 12 5.8 21.2 12 21.2 12 17.5 18.2 12 18.2 2.8 12 2.8 12z', 'stroke-linejoin': 'round' }],
    ['circle', { cx: 12, cy: 12, r: 2.9 }],
  ],
  code: [
    ['path', { d: 'M8.6 8.2L4.5 12l4.1 3.8', 'stroke-linejoin': 'round' }],
    ['path', { d: 'M15.4 8.2L19.5 12l-4.1 3.8', 'stroke-linejoin': 'round' }],
    ['line', { x1: 13.4, y1: 5, x2: 10.6, y2: 19 }],
  ],
  wrap: [
    ['line', { x1: 4, y1: 6.5, x2: 20, y2: 6.5 }],
    ['path', { d: 'M4 12h12.5a3 3 0 010 6H12', 'stroke-linejoin': 'round' }],
    ['path', { d: 'M13.8 15.8L11.6 18l2.2 2.2', 'stroke-linejoin': 'round' }],
  ],
  lock: [
    ['rect', { x: 5, y: 10.5, width: 14, height: 9.5, rx: 1.8 }],
    ['path', { d: 'M8.3 10.5V7.8a3.7 3.7 0 017.4 0v2.7' }],
  ],
  tag: [
    ['path', { d: 'M11.2 3.5H20v8.8l-8.4 8.4a1.6 1.6 0 01-2.3 0l-6.5-6.5a1.6 1.6 0 010-2.3z', 'stroke-linejoin': 'round' }],
    ['circle', { cx: 16.2, cy: 7.8, r: 1.3 }],
  ],
  doc: [
    ['path', { d: 'M6 3.5h7.5L19 9v11.5a1 1 0 01-1 1H6a1 1 0 01-1-1v-16a1 1 0 011-1z', 'stroke-linejoin': 'round' }],
    ['path', { d: 'M13.2 3.6V9H19' }],
    ['path', { d: 'M8 13h8M8 16.5h5' }],
  ],
  filter: [
    ['path', { d: 'M3.5 5.5h17l-6.6 7.6v5.3l-3.8 2v-7.3z', 'stroke-linejoin': 'round' }],
  ],
  sort: [
    ['path', { d: 'M7 4.5v15M4 16.5l3 3 3-3', 'stroke-linejoin': 'round' }],
    ['line', { x1: 12.5, y1: 7, x2: 20.5, y2: 7 }],
    ['line', { x1: 12.5, y1: 12, x2: 18, y2: 12 }],
    ['line', { x1: 12.5, y1: 17, x2: 15.5, y2: 17 }],
  ],
  flame: [
    ['path', { d: 'M12 3.2c3.6 3.3 5.6 6 5.6 8.8a5.6 5.6 0 11-11.2 0c0-1.5.6-2.9 1.7-4.2.4 1.3 1.1 2.1 2 2.4-.3-2.6.3-4.9 1.9-7z', 'stroke-linejoin': 'round' }],
  ],
  refresh: [
    ['path', { d: 'M20.2 10.5A8.3 8.3 0 005.4 7.2', 'stroke-linejoin': 'round' }],
    ['path', { d: 'M3.8 13.5a8.3 8.3 0 0014.8 3.3', 'stroke-linejoin': 'round' }],
    ['path', { d: 'M4.6 3.4v3.9h3.9M19.4 20.6v-3.9h-3.9', 'stroke-linejoin': 'round' }],
  ],
  play: [
    ['path', { d: 'M8 5.4l10 6.6-10 6.6z', 'stroke-linejoin': 'round' }],
  ],
  pause: [
    ['line', { x1: 9.5, y1: 5, x2: 9.5, y2: 19 }],
    ['line', { x1: 14.5, y1: 5, x2: 14.5, y2: 19 }],
  ],
  pencil: [
    ['path', { d: 'M4 20h4l10-10-4-4L4 16z', 'stroke-linejoin': 'round' }],
    ['line', { x1: 13.5, y1: 6.5, x2: 17.5, y2: 10.5 }],
  ],
  link: [
    ['path', { d: 'M10 14a4 4 0 015.7 0l2.6-2.6a4 4 0 00-5.7-5.7L10.4 8.2', 'stroke-linejoin': 'round' }],
    ['path', { d: 'M14 10a4 4 0 01-5.7 0l-2.6 2.6a4 4 0 005.7 5.7l2.2-2.5', 'stroke-linejoin': 'round' }],
  ],
};

const DEFAULT_ICON = [
  ['circle', { cx: 12, cy: 12, r: 8 }],
];

export function icon(name, { size = 16, className = '' } = {}) {
  const svg = shape('svg', {
    viewBox: '0 0 24 24',
    width: String(size),
    height: String(size),
    fill: 'none',
    stroke: 'currentColor',
    'stroke-width': '1.7',
    'stroke-linecap': 'round',
    'stroke-linejoin': 'round',
    'aria-hidden': 'true',
    focusable: 'false',
  });
  if (className) svg.setAttribute('class', className);
  const spec = ICONS[name] || DEFAULT_ICON;
  for (const [tag, attrs] of spec) svg.append(shape(tag, attrs));
  return svg;
}

export const ROUTE_ICONS = Object.freeze({
  overview: 'overview',
  chat: 'chat',
  sessions: 'sessions',
  fleet: 'fleet',
  kanban: 'kanban',
  'run-inspector': 'run-inspector',
  cron: 'cron',
  activity: 'activity',
  alerts: 'alerts',
  analytics: 'analytics',
  issues: 'issues',
  permits: 'permits',
  'room-binding': 'room-binding',
  threads: 'threads',
  'action-audit': 'action-audit',
  skills: 'skills',
  memory: 'memory',
  profiles: 'profiles',
  models: 'models',
  tools: 'tools',
  mcp: 'mcp',
  plugins: 'plugins',
  webhooks: 'webhooks',
  channels: 'channels',
  artifacts: 'artifacts',
  files: 'files',
  logs: 'logs',
  'command-center': 'command-center',
  settings: 'settings',
  'llama-proxy': 'llama-proxy',
});

export { ICONS };
