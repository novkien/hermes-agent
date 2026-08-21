// Federated search — pure logic: debounce, per-source bounds, timeout,
// cancellation, navigation commands. DOM glue lives in src/search.js.

export function createSearcher({ debounceMs = 250, perSourceLimit = 8, timeoutMs = 4000 } = {}) {
  let timer = null;
  let queryHandler = null;
  let seq = 0;
  let pendingResolvers = [];

  return {
    onQuery(fn) {
      queryHandler = fn;
    },
    input(raw) {
      const q = String(raw || '').trim();
      if (!q) return Promise.resolve({});
      seq += 1;
      const mySeq = seq;
      // A previously scheduled debounce must resolve as cancelled when a newer
      // query supersedes it, otherwise its promise stays pending forever.
      if (timer) {
        clearTimeout(timer);
        timer = null;
        pendingResolvers.forEach((r) => r({ cancelled: true }));
        pendingResolvers = [];
      }
      return new Promise((resolve) => {
        pendingResolvers.push(resolve);
        timer = setTimeout(async () => {
          pendingResolvers = [];
          try {
            const results = await queryHandler(q);
            if (mySeq === seq) resolve(results);
            else resolve({ cancelled: true });
          } catch (err) {
            if (mySeq === seq) resolve({ error: String(err && err.message || err) });
            else resolve({ cancelled: true });
          }
        }, debounceMs);
      });
    },
    cancel() {
      seq += 1;
      clearTimeout(timer);
    },
    boundResults(sources) {
      const out = {};
      for (const [name, items] of Object.entries(sources || {})) {
        out[name] = Array.isArray(items) ? items.slice(0, perSourceLimit) : [];
      }
      return out;
    },
    // Resolves to the first source value or the timeout sentinel, whichever first.
    withTimeout(promise, label) {
      let timer = null;
      return Promise.race([
        Promise.resolve(promise).then((v) => {
          if (timer) clearTimeout(timer);
          return { status: 'ok', label, value: v };
        }),
        new Promise((resolve) => {
          timer = setTimeout(() => resolve({ status: 'timeout', label }), timeoutMs);
        }),
      ]);
    },
    get seq() {
      return seq;
    },
  };
}

export function buildNavCommands(routes, profiles) {
  const commands = [];
  for (const route of Object.values(routes || {})) {
    commands.push({ type: 'nav', label: route.label, route: route.key, profile: null });
  }
  for (const p of profiles || []) {
    const profileId = p?.id || p?.name || p?.path || p?.profile_id || 'default';
    const profileLabel = p?.name || p?.id || p?.path || p?.profile_id || 'default';
    commands.push({ type: 'profile', label: `Switch profile → ${profileLabel}`, profile: profileId });
  }
  return commands;
}
