// Bounded route-root cache. Route instances mount into their own root once;
// navigation only changes visibility, so a background refresh cannot blank the
// shared workspace or destroy focus/selection inside an unchanged route.

export function createRetainedRoutes({
  container,
  limit = 10,
  createRoot,
  activeElement = () => globalThis.document?.activeElement || null,
  defer = (fn) => queueMicrotask(fn),
  onEvict = () => {},
} = {}) {
  if (!container || typeof container.append !== 'function') throw new Error('retained routes require a container');
  if (typeof createRoot !== 'function') throw new Error('retained routes require createRoot');

  const entries = new Map();
  let activeId = null;
  let clock = 0;

  function setInactive(entry) {
    const focused = activeElement();
    if (focused && entry.root.contains?.(focused)) entry.lastFocus = focused;
    entry.root.hidden = true;
    entry.root.inert = true;
    entry.root.setAttribute?.('aria-hidden', 'true');
  }

  function setActive(entry) {
    entry.root.hidden = false;
    entry.root.inert = false;
    entry.root.removeAttribute?.('aria-hidden');
    if (entry.lastFocus?.isConnected && typeof entry.lastFocus.focus === 'function') {
      defer(() => entry.lastFocus?.focus?.({ preventScroll: true }));
    }
  }

  function evictOverflow() {
    const evicted = [];
    while (entries.size > Math.max(1, Number(limit) || 10)) {
      const candidate = [...entries.values()]
        .filter((entry) => entry.id !== activeId)
        .sort((a, b) => a.usedAt - b.usedAt)[0];
      if (!candidate) break;
      entries.delete(candidate.id);
      candidate.root.remove?.();
      onEvict(candidate.id, candidate.root);
      evicted.push(candidate.id);
    }
    return evicted;
  }

  function ensure(id) {
    let entry = entries.get(id);
    if (entry) return entry.root;
    const root = createRoot(id);
    root.hidden = true;
    root.inert = true;
    root.setAttribute?.('aria-hidden', 'true');
    entry = { id, root, usedAt: ++clock, lastFocus: null };
    entries.set(id, entry);
    container.append(root);
    return root;
  }

  function activate(id) {
    const root = ensure(id);
    for (const entry of entries.values()) {
      if (entry.id === id) continue;
      setInactive(entry);
    }
    const entry = entries.get(id);
    entry.usedAt = ++clock;
    activeId = id;
    setActive(entry);
    return { root, evicted: evictOverflow() };
  }

  function remove(id) {
    const entry = entries.get(id);
    if (!entry) return false;
    entries.delete(id);
    entry.root.remove?.();
    if (activeId === id) activeId = null;
    onEvict(id, entry.root);
    return true;
  }

  return {
    ensure,
    activate,
    remove,
    get: (id) => entries.get(id)?.root || null,
    has: (id) => entries.has(id),
    ids: () => [...entries.keys()],
    get activeId() { return activeId; },
  };
}
