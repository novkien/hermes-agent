// Path guard for Files/Logs tabs — pure logic, no DOM.
// Bounded listing of API-exposed roots only; traversal attempts rejected
// client-side too (never send `..` or absolute paths) — AC-28, feature matrix §10.

// API-exposed managed-file roots (u02 evidence: /api/files list_managed_files).
// These are logical namespaces, not arbitrary filesystem paths.
export const FILE_ROOTS = Object.freeze(['/api/files', '/api/fs/list']);

const SEGMENT_BLOCK = /(^|[\/\\])\.\.([\/\\]|$)/;

export function sanitizeRelativePath(input) {
  if (input == null) return null;
  const s = String(input).replace(/\\/g, '/');
  if (s.length === 0 || s === '.' || s === '/' || s === './') return null;
  if (s.includes('\0')) return null;
  if (s.startsWith('/')) return null; // absolute paths rejected client-side
  if (SEGMENT_BLOCK.test(s)) return null; // traversal rejected outright
  const parts = s.split('/').filter((p) => p !== '' && p !== '.');
  if (parts.some((p) => p === '..')) return null;
  return parts.join('/');
}

/**
 * isSafeManagedPath(root, relativePath) — a path is safe only when:
 *  - it is a relative path (no leading /, no drive/absolute forms)
 *  - it contains no .. traversal segments (after normalization)
 *  - it is non-empty and contains no null bytes
 */
export function isSafeManagedPath(root, relativePath) {
  if (!root || !root.startsWith('/api/')) return false;
  const clean = sanitizeRelativePath(relativePath);
  if (clean === null) return false;
  // absolute-path forms rejected by sanitize (leading / stripped, then empty -> null)
  // here: any input that still contains an absolute or traversal form after sanitize is unsafe
  const raw = String(relativePath == null ? '' : relativePath).replace(/\\/g, '/');
  if (raw.startsWith('/')) return false; // absolute paths rejected client-side
  if (SEGMENT_BLOCK.test(raw)) return false;
  if (raw.split('/').includes('..')) return false;
  return true;
}
