// Profile context.
// Canonical source of truth: document query `?profile=<id>` only.
// Legacy `#/route?profile=<id>` links remain readable and are normalized on
// boot/profile switch so a refresh never duplicates the parameter.

import { buildHash, parseHash } from './pure/hash-router.js';

const PROFILE_KEY = 'profile';

export function readProfileFromLocation(location = window.location) {
  const url = new URL(location.href);
  const direct = url.searchParams.get(PROFILE_KEY);
  if (direct) return direct;
  const legacy = parseHash(url.hash).params[PROFILE_KEY];
  return legacy || 'default';
}

export function setProfileInLocation(profile, location = window.location) {
  const url = new URL(location.href);
  url.searchParams.set(PROFILE_KEY, profile || 'default');
  const route = parseHash(url.hash);
  delete route.params[PROFILE_KEY];
  const hash = buildHash(route.path, route.params);
  history.replaceState(null, '', `${url.pathname}${url.search}${hash}`);
}

export function profileCachePrefix(profile) {
  return `profile:${encodeURIComponent(profile || 'default')}:`;
}

export function scopedUrl(path, profile) {
  const url = new URL(path, window.location.origin);
  if (profile) url.searchParams.set(PROFILE_KEY, profile);
  return url.toString();
}
