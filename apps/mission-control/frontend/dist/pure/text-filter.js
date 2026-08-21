// Client-side text filtering, shared by every list tab.
//
// Sixteen of this dashboard's tabs render a list and none of them could be
// searched: the only way to find one row among 5,300 sessions, 1,900 kanban
// cards or 173 skills was to scroll. Each tab that needed it was also about to
// grow its own slightly different matcher, so the matching rule lives here once
// and is tested once.
//
// The rule is deliberately plain, because a filter box that behaves cleverly is
// a filter box you cannot predict:
//
//   * whitespace splits the query into terms, and EVERY term must match
//     somewhere in the row (`aqua burst` finds "Aquarium Burst" even though no
//     single field contains that string with that spacing);
//   * matching is case-insensitive substring, not fuzzy — a typo should return
//     nothing rather than something surprising;
//   * a quoted "phrase with spaces" is one term.

/** Split a raw query into the terms every row must satisfy. */
export function queryTerms(query) {
  const raw = String(query == null ? '' : query).trim().toLowerCase();
  if (!raw) return [];
  const terms = [];
  // Quoted runs first, so an intentional phrase survives the whitespace split.
  const pattern = /"([^"]*)"|(\S+)/g;
  let match = pattern.exec(raw);
  while (match) {
    const term = (match[1] !== undefined ? match[1] : match[2]).trim();
    if (term) terms.push(term);
    match = pattern.exec(raw);
  }
  return terms;
}

/**
 * The searchable text of one row.
 *
 * `fields` may be key names or accessor functions; anything that resolves to
 * null, an object or an empty string contributes nothing. Numbers are included
 * because ids, ports and counts are exactly what people paste into a filter.
 */
export function rowText(row, fields) {
  if (row === null || row === undefined) return '';
  const parts = [];
  for (const field of fields || []) {
    const value = typeof field === 'function' ? field(row) : row[field];
    if (value === null || value === undefined) continue;
    if (typeof value === 'string') { if (value) parts.push(value); continue; }
    if (typeof value === 'number' || typeof value === 'boolean') { parts.push(String(value)); continue; }
    if (Array.isArray(value)) {
      for (const item of value) {
        if (typeof item === 'string' || typeof item === 'number') parts.push(String(item));
      }
    }
  }
  return parts.join(' ').toLowerCase();
}

/** True when every term of `query` appears somewhere in the row's fields. */
export function matchesQuery(row, query, fields) {
  const terms = queryTerms(query);
  if (!terms.length) return true;
  const haystack = rowText(row, fields);
  if (!haystack) return false;
  return terms.every((term) => haystack.includes(term));
}

/**
 * Filter a list. An empty query returns the original array (not a copy), so a
 * tab that filters on every keystroke does not churn the heap while idle.
 */
export function filterRows(rows, query, fields) {
  const list = Array.isArray(rows) ? rows : [];
  const terms = queryTerms(query);
  if (!terms.length) return list;
  return list.filter((row) => {
    const haystack = rowText(row, fields);
    return haystack ? terms.every((term) => haystack.includes(term)) : false;
  });
}

/**
 * "12 of 387" — the caption a filtered list needs so a short list never reads
 * as a small dataset.
 */
export function filterSummary(shown, total, noun = 'row') {
  const plural = total === 1 ? noun : `${noun}s`;
  if (shown === total) return `${total} ${plural}`;
  return `${shown} of ${total} ${plural}`;
}
