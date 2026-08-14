// Memory editor — reads/writes local .hermes/memories/MEMORY.md and USER.md.

import {
  clear,
  el,
  errorPanel,
  skeleton,
  iconButton,
  segmented,
  statusChip,
  unavailableState,
} from '../ui.js';
import { createCodeEditor } from '../components/code-editor.js';
import { createForm } from '../components/form.js';
import { icon } from '../icons.js';
import { toast } from '../components/toast.js';
import {
  confirmAction, loadEnvelope, paint, runMutation, tabToolbar,
} from './_kit.js';

export const ROUTE = 'memory';
export const LABEL = 'Memory';
export const GROUP = 'BUILD & INTEGRATE';
export const READ_ONLY_NOTE = 'editable local files under .hermes/memories';

const FILES = Object.freeze([
  { id: 'memory', label: 'MEMORY.md' },
  { id: 'user', label: 'USER.md' },
]);

export const SOURCE_ENDPOINTS = Object.freeze([
  '/api/memory/MEMORY.md',
  '/api/memory/USER.md',
  '/api/upstream/api/memory',
]);

/** What `POST /api/memory/reset` can wipe. Destructive; confirm-gated. */
export const RESET_TARGETS = Object.freeze([
  { value: 'all', label: 'Everything' },
  { value: 'memory', label: 'MEMORY.md only' },
  { value: 'user', label: 'USER.md only' },
]);

const PROVIDER_TONE = { ready: 'ok', unavailable: 'idle', error: 'danger', needs_setup: 'warn' };

/**
 * Pure: normalize the provider list from `GET /api/memory`.
 *
 * `available` is "the dependencies are installed", `configured` is "the config
 * block exists" — a provider can be configured and still unusable, which is
 * exactly the case the old file-only tab could never show.
 */
export function memoryProviderRows(payload) {
  const data = payload && typeof payload === 'object' ? payload : {};
  const active = data.active || '';
  const list = Array.isArray(data.providers) ? data.providers : [];
  return list.map((provider) => ({
    name: provider?.name ?? '',
    description: provider?.description ?? '',
    available: provider?.available === true,
    configured: provider?.configured === true,
    status: provider?.status ?? 'unknown',
    active: (provider?.name ?? '') === active,
    dependencies_installed: provider?.setup?.dependencies_installed === true,
    pip_dependencies: provider?.setup?.pip_dependencies ?? [],
    external_dependencies: provider?.setup?.external_dependencies ?? [],
    required_env: provider?.setup?.required_env ?? [],
  }));
}

/** Pure: map one provider config field descriptor to a form field spec. */
export function providerConfigField(field) {
  const kind = field?.kind || 'text';
  const base = {
    key: field.key,
    label: field.label || field.key,
    hint: field.description || '',
    required: field.required === true,
    span: 2,
  };
  if (kind === 'select') {
    return { ...base, type: 'select', options: (field.options || []).map((o) => ({ value: o.value, label: o.label || o.value })) };
  }
  if (kind === 'number') return { ...base, type: 'number' };
  if (kind === 'secret') {
    // A secret's current value never comes back from upstream — only `is_set`.
    // Blank means "leave it alone", which the hint has to say out loud.
    return { ...base, redacted: true, hint: `${base.hint}${base.hint ? ' · ' : ''}${field.is_set ? 'already set — blank leaves it unchanged' : 'not set'}` };
  }
  return base;
}

const STATE_NOT_FOUND = {
  freshness: 'unavailable',
  request_id: null,
};

function makeFileState(file) {
  return {
    file,
    loading: false,
    loaded: false,
    saving: false,
    dirty: false,
    content: '',
    baseline: '',
    meta: null,
    error: null,
    requestId: null,
  };
}

export function createMemory({ api, profile, refreshInspector }) {
  const root = el('div', { class: 'tab tab-memory' });
  // The standard tab header: Memory was the last editing surface with no
  // "updated" stamp and no refresh control, so the only way to see whether the
  // file on screen still matched disk was to switch tabs and come back — on a
  // pane that holds unsaved edits, that is the worst possible way to find out.
  const toolbar = el('div', { class: 'memory-toolbar-host' });
  let toolbarHintText = '.hermes/memories/MEMORY.md · USER.md';

  function renderToolbar() {
    const state = activeState();
    paint(toolbar, tabToolbar({
      title: 'Memory',
      subtitle: toolbarHintText,
      filters: [segmented([
        { value: 'files', label: 'Files' },
        { value: 'providers', label: 'Providers', count: memoryProviderRows(providerPayload).length || null },
      ], {
        value: view,
        ariaLabel: 'Memory view',
        onChange: (next) => { void setView(next); },
      })],
      meta: (view === 'providers' ? providerMeta : state?.meta) || null,
      onRefresh: () => (view === 'providers'
        ? loadProviders()
        : loadFile(activeFileId, { force: true })),
    }));
  }

  const sidebar = el('div', { class: 'memory-sidebar' });
  const main = el('section', { class: 'memory-main' });
  // The provider surface is a sibling of the editor rather than a second tab:
  // both answer "what does this agent remember", and swapping panes keeps the
  // file editor's state (including unsaved edits) alive underneath.
  const providersPane = el('section', { class: 'memory-main stack-sm', style: 'display:none' });

  const mainHeader = el('div', { class: 'memory-main-head' });
  const actionBar = el('div', { class: 'memory-actions' });
  const metaRow = el('div', { class: 'memory-meta-row' });
  const editorWrap = el('div', { class: 'memory-editor-wrap' });

  main.append(mainHeader, metaRow, editorWrap);
  root.append(toolbar, main, providersPane);

  let view = 'files';
  let providerPayload = null;
  let providerMeta = null;
  let providerConfigs = new Map();

  const fileStates = new Map(FILES.map((file) => [file.id, makeFileState(file)]));
  const cardRefs = new Map();

  let activeFileId = FILES[0].id;
  let activeEditor = null;
  let activeEditorFile = null;
  let inspectorHost = null;

  const controls = {
    title: el('h2', { class: 'memory-file-title' }),
    dirtyTag: el('span', { class: 'memory-dirty' }),
    stateTag: el('span', { class: 'memory-state' }),
    sourceTag: el('span', { class: 'memory-source' }),
    saveBtn: iconButton({
      icon: 'save',
      label: 'Save',
      onClick: () => { void saveActive(); },
    }),
    reloadBtn: iconButton({
      icon: 'retry',
      label: 'Reload',
      onClick: () => { void reloadActive({ force: true }); },
    }),
    copyBtn: iconButton({
      icon: 'copy',
      label: 'Copy',
      onClick: () => { void copyActive(); },
    }),
    downloadBtn: iconButton({
      icon: 'download',
      label: 'Download',
      onClick: () => { void downloadActive(); },
    }),
  };

  mainHeader.append(
    controls.title,
    controls.dirtyTag,
    controls.stateTag,
    controls.sourceTag,
    actionBar,
  );
  actionBar.append(
    controls.saveBtn,
    controls.reloadBtn,
    controls.copyBtn,
    controls.downloadBtn,
  );

  function endpointFor(fileId) {
    const state = fileStates.get(fileId);
    if (!state) return null;
    return `/api/memory/${encodeURIComponent(state.file.label)}`;
  }

  function activeState() {
    return fileStates.get(activeFileId) || null;
  }

  function confirmDiscard() {
    const state = activeState();
    if (!state?.dirty) return true;
    return window.confirm(`Discard unsaved edits in ${state.file.label}?`);
  }

  function normalizeText(payload) {
    if (typeof payload === 'string') return payload;
    if (payload && typeof payload.content === 'string') return payload.content;
    return '';
  }

  function fileTag(fileId) {
    return fileStates.get(fileId)?.file.label || fileId;
  }

  function renderSidebar() {
    clear(sidebar);
    cardRefs.clear();
    for (const { id, label } of FILES) {
      const state = fileStates.get(id);
      const item = state || makeFileState({ id, label });
      const badge = el('span', {
        class: `memory-card-state${item.dirty ? ' is-dirty' : ''}`,
        text: item.dirty ? 'unsaved' : item.loaded ? 'loaded' : item.error ? 'error' : item.loading ? 'loading' : 'ready',
      });
      const status = item.meta && item.meta.freshness ? item.meta.freshness : (item.loaded ? 'live' : 'idle');
      const card = el('button', {
        type: 'button',
        class: `memory-card${id === activeFileId ? ' is-active' : ''}`,
        onclick: () => { void selectFile(id); },
      }, [
        el('div', { class: 'memory-card-title' }, [
          el('span', { text: label }),
          icon('code', { size: 14 }),
        ]),
        el('div', { class: 'memory-card-meta', text: `state: ${status}` }),
        badge,
      ]);
      sidebar.append(card);
      cardRefs.set(id, { card, badge });
    }
  }

  function updateSideCard(fileId) {
    const refs = cardRefs.get(fileId);
    const state = fileStates.get(fileId);
    if (!refs || !state) return;
    refs.card.classList.toggle('is-active', fileId === activeFileId);
    refs.badge.classList.toggle('is-dirty', Boolean(state.dirty));
    if (state.error) {
      refs.badge.textContent = 'error';
      refs.badge.classList.remove('is-dirty');
      return;
    }
    if (state.loading) {
      refs.badge.textContent = 'loading';
      return;
    }
    if (state.dirty) {
      refs.badge.textContent = 'unsaved';
      return;
    }
    refs.badge.textContent = state.loaded ? 'loaded' : 'ready';
  }

  function renderMeta(state) {
    clear(metaRow);
    controls.stateTag.textContent = state?.loading ? 'loading…' : state?.error ? 'failed' : state?.saving ? 'saving…' : state?.meta?.freshness || 'ready';
    controls.sourceTag.textContent = state?.meta?.source_id ? `source: ${state.meta.source_id}` : 'source: adapter';
    if (state?.requestId) {
      metaRow.append(el('span', { class: 'memory-req-id', text: `request-id: ${state.requestId}` }));
    }
  }

  function renderHeader(state) {
    controls.title.textContent = state?.file?.label || fileTag(activeFileId);
    controls.dirtyTag.textContent = state?.dirty ? 'unsaved edits' : '';
    renderMeta(state);
  }

  function updateActionBars() {
    const state = activeState();
    controls.saveBtn.disabled = !state || !state.dirty || state.loading || state.saving;
    controls.reloadBtn.disabled = !state || state.loading || state.saving;
    controls.copyBtn.disabled = !state || state.loading;
    controls.downloadBtn.disabled = !state || state.loading;

    if (controls.saveBtn.disabled) {
      controls.saveBtn.title = state?.dirty ? 'Save current file' : 'No changes to save';
    } else {
      controls.saveBtn.title = 'Save current file';
    }

    renderHeader(state);
    renderMeta(state);
    // Keeps the header's freshness stamp tied to the file actually on screen —
    // it changes with every load, save and file switch.
    renderToolbar();
    if (state) updateSideCard(state.file.id);
  }

  async function loadFile(fileId, { force = false } = {}) {
    const state = fileStates.get(fileId);
    if (!state || state.loading) return;
    if (!force && state.loaded) return;

    const path = endpointFor(fileId);
    if (!path) {
      state.error = 'Unknown memory file';
      updateActionBars();
      renderEditor(state);
      return;
    }

    if (state.error) state.error = null;
    state.requestId = null;
    if (activeEditor && activeEditorFile === fileId) {
      activeEditor.destroy();
      activeEditor = null;
      activeEditorFile = null;
    }
    state.loading = true;
    if (fileId === activeFileId) {
      renderEditor(state);
    }
    updateActionBars();

    try {
      const envelope = await api.get(path, { profile });
      const content = normalizeText(envelope?.data);
      state.meta = envelope?.meta || {
        ...STATE_NOT_FOUND,
        source_id: 'adapter',
      };
      state.requestId = state.meta.request_id || null;
      state.content = content;
      state.baseline = content;
      state.dirty = false;
      state.loaded = true;
      state.error = null;
    } catch (err) {
      state.error = err.message || 'load failed';
      state.requestId = err.request_id || err.requestId || null;
      state.loaded = false;
      state.content = '';
      state.baseline = '';
      state.dirty = false;
      state.meta = {
        ...STATE_NOT_FOUND,
        source_id: 'adapter',
        request_id: state.requestId,
      };
      if (activeEditor && activeEditorFile === fileId) {
        activeEditor.destroy();
        activeEditor = null;
        activeEditorFile = null;
      }
    } finally {
      state.loading = false;
      updateActionBars();
      renderEditor(state);
      renderSidebar();
      updateSideCard(fileId);
    }
  }

  function setEditorValue(state) {
    if (!activeEditor) return;
    activeEditor.setValue(state.content, { language: 'markdown' });
    activeEditor.markClean(state.baseline);
    state.dirty = false;
  }

  function createEditor(state) {
    if (activeEditor) {
      activeEditor.destroy();
      activeEditor = null;
      activeEditorFile = null;
    }

    activeEditor = createCodeEditor({
      value: state.content,
      language: 'markdown',
      onChange: (next) => {
        state.content = next;
        state.dirty = next !== state.baseline;
        updateActionBars();
      },
      onDirtyChange: (dirty) => {
        state.dirty = Boolean(dirty);
        updateActionBars();
        updateSideCard(state.file.id);
      },
      onSave: () => { void saveActive(); },
    });

    activeEditor.markClean(state.baseline);
    activeEditorFile = state.file.id;
    editorWrap.append(activeEditor.node);
    activeEditor.focus();
  }

  function renderEditor(state) {
    clear(editorWrap);
    if (!state) {
      editorWrap.append(el('div', { class: 'memory-editor-empty', text: 'No file selected' }));
      return;
    }

    if (state.loading) {
      editorWrap.append(skeleton({ lines: 14 }));
      return;
    }

    if (state.error) {
      editorWrap.append(errorPanel({
        message: `${state.file.label}: ${state.error}`,
        requestId: state.requestId,
        onRetry: () => { void loadFile(state.file.id, { force: true }); },
      }));
      return;
    }

    if (!state.loaded && !state.content) {
      editorWrap.append(unavailableState({
        reason: `${state.file.label} not available yet`,
        requestId: state.requestId,
      }));
      return;
    }

    if (activeEditor && activeEditorFile === state.file.id) {
      // keep existing editor and force latest value when switching from a
      // failed reload to a successful read.
      setEditorValue(state);
      editorWrap.append(activeEditor.node);
      return;
    }

    createEditor(state);
  }

  async function saveActive() {
    const state = activeState();
    if (!state || !state.dirty || state.saving) return;
    if (!state.loaded) {
      toast('Cannot save before the file is loaded', { tone: 'warn' });
      return;
    }

    state.saving = true;
    updateActionBars();

    const path = endpointFor(state.file.id);
    try {
      const saved = await api.put(path, { content: state.content }, { profile });
      const content = normalizeText(saved?.data);
      const next = content !== '' ? content : state.content;
      state.content = next;
      state.baseline = next;
      state.dirty = false;
      state.meta = saved?.meta || state.meta;
      state.requestId = state.meta?.request_id || null;
      if (activeEditor && activeEditorFile === state.file.id) {
        activeEditor.markClean(next);
      }
      toast(`Saved ${state.file.label}`, { tone: 'ok' });
    } catch (err) {
      toast(`Save failed: ${err.message || 'error'}`, { tone: 'warn', detail: err.request_id || null });
    } finally {
      state.saving = false;
      updateSideCard(state.file.id);
      updateActionBars();
    }
  }

  async function reloadActive({ force = false } = {}) {
    const state = activeState();
    if (!state) return;
    if (!force && !state.dirty) {
      await loadFile(state.file.id, { force: true });
      return;
    }
    if (state.dirty && !confirmDiscard()) return;
    if (activeEditor) {
      activeEditor.destroy();
      activeEditor = null;
      activeEditorFile = null;
    }
    await loadFile(state.file.id, { force: true });
  }

  async function selectFile(fileId) {
    const next = fileStates.get(fileId);
    if (!next || fileId === activeFileId) return;
    if (!confirmDiscard()) return;

    if (activeEditor) {
      activeEditor.destroy();
      activeEditor = null;
      activeEditorFile = null;
    }

    activeFileId = fileId;
    renderSidebar();
    renderHeader(next);
    renderEditor(next);
    updateActionBars();
    await loadFile(fileId);
  }

  async function copyActive() {
    const state = activeState();
    if (!state) return;
    try {
      await navigator.clipboard.writeText(state.content || '');
      toast(`Copied ${state.file.label} content`, { tone: 'ok' });
    } catch (err) {
      toast(`Copy failed: ${err.message || 'clipboard blocked'}`, { tone: 'warn' });
    }
  }

  function downloadActive() {
    const state = activeState();
    if (!state) return;
    const blob = new Blob([state.content || ''], { type: 'text/markdown;charset=utf-8' });
    const href = URL.createObjectURL(blob);
    const anchor = el('a', {
      href,
      download: state.file.label,
      style: 'display:none',
    });
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(href);
  }

  // ---------------------------------------------------------------- providers

  function renderViewSwitch() {
    renderToolbar();
  }

  async function setView(next) {
    view = next;
    main.style.display = next === 'files' ? '' : 'none';
    providersPane.style.display = next === 'providers' ? '' : 'none';
    toolbarHintText = next === 'files'
      ? '.hermes/memories/MEMORY.md · USER.md'
      : 'provider selection, configuration and reset';
    renderViewSwitch();
    if (next === 'providers') await loadProviders();
  }

  async function loadProviders() {
    clear(providersPane);
    providersPane.append(skeleton({ lines: 6 }));
    const result = await loadEnvelope(api, '/api/upstream/api/memory', { profile, allowEmpty: false });
    providerMeta = result.meta;
    providerPayload = result.state === 'ready' ? result.data : null;
    renderViewSwitch();
    renderProviders(result);
  }

  async function loadProviderConfig(name) {
    if (providerConfigs.has(name)) return providerConfigs.get(name);
    const result = await loadEnvelope(api, `/api/upstream/api/memory/providers/${encodeURIComponent(name)}/config`, {
      profile,
      allowEmpty: false,
    });
    const config = result.state === 'ready' ? result.data : null;
    providerConfigs.set(name, config);
    return config;
  }

  function providerConfigForm(name, config, { setup = false } = {}) {
    const fields = Array.isArray(config?.fields) ? config.fields : [];
    if (!fields.length) {
      return el('div', { class: 'field-hint', text: 'This provider takes no configuration.' });
    }
    const values = {};
    for (const field of fields) {
      // Secrets come back blank with `is_set`; anything else round-trips.
      values[field.key] = field.kind === 'secret' ? '' : (field.value ?? '');
    }
    const form = createForm({
      submitLabel: setup ? 'Run setup' : 'Save configuration',
      submitIcon: setup ? 'spark' : 'save',
      note: setup
        ? 'Setup installs dependencies and writes the values in one step.'
        : 'Only changed fields are sent. A blank secret leaves the stored one untouched.',
      values,
      fields: fields.map(providerConfigField),
      onSubmit: async (diff) => {
        if (!Object.keys(diff).length) return;
        const path = setup
          ? `/api/upstream/api/memory/providers/${encodeURIComponent(name)}/setup`
          : `/api/upstream/api/memory/providers/${encodeURIComponent(name)}/config`;
        form.setBusy(true);
        const res = await runMutation(
          () => (setup
            ? api.post(path, { values: diff }, { profile })
            : api.put(path, { values: diff }, { profile })),
          { pending: setup ? 'Run setup' : 'Save configuration', ok: `${name} ${setup ? 'set up' : 'configured'}` },
        );
        form.setBusy(false);
        if (res) {
          providerConfigs.delete(name);
          await loadProviders();
        }
      },
    });
    return form.node;
  }

  function providerCard(provider) {
    const card = el('div', { class: 'sub-card' });
    card.append(el('div', { class: 'choice-row' }, [
      el('div', { class: 'cell-stack' }, [
        el('span', { class: 'cell-strong', text: provider.name }),
        el('span', { class: 'cell-dim', text: provider.description }),
      ]),
      el('div', { class: 'inline-chips' }, [
        provider.active ? statusChip('ok', 'active') : null,
        statusChip(PROVIDER_TONE[provider.status] || 'idle', provider.status),
        provider.dependencies_installed ? null : statusChip('warn', 'deps missing'),
      ].filter(Boolean)),
    ]));

    if (!provider.dependencies_installed) {
      const deps = [
        ...provider.pip_dependencies.map((d) => `pip: ${d}`),
        ...provider.external_dependencies.map((d) => `${d.name}: ${d.install}`),
      ];
      if (deps.length) {
        card.append(el('div', { class: 'field-hint', text: `Needs ${deps.join(' · ')}` }));
      }
    }

    const actions = el('div', { class: 'inline-chips' });
    if (!provider.active) {
      actions.append(el('button', {
        class: 'btn btn-sm btn-accent',
        type: 'button',
        disabled: !provider.available,
        title: provider.available ? `Make ${provider.name} the active memory provider` : 'Dependencies are not installed',
        text: 'Activate',
        onclick: () => runMutation(
          () => api.put('/api/upstream/api/memory/provider', { provider: provider.name }, { profile }),
          { pending: 'Set provider', ok: `${provider.name} is now the memory provider`, onDone: () => loadProviders() },
        ),
      }));
    }

    const configHost = el('div');
    let expanded = false;
    actions.append(el('button', {
      class: 'btn btn-sm',
      type: 'button',
      text: 'Configure',
      onclick: async () => {
        expanded = !expanded;
        clear(configHost);
        if (!expanded) return;
        configHost.append(el('div', { class: 'field-hint', text: 'loading configuration…' }));
        const config = await loadProviderConfig(provider.name);
        clear(configHost);
        if (!config) {
          configHost.append(el('div', { class: 'field-hint', text: 'This provider exposes no configuration endpoint.' }));
          return;
        }
        configHost.append(providerConfigForm(provider.name, config, { setup: !provider.dependencies_installed }));
      },
    }));
    card.append(actions, configHost);
    return card;
  }

  function renderProviders(result) {
    clear(providersPane);
    if (result.state !== 'ready') {
      providersPane.append(unavailableState({
        reason: result.reason || 'Memory providers unavailable',
        requestId: result.requestId,
      }));
      return;
    }

    const providers = memoryProviderRows(providerPayload);
    const active = providers.find((p) => p.active);
    providersPane.append(el('div', { class: 'tab-banner' }, [
      icon('memory', { size: 14 }),
      el('div', { class: 'cell-stack' }, [
        el('span', { class: 'cell-strong', text: active ? `Active provider: ${active.name}` : 'No memory provider selected' }),
        el('span', {
          class: 'cell-dim',
          text: `${providers.filter((p) => p.available).length} of ${providers.length} providers have their dependencies installed`,
        }),
      ]),
    ]));

    const list = el('div', { class: 'stack-sm' });
    for (const provider of providers) list.append(providerCard(provider));
    providersPane.append(list);

    // Reset is the only destructive control in this tab, so it sits apart from
    // the provider list with its own arming step and target selector.
    const resetSelect = el('select', { class: 'select input-sm' });
    for (const target of RESET_TARGETS) {
      resetSelect.append(el('option', { value: target.value, text: target.label }));
    }
    providersPane.append(el('div', { class: 'sub-card' }, [
      el('div', { class: 'cell-strong', text: 'Reset memory' }),
      el('div', { class: 'field-hint', text: 'Wipes stored memory on the Hermes host. There is no undo.' }),
      el('div', { class: 'inline-chips' }, [
        resetSelect,
        confirmAction({
          label: 'Reset',
          iconName: 'trash',
          confirmLabel: 'Reset — confirm?',
          onConfirm: () => runMutation(
            () => api.post('/api/upstream/api/memory/reset?confirm=true', { target: resetSelect.value }, { profile }),
            {
              pending: 'Reset memory',
              ok: `Memory reset (${resetSelect.value})`,
              onDone: async () => {
                await Promise.all(FILES.map((file) => loadFile(file.id, { force: true }).catch(() => {})));
                await loadProviders();
              },
            },
          ),
        }),
      ]),
    ]));
  }

  function renderInspector(container) {
    inspectorHost = container;
    clear(container);
    container.append(el('div', { class: 'memory-inspector-head' }, [
      el('div', { class: 'memory-inspector-title' }, [
        icon('code', { size: 15 }),
        el('span', { text: 'Memory files' }),
      ]),
      el('div', {
        class: 'memory-inspector-path',
        text: 'jarvis@192.168.1.128 · ~/.hermes/memories',
      }),
    ]));
    container.append(sidebar);
    renderSidebar();
  }

  async function activate() {
    if (!inspectorHost && typeof refreshInspector === 'function') refreshInspector();
    renderViewSwitch();
    renderSidebar();
    const startup = fileStates.get(activeFileId);
    renderHeader(startup);
    await Promise.all(FILES.map((file) => loadFile(file.id).catch(() => {})));
    if (startup) {
      renderEditor(startup);
    }
    updateActionBars();
  }

  function deactivate() {
    if (activeEditor) {
      activeEditor.destroy();
      activeEditor = null;
      activeEditorFile = null;
    }
  }

  return {
    mount(container) {
      clear(container);
      container.append(root);
    },
    activate,
    deactivate,
    refresh() {
      if (view === 'providers') return loadProviders();
      return loadFile(activeFileId, { force: true });
    },
    renderInspector,
    get data() {
      const state = activeState();
      return state ? state.meta || { freshness: 'unsupported', request_id: null } : null;
    },
  };
}

export function renderMemory(envelope) {
  const data = envelope?.data;
  return {
    rows: [{
      id: data?.file || 'memory.md',
      text: data?.content || '',
      freshness: envelope?.meta?.freshness,
    }],
    state: envelope?.meta?.freshness === 'unavailable' ? 'unavailable' : null,
  };
}

export function renderMemorySearch(envelope, query) {
  const rows = renderMemory(envelope).rows;
  if (!query) return { rows };
  const lower = String(query).toLowerCase();
  const matched = rows.filter((row) => String(row.text).toLowerCase().includes(lower));
  return { rows: matched };
}
