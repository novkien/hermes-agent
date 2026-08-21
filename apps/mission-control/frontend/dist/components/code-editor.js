// Code editor: gutter + highlight layer + textarea, no external editor library.
//
// The two layers share one scrolling box: the highlight `<pre>` sets the
// content size and the textarea is absolutely positioned over it, so there is
// no scroll-sync loop to drift. Both layers use identical font metrics and the
// same `white-space`, which is what keeps the caret sitting on its glyph in
// both no-wrap and wrap modes.

import { el, clear } from '../ui.js';
import { icon } from '../icons.js';
import { highlight } from '../pure/code-highlight.js';

// Above this many lines the highlight pass costs more than it is worth on a
// low-power host, so the editor falls back to plain text.
const HIGHLIGHT_LINE_BUDGET = 4000;
const HIGHLIGHT_DEBOUNCE_MS = 90;
const INDENT = '  ';

export function createCodeEditor({
  value = '',
  language = 'markdown',
  readOnly = false,
  wrap = false,
  placeholder = '',
  onChange = () => {},
  onSave = null,
  onDirtyChange = () => {},
} = {}) {
  let baseline = String(value ?? '');
  let currentLanguage = language;
  let wrapped = Boolean(wrap);
  let dirty = false;
  let highlightTimer = null;

  const code = el('pre', { class: 'ide-code', 'aria-hidden': 'true' });
  const input = el('textarea', {
    class: 'ide-input',
    spellcheck: 'false',
    autocapitalize: 'off',
    autocomplete: 'off',
    autocorrect: 'off',
    'aria-label': 'Source editor',
  });
  input.value = baseline;
  input.placeholder = placeholder;
  input.readOnly = Boolean(readOnly);

  const sizer = el('div', { class: 'ide-sizer' }, [code, input]);
  const body = el('div', { class: 'ide-body' }, [sizer]);

  const posLabel = el('span', { class: 'ide-stat', text: 'Ln 1, Col 1' });
  const sizeLabel = el('span', { class: 'ide-stat' });
  const dirtyLabel = el('span', { class: 'ide-stat ide-stat-dirty', text: '' });
  const langLabel = el('span', { class: 'ide-stat', text: currentLanguage });

  const wrapToggle = el('button', {
    class: 'ide-stat-btn',
    type: 'button',
    title: 'Toggle word wrap',
    onclick: () => setWrap(!wrapped),
  }, [icon('wrap', { size: 12 }), el('span', { text: 'wrap' })]);

  const statusbar = el('div', { class: 'ide-status' }, [
    posLabel,
    sizeLabel,
    dirtyLabel,
    el('span', { class: 'ide-status-gap' }),
    wrapToggle,
    langLabel,
  ]);

  const root = el('div', { class: 'ide' }, [body, statusbar]);
  applyWrap();

  function applyWrap() {
    root.classList.toggle('ide-wrap', wrapped);
    wrapToggle.classList.toggle('active', wrapped);
  }

  function paint() {
    const text = input.value;
    const lines = text.split('\n');
    const tokenized = lines.length > HIGHLIGHT_LINE_BUDGET
      ? lines.map((line) => [{ text: line, kind: 'text' }])
      : highlight(text, currentLanguage);

    clear(code);
    const frag = document.createDocumentFragment();
    tokenized.forEach((tokens, index) => {
      const row = el('div', { class: 'ide-row' });
      row.append(el('span', { class: 'ide-ln', 'aria-hidden': 'true', text: String(index + 1) }));
      const src = el('span', { class: 'ide-src' });
      for (const tok of tokens) {
        if (!tok.text) continue;
        if (tok.kind === 'text') src.append(document.createTextNode(tok.text));
        else src.append(el('span', { class: `tok tok-${tok.kind}`, text: tok.text }));
      }
      // A trailing newline must still occupy a row, so empty lines get a
      // zero-width filler rather than collapsing to zero height.
      if (!src.childNodes.length) src.append(document.createTextNode('​'));
      row.append(src);
      frag.append(row);
    });
    code.append(frag);

    sizeLabel.textContent = `${lines.length} lines · ${text.length} chars`;
    langLabel.textContent = currentLanguage;
  }

  function schedulePaint() {
    clearTimeout(highlightTimer);
    highlightTimer = setTimeout(paint, HIGHLIGHT_DEBOUNCE_MS);
  }

  function updateCaret() {
    const upto = input.value.slice(0, input.selectionStart);
    const line = upto.split('\n');
    posLabel.textContent = `Ln ${line.length}, Col ${line[line.length - 1].length + 1}`;
  }

  function setDirty(next) {
    if (dirty === next) return;
    dirty = next;
    dirtyLabel.textContent = dirty ? 'unsaved' : '';
    root.classList.toggle('is-dirty', dirty);
    onDirtyChange(dirty);
  }

  input.addEventListener('input', () => {
    schedulePaint();
    updateCaret();
    setDirty(input.value !== baseline);
    onChange(input.value);
  });
  input.addEventListener('keyup', updateCaret);
  input.addEventListener('click', updateCaret);
  input.addEventListener('scroll', () => {
    // The textarea has nothing of its own to scroll, but a caret move can still
    // nudge it; fold any nudge back into the shared scroller.
    if (input.scrollTop) {
      body.scrollTop += input.scrollTop;
      input.scrollTop = 0;
    }
    if (input.scrollLeft) {
      body.scrollLeft += input.scrollLeft;
      input.scrollLeft = 0;
    }
  });

  input.addEventListener('keydown', (event) => {
    const meta = event.metaKey || event.ctrlKey;
    if (meta && event.key.toLowerCase() === 's') {
      event.preventDefault();
      if (onSave) onSave(input.value);
      return;
    }
    if (event.key === 'Tab') {
      event.preventDefault();
      indent(event.shiftKey);
    }
  });

  function indent(outdent) {
    const { selectionStart: start, selectionEnd: end, value: text } = input;
    const lineStart = text.lastIndexOf('\n', start - 1) + 1;

    if (start === end && !outdent) {
      replaceRange(start, end, INDENT, start + INDENT.length);
      return;
    }

    const lineEnd = text.indexOf('\n', end);
    const blockEnd = lineEnd === -1 ? text.length : lineEnd;
    const block = text.slice(lineStart, blockEnd);
    const next = outdent
      ? block.split('\n').map((line) => line.replace(new RegExp(`^( {1,${INDENT.length}}|\t)`), '')).join('\n')
      : block.split('\n').map((line) => INDENT + line).join('\n');
    replaceRange(lineStart, blockEnd, next, lineStart, lineStart + next.length);
  }

  function replaceRange(from, to, text, selStart, selEnd = selStart) {
    input.setRangeText(text, from, to, 'preserve');
    input.selectionStart = selStart;
    input.selectionEnd = selEnd;
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function setWrap(next) {
    wrapped = Boolean(next);
    applyWrap();
  }

  paint();
  updateCaret();

  return {
    node: root,
    input,
    focus() {
      input.focus();
    },
    getValue() {
      return input.value;
    },
    /** Load a new document; resets the dirty baseline. */
    setValue(next, { language: nextLanguage } = {}) {
      baseline = String(next ?? '');
      if (nextLanguage) currentLanguage = nextLanguage;
      input.value = baseline;
      paint();
      updateCaret();
      setDirty(false);
      body.scrollTop = 0;
      body.scrollLeft = 0;
    },
    /** Accept the current buffer as saved without reloading it. */
    markClean(next) {
      baseline = next === undefined ? input.value : String(next);
      setDirty(input.value !== baseline);
    },
    revert() {
      input.value = baseline;
      paint();
      updateCaret();
      setDirty(false);
      onChange(baseline);
    },
    setReadOnly(next) {
      input.readOnly = Boolean(next);
      root.classList.toggle('is-readonly', Boolean(next));
    },
    setWrap,
    get dirty() {
      return dirty;
    },
    get baseline() {
      return baseline;
    },
    destroy() {
      clearTimeout(highlightTimer);
    },
  };
}
