// Markdown token tree → DOM. Shared by any surface that shows prose it did not
// author (chat replies, skill documents, previews).
//
// Nodes are built with `el`/textContent only — never innerHTML — so a document
// can style itself without being able to inject markup.

import { el, clear } from './ui.js';
import { parseMarkdown, looksLikeMarkdown } from './pure/markdown.js';
import { highlight } from './pure/code-highlight.js';
import { icon } from './icons.js';

export function truncate(value, limit = 4000) {
  const text = String(value || '');
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

/**
 * renderMarkdown(target, text, opts) — replaces `target`'s children.
 * Plain text keeps the caller's `pre-wrap` treatment (the `md` class is only
 * added once the body actually holds block elements), so a one-line reply still
 * looks typed rather than typeset.
 */
export function renderMarkdown(target, text, { limit = 4000, force = false } = {}) {
  clear(target);
  const raw = String(text ?? '');
  if (!force && !looksLikeMarkdown(raw)) {
    target.textContent = truncate(raw, limit);
    target.classList.remove('md');
    return target;
  }
  target.classList.add('md');
  for (const block of parseMarkdown(truncate(raw, limit))) {
    const node = renderBlock(block);
    if (node) target.append(node);
  }
  return target;
}

export function renderBlock(block) {
  switch (block.type) {
    case 'code':
      return renderCodeBlock(block);
    case 'heading':
      return el(`h${Math.min(block.level + 2, 6)}`, { class: 'md-h' }, renderSpans(block.spans));
    case 'rule':
      return el('hr', { class: 'md-rule' });
    case 'quote':
      return el('blockquote', { class: 'md-quote' }, renderSpans(block.spans));
    case 'list': {
      const list = el(block.ordered ? 'ol' : 'ul', { class: 'md-list' });
      for (const item of block.items) list.append(el('li', {}, renderSpans(item)));
      return list;
    }
    case 'table':
      return renderTable(block);
    default:
      return el('p', { class: 'md-p' }, renderSpans(block.spans));
  }
}

/**
 * Fenced code, tokenized and copyable.
 *
 * Tokens become nodes via `textContent` only — the tokenizer guarantees every
 * line re-joins to its exact source, so highlighting can never alter the text a
 * reader is about to copy. An unlabelled fence stays plain: guessing a language
 * for output that is probably a log or a diff colours it wrong.
 */
export function renderCodeBlock(block) {
  const wrap = el('div', { class: 'md-code-wrap' });
  const pre = el('pre', { class: 'md-code' });
  const code = el('code');

  const lang = String(block.lang || '').toLowerCase();
  if (lang) {
    pre.setAttribute('data-lang', lang);
    const lines = highlight(block.text, lang);
    lines.forEach((tokens, index) => {
      if (index) code.append(document.createTextNode('\n'));
      for (const tok of tokens) {
        code.append(tok.kind && tok.kind !== 'text'
          ? el('span', { class: `tok tok-${tok.kind}`, text: tok.text })
          : document.createTextNode(tok.text));
      }
    });
  } else {
    code.textContent = block.text;
  }
  pre.append(code);

  const copy = el('button', {
    class: 'md-code-copy',
    type: 'button',
    title: 'Copy code',
    'aria-label': 'Copy code',
    onclick: () => copyText(block.text, copy),
  }, [icon('copy', { size: 12 })]);

  wrap.append(copy, pre);
  return wrap;
}

/**
 * Copy with visible confirmation. `navigator.clipboard` is unavailable on
 * insecure origins — which this dashboard is, over plain HTTP on the LAN — so
 * the textarea fallback is the path that actually runs here, not a legacy
 * branch.
 */
export function copyText(text, button = null) {
  const done = (ok) => {
    if (!button) return;
    button.classList.toggle('is-done', ok);
    button.classList.toggle('is-failed', !ok);
    button.title = ok ? 'Copied' : 'Copy failed';
    setTimeout(() => {
      button.classList.remove('is-done', 'is-failed');
      button.title = 'Copy';
    }, 1400);
  };

  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(String(text ?? '')).then(() => done(true), () => done(false));
    return;
  }
  const scratch = el('textarea', { class: 'copy-scratch', 'aria-hidden': 'true' });
  scratch.value = String(text ?? '');
  document.body.append(scratch);
  scratch.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (_err) {
    ok = false;
  }
  scratch.remove();
  done(ok);
}

export function renderTable(block) {
  // Wide tables scroll inside their own box rather than stretching the column.
  const scroller = el('div', { class: 'md-table-wrap' });
  const table = el('table', { class: 'md-table' });

  const thead = el('thead');
  const headRow = el('tr');
  block.head.forEach((cell, column) => {
    const th = el('th', {}, renderSpans(cell));
    if (block.align[column]) th.style.textAlign = block.align[column];
    headRow.append(th);
  });
  thead.append(headRow);
  table.append(thead);

  const tbody = el('tbody');
  for (const row of block.rows) {
    const tr = el('tr');
    // Ragged rows are common in generated tables; pad so columns stay aligned.
    for (let column = 0; column < block.head.length; column += 1) {
      const td = el('td', {}, renderSpans(row[column] || [{ type: 'text', text: '' }]));
      if (block.align[column]) td.style.textAlign = block.align[column];
      tr.append(td);
    }
    tbody.append(tr);
  }
  table.append(tbody);

  scroller.append(table);
  return scroller;
}

export function renderSpans(spans) {
  return (spans || []).map((span) => {
    switch (span.type) {
      case 'code': return el('code', { class: 'md-inline-code', text: span.text });
      case 'strong': return el('strong', { text: span.text });
      case 'em': return el('em', { text: span.text });
      case 'strike': return el('s', { text: span.text });
      case 'link': {
        const link = el('a', { class: 'md-link', text: span.text, href: span.href });
        link.setAttribute('target', '_blank');
        link.setAttribute('rel', 'noopener noreferrer');
        return link;
      }
      default: return document.createTextNode(span.text);
    }
  });
}
