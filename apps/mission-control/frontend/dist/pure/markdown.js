// Minimal Markdown → token tree. Pure: no DOM, no innerHTML anywhere in the
// pipeline, so the renderer can build nodes with textContent and nothing an
// agent writes can become markup.
//
// Covers what agent replies actually use: fenced code, pipe tables, headings,
// lists, blockquotes, rules, and inline code/bold/italic/strike/links.

const FENCE = /^\s*(`{3,}|~{3,})\s*([^\s`]*)\s*$/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const RULE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const QUOTE = /^\s*>\s?(.*)$/;
const BULLET = /^\s*[-*+]\s+(.*)$/;
const ORDERED = /^\s*\d+[.)]\s+(.*)$/;
const TABLE_DIVIDER = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;

// Only schemes that are safe to hand to an anchor. Anything else stays text.
const SAFE_HREF = /^(?:https?:\/\/|mailto:)/i;

export function parseMarkdown(input) {
  const lines = String(input ?? '').replace(/\r\n/g, '\n').split('\n');
  const blocks = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    if (!line.trim()) { index += 1; continue; }

    const fence = line.match(FENCE);
    if (fence) {
      const marker = fence[1];
      const body = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith(marker)) {
        body.push(lines[index]);
        index += 1;
      }
      index += 1; // closing fence (or end of input)
      blocks.push({ type: 'code', lang: fence[2] || '', text: body.join('\n') });
      continue;
    }

    const table = readTable(lines, index);
    if (table) { blocks.push(table.block); index = table.next; continue; }

    const heading = line.match(HEADING);
    if (heading) {
      blocks.push({ type: 'heading', level: heading[1].length, spans: parseInline(heading[2]) });
      index += 1;
      continue;
    }

    if (RULE.test(line)) { blocks.push({ type: 'rule' }); index += 1; continue; }

    if (QUOTE.test(line)) {
      const body = [];
      while (index < lines.length && QUOTE.test(lines[index])) {
        body.push(lines[index].match(QUOTE)[1]);
        index += 1;
      }
      blocks.push({ type: 'quote', spans: parseInline(body.join(' ').trim()) });
      continue;
    }

    if (BULLET.test(line) || ORDERED.test(line)) {
      const ordered = !BULLET.test(line) && ORDERED.test(line);
      const pattern = ordered ? ORDERED : BULLET;
      const items = [];
      while (index < lines.length && pattern.test(lines[index])) {
        items.push(parseInline(lines[index].match(pattern)[1]));
        index += 1;
      }
      blocks.push({ type: 'list', ordered, items });
      continue;
    }

    const paragraph = [];
    while (index < lines.length && lines[index].trim()
      && !FENCE.test(lines[index]) && !HEADING.test(lines[index])
      && !RULE.test(lines[index]) && !QUOTE.test(lines[index])
      && !BULLET.test(lines[index]) && !ORDERED.test(lines[index])
      && !readTable(lines, index)) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    // A line that only a table could claim still has to advance the cursor.
    if (!paragraph.length) { index += 1; continue; }
    blocks.push({ type: 'para', spans: parseInline(paragraph.join('\n')) });
  }

  return blocks;
}

// A table is a header row plus a `|---|---|` divider. Without the divider the
// pipes are just punctuation, so the block stays a paragraph.
function readTable(lines, start) {
  const header = lines[start];
  const divider = lines[start + 1];
  if (!header || !divider) return null;
  if (!header.includes('|') || !TABLE_DIVIDER.test(divider)) return null;
  if (!divider.includes('-')) return null;

  const head = splitRow(header);
  const align = splitRow(divider).map((cell) => {
    const left = cell.startsWith(':');
    const right = cell.endsWith(':');
    if (left && right) return 'center';
    if (right) return 'right';
    return left ? 'left' : '';
  });
  if (!head.length) return null;

  const rows = [];
  let index = start + 2;
  while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
    rows.push(splitRow(lines[index]));
    index += 1;
  }

  return {
    block: {
      type: 'table',
      head: head.map(parseInline),
      align,
      rows: rows.map((row) => row.map(parseInline)),
    },
    next: index,
  };
}

function splitRow(line) {
  let text = line.trim();
  if (text.startsWith('|')) text = text.slice(1);
  if (text.endsWith('|')) text = text.slice(0, -1);
  // Split on pipes that are not escaped.
  return text.split(/(?<!\\)\|/).map((cell) => cell.replace(/\\\|/g, '|').trim());
}

const INLINE = new RegExp([
  '(`+)([\\s\\S]*?)\\1',          // code — first, so markers inside it stay literal
  '\\[([^\\]]*)\\]\\(([^)\\s]+)\\)', // link
  '\\*\\*([\\s\\S]+?)\\*\\*',      // strong
  '__([\\s\\S]+?)__',
  '~~([\\s\\S]+?)~~',              // strike
  '\\*([^*\\n]+?)\\*',             // em
  '_([^_\\n]+?)_',
].join('|'), 'g');

export function parseInline(input) {
  const text = String(input ?? '');
  const spans = [];
  let cursor = 0;

  INLINE.lastIndex = 0;
  let match = INLINE.exec(text);
  while (match) {
    if (match.index > cursor) spans.push({ type: 'text', text: text.slice(cursor, match.index) });

    if (match[2] !== undefined) spans.push({ type: 'code', text: match[2].trim() });
    else if (match[4] !== undefined) {
      const href = match[4];
      if (SAFE_HREF.test(href)) spans.push({ type: 'link', text: match[3] || href, href });
      else spans.push({ type: 'text', text: match[0] });
    } else if (match[5] !== undefined) spans.push({ type: 'strong', text: match[5] });
    else if (match[6] !== undefined) spans.push({ type: 'strong', text: match[6] });
    else if (match[7] !== undefined) spans.push({ type: 'strike', text: match[7] });
    else if (match[8] !== undefined) spans.push({ type: 'em', text: match[8] });
    else if (match[9] !== undefined) spans.push({ type: 'em', text: match[9] });

    cursor = match.index + match[0].length;
    match = INLINE.exec(text);
  }

  if (cursor < text.length) spans.push({ type: 'text', text: text.slice(cursor) });
  return spans.length ? spans : [{ type: 'text', text }];
}

// Whether rendering is worth it at all — plain prose keeps the cheaper path and
// avoids paragraph-splitting text that was never markdown.
export function looksLikeMarkdown(text) {
  const value = String(text ?? '');
  if (!value) return false;
  return /(^|\n)\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s|```|~~~)/.test(value)
    || /\|[^\n]*\|/.test(value)
    || /(\*\*|__|~~|`)/.test(value)
    || /\[[^\]]*\]\((?:https?:\/\/|mailto:)[^)\s]+\)/.test(value);
}
