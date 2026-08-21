// Shaping for OpenRouter's own public model catalog, used ONLY to widen the
// composer's OpenRouter picker beyond Hermes' curated 34. It never touches
// how a turn actually runs — chat calls still go through the gateway with
// its own OpenRouter credentials exactly as before; this is picker-listing
// data only, fetched by the browser directly from OpenRouter's public,
// unauthenticated `GET /api/v1/models` (CORS-open, no key required).
//
// OpenRouter's public API carries no popularity/usage-rank field — verified
// live (2026-08-13): `/api/v1/models` returns id/name/created/pricing/
// supported_parameters, nothing usage-based, and their internal rankings API
// (`/api/frontend/...`) is CORS-blocked outside openrouter.ai itself. The
// honest proxy available is recency (`created`, descending) — OpenRouter's
// own default ordering already roughly follows it (newest releases first,
// legacy models like the original GPT-4/GPT-3.5 trail at the end) — so
// results are labelled "newest", never "popular".

/**
 * Hermes filters OpenRouter's catalog to tool-calling-capable models before
 * ever offering one (`hermes_cli/models.py::_openrouter_model_supports_tools`,
 * ported from Kilo-Org/kilocode#9068) — surfacing one without that support
 * would just be a guaranteed runtime failure the moment it is selected. This
 * mirrors that same filter so the widened list carries the identical bar.
 */
export function supportsTools(item) {
  const params = item?.supported_parameters;
  if (!Array.isArray(params)) return true; // absent/malformed — permissive, matches upstream
  return params.includes('tools');
}

/**
 * Shape OpenRouter's raw `/api/v1/models` payload into picker rows, newest
 * first, minus whatever Hermes' curated list already offers (no point
 * listing a model twice under two different sections).
 */
export function shapeOpenRouterCatalog(rawModels, { exclude = [], limit = 60 } = {}) {
  const excluded = new Set(exclude);
  const list = Array.isArray(rawModels) ? rawModels : [];
  return list
    .filter((item) => item && typeof item === 'object')
    .filter((item) => typeof item.id === 'string' && item.id && !excluded.has(item.id))
    .filter(supportsTools)
    .sort((a, b) => (Number(b.created) || 0) - (Number(a.created) || 0))
    .slice(0, Math.max(0, limit))
    .map((item) => ({ id: item.id, name: String(item.name || item.id) }));
}

/**
 * Live fetch, kept out of the pure shaping function above so it stays
 * testable in Node without a network. Errors are the caller's problem — this
 * enrichment is optional, the curated list already works without it.
 */
export async function fetchOpenRouterCatalog() {
  const res = await fetch('https://openrouter.ai/api/v1/models', { headers: { accept: 'application/json' } });
  if (!res.ok) throw new Error(`OpenRouter catalog fetch failed (${res.status})`);
  const body = await res.json();
  return Array.isArray(body?.data) ? body.data : [];
}
