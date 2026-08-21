// Pure syntax tokenizer for the in-app code editor. No DOM, no innerHTML:
// the caller turns tokens into nodes with textContent, so a file that contains
// markup can never become markup.
//
// Covers exactly what SKILL.md files are: a YAML frontmatter block followed by
// Markdown prose. `yaml` and `markdown` are also usable on their own.
//
// A `generic` mode was added for fenced code blocks in chat transcripts: one
// C-family lexer (strings, comments, numbers, keywords, call names) rather than
// a per-language grammar. It is deliberately approximate — the alternative was
// a second highlighter living inside the chat tab, which is exactly the
// duplication this module exists to prevent.

const FENCE = /^\s*(?:`{3,}|~{3,})/;
const HEADING = /^(#{1,6}\s)(.*)$/;
const RULE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const QUOTE = /^(\s*>\s?)(.*)$/;
const LIST = /^(\s*(?:[-*+]|\d+[.)])\s+)(.*)$/;
const YAML_KEY = /^(\s*-?\s*)([A-Za-z0-9_.-]+)(\s*:)(.*)$/;
const YAML_ITEM = /^(\s*-\s+)(.*)$/;
const YAML_COMMENT = /^(\s*)(#.*)$/;

// Inline Markdown, scanned left to right. Order matters: code spans win over
// emphasis so `**not bold**` inside backticks stays literal.
const INLINE = [
  ['code', /`[^`\n]+`/],
  ['link', /\[[^\]\n]*\]\([^)\s]*\)/],
  ['bold', /\*\*[^*\n]+\*\*|__[^_\n]+__/],
  ['italic', /\*[^*\n]+\*|_[^_\n]+_/],
  ['url', /https?:\/\/[^\s<>)]+/],
];

function token(text, kind = 'text') {
  return { text, kind };
}

/** Split one line of Markdown prose into inline tokens. */
export function tokenizeInline(line) {
  const source = String(line ?? '');
  if (!source) return [];
  const out = [];
  let index = 0;

  while (index < source.length) {
    let best = null;
    for (const [kind, pattern] of INLINE) {
      const re = new RegExp(pattern.source, 'g');
      re.lastIndex = index;
      const match = re.exec(source);
      if (match && (best === null || match.index < best.index)) {
        best = { index: match.index, text: match[0], kind };
      }
    }
    if (!best) break;
    if (best.index > index) out.push(token(source.slice(index, best.index)));
    out.push(token(best.text, best.kind));
    index = best.index + best.text.length;
  }

  if (index < source.length) out.push(token(source.slice(index)));
  return out.length ? out : [token(source)];
}

function tokenizeYamlLine(line) {
  const comment = line.match(YAML_COMMENT);
  if (comment) {
    return [comment[1] ? token(comment[1]) : null, token(comment[2], 'comment')].filter(Boolean);
  }

  const key = line.match(YAML_KEY);
  if (key) {
    const [, lead, name, colon, rest] = key;
    const out = [];
    if (lead) out.push(token(lead, 'punct'));
    out.push(token(name, 'key'));
    out.push(token(colon, 'punct'));
    if (rest) out.push(token(rest, valueKind(rest)));
    return out;
  }

  const item = line.match(YAML_ITEM);
  if (item) return [token(item[1], 'punct'), token(item[2], valueKind(item[2]))];

  return [token(line, line.trim() ? 'string' : 'text')];
}

function valueKind(raw) {
  const value = String(raw).trim();
  if (!value) return 'text';
  if (/^(true|false|null|~)$/i.test(value)) return 'const';
  if (/^-?\d+(\.\d+)?$/.test(value)) return 'number';
  return 'string';
}

function tokenizeMarkdownLine(line) {
  if (RULE.test(line) && line.trim()) return [token(line, 'punct')];

  const heading = line.match(HEADING);
  if (heading) return [token(heading[1], 'punct'), token(heading[2], 'heading')];

  const quote = line.match(QUOTE);
  if (quote) return [token(quote[1], 'punct'), ...tokenizeInline(quote[2]).map((t) => (t.kind === 'text' ? token(t.text, 'quote') : t))];

  const list = line.match(LIST);
  if (list) return [token(list[1], 'punct'), ...tokenizeInline(list[2])];

  return tokenizeInline(line);
}

/**
 * highlight(text, language) → array of lines, each an array of {text, kind}.
 * Concatenating every token of a line always reproduces that line exactly,
 * which is what keeps the highlight layer aligned with the textarea above it.
 */
export function highlight(text, language = 'markdown') {
  const lines = String(text ?? '').replace(/\r\n/g, '\n').split('\n');
  const lang = String(language || 'markdown').toLowerCase();

  if (lang === 'none' || lang === 'text') return lines.map((line) => [token(line)]);
  if (lang === 'yaml' || lang === 'yml') return lines.map((line) => tokenizeYamlLine(line));
  if (lang !== 'markdown' && lang !== 'md') return tokenizeGeneric(lines, lang);

  const out = [];
  // A leading `---` opens frontmatter; the same marker closes it.
  let inFrontmatter = lines[0] !== undefined && lines[0].trim() === '---';
  let inFence = false;

  lines.forEach((line, index) => {
    if (inFrontmatter && (index === 0 || line.trim() === '---')) {
      out.push([token(line, 'meta')]);
      if (index > 0) inFrontmatter = false;
      return;
    }
    if (inFrontmatter) {
      out.push(tokenizeYamlLine(line));
      return;
    }
    if (FENCE.test(line)) {
      inFence = !inFence;
      out.push([token(line, 'meta')]);
      return;
    }
    if (inFence) {
      out.push([token(line, 'code')]);
      return;
    }
    out.push(tokenizeMarkdownLine(line));
  });

  return out;
}

/* ------------------------------------------------------ generic code mode -- */

// One keyword set across the languages that actually show up in agent
// transcripts. A word being a keyword in a language it was not written in
// colours a token slightly wrong; it never corrupts the text, because every
// token still concatenates back to the original line.
const GENERIC_KEYWORDS = new Set([
  'as', 'async', 'await', 'break', 'case', 'catch', 'class', 'const', 'continue',
  'def', 'default', 'del', 'elif', 'else', 'except', 'export', 'extends',
  'finally', 'fn', 'for', 'from', 'func', 'function', 'global', 'if', 'impl',
  'import', 'in', 'interface', 'is', 'lambda', 'let', 'match', 'mut', 'new',
  'nonlocal', 'not', 'or', 'and', 'package', 'pass', 'private', 'public',
  'raise', 'return', 'select', 'static', 'struct', 'switch', 'throw', 'trait',
  'try', 'type', 'use', 'var', 'while', 'with', 'yield',
]);
const GENERIC_CONSTANTS = new Set([
  'true', 'false', 'null', 'nil', 'None', 'True', 'False', 'undefined', 'NaN',
  'self', 'this', 'super',
]);

// Ordered: a string or comment opener wins over anything inside it, which is
// what stops a `#` inside a quoted path from swallowing the rest of the line.
const GENERIC_RULES = [
  ['comment', /\/\/[^\n]*|#[^\n]*|--[^\n]*/],
  ['string', /"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`/],
  ['number', /\b0[xX][0-9a-fA-F]+\b|\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/],
  ['word', /[A-Za-z_$][A-Za-z0-9_$]*/],
  ['punct', /[{}()[\];,.:=+\-*/%<>!&|^~?@]+/],
];

// Block comments span lines, so the state has to survive between them.
const BLOCK_COMMENT_OPEN = /\/\*/;
const BLOCK_COMMENT_CLOSE = /\*\//;
const TRIPLE_QUOTE = /"""|'''/;

function classifyWord(word, rest) {
  if (GENERIC_KEYWORDS.has(word)) return 'keyword';
  if (GENERIC_CONSTANTS.has(word)) return 'const';
  // A word immediately followed by `(` reads as a call; that is the single
  // most useful distinction in a wall of code.
  if (/^\s*\(/.test(rest)) return 'key';
  return 'text';
}

function tokenizeGenericLine(line) {
  const source = String(line ?? '');
  if (!source) return [];
  const out = [];
  let index = 0;

  while (index < source.length) {
    let best = null;
    for (const [kind, pattern] of GENERIC_RULES) {
      const re = new RegExp(pattern.source, 'g');
      re.lastIndex = index;
      const match = re.exec(source);
      if (match && (best === null || match.index < best.index)) {
        best = { index: match.index, text: match[0], kind };
      }
      if (best && best.index === index) break;
    }
    if (!best) break;
    if (best.index > index) out.push(token(source.slice(index, best.index)));
    const kind = best.kind === 'word'
      ? classifyWord(best.text, source.slice(best.index + best.text.length))
      : best.kind;
    out.push(token(best.text, kind));
    index = best.index + best.text.length;
  }

  if (index < source.length) out.push(token(source.slice(index)));
  return out.length ? out : [token(source)];
}

function tokenizeGeneric(lines, _lang) {
  const out = [];
  let inBlockComment = false;
  let inTripleQuote = false;

  for (const line of lines) {
    if (inBlockComment) {
      out.push([token(line, 'comment')]);
      if (BLOCK_COMMENT_CLOSE.test(line)) inBlockComment = false;
      continue;
    }
    if (inTripleQuote) {
      out.push([token(line, 'string')]);
      if (TRIPLE_QUOTE.test(line)) inTripleQuote = false;
      continue;
    }
    if (BLOCK_COMMENT_OPEN.test(line) && !BLOCK_COMMENT_CLOSE.test(line)) {
      inBlockComment = true;
      out.push([token(line, 'comment')]);
      continue;
    }
    // An odd number of triple quotes opens a docstring that runs on.
    const triples = (line.match(/"""|'''/g) || []).length;
    if (triples % 2 === 1) {
      inTripleQuote = true;
      out.push([token(line, 'string')]);
      continue;
    }
    out.push(tokenizeGenericLine(line));
  }
  return out;
}

/** Language guess from a file path; falls back to Markdown. */
export function languageForPath(path) {
  const name = String(path || '').toLowerCase();
  if (name.endsWith('.yaml') || name.endsWith('.yml')) return 'yaml';
  if (name.endsWith('.md') || name.endsWith('.markdown')) return 'markdown';
  return 'markdown';
}
