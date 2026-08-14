// Staged preload orchestrator.
// (1) shell+auth+profiles+capabilities+health+overview on load;
// (2) idle route-code prefetch (dynamic import); (3) summary prefetch <=4
// concurrent; (4) heavy on first activation; (5) cache per
// profile/route/filter/fingerprint with stale-while-revalidate;
// (6) cancel obsolete in-flight on route/profile change;
// (7) single-flight dedupe. Instrumentation: window.__amcPrefetch + debug panel.

import { createPreloadQueue, createCache, singleFlight } from './pure/preload.js';

export function createPrefetch({ maxConcurrent = 4, onRouteChange = null } = {}) {
  const queue = createPreloadQueue({ maxConcurrent });
  const cache = createCache({ maxEntries: 16 });
  const flight = singleFlight();
  const stats = {
    scheduled: 0,
    completed: 0,
    errors: 0,
    servedFromCache: 0,
    cancelled: 0,
    active: () => queue.active,
    pending: () => queue.pending,
    cacheSize: () => cache.size,
  };
  let generation = 0; // bumped on route/profile change → cancels obsolete work
  let activeGen = null;

  function currentGen() {
    return generation;
  }

  function invalidateObsolete() {
    generation += 1;
  }

  async function prefetch(key, fn, { heavy = false, onActivation = false } = {}) {
    // Heavy lists/details: only on first activation or explicit priority.
    if (heavy && !onActivation && !stats.forceHeavy) return null;
    const gen = generation;
    stats.scheduled += 1;
    const cacheKey = key;
    const cached = cache.get(cacheKey);
    if (cached) {
      stats.servedFromCache += 1;
      return cached;
    }
    try {
      const result = await flight.run(cacheKey, async () => {
        // Single-flight: concurrent identical keys share one upstream fetch.
        if (activeGen !== null && activeGen !== gen) {
          stats.cancelled += 1;
          throw new Error('obsolete');
        }
        const value = await queue.push(cacheKey, fn);
        cache.set(cacheKey, value);
        return value;
      });
      stats.completed += 1;
      return result;
    } catch (err) {
      if (err && err.message === 'obsolete') {
        stats.cancelled += 1;
        return null;
      }
      stats.errors += 1;
      throw err;
    }
  }

  function registerRouteCode(routeKey, importFn) {
    // Idle-time code prefetch: dynamic import scheduled on requestIdleCallback.
    const schedule = () => {
      if (typeof window !== 'undefined' && window.requestIdleCallback) {
        window.requestIdleCallback(() => {
          if (stats.forceHeavy) return;
          importFn().catch(() => {});
        }, { timeout: 3000 });
      } else {
        setTimeout(() => importFn().catch(() => {}), 1000);
      }
    };
    return { schedule };
  }

  function expose() {
    if (typeof window !== 'undefined') {
      window.__amcPrefetch = { stats, invalidateObsolete, cache, queue };
    }
  }

  return {
    prefetch,
    invalidateObsolete,
    currentGen,
    registerRouteCode,
    expose,
    get stats() {
      return stats;
    },
    get cache() {
      return cache;
    },
  };
}
