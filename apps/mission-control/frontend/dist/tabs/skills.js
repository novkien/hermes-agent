// Skills — the profile-scoped skill library.
//
// Layout: the inspector column holds one card per skill (select, plus the
// enable/disable/archive/delete controls); the workspace holds the selected
// skill's SKILL.md in an editor, with a rendered preview and a metadata view
// of its frontmatter.
//
// Sources, both verified against the upstream dashboard:
//   GET /api/skills                  → inventory
//   GET /api/skills/content?name=…   → {name, content, path}
//
// Writes are capability-gated. The BFF advertises what it will forward in
// `meta.mutations_supported`; until an action appears there its control renders
// disabled with the reason on hover, and MUTATION_ROUTES is never reached. That
// keeps the surface honest instead of offering buttons that fail.

import { listRows } from '../pure/envelope-list.js';
import { readOnlyBadge } from '../pure/capability-badge.js';
import {
  actionsForStatus,
  filterSkills,
  normalizeSkills,
  parseFrontmatter,
  skillCategories,
  skillStats,
  supportedActions,
} from '../pure/skill-shape.js';
import {
  el,
  clear,
  skeleton,
  emptyState,
  unavailableState,
  errorPanel,
  iconButton,
  confirmButton,
  segmented,
  metaItem,
  kpi,
  debounce,
} from '../ui.js';
import { icon } from '../icons.js';
import { paint, tabToolbar } from './_kit.js';
import { provenanceBadge } from '../provenance.js';
import { renderMarkdown } from '../markdown-render.js';
import { createCodeEditor } from '../components/code-editor.js';
import { rackShell, rackCard, rackMore } from '../components/rack.js';
import { toast } from '../components/toast.js';

export const ROUTE = 'skills';
export const LABEL = 'Skills';
export const GROUP = 'BUILD & INTEGRATE';
export const READ_ONLY_NOTE = readOnlyBadge('skills');
export const SOURCE_ENDPOINTS = Object.freeze(['/api/skills', '/api/skills/content']);

export const DISABLED_ACTIONS = Object.freeze({
  toggle: 'not exposed in dashboard v1 (native skill tooling)',
  install: 'not exposed in dashboard v1',
  update: 'not exposed in dashboard v1',
  uninstall: 'not exposed in dashboard v1',
});

// Only reached for an action the BFF has advertised as forwardable. Each shape
// mirrors the dashboard's own OpenAPI declaration — note that toggle is PUT,
// and that removal lives under the skill hub.
//
// `archive` has no upstream route at all; its entry is omitted so a capability
// bug can never dispatch it somewhere that would 404.
const MUTATION_ROUTES = Object.freeze({
  save: { method: 'put', path: '/api/upstream/api/skills/content', body: (skill, extra) => ({ name: skill.name, content: extra.content }) },
  enable: { method: 'put', path: '/api/upstream/api/skills/toggle', body: (skill) => ({ name: skill.name, enabled: true }) },
  disable: { method: 'put', path: '/api/upstream/api/skills/toggle', body: (skill) => ({ name: skill.name, enabled: false }) },
  delete: { method: 'post', path: '/api/upstream/api/skills/hub/uninstall', body: (skill) => ({ name: skill.name }) },
});

const UNSUPPORTED_REASON = 'upstream exposes no write route for this action';
const CARD_BATCH = 60;

/**
 * Pure: normalize the inventory envelope into render rows. Kept as the module's
 * data contract — the tab renders from it and the acceptance checks read it.
 */
export function renderSkills(envelope) {
  return listRows(envelope, {
    pick: (raw) => (Array.isArray(raw) ? raw : raw.skills || raw.items || raw.list || null),
    map: (skill) => ({
      id: skill.name ?? skill.id ?? skill.path ?? null,
      name: skill.name ?? skill.id ?? null,
      status: skill.status ?? (skill.enabled === true ? 'enabled' : skill.enabled === false ? 'disabled' : null),
      state: skill.status ?? (skill.enabled === true ? 'enabled' : skill.enabled === false ? 'disabled' : null),
      category: skill.category ?? skill.group ?? null,
      description: skill.description ?? skill.summary ?? null,
      version: skill.version ?? null,
      usage: skill.usage ?? null,
      provenance: skill.provenance ?? null,
      disabledActions: { ...DISABLED_ACTIONS },
    }),
  });
}

// _kit provides the shared tab header.
export function createSkills({ api, profile, refreshInspector }) {
  const root = el('div', { class: 'tab tab-skills' });
  const toolbar = el('div', { class: 'skills-toolbar-host' });
  const stage = el('div', { class: 'skills-stage' });
  root.append(toolbar, stage);

  // Esc anywhere in the workspace (editor included) returns to the library.
  // deselect() still guards unsaved edits, so this cannot lose work.
  root.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !selected) return;
    event.preventDefault();
    deselect();
  });

  let listEnvelope = null;
  let rows = [];
  let capabilities = supportedActions(null);
  let selected = null;
  let docEnvelope = null;
  let docLoading = false;
  let editor = null;
  let docMode = 'source';
  let inspectorHost = null;
  let visible = CARD_BATCH;
  let busyAction = null;
  let pendingRefresh = null;
  // The action bar is rebuilt on every render, so the editor's dirty callback
  // has to reach the *current* one rather than the node it was created with.
  let docActionsRow = null;

  const view = { query: '', category: 'all', status: 'all', sort: 'usage' };
  const docCache = new Map();

  /* --------------------------------------------------------------- data -- */

  async function load() {
    clear(stage);
    stage.append(skeleton({ lines: 8 }));
    try {
      listEnvelope = await api.get('/api/skills', { profile });
    } catch (err) {
      listEnvelope = {
        data: null,
        meta: {
          source_id: 'hermes-dashboard',
          freshness: 'unavailable',
          request_id: err.request_id || null,
          degraded_reason: err.message,
        },
      };
    }

    rows = normalizeSkills(listEnvelope?.data);
    capabilities = supportedActions(listEnvelope?.meta);
    // A selection that no longer exists upstream is dropped rather than left
    // pointing at a skill the rack can't show.
    if (selected && !rows.some((skill) => skill.name === selected)) selected = null;
    visible = CARD_BATCH;
    render();
    notifyInspector();
    if (selected) await loadDoc(selected);
  }

  async function loadDoc(name, { force = false } = {}) {
    if (!name) return;
    if (!force && docCache.has(name)) {
      docEnvelope = docCache.get(name);
      render();
      return;
    }
    docLoading = true;
    render();
    try {
      docEnvelope = await api.get(`/api/skills/content?name=${encodeURIComponent(name)}`, { profile });
    } catch (err) {
      docEnvelope = {
        data: null,
        meta: {
          source_id: 'hermes-dashboard',
          freshness: 'unavailable',
          request_id: err.request_id || null,
          degraded_reason: err.message,
        },
      };
    }
    docLoading = false;
    if (selected !== name) return; // selection moved while the read was in flight
    // Only successful reads are cached: a transient failure must stay retryable.
    if (docEnvelope?.data) docCache.set(name, docEnvelope);
    render();
  }

  function selectedSkill() {
    return rows.find((skill) => skill.name === selected) || null;
  }

  function select(name) {
    if (selected === name) return;
    if (editor?.dirty && !confirmDiscard()) return;
    selected = name;
    docEnvelope = null;
    docMode = 'source';
    editor?.destroy?.();
    editor = null;
    // Paint the skeleton immediately: the read below is a network round-trip.
    docLoading = true;
    render();
    notifyInspector();
    // The selection is deliberately not pushed to the URL: the shell's
    // navigate() re-mounts and re-activates the tab, so writing the hash on
    // every click would reload the whole inventory. An inbound `?skill=` from
    // the palette is still honoured in activate().
    loadDoc(name);
  }

  /** Drop the selection and return to the library overview. */
  function deselect() {
    if (!selected) return;
    if (editor?.dirty && !confirmDiscard()) return;
    selected = null;
    docEnvelope = null;
    docLoading = false;
    docMode = 'source';
    editor?.destroy?.();
    editor = null;
    render();
    notifyInspector();
  }

  function confirmDiscard() {
    return window.confirm('This skill has unsaved edits. Discard them?');
  }

  /* ------------------------------------------------------------ actions -- */

  /**
   * One entry point for every write. Unsupported actions never reach the
   * network: they report the capability gap instead.
   */
  async function runAction(skill, action, extra = {}) {
    if (!skill) return;
    if (!capabilities[action]) {
      toast(`${action} is not available for this source`, {
        tone: 'warn',
        detail: UNSUPPORTED_REASON,
      });
      return;
    }
    const spec = MUTATION_ROUTES[action];
    if (!spec) return;

    busyAction = `${skill.name}:${action}`;
    render();
    notifyInspector();
    try {
      await api[spec.method || 'post'](spec.path, spec.body(skill, extra), { profile });
      toast(`${skill.name} — ${action} applied`, { tone: 'ok' });
      docCache.delete(skill.name);
      if (action === 'save') {
        // The buffer just became the source of truth; keep it and drop the
        // dirty flag rather than replacing what the user is looking at.
        editor?.markClean();
      } else {
        editor?.destroy?.();
        editor = null;
      }
      if (action === 'delete' && selected === skill.name) selected = null;
      busyAction = null;
      await load();
      if (action === 'delete') {
        // Uninstall returns a job id and finishes on the upstream's own clock,
        // so the inventory read above can still contain the skill. Re-read once
        // the job has had time to land instead of showing a stale row.
        pendingRefresh = window.setTimeout(() => { pendingRefresh = null; load(); }, 2000);
      }
    } catch (err) {
      busyAction = null;
      toast(`${action} failed`, { tone: 'danger', detail: err.message });
      render();
      notifyInspector();
    }
  }

  function saveDoc() {
    const skill = selectedSkill();
    if (!skill || !editor) return;
    runAction(skill, 'save', { content: editor.getValue() });
  }

  function copyDoc() {
    if (!editor) return;
    const text = editor.getValue();
    const done = () => toast('SKILL.md copied to clipboard', { tone: 'ok' });
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(done, () => toast('Copy blocked by the browser', { tone: 'warn' }));
      return;
    }
    editor.input.select();
    done();
  }

  function downloadDoc() {
    if (!editor || !selected) return;
    const blob = new Blob([editor.getValue()], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const link = el('a', { href: url, download: `${selected}.md` });
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  /* ---------------------------------------------------------- workspace -- */

  // The standard tab header, so Skills reports its freshness and takes the
  // global `r` refresh like every other tab. It used to carry a hand-rolled bar
  // with its own Refresh button and no "updated" stamp, which meant a library
  // of 173 entries gave no clue how old the list on screen was.
  function renderToolbar() {
    const stats = skillStats(rows);
    paint(toolbar, tabToolbar({
      title: 'Skills',
      subtitle: rows.length
        ? `${stats.total} skill${stats.total === 1 ? '' : 's'} · ${stats.categories} categories`
        : 'agent, bundled and hub skills',
      actions: [
        listEnvelope?.meta ? provenanceBadge(listEnvelope.meta, { empty: !rows.length }) : null,
        el('span', {
          class: `chip ${capabilities.save ? 'chip-active' : 'chip-info'}`,
          text: capabilities.save ? 'editable' : READ_ONLY_NOTE,
        }),
      ].filter(Boolean),
      meta: listEnvelope?.meta || null,
      onRefresh: () => load(),
    }));
  }

  function render() {
    renderToolbar();
    clear(stage);

    const meta = listEnvelope?.meta;
    if (meta?.freshness === 'unavailable' || meta?.freshness === 'unsupported') {
      stage.append(unavailableState({
        reason: meta.degraded_reason || 'skills source unavailable',
        requestId: meta.request_id,
      }));
      return;
    }
    if (!rows.length) {
      stage.append(emptyState({ title: 'No skills', note: 'This profile has no skills installed.' }));
      return;
    }
    stage.append(selected ? renderDoc() : renderLibrary());
  }

  /** No selection: the library overview. */
  function renderLibrary() {
    const stats = skillStats(rows);
    const box = el('div', { class: 'skill-library' });

    box.append(el('div', { class: 'skill-library-hero' }, [
      icon('skills', { size: 26, className: 'skill-library-icon' }),
      el('div', {}, [
        el('div', { class: 'skill-library-title', text: 'Skill library' }),
        el('div', {
          class: 'skill-library-sub',
          text: 'Pick a skill from the rack on the right to read and edit its SKILL.md.',
        }),
      ]),
    ]));

    box.append(el('div', { class: 'kpi-row' }, [
      kpi({ label: 'Total', value: stats.total, iconName: 'skills' }),
      kpi({ label: 'Enabled', value: stats.enabled, iconName: 'power', tone: 'ok' }),
      kpi({ label: 'Disabled', value: stats.disabled, iconName: 'ban', tone: stats.disabled ? 'warn' : undefined }),
      kpi({ label: 'Archived', value: stats.archived, iconName: 'archive' }),
      kpi({ label: 'Categories', value: stats.categories, iconName: 'tag' }),
      kpi({ label: 'Total uses', value: stats.usage.toLocaleString(), iconName: 'activity' }),
    ]));

    const top = [...rows].sort((a, b) => b.usage - a.usage).slice(0, 12).filter((skill) => skill.usage > 0);
    if (top.length) {
      const max = top[0].usage || 1;
      const list = el('div', { class: 'skill-bars' });
      for (const skill of top) {
        list.append(el('button', {
          class: 'skill-bar',
          type: 'button',
          onclick: () => select(skill.name),
        }, [
          el('span', { class: 'skill-bar-name', text: skill.name }),
          el('span', { class: 'skill-bar-track' }, [
            el('span', { class: 'skill-bar-fill', style: `width:${Math.max(3, (skill.usage / max) * 100)}%` }),
          ]),
          el('span', { class: 'skill-bar-value mono', text: String(skill.usage) }),
        ]));
      }
      box.append(el('section', { class: 'skill-section' }, [
        el('div', { class: 'skill-section-title' }, [icon('flame', { size: 13 }), 'Most used']),
        list,
      ]));
    }

    const categories = skillCategories(rows);
    if (categories.length) {
      const chips = el('div', { class: 'skill-chip-row' });
      for (const entry of categories) {
        chips.append(el('button', {
          class: 'skill-chip',
          type: 'button',
          onclick: () => {
            view.category = entry.name;
            notifyInspector();
          },
        }, [
          el('span', { text: entry.name }),
          el('span', { class: 'skill-chip-count', text: String(entry.count) }),
        ]));
      }
      box.append(el('section', { class: 'skill-section' }, [
        el('div', { class: 'skill-section-title' }, [icon('tag', { size: 13 }), 'Categories']),
        chips,
      ]));
    }

    return box;
  }

  /** A skill is selected: hero + document workbench. */
  function renderDoc() {
    const skill = selectedSkill();
    const box = el('div', { class: 'skill-doc' });
    if (!skill) return box;

    box.append(renderHero(skill));

    if (docLoading) {
      box.append(el('div', { class: 'skill-doc-body' }, [skeleton({ lines: 12 })]));
      return box;
    }

    const docMeta = docEnvelope?.meta;
    if (!docEnvelope || docMeta?.freshness === 'unavailable' || docMeta?.freshness === 'unsupported') {
      box.append(unavailableState({
        reason: docMeta?.degraded_reason || 'skill document unavailable',
        requestId: docMeta?.request_id,
      }));
      return box;
    }

    const content = String(docEnvelope?.data?.content ?? '');
    const path = docEnvelope?.data?.path || skill.path;

    if (!editor) {
      // The buffer stays editable even when the source refuses writes: reading
      // a skill usually means wanting to change it, and an editable buffer can
      // still be copied or downloaded. Only Save is gated, and the notice above
      // says so before the first keystroke.
      editor = createCodeEditor({
        value: content,
        language: 'markdown',
        placeholder: '# SKILL.md',
        onSave: () => saveDoc(),
        onDirtyChange: () => renderDocActions(),
      });
    }

    docActionsRow = el('div', { class: 'doc-actions' });
    renderDocActions();

    const modes = segmented([
      { value: 'source', label: 'SKILL.md', title: 'Editable source' },
      { value: 'preview', label: 'Preview', title: 'Rendered Markdown' },
      { value: 'meta', label: 'Frontmatter', title: 'Parsed metadata' },
    ], {
      value: docMode,
      ariaLabel: 'Document view',
      onChange: (next) => {
        docMode = next;
        renderPane(pane, content, path);
      },
    });

    box.append(el('div', { class: 'doc-bar' }, [
      modes,
      el('span', { class: 'doc-bar-gap' }),
      path ? el('span', { class: 'doc-path mono', text: path, title: path }) : null,
      docMeta ? provenanceBadge(docMeta) : null,
      docActionsRow,
    ].filter(Boolean)));

    if (!capabilities.save) {
      box.append(el('div', { class: 'doc-notice' }, [
        icon('lock', { size: 13 }),
        el('span', {
          text: 'Read-only source: the BFF advertises no skill write route, so edits cannot be saved back. '
            + 'You can still edit locally, copy, or download this document.',
        }),
      ]));
    }

    const pane = el('div', { class: 'doc-pane' });
    renderPane(pane, content, path);
    box.append(pane);
    return box;
  }

  function renderPane(pane, content, path) {
    clear(pane);
    if (docMode === 'source') {
      pane.append(editor.node);
      return;
    }
    if (docMode === 'preview') {
      const { body } = parseFrontmatter(editor ? editor.getValue() : content);
      const doc = el('div', { class: 'doc-preview md' });
      renderMarkdown(doc, body, { limit: 200000, force: true });
      pane.append(doc);
      return;
    }

    const { fields, hasFrontmatter } = parseFrontmatter(editor ? editor.getValue() : content);
    const table = el('div', { class: 'kv doc-meta' });
    if (path) {
      table.append(el('div', { class: 'kv-row' }, [
        el('span', { class: 'kv-k', text: 'path' }),
        el('span', { class: 'kv-v mono', text: path }),
      ]));
    }
    if (!hasFrontmatter) {
      table.append(el('div', { class: 'kv-row' }, [
        el('span', { class: 'kv-k', text: 'frontmatter' }),
        el('span', { class: 'kv-v', text: 'none — this document has no YAML header' }),
      ]));
    }
    for (const [key, value] of Object.entries(fields)) {
      table.append(el('div', { class: 'kv-row' }, [
        el('span', { class: 'kv-k', text: key }),
        el('span', { class: 'kv-v', text: value }),
      ]));
    }
    pane.append(table);
  }

  function renderHero(skill) {
    const hero = el('div', { class: 'skill-hero' });

    // Selecting a skill replaces the library overview, so the only way back is
    // an explicit control — the rack has no "nothing selected" row to click.
    hero.append(el('button', {
      class: 'skill-back',
      type: 'button',
      title: 'Back to the skill library (Esc)',
      onclick: () => deselect(),
    }, [icon('arrow-left', { size: 13 }), el('span', { text: 'Skill library' })]));

    hero.append(el('div', { class: 'skill-hero-top' }, [
      el('span', { class: `rack-dot rack-dot-${skill.status}`, title: skill.status }),
      el('h1', { class: 'skill-hero-name', text: skill.name }),
      el('span', { class: `chip chip-${skill.status}`, text: skill.status }),
      skill.provenance ? el('span', { class: 'chip chip-info', text: skill.provenance }) : null,
    ].filter(Boolean)));

    if (skill.description) hero.append(el('p', { class: 'skill-hero-desc', text: skill.description }));

    hero.append(el('div', { class: 'skill-hero-meta' }, [
      metaItem('category', skill.category || '—'),
      metaItem('version', skill.version || '—', { mono: true }),
      metaItem('uses', skill.usage.toLocaleString(), { mono: true }),
    ]));

    return hero;
  }

  function renderDocActions() {
    const container = docActionsRow;
    const skill = selectedSkill();
    if (!container || !skill || !editor) return;
    clear(container);
    const dirty = Boolean(editor.dirty);
    const saving = busyAction === `${skill.name}:save`;

    if (dirty) container.append(el('span', { class: 'doc-dirty', text: 'unsaved changes' }));

    const save = el('button', {
      class: `btn btn-sm${dirty ? ' btn-primary' : ''}`,
      title: capabilities.save ? 'Save SKILL.md (Ctrl/Cmd+S)' : `Save — ${UNSUPPORTED_REASON}`,
      onclick: () => saveDoc(),
    }, [icon('save', { size: 12 }), saving ? ' Saving…' : ' Save']);
    if (!capabilities.save || !dirty || saving) save.disabled = true;
    container.append(save);

    const revert = el('button', {
      class: 'btn btn-sm',
      title: 'Discard local edits',
      onclick: () => {
        editor.revert();
        renderDocActions();
      },
    }, [icon('retry', { size: 12 }), ' Revert']);
    if (!dirty) revert.disabled = true;
    container.append(revert);

    container.append(iconButton({ icon: 'copy', label: 'Copy source', onClick: copyDoc }));
    container.append(iconButton({ icon: 'download', label: 'Download .md', onClick: downloadDoc }));
    container.append(iconButton({
      icon: 'retry',
      label: 'Reload from source',
      onClick: () => {
        if (editor?.dirty && !confirmDiscard()) return;
        editor = null;
        loadDoc(skill.name, { force: true });
      },
    }));
  }

  /* ---------------------------------------------------------- inspector -- */

  function notifyInspector() {
    if (typeof refreshInspector === 'function') refreshInspector();
    else if (inspectorHost) renderInspector(inspectorHost);
  }

  const onSearch = debounce((value) => {
    view.query = value;
    visible = CARD_BATCH;
    if (inspectorHost) renderRack(inspectorHost);
  }, 160);

  function renderInspector(container) {
    inspectorHost = container;
    renderRack(container);
  }

  function renderRack(container) {
    clear(container);

    const rack = rackShell({
      title: 'Skill rack',
      iconName: 'skills',
      searchPlaceholder: 'Search skills…',
      onSearch,
    });

    const stats = skillStats(rows);
    rack.toolbar.append(segmented([
      { value: 'all', label: 'All', count: stats.total },
      { value: 'enabled', label: 'On', count: stats.enabled },
      { value: 'disabled', label: 'Off', count: stats.disabled },
      { value: 'archived', label: 'Arch', count: stats.archived },
    ], {
      value: view.status,
      ariaLabel: 'Filter by status',
      onChange: (next) => {
        view.status = next;
        visible = CARD_BATCH;
        renderRack(container);
      },
    }));

    const categorySelect = el('select', {
      class: 'select rack-select',
      'aria-label': 'Filter by category',
      onchange: (event) => {
        view.category = event.target.value;
        visible = CARD_BATCH;
        renderRack(container);
      },
    });
    categorySelect.append(el('option', { value: 'all', text: `All categories (${stats.categories})` }));
    for (const entry of skillCategories(rows)) {
      const option = el('option', { value: entry.name, text: `${entry.name} (${entry.count})` });
      option.selected = view.category === entry.name;
      categorySelect.append(option);
    }

    const sortSelect = el('select', {
      class: 'select rack-select',
      'aria-label': 'Sort skills',
      onchange: (event) => {
        view.sort = event.target.value;
        renderRack(container);
      },
    });
    for (const [value, label] of [['usage', 'Most used'], ['name', 'Name A–Z'], ['category', 'Category']]) {
      const option = el('option', { value, text: label });
      option.selected = view.sort === value;
      sortSelect.append(option);
    }
    rack.toolbar.append(el('div', { class: 'rack-selects' }, [categorySelect, sortSelect]));

    const filtered = filterSkills(rows, view);
    rack.setCount(filtered.length, rows.length);
    // Name the actions the source cannot serve rather than calling the whole
    // rack read-only: editing is live, so a blanket notice would be wrong and
    // a silent grey-out leaves no explanation for the disabled controls.
    const missing = ['enable', 'disable', 'archive', 'delete'].filter((name) => !capabilities[name]);
    if (!capabilities.save && missing.length === 4) {
      rack.setNote('actions unavailable — source is read-only', 'warn');
    } else if (missing.length) {
      rack.setNote(`${missing.join(', ')} not offered by the source`, 'warn');
    }

    if (!filtered.length) {
      rack.list.append(el('div', { class: 'rack-empty', text: rows.length ? 'No skill matches this filter.' : 'No skills installed.' }));
    }

    for (const skill of filtered.slice(0, visible)) {
      rack.list.append(buildCard(skill, container));
    }
    if (filtered.length > visible) {
      rack.footer.append(rackMore(filtered.length - visible, () => {
        visible += CARD_BATCH;
        renderRack(container);
      }));
    }

    container.append(rack.node);
  }

  function buildCard(skill, container) {
    const allowed = actionsForStatus(skill.status);
    const busy = (action) => busyAction === `${skill.name}:${action}`;

    const action = (name, iconName, label, tone) => {
      if (!allowed[name]) return null;
      const disabled = !capabilities[name] || busy(name) || Boolean(busyAction);
      return iconButton({
        icon: iconName,
        label,
        tone,
        disabled,
        disabledReason: capabilities[name] ? 'another action is running' : UNSUPPORTED_REASON,
        onClick: () => runAction(skill, name),
      });
    };

    const remove = confirmButton({
      icon: 'trash',
      label: 'Delete skill',
      confirmLabel: 'Click again to delete',
      disabled: !capabilities.delete || Boolean(busyAction),
      disabledReason: capabilities.delete ? 'another action is running' : UNSUPPORTED_REASON,
      onConfirm: () => runAction(skill, 'delete'),
    });

    return rackCard({
      id: skill.name,
      title: skill.name,
      description: skill.description,
      status: skill.status,
      statusLabel: skill.status,
      active: skill.name === selected,
      onSelect: (name) => {
        select(name);
        if (container) renderRack(container);
      },
      badges: [
        skill.category ? { label: skill.category, tone: 'cat' } : null,
        skill.provenance ? { label: skill.provenance, tone: skill.provenance } : null,
      ].filter(Boolean),
      metrics: [{ icon: 'activity', value: skill.usage, label: 'uses', title: `${skill.usage} recorded uses` }],
      actions: [
        action('enable', 'power', 'Enable skill', 'ok'),
        action('disable', 'ban', 'Disable skill', 'warn'),
        action('archive', 'archive', 'Archive skill'),
        remove,
      ].filter(Boolean),
    });
  }

  /* -------------------------------------------------------------- shell -- */

  return {
    // mount only attaches the shell; the shell calls activate() straight after,
    // and that is the single place the inventory is read.
    mount(container) {
      clear(container);
      container.append(root);
      clear(stage);
      stage.append(skeleton({ lines: 8 }));
    },
    activate(params = {}) {
      if (params?.skill && params.skill !== selected) selected = params.skill;
      return load().catch((err) => {
        clear(stage);
        stage.append(errorPanel({
          message: `Skills failed: ${err.message}`,
          requestId: err.request_id,
          onRetry: () => load(),
        }));
      });
    },
    deactivate() {
      editor?.destroy?.();
      if (pendingRefresh) {
        window.clearTimeout(pendingRefresh);
        pendingRefresh = null;
      }
      return { selection: selected };
    },
    refresh: () => load(),
    renderInspector,
    get filters() {
      return { ...view };
    },
    get data() {
      return listEnvelope?.data ?? null;
    },
  };
}
