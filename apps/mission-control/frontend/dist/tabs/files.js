// Files tab — upstream /api/files + /api/files/read.
//
// A real browser: breadcrumb navigation, typed rows, and preview/download from
// the `data_url` the read route returns. Writes (upload, mkdir, delete) exist
// upstream but are deliberately not proxied — arbitrary filesystem mutation
// from a browser is a different risk class from configuring Hermes.

import { listRows } from '../pure/envelope-list.js';
import { isSafeManagedPath, FILE_ROOTS } from '../pure/path-guard.js';
import { el, clear, iconButton, statusChip, fmtTime } from '../ui.js';
import { icon } from '../icons.js';
import { createTable } from '../components/table.js';
import { createDetail } from '../components/detail.js';
import { toast } from '../components/toast.js';
import {
  applyStateToTable, filterInput, loadEnvelope, paint, sideHint, tabToolbar,
} from './_kit.js';
import { filterRows, filterSummary } from '../pure/text-filter.js';

export const ROUTE = 'files';
export const LABEL = 'Files';
export const GROUP = 'BUILD & INTEGRATE';

export const SOURCE_ENDPOINTS = Object.freeze(['/api/files']);

export const DISABLED_ACTIONS = Object.freeze({
  upload: 'not proxied — arbitrary filesystem writes from a browser',
  mkdir: 'not proxied — arbitrary filesystem writes from a browser',
  delete: 'not proxied — arbitrary filesystem writes from a browser',
  write: 'not proxied — arbitrary filesystem writes from a browser',
});

export { FILE_ROOTS };

/** Guard helper used by the tab: safe paths only. */
export function assertSafePath(root, relativePath) {
  return isSafeManagedPath(root, relativePath);
}

export function renderFiles(envelope) {
  return listRows(envelope, {
    pick: (raw) => raw.entries || raw.files || raw.items || raw.list || null,
    map: (f) => ({
      id: f.path ?? f.id ?? f.name ?? null,
      name: f.name ?? null,
      path: f.path ?? null,
      is_directory: f.is_directory === true,
      guarded: true,
      size: f.size ?? null,
      mime: f.mime_type ?? f.content_type ?? f.mime ?? null,
      modified: f.mtime ?? f.modified_at ?? f.updated_at ?? null,
      disabledActions: { ...DISABLED_ACTIONS },
    }),
  });
}

export function humanSize(bytes) {
  if (bytes === null || bytes === undefined) return null;
  const n = Number(bytes);
  if (!Number.isFinite(n)) return null;
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = n / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

/** Split an absolute path into cumulative breadcrumb segments. */
export function breadcrumbSegments(path) {
  const clean = String(path || '').replace(/\/+$/, '');
  if (!clean || clean === '/') return [{ label: '/', path: '/' }];
  const parts = clean.split('/').filter(Boolean);
  const out = [{ label: '/', path: '/' }];
  let current = '';
  for (const part of parts) {
    current += `/${part}`;
    out.push({ label: part, path: current });
  }
  return out;
}

const PREVIEWABLE = /^(text\/|application\/(json|xml|yaml|x-yaml|javascript|x-sh))/;

export function createFiles({ api, profile, toolbar }) {
  const root = el('div', { class: 'tab tab-files' });
  const crumbs = el('nav', { class: 'crumbs', 'aria-label': 'Path' });
  const main = el('div', { class: 'split-main' });
  let inspectorHost = null;
  root.append(crumbs, main);

  let cwd = null;
  let parent = null;
  let rows = [];
  // A workspace directory can hold hundreds of entries (this deployment's
  // renders 1,888 nodes), and the only way to find one was to read the whole
  // listing. Filtering is per-directory and resets on navigation, which is
  // what "find something in this folder" means.
  let query = '';
  const SEARCH_FIELDS = ['name', 'path', 'kind', 'content_type'];

  function visibleRows() {
    return filterRows(rows, query, SEARCH_FIELDS);
  }
  let meta = null;
  let selected = null;

  const table = createTable({
    rowId: (row) => row.path,
    emptyTitle: 'Empty directory',
    sort: { key: 'name', dir: 'asc' },
    columns: [
      {
        key: 'name',
        label: 'Name',
        sortable: true,
        // Directories first, then alphabetical — the ordering every file
        // browser uses, expressed as the sort key rather than a post-sort pass.
        sortValue: (row) => `${row.is_directory ? '0' : '1'}${row.name}`,
        render: (row) => el('span', { class: 'cell-file' }, [
          icon(row.is_directory ? 'archive' : 'doc', { size: 13, className: 'cell-file-icon' }),
          el('span', { class: row.is_directory ? 'cell-strong' : '', text: row.name }),
        ]),
      },
      {
        key: 'size',
        label: 'Size',
        width: '90px',
        align: 'right',
        sortable: true,
        sortValue: (row) => (row.is_directory ? -1 : Number(row.size ?? 0)),
        render: (row) => (row.is_directory ? null : humanSize(row.size)),
      },
      { key: 'mime_type', label: 'Type', width: '150px', sortable: true, mono: true },
      {
        key: 'mtime',
        label: 'Modified',
        width: '150px',
        sortable: true,
        render: (row) => (row.mtime ? fmtTime(new Date(row.mtime * 1000).toISOString()) : null),
        className: 'mono',
      },
    ],
    rowActions: (row) => (row.is_directory
      ? [iconButton({ icon: 'chevron-right', label: `Open ${row.name}`, onClick: () => navigateTo(row.path) })]
      : [iconButton({ icon: 'eye', label: `Preview ${row.name}`, onClick: () => { selected = row; renderSide(); } })]),
    onSelect: (row) => {
      if (row.is_directory) { navigateTo(row.path); return; }
      selected = row;
      renderSide();
    },
  });
  main.append(table.node);

  function navigateTo(path) {
    selected = null;
    // The filter describes the folder you are in, not a search of the tree, so
    // it clears when you leave — otherwise the next folder opens looking empty.
    query = '';
    return load(path);
  }

  async function load(path = null) {
    table.setLoading();
    const query = path ? `?path=${encodeURIComponent(path)}` : '';
    const result = await loadEnvelope(api, `/api/upstream/api/files${query}`, { profile, allowEmpty: false });
    meta = result.meta;
    if (result.state !== 'ready') {
      applyStateToTable(table, result);
      renderCrumbs();
      renderToolbar(toolbar);
      renderSide();
      return;
    }
    const body = result.data || {};
    cwd = body.path || path || null;
    parent = body.parent || null;
    rows = Array.isArray(body.entries) ? body.entries : [];
    applyStateToTable(table, {
      ...result,
      state: visibleRows().length ? 'ready' : 'empty',
      data: visibleRows(),
    });
    renderCrumbs();
    renderToolbar(toolbar);
    renderSide();
  }

  function renderCrumbs() {
    clear(crumbs);
    if (!cwd) return;
    const segments = breadcrumbSegments(cwd);
    segments.forEach((segment, index) => {
      if (index) crumbs.append(el('span', { class: 'crumbs-sep', text: '/' }));
      const last = index === segments.length - 1;
      crumbs.append(last
        ? el('span', { class: 'crumb crumb-current', text: segment.label })
        : el('button', {
          class: 'crumb', type: 'button',
          onclick: () => navigateTo(segment.path),
        }, segment.label));
    });
  }

  function renderToolbar(host) {
    if (!host) return;
    const dirs = rows.filter((r) => r.is_directory).length;
    paint(host, tabToolbar({
      title: 'Files',
      subtitle: query
        ? filterSummary(visibleRows().length, rows.length, 'entry')
        : rows.length ? `${dirs} folder${dirs === 1 ? '' : 's'} · ${rows.length - dirs} file${rows.length - dirs === 1 ? '' : 's'}` : '',
      filters: [filterInput({
        value: query,
        placeholder: 'Find in this folder…',
        ariaLabel: 'Filter directory entries',
        onChange: (next) => {
          if (next === query) return;
          query = next;
          table.setRows(visibleRows());
          renderToolbar(host);
        },
      })],
      meta,
      onRefresh: () => load(cwd),
      actions: [
        parent ? iconButton({ icon: 'arrow-left', label: 'Up one level', onClick: () => navigateTo(parent) }) : null,
      ].filter(Boolean),
    }));
  }

  function previewSection(row) {
    const host = el('div');
    host.append(el('div', { class: 'field-hint', text: 'loading…' }));

    api.get(`/api/upstream/api/files/read?path=${encodeURIComponent(row.path)}`, { profile })
      .then((res) => {
        const body = res.data || {};
        const dataUrl = body.data_url || '';
        clear(host);

        const download = el('a', {
          class: 'btn btn-sm',
          href: dataUrl,
          download: body.name || row.name,
        }, 'Download');
        host.append(el('div', { class: 'form-actions' }, [download]));

        const mime = body.mime_type || row.mime_type || '';
        if (mime.startsWith('image/')) {
          host.append(el('img', { class: 'file-preview-img', src: dataUrl, alt: row.name }));
          return;
        }
        if (!PREVIEWABLE.test(mime) && body.size > 262144) {
          host.append(el('div', { class: 'field-hint', text: 'Binary or large file — download to inspect it.' }));
          return;
        }
        let text = '';
        try {
          text = atob(String(dataUrl).split(',')[1] || '');
        } catch {
          host.append(el('div', { class: 'field-hint', text: 'Could not decode this file for preview.' }));
          return;
        }
        host.append(el('pre', { class: 'mono pre-wrap file-preview', text: text.slice(0, 20000) }));
        if (text.length > 20000) {
          host.append(el('div', { class: 'field-hint', text: 'Preview truncated at 20 000 characters.' }));
        }
      })
      .catch((err) => {
        paint(host, el('div', { class: 'notice notice-warn' }, [
          el('div', { text: err.message || 'File could not be read' }),
          el('div', { class: 'field-hint', text: 'Hermes refuses to serve files it classes as sensitive.' }),
        ]));
        toast('File preview unavailable', { tone: 'warn', detail: err.request_id ? `request ${err.request_id}` : '' });
      });

    return host;
  }

  function renderInspector(container) {
    inspectorHost = container;
    renderSide();
  }

  function renderSide() {
    if (!inspectorHost) return;
    if (!selected) {
      paint(inspectorHost, sideHint('Select a file', [
        'Browse the roots Hermes exposes; click a folder to descend.',
        'Text and images preview inline; anything else can be downloaded.',
        'Upload, delete and mkdir exist upstream but are not proxied here on purpose.',
      ]));
      return;
    }
    paint(inspectorHost, createDetail({
      title: selected.name,
      meta,
      chips: [
        selected.mime_type ? el('span', { class: 'chip', text: selected.mime_type }) : null,
        selected.size !== null && selected.size !== undefined ? statusChip('idle', humanSize(selected.size)) : null,
      ].filter(Boolean),
      fields: [
        { label: 'Path', value: selected.path, mono: true },
        { label: 'Modified', value: selected.mtime ? fmtTime(new Date(selected.mtime * 1000).toISOString()) : null, mono: true },
      ],
      sections: [{ title: 'Preview', node: previewSection(selected) }],
    }));
  }

  renderSide();

  return {
    mount(container) { clear(container); container.append(root); },
    renderInspector,
    activate() { return load(cwd); },
    deactivate() { return {}; },
    refresh: () => load(cwd),
    renderToolbar,
    get data() { return rows; },
  };
}
