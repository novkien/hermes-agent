// Pure shaping for the Skills surface: normalize the upstream inventory,
// derive the filter/sort view, read frontmatter out of a SKILL.md buffer, and
// decide which card actions the current source actually supports.
//
// No DOM and no network — everything here is a function of its arguments so
// the tab can be reasoned about (and tested) without a browser.

export const SKILL_ACTIONS = Object.freeze(['enable', 'disable', 'archive', 'delete', 'save']);

export const SKILL_STATUSES = Object.freeze(['enabled', 'disabled', 'archived']);

function firstOf(source, keys, fallback = null) {
  if (!source || typeof source !== 'object') return fallback;
  for (const key of keys) {
    const value = source[key];
    if (value !== undefined && value !== null && value !== '') return value;
  }
  return fallback;
}

function toBool(value) {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') return ['1', 'true', 'yes', 'on', 'enabled'].includes(value.toLowerCase());
  return null;
}

/**
 * Status is derived, never invented: an explicit `archived` flag wins, then an
 * explicit status string, then the `enabled` boolean. A source that says
 * nothing stays `unknown` rather than being guessed into `enabled`.
 */
export function skillStatus(raw) {
  if (!raw || typeof raw !== 'object') return 'unknown';
  if (toBool(raw.archived) === true) return 'archived';
  const declared = String(firstOf(raw, ['status', 'state'], '') || '').toLowerCase();
  if (SKILL_STATUSES.includes(declared)) return declared;
  const enabled = toBool(raw.enabled);
  if (enabled === true) return 'enabled';
  if (enabled === false) return 'disabled';
  return 'unknown';
}

/** Normalize one upstream skill record into the shape the tab renders. */
export function normalizeSkill(raw) {
  const source = raw && typeof raw === 'object' ? raw : { name: raw };
  const name = firstOf(source, ['name', 'id', 'slug'], null);
  const status = skillStatus(source);
  const usage = Number(firstOf(source, ['usage', 'usage_count', 'uses', 'invocations'], 0));

  return {
    id: name || firstOf(source, ['path'], null),
    name,
    description: firstOf(source, ['description', 'summary', 'about'], ''),
    category: firstOf(source, ['category', 'group', 'kind'], null),
    provenance: firstOf(source, ['provenance', 'origin', 'source'], null),
    version: firstOf(source, ['version'], null),
    path: firstOf(source, ['path', 'file', 'location'], null),
    usage: Number.isFinite(usage) ? usage : 0,
    status,
    enabled: status === 'enabled',
    raw: source,
  };
}

/** Pull the skill list out of whichever envelope shape the source returns. */
export function pickSkillList(raw) {
  if (Array.isArray(raw)) return raw;
  if (!raw || typeof raw !== 'object') return null;
  return raw.skills || raw.items || raw.list || raw.data || null;
}

export function normalizeSkills(raw) {
  const list = pickSkillList(raw);
  if (!Array.isArray(list)) return [];
  return list.map(normalizeSkill).filter((skill) => skill.name);
}

/** Counts for the library overview. Categories exclude the uncategorized bucket. */
export function skillStats(rows = []) {
  const stats = {
    total: rows.length,
    enabled: 0,
    disabled: 0,
    archived: 0,
    unknown: 0,
    usage: 0,
    categories: 0,
    provenance: {},
  };
  const categories = new Set();

  for (const skill of rows) {
    stats[skill.status] = (stats[skill.status] || 0) + 1;
    stats.usage += skill.usage || 0;
    if (skill.category) categories.add(skill.category);
    const origin = skill.provenance || 'unknown';
    stats.provenance[origin] = (stats.provenance[origin] || 0) + 1;
  }

  stats.categories = categories.size;
  return stats;
}

/** Sorted, de-duplicated category list with counts, for the filter menu. */
export function skillCategories(rows = []) {
  const counts = new Map();
  for (const skill of rows) {
    const key = skill.category || '(uncategorized)';
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return [...counts.entries()]
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

const SORTERS = {
  usage: (a, b) => (b.usage - a.usage) || a.name.localeCompare(b.name),
  name: (a, b) => a.name.localeCompare(b.name),
  category: (a, b) => String(a.category || '~').localeCompare(String(b.category || '~')) || a.name.localeCompare(b.name),
};

/**
 * filterSkills(rows, view) — the one place the rack's visible set is decided.
 * `status: 'all'` still hides nothing; archived skills are only hidden when a
 * narrower status is selected, so nothing silently disappears.
 */
export function filterSkills(rows = [], { query = '', category = 'all', status = 'all', sort = 'usage' } = {}) {
  const needle = String(query || '').trim().toLowerCase();
  const wantCategory = category && category !== 'all' ? category : null;
  const wantStatus = status && status !== 'all' ? status : null;

  const out = rows.filter((skill) => {
    if (wantStatus && skill.status !== wantStatus) return false;
    if (wantCategory) {
      const own = skill.category || '(uncategorized)';
      if (own !== wantCategory) return false;
    }
    if (!needle) return true;
    return `${skill.name} ${skill.description} ${skill.category || ''} ${skill.provenance || ''}`
      .toLowerCase()
      .includes(needle);
  });

  return out.sort(SORTERS[sort] || SORTERS.usage);
}

/**
 * Split a SKILL.md buffer into its YAML frontmatter block and body. The parse
 * is deliberately shallow — scalar keys only — because it feeds a read-only
 * metadata panel, not a config loader.
 */
export function parseFrontmatter(text) {
  const raw = String(text ?? '').replace(/\r\n/g, '\n');
  const lines = raw.split('\n');
  if (lines[0]?.trim() !== '---') return { fields: {}, body: raw, frontmatter: '', hasFrontmatter: false };

  const end = lines.findIndex((line, index) => index > 0 && line.trim() === '---');
  if (end === -1) return { fields: {}, body: raw, frontmatter: '', hasFrontmatter: false };

  const block = lines.slice(1, end);
  const fields = {};
  for (const line of block) {
    // Only top-level scalars; nested blocks stay visible in the raw editor.
    const match = line.match(/^([A-Za-z0-9_.-]+)\s*:\s*(.*)$/);
    if (!match) continue;
    const value = match[2].trim().replace(/^["']|["']$/g, '');
    if (value) fields[match[1]] = value;
  }

  return {
    fields,
    frontmatter: block.join('\n'),
    body: lines.slice(end + 1).join('\n').replace(/^\n+/, ''),
    hasFrontmatter: true,
  };
}

/**
 * Which actions this source will actually accept. The BFF advertises writes in
 * `meta.mutations_supported`; anything absent is reported as unsupported so the
 * card renders a disabled control with a reason instead of a button that fails.
 */
export function supportedActions(meta) {
  const declared = Array.isArray(meta?.mutations_supported) ? meta.mutations_supported : [];
  const set = new Set(declared.map((entry) => String(entry).toLowerCase()));
  const out = {};
  for (const action of SKILL_ACTIONS) {
    out[action] = set.has(action) || set.has(`skill_${action}`) || set.has(`skill.${action}`);
  }
  return out;
}

/** The actions that make sense for a skill in its current status. */
export function actionsForStatus(status) {
  return {
    enable: status !== 'enabled',
    disable: status === 'enabled' || status === 'unknown',
    archive: status !== 'archived',
    delete: true,
  };
}
