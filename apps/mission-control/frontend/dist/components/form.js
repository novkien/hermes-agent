// Declarative form builder with dirty tracking and diff-aware submit.
//
// The diff is not a nicety: the config-write path must send only the fields the
// operator actually touched, because resubmitting an untouched redacted field
// would push the "[redacted]" sentinel upstream over a real secret.

import { el, clear } from '../ui.js';
import { icon } from '../icons.js';

const SENTINEL = '[redacted]';

function sameValue(a, b) {
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((v, i) => String(v) === String(b[i]));
  }
  if (a === null || a === undefined) return b === null || b === undefined || b === '';
  if (b === null || b === undefined) return a === '';
  return String(a) === String(b);
}

function coerce(field, raw) {
  if (field.type === 'number') {
    if (raw === '' || raw === null) return null;
    const n = Number(raw);
    return Number.isFinite(n) ? n : raw;
  }
  if (field.type === 'toggle') return Boolean(raw);
  return raw;
}

function buildControl(field, value, onInput) {
  switch (field.type) {
    case 'textarea': {
      const node = el('textarea', {
        rows: field.rows || 4,
        placeholder: field.placeholder || '',
        spellcheck: 'false',
      });
      node.value = value ?? '';
      node.addEventListener('input', () => onInput(node.value));
      return node;
    }
    case 'select': {
      const node = el('select', { class: 'select' });
      const opts = typeof field.options === 'function' ? field.options() : (field.options || []);
      for (const opt of opts) {
        const o = typeof opt === 'object' ? opt : { value: opt, label: String(opt) };
        node.append(el('option', { value: o.value }, o.label));
      }
      node.value = value ?? '';
      node.addEventListener('change', () => onInput(node.value));
      return node;
    }
    case 'toggle': {
      const node = el('button', {
        class: 'switch',
        type: 'button',
        role: 'switch',
        'aria-checked': value ? 'true' : 'false',
        'aria-label': field.label || field.key,
      });
      node.addEventListener('click', () => {
        const next = node.getAttribute('aria-checked') !== 'true';
        node.setAttribute('aria-checked', next ? 'true' : 'false');
        onInput(next);
      });
      return node;
    }
    case 'tags': {
      const box = el('div', { class: 'taglist' });
      let tags = Array.isArray(value) ? value.slice() : [];
      const input = el('input', { class: 'input', placeholder: field.placeholder || 'add…', style: 'flex:1 1 90px;min-width:90px' });
      function paint() {
        clear(box);
        tags.forEach((tag, i) => {
          box.append(el('span', { class: 'taglist-tag' }, [
            String(tag),
            el('button', {
              class: 'taglist-x', type: 'button', 'aria-label': `remove ${tag}`,
              onclick: () => { tags.splice(i, 1); paint(); onInput(tags.slice()); },
            }, '×'),
          ]));
        });
        box.append(input);
      }
      input.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' && event.key !== ',') return;
        event.preventDefault();
        const raw = input.value.trim().replace(/,$/, '');
        if (!raw || tags.includes(raw)) { input.value = ''; return; }
        tags.push(raw);
        input.value = '';
        paint();
        onInput(tags.slice());
      });
      paint();
      return box;
    }
    default: {
      const node = el('input', {
        class: 'input',
        type: field.type === 'number' ? 'number' : 'text',
        placeholder: field.placeholder || '',
        spellcheck: 'false',
      });
      node.value = value === null || value === undefined ? '' : String(value);
      node.addEventListener('input', () => onInput(node.value));
      return node;
    }
  }
}

/**
 * @param {object} opts
 * @param {Array}  opts.fields   {key, label, type, hint, required, options, rows, validate,
 *                                disabled, disabledReason, redacted, span}
 * @param {object} [opts.values] initial values (the baseline the diff is taken against)
 * @param {Function} opts.onSubmit  (diff, allValues) -> Promise|void
 */
export function createForm({
  fields = [],
  values = {},
  onSubmit = () => {},
  onCancel = null,
  submitLabel = 'Save',
  submitIcon = 'save',
  note = '',
} = {}) {
  const root = el('form', { class: 'form' });
  const grid = el('div', { class: 'form-grid' });
  let baseline = { ...values };
  let current = { ...values };
  const errors = new Map();
  const refs = new Map();

  const submitBtn = el('button', { class: 'btn btn-accent', type: 'submit' }, [icon(submitIcon, { size: 12 }), ` ${submitLabel}`]);
  const dirtyNote = el('span', { class: 'form-note' });

  function diff() {
    const out = {};
    for (const field of fields) {
      if (field.disabled) continue;
      const next = current[field.key];
      if (sameValue(next, baseline[field.key])) continue;
      // A field still holding the server's mask was never edited by a human.
      if (typeof next === 'string' && next === SENTINEL) continue;
      out[field.key] = next;
    }
    return out;
  }

  function validate() {
    errors.clear();
    for (const field of fields) {
      if (field.disabled) continue;
      const value = current[field.key];
      if (field.required && (value === null || value === undefined || value === '')) {
        errors.set(field.key, 'Required');
        continue;
      }
      if (field.validate) {
        const message = field.validate(value, current);
        if (message) errors.set(field.key, message);
      }
    }
    return errors.size === 0;
  }

  function paintState() {
    const changed = Object.keys(diff());
    submitBtn.disabled = changed.length === 0;
    dirtyNote.textContent = changed.length
      ? `${changed.length} change${changed.length === 1 ? '' : 's'} pending`
      : (note || '');
    for (const [key, ref] of refs) {
      const message = errors.get(key);
      ref.field.classList.toggle('is-invalid', Boolean(message));
      ref.field.classList.toggle('is-dirty', !sameValue(current[key], baseline[key]));
      ref.error.textContent = message || '';
      ref.dot.style.visibility = sameValue(current[key], baseline[key]) ? 'hidden' : 'visible';
    }
  }

  for (const field of fields) {
    // A span is a request, not a guarantee: it is honoured by the container
    // query in styles.css only when the form is wide enough to have a second
    // column. Setting `grid-column` inline here would force an implicit column
    // in the 320px inspector and collapse the field to a sliver.
    const wrap = el('div', {
      class: `field${Number(field.span) >= 2 ? ' field-span-2' : ''}`,
    });
    const dot = el('span', { class: 'field-dirty-dot', style: 'visibility:hidden' });
    const label = el('label', { class: 'field-label' }, [
      field.label || field.key,
      field.required ? el('span', { class: 'field-required', text: '*' }) : null,
      dot,
    ].filter(Boolean));
    const control = buildControl(field, current[field.key], (raw) => {
      current[field.key] = coerce(field, raw);
      validate();
      paintState();
    });
    if (field.disabled) {
      control.disabled = true;
      if (field.disabledReason) control.title = field.disabledReason;
    }
    const errorNode = el('div', { class: 'field-error' });
    wrap.append(label, control);
    if (field.hint || (field.disabled && field.disabledReason)) {
      wrap.append(el('div', { class: 'field-hint', text: field.hint || field.disabledReason }));
    }
    wrap.append(errorNode);
    refs.set(field.key, { field: wrap, control, error: errorNode, dot });
    grid.append(wrap);
  }

  const actions = el('div', { class: 'form-actions' }, [submitBtn]);
  if (onCancel) {
    actions.append(el('button', { class: 'btn btn-sm', type: 'button', onclick: onCancel }, 'Cancel'));
  }
  actions.append(dirtyNote);

  root.append(grid, actions);
  root.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!validate()) { paintState(); return; }
    const changed = diff();
    if (!Object.keys(changed).length) return;
    submitBtn.disabled = true;
    try {
      await onSubmit(changed, { ...current });
    } finally {
      paintState();
    }
  });

  paintState();

  return {
    node: root,
    get values() { return { ...current }; },
    get diff() { return diff(); },
    /** Adopt the server's post-save state as the new clean baseline. */
    commit(next) {
      baseline = { ...(next || current) };
      current = { ...baseline };
      for (const field of fields) {
        const ref = refs.get(field.key);
        if (!ref) continue;
        const value = current[field.key];
        if (field.type === 'toggle') ref.control.setAttribute('aria-checked', value ? 'true' : 'false');
        else if (field.type !== 'tags') ref.control.value = value === null || value === undefined ? '' : String(value);
      }
      errors.clear();
      paintState();
    },
    setBusy(busy) { submitBtn.disabled = busy || Object.keys(diff()).length === 0; },
  };
}
