#!/usr/bin/env node
//
// Contract checks for the Skills surface's pure layer: inventory shaping, the
// rack's filter/sort view, frontmatter parsing, the write-capability gate, and
// the editor's syntax tokenizer.
//
// The tokenizer assertion that matters most is the round-trip one: the
// highlight layer sits under a textarea, so any line whose tokens do not
// re-join into the original text would push the caret off its glyph.

import assert from 'node:assert/strict';
import {
  actionsForStatus,
  filterSkills,
  normalizeSkill,
  normalizeSkills,
  parseFrontmatter,
  skillCategories,
  skillStats,
  skillStatus,
  supportedActions,
} from '../frontend/dist/pure/skill-shape.js';
import { highlight, languageForPath, tokenizeInline } from '../frontend/dist/pure/code-highlight.js';

/* ------------------------------------------------------------- inventory -- */

// The live upstream shape (GET /api/skills), verified 2026-08-09.
const upstream = [
  { name: 'jarvis-report', description: 'delivery protocol', category: 'automation', enabled: true, usage: 255, provenance: 'agent' },
  { name: 'smspool-otp', description: 'otp rental', category: 'automation', enabled: false, usage: 31, provenance: 'bundled' },
  { name: 'orphan', description: '', enabled: true, usage: 0, provenance: 'hub' },
  { name: 'shelved', description: 'old', category: 'devops', archived: true, usage: 9, provenance: 'agent' },
];

const rows = normalizeSkills(upstream);
assert.equal(rows.length, 4);
assert.deepEqual(rows.map((row) => row.status), ['enabled', 'disabled', 'enabled', 'archived']);

// Envelope variants all resolve to the same list.
assert.equal(normalizeSkills({ skills: upstream }).length, 4);
assert.equal(normalizeSkills({ items: upstream }).length, 4);
assert.deepEqual(normalizeSkills(null), []);
assert.deepEqual(normalizeSkills({ nothing: true }), []);

// Status is derived, never guessed: a record that says nothing stays unknown.
assert.equal(skillStatus({ name: 'x' }), 'unknown');
assert.equal(skillStatus({ enabled: false }), 'disabled');
assert.equal(skillStatus({ status: 'archived' }), 'archived');
assert.equal(skillStatus({ enabled: true, archived: true }), 'archived');

// A bare string row still yields a usable card rather than being dropped.
assert.equal(normalizeSkill('lonely').name, 'lonely');
assert.equal(normalizeSkill({ name: 'n', usage: 'not-a-number' }).usage, 0);

const stats = skillStats(rows);
assert.equal(stats.total, 4);
assert.equal(stats.enabled, 2);
assert.equal(stats.disabled, 1);
assert.equal(stats.archived, 1);
assert.equal(stats.usage, 295);
assert.equal(stats.categories, 2); // automation + devops; uncategorized is not a category
assert.deepEqual(stats.provenance, { agent: 2, bundled: 1, hub: 1 });

assert.deepEqual(skillCategories(rows).map((entry) => `${entry.name}:${entry.count}`),
  ['(uncategorized):1', 'automation:2', 'devops:1']);

/* ----------------------------------------------------------------- view -- */

assert.deepEqual(filterSkills(rows, {}).map((row) => row.name),
  ['jarvis-report', 'smspool-otp', 'shelved', 'orphan']); // usage desc
assert.deepEqual(filterSkills(rows, { sort: 'name' }).map((row) => row.name),
  ['jarvis-report', 'orphan', 'shelved', 'smspool-otp']);
assert.deepEqual(filterSkills(rows, { status: 'enabled' }).map((row) => row.name),
  ['jarvis-report', 'orphan']);
assert.deepEqual(filterSkills(rows, { category: 'automation' }).map((row) => row.name),
  ['jarvis-report', 'smspool-otp']);
assert.deepEqual(filterSkills(rows, { category: '(uncategorized)' }).map((row) => row.name), ['orphan']);
// Search spans name, description, category and provenance.
assert.deepEqual(filterSkills(rows, { query: 'OTP' }).map((row) => row.name), ['smspool-otp']);
assert.deepEqual(filterSkills(rows, { query: 'hub' }).map((row) => row.name), ['orphan']);
assert.deepEqual(filterSkills(rows, { query: 'nothing-matches' }), []);
// `all` hides nothing, archived included.
assert.equal(filterSkills(rows, { status: 'all' }).length, 4);

/* ---------------------------------------------------------- capabilities -- */

// The default: upstream advertises no writes, so every action is gated off.
const gated = supportedActions({ mutations_supported: [] });
assert.deepEqual(gated, { enable: false, disable: false, archive: false, delete: false, save: false });
assert.deepEqual(supportedActions(null).save, false);

// Once the BFF advertises a write, only that action opens.
const partial = supportedActions({ mutations_supported: ['skill_save', 'enable'] });
assert.equal(partial.save, true);
assert.equal(partial.enable, true);
assert.equal(partial.delete, false);

// Status decides which controls are even offered.
assert.deepEqual(actionsForStatus('enabled'), { enable: false, disable: true, archive: true, delete: true });
assert.deepEqual(actionsForStatus('archived'), { enable: true, disable: false, archive: false, delete: true });

/* ----------------------------------------------------------- frontmatter -- */

const doc = [
  '---',
  'name: jarvis-report',
  'description: "Reference for schedule developers"',
  'version: 3.0.0',
  'metadata:',
  '  hermes:',
  '      tags: [automation, report]',
  '---',
  '',
  '# Jarvis Report',
  '',
  'Body text.',
].join('\n');

const parsed = parseFrontmatter(doc);
assert.equal(parsed.hasFrontmatter, true);
assert.equal(parsed.fields.name, 'jarvis-report');
assert.equal(parsed.fields.description, 'Reference for schedule developers'); // quotes stripped
assert.equal(parsed.fields.version, '3.0.0');
assert.equal(parsed.fields.metadata, undefined); // block openers carry no scalar
assert.equal(parsed.body.startsWith('# Jarvis Report'), true);

// No header, or an unterminated one, leaves the buffer untouched.
assert.deepEqual(parseFrontmatter('# plain\n\ntext').hasFrontmatter, false);
assert.equal(parseFrontmatter('# plain\n\ntext').body, '# plain\n\ntext');
assert.equal(parseFrontmatter('---\nname: x\nno terminator').hasFrontmatter, false);
assert.equal(parseFrontmatter('').body, '');

/* ------------------------------------------------------------- tokenizer -- */

// Round-trip: every line's tokens must re-join into that exact line, or the
// highlight layer drifts out from under the caret.
function assertRoundTrip(text, language) {
  const lines = text.replace(/\r\n/g, '\n').split('\n');
  const tokenized = highlight(text, language);
  assert.equal(tokenized.length, lines.length, `line count for ${language}`);
  tokenized.forEach((tokens, index) => {
    assert.equal(tokens.map((token) => token.text).join(''), lines[index],
      `round-trip failed on line ${index + 1}: ${JSON.stringify(lines[index])}`);
  });
}

assertRoundTrip(doc, 'markdown');
assertRoundTrip(doc, 'yaml');
assertRoundTrip(doc, 'none');
assertRoundTrip([
  '# Heading',
  '',
  '- a list with `code` and **bold** and [a link](https://example.com)',
  '> quoted *text*',
  '```bash',
  'echo "# not a heading"',
  '```',
  '---',
  'trailing',
  '',
].join('\n'), 'markdown');
assertRoundTrip('single line, no newline', 'markdown');
assertRoundTrip('', 'markdown');

// Frontmatter is tokenized as YAML, the body as Markdown.
const tokens = highlight(doc, 'markdown');
assert.equal(tokens[0][0].kind, 'meta'); // opening ---
assert.equal(tokens[1][0].kind, 'key'); // name
assert.equal(tokens[7][0].kind, 'meta'); // closing ---
assert.equal(tokens[9].some((token) => token.kind === 'heading'), true);

// Fenced code is not re-tokenized as Markdown.
const fenced = highlight('```\n# still code\n```', 'markdown');
assert.equal(fenced[1][0].kind, 'code');

// Inline: code spans win over emphasis inside them.
assert.deepEqual(tokenizeInline('a `**b**` c').map((token) => token.kind), ['text', 'code', 'text']);
assert.equal(tokenizeInline('plain').length, 1);
assert.deepEqual(tokenizeInline(''), []);

/* --------------------------------------------------- generic code mode -- */

// Fenced code blocks in chat transcripts go through the same tokenizer rather
// than a second highlighter living in the chat tab. The round-trip invariant
// matters just as much here: a token stream that does not re-join to the exact
// source would silently corrupt code the operator is about to copy.
const codeSample = [
  'def build(name, /* inline */ count=3):',
  '    """docstring',
  '    keeps going"""',
  '    # comment with "quotes" and a # inside',
  "    items = ['a', 'b']  // trailing",
  '    return {"n": count * 0x1F, "ok": True}',
].join('\n');

assertRoundTrip(codeSample, 'python');
assertRoundTrip(codeSample, 'javascript');
assertRoundTrip('', 'python');
assertRoundTrip('plain line with no syntax at all', 'rust');

const code = highlight(codeSample, 'python');
assert.equal(code[0][0].kind, 'keyword');          // def
assert.equal(code[0].some((t) => t.kind === 'key'), true);   // build(
// A docstring opened on one line stays a string until it closes.
assert.equal(code[1][0].kind, 'string');
assert.equal(code[2][0].kind, 'string');
// A `#` inside a string must not swallow the rest of the line as a comment.
assert.equal(code[3].some((t) => t.kind === 'comment'), true);
assert.equal(code[4].some((t) => t.kind === 'string'), true);
assert.equal(code[5].some((t) => t.kind === 'number'), true);
assert.equal(code[5].some((t) => t.kind === 'const'), true); // True

// Markdown and YAML keep their own modes — generic must not capture them.
assert.equal(highlight('# heading', 'markdown')[0][1].kind, 'heading');
assert.equal(highlight('key: value', 'yaml')[0][0].kind, 'key');

assert.equal(languageForPath('/skills/x/SKILL.md'), 'markdown');
assert.equal(languageForPath('/a/b.yaml'), 'yaml');
assert.equal(languageForPath(null), 'markdown');

console.log('SKILLS_SURFACE_TESTS=PASS');
