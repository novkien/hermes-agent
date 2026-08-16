// Small keyed reconciler for native DOM list/card/rack surfaces. Callers own
// the shape of a node; this helper owns identity, ordering and bounded FLIP.

export function createKeyedReconciler({
  container,
  key,
  create,
  update = () => {},
  remove = (node) => node.remove(),
  reducedMotion = () => globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true,
  flipLimit = 200,
} = {}) {
  if (!container || typeof container.append !== 'function') throw new Error('keyed reconciler requires a container');
  if (typeof key !== 'function' || typeof create !== 'function') throw new Error('keyed reconciler requires key/create');
  const entries = new Map();

  function reconcile(items = []) {
    const list = Array.isArray(items) ? items : [];
    const before = new Map();
    if (list.length <= flipLimit && !reducedMotion()) {
      for (const [id, entry] of entries) {
        if (entry.node.isConnected && typeof entry.node.getBoundingClientRect === 'function') {
          before.set(id, entry.node.getBoundingClientRect());
        }
      }
    }

    const retained = new Set();
    list.forEach((item, index) => {
      const id = String(key(item, index));
      if (retained.has(id)) throw new Error(`duplicate keyed DOM id: ${id}`);
      retained.add(id);
      let entry = entries.get(id);
      if (!entry) {
        entry = { node: create(item, index, id), item: undefined };
        entries.set(id, entry);
      }
      update(entry.node, item, entry.item, index, id);
      entry.item = item;
      container.append(entry.node);
    });

    for (const [id, entry] of entries) {
      if (retained.has(id)) continue;
      remove(entry.node, entry.item, id);
      entries.delete(id);
    }

    for (const [id, first] of before) {
      const node = entries.get(id)?.node;
      if (!node?.isConnected || typeof node.animate !== 'function') continue;
      const last = node.getBoundingClientRect();
      const x = first.left - last.left;
      const y = first.top - last.top;
      if (x || y) node.animate(
        [{ transform: `translate(${x}px, ${y}px)` }, { transform: 'translate(0, 0)' }],
        { duration: 140, easing: 'ease-out' },
      );
    }
    return list.map((item, index) => entries.get(String(key(item, index))).node);
  }

  function dispose() {
    for (const [id, entry] of entries) remove(entry.node, entry.item, id);
    entries.clear();
  }

  return {
    reconcile,
    dispose,
    get: (id) => entries.get(String(id))?.node || null,
    get size() { return entries.size; },
  };
}
