// Declarative data table. Every list surface in the app renders through this
// so sorting, selection, keyboard nav and the four data states behave the same
// everywhere instead of being re-invented per tab.

import { el, clear, skeleton, emptyState, errorPanel, unavailableState, unsupportedState } from '../ui.js';
import { icon } from '../icons.js';

const ALIGN_CLASS = { right: 'right', center: 'center', num: 'right' };

function cellValue(row, col) {
  if (typeof col.value === 'function') return col.value(row);
  return row?.[col.key];
}

function defaultCompare(a, b) {
  if (a === b) return 0;
  if (a === null || a === undefined || a === '') return 1;
  if (b === null || b === undefined || b === '') return -1;
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
}

/**
 * @param {object} opts
 * @param {Array} opts.columns  {key, label, width, align, render, sortable, sortValue, className, title}
 * @param {Function} [opts.rowId]      row -> stable id (enables selection persistence)
 * @param {Function} [opts.onSelect]   (row, id) -> void; also drives inspector wiring
 * @param {Function} [opts.rowActions] row -> Node[]  (rendered in a trailing hover column)
 * @param {Function} [opts.rowClass]   row -> extra class string
 * @param {boolean}  [opts.pinFirst]   sticky first column for wide tables
 */
export function createTable({
  columns = [],
  rowId = (row, index) => row?.id ?? row?.name ?? String(index),
  onSelect = null,
  rowActions = null,
  rowClass = null,
  pinFirst = false,
  emptyTitle = 'No rows',
  emptyNote = '',
  sort: initialSort = null,
} = {}) {
  const wrap = el('div', { class: 'table-wrap' });
  let rows = [];
  let sortKey = initialSort?.key ?? null;
  let sortDir = initialSort?.dir === 'desc' ? 'desc' : 'asc';
  let selectedId = null;
  const table = el('table', { class: `table${pinFirst ? ' table-pinned' : ''}` });
  const head = buildHead();
  const body = el('tbody');
  table.append(head, body);
  const rowNodes = new Map();

  function sortedRows() {
    if (!sortKey) return rows;
    const col = columns.find((c) => c.key === sortKey);
    if (!col) return rows;
    const pick = col.sortValue || ((row) => cellValue(row, col));
    const dir = sortDir === 'desc' ? -1 : 1;
    // Slice first: never reorder the caller's array in place.
    return rows.slice().sort((a, b) => dir * defaultCompare(pick(a), pick(b)));
  }

  function buildHead() {
    const tr = el('tr');
    for (const col of columns) {
      const align = ALIGN_CLASS[col.align] || '';
      const th = el('th', {
        class: [align, col.className || ''].filter(Boolean).join(' ') || null,
        style: col.width ? `width:${col.width}` : null,
        scope: 'col',
      });
      if (col.sortable) {
        const active = sortKey === col.key;
        const btn = el('button', {
          class: 'th-sort',
          type: 'button',
          'aria-sort': active ? (sortDir === 'desc' ? 'descending' : 'ascending') : 'none',
          onclick: () => {
            if (sortKey === col.key) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
            else { sortKey = col.key; sortDir = 'asc'; }
            render();
          },
        }, [col.label, icon('sort', { size: 10, className: 'th-sort-arrow' })]);
        th.append(btn);
      } else {
        th.textContent = col.label ?? '';
      }
      tr.append(th);
    }
    if (rowActions) tr.append(el('th', { class: 'right', style: 'width:1%', scope: 'col', text: '' }));
    return el('thead', {}, [tr]);
  }

  function select(id, row, node) {
    selectedId = id;
    for (const other of wrap.querySelectorAll('tbody tr.is-selected')) other.classList.remove('is-selected');
    if (node) node.classList.add('is-selected');
    if (onSelect) onSelect(row, id);
  }

  function rowFingerprint(row) {
    try { return JSON.stringify(row); } catch (_err) { return String(row); }
  }

  function paintRow(entry, row, index, id) {
      const tr = entry.node;
      const extra = rowClass ? rowClass(row) : '';
      tr.className = [extra, id === selectedId ? 'is-selected' : ''].filter(Boolean).join(' ');
      if (onSelect) tr.tabIndex = 0;
      else tr.removeAttribute('tabindex');
      tr.dataset.rowId = id;
      clear(tr);
      for (const col of columns) {
        const align = ALIGN_CLASS[col.align] || '';
        const td = el('td', {
          class: [align, col.mono ? 'mono' : '', col.className || ''].filter(Boolean).join(' ') || null,
        });
        const rendered = col.render ? col.render(row, index) : cellValue(row, col);
        if (rendered === null || rendered === undefined || rendered === '') td.textContent = '—';
        else if (rendered.nodeType) td.append(rendered);
        else td.textContent = String(rendered);
        if (col.title) td.title = String(col.title(row) ?? '');
        tr.append(td);
      }
      if (rowActions) {
        const actions = el('div', { class: 'row-actions' }, rowActions(row) || []);
        tr.append(el('td', { class: 'right' }, [actions]));
      }
      if (onSelect) {
        entry.row = row;
      }
  }

  function buildRow(row, index, id) {
      const tr = el('tr');
      const entry = { node: tr, row, fingerprint: '', index };
      if (onSelect) {
        tr.addEventListener('click', () => select(id, entry.row, tr));
        tr.addEventListener('keydown', (event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            select(id, entry.row, tr);
          } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            const sibling = event.key === 'ArrowDown' ? tr.nextElementSibling : tr.previousElementSibling;
            if (sibling) sibling.focus();
          }
        });
      }
      paintRow(entry, row, index, id);
      return entry;
  }

  function reconcileBody(list) {
    const previousPositions = new Map();
    if (!globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      for (const [id, entry] of rowNodes) {
        if (entry.node.isConnected) previousPositions.set(id, entry.node.getBoundingClientRect());
      }
    }
    const retained = new Set();
    list.forEach((row, index) => {
      const id = String(rowId(row, index));
      const fingerprint = rowFingerprint(row);
      let entry = rowNodes.get(id);
      if (!entry) {
        entry = buildRow(row, index, id);
        rowNodes.set(id, entry);
      } else if (entry.fingerprint !== fingerprint) {
        paintRow(entry, row, index, id);
      } else {
        entry.row = row;
        entry.node.classList.toggle('is-selected', id === selectedId);
      }
      entry.fingerprint = fingerprint;
      entry.index = index;
      retained.add(id);
      body.append(entry.node);
    });
    for (const [id, entry] of rowNodes) {
      if (retained.has(id)) continue;
      entry.node.remove();
      rowNodes.delete(id);
    }
    for (const [id, before] of previousPositions) {
      const node = rowNodes.get(id)?.node;
      if (!node?.isConnected || typeof node.animate !== 'function') continue;
      const after = node.getBoundingClientRect();
      const x = before.left - after.left;
      const y = before.top - after.top;
      if (!x && !y) continue;
      node.animate(
        [{ transform: `translate(${x}px, ${y}px)` }, { transform: 'translate(0, 0)' }],
        { duration: 140, easing: 'ease-out' },
      );
    }
  }

  function render() {
    const list = sortedRows();
    if (!list.length) {
      reconcileBody([]);
      clear(wrap);
      wrap.append(emptyState({ title: emptyTitle, note: emptyNote }));
      return;
    }
    reconcileBody(list);
    if (table.parentNode !== wrap) {
      clear(wrap);
      wrap.append(table);
    }
    wrap.classList.remove('is-stale', 'is-syncing');
    wrap.removeAttribute('title');
    wrap.removeAttribute('aria-busy');
  }

  function showState(node) {
    clear(wrap);
    wrap.append(node);
  }

  function retainWithWarning(opts, fallback) {
    if (!rows.length) {
      showState(fallback(opts || {}));
      return;
    }
    wrap.classList.remove('is-syncing');
    wrap.classList.add('is-stale');
    wrap.removeAttribute('aria-busy');
    wrap.title = opts?.message || opts?.reason || 'Showing last-known-good data';
  }

  render();

  return {
    node: wrap,
    setRows(next) { rows = Array.isArray(next) ? next : []; render(); },
    setLoading() {
      if (!rows.length) showState(skeleton({ lines: 6, height: 14 }));
      else {
        wrap.classList.add('is-syncing');
        wrap.setAttribute('aria-busy', 'true');
      }
    },
    setError(opts) {
      retainWithWarning(opts, errorPanel);
    },
    setUnavailable(opts) { retainWithWarning(opts, unavailableState); },
    setUnsupported(opts) { showState(unsupportedState(opts || {})); },
    /** Re-apply a selection made elsewhere (e.g. restored from the URL). */
    setSelected(id) {
      selectedId = id === null || id === undefined ? null : String(id);
      for (const tr of wrap.querySelectorAll('tbody tr')) {
        tr.classList.toggle('is-selected', tr.dataset.rowId === selectedId);
      }
    },
    get selectedId() { return selectedId; },
  };
}
