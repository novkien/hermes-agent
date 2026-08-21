// Turns bursty arrival into steady reveal.
//
// Text never reaches a browser at the rate it was written. The gateway
// coalesces, TCP coalesces, a relay from another process batches frames to
// avoid a request per token, and a backgrounded tab wakes up holding a second's
// worth of text at once. Painting each burst the instant it lands reproduces
// every one of those hiccups on screen — the reader sees chunks slam in and
// then nothing, which reads as a stuttering connection even when the transport
// is perfectly healthy.
//
// So arrival and reveal are separated: frames update a target, and the view
// walks toward that target at a rate estimated from how fast text is actually
// coming. The estimate is the point — this is not a fixed typewriter speed,
// which would throttle a fast model and crawl through a slow one. Nothing is
// ever delayed by more than `maxLagMs`, and a burst too large to be prose (a
// completed message arriving whole, a pasted block) is shown immediately rather
// than animated, because animating it would be a lie about when it arrived.
//
// Pure and time-injected: every method takes `now`, so the whole behaviour is
// testable in Node without a clock or a DOM.

export const DEFAULT_MIN_CPS = 30;
export const DEFAULT_MAX_CPS = 1400;
// Floor and ceiling on how far behind the reveal may run.
//
// A single fixed cap was the flaw in the first version of this: at 320ms, a
// stream that arrives in big lumps a few seconds apart got each lump wiped onto
// the screen inside a third of a second and then sat still — which is the
// stuttering this module exists to remove, not a fix for it. The budget now
// follows the observed gap BETWEEN arrivals, so a lumpy stream types out
// continuously across the pause that follows it, while a stream that already
// arrives smoothly keeps the small budget and stays nearly instant.
export const DEFAULT_MIN_LAG_MS = 260;
export const DEFAULT_MAX_LAG_MS = 2000;
// Bigger than any single burst of prose: this is a whole message landing at
// once, and holding it back would be theatre rather than smoothing.
export const DEFAULT_BURST_CHARS = 6000;
// Weight on the newest observation. Low enough that one late frame does not
// swing the pace, high enough to follow a model that genuinely speeds up.
export const RATE_SMOOTHING = 0.25;

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

export function createDeltaPacer({
  minCps = DEFAULT_MIN_CPS,
  maxCps = DEFAULT_MAX_CPS,
  minLagMs = DEFAULT_MIN_LAG_MS,
  maxLagMs = DEFAULT_MAX_LAG_MS,
  burstChars = DEFAULT_BURST_CHARS,
} = {}) {
  const states = new Map();

  function stateFor(key) {
    let state = states.get(key);
    if (!state) {
      state = {
        target: 0, shown: 0, cps: 0, gapMs: 0,
        arrivedAt: 0, paintedAt: 0, done: false,
      };
      states.set(key, state);
    }
    return state;
  }

  // How long this block's reveal may run behind: about one inter-arrival gap,
  // so the reveal of one lump lasts until roughly when the next one lands.
  function lagBudget(state) {
    if (!state.gapMs) return minLagMs;
    return clamp(state.gapMs * 1.1, minLagMs, maxLagMs);
  }

  return {
    /** Record how much text exists for `key` now. Returns nothing paintable. */
    observe(key, target, now) {
      const state = stateFor(key);
      const length = Math.max(0, Number(target) || 0);
      if (length <= state.target) {
        // Text never shrinks mid-turn; a smaller target means the block was
        // rebuilt, so follow it rather than holding a stale, longer prefix.
        if (length < state.target) {
          state.target = length;
          state.shown = Math.min(state.shown, length);
        }
        return;
      }
      if (state.arrivedAt) {
        const elapsed = now - state.arrivedAt;
        if (elapsed > 0) {
          const instant = (length - state.target) / (elapsed / 1000);
          state.cps = state.cps
            ? state.cps + (instant - state.cps) * RATE_SMOOTHING
            : instant;
          state.gapMs = state.gapMs
            ? state.gapMs + (elapsed - state.gapMs) * RATE_SMOOTHING
            : elapsed;
        }
      }
      // Restart the clock whenever the previous text had been fully revealed.
      //
      // The reveal advances by `rate × time-since-last-paint`, and the pump that
      // calls it stops once there is nothing left to reveal. So the first step
      // after a quiet gap carried the WHOLE gap as its timestep — three seconds
      // of it, for a stream that arrives in lumps — and multiplied it out into
      // an instant dump of the entire lump. Only the first lump of such a stream
      // ever looked paced; every one after it jumped, which is exactly the
      // stutter this module is supposed to remove.
      const caughtUp = state.shown >= state.target;
      state.target = length;
      state.arrivedAt = now;
      if (caughtUp || !state.paintedAt) state.paintedAt = now;
    },

    /** How many characters of `key` may be on screen at `now`. */
    revealed(key, now) {
      const state = states.get(key);
      if (!state) return 0;
      if (state.done) {
        state.shown = state.target;
        return state.target;
      }

      const backlog = state.target - state.shown;
      if (backlog <= 0) {
        state.paintedAt = now;
        return Math.floor(state.shown);
      }
      // Bigger than anything a model streams token by token: this is a whole
      // message landing at once, and pretending to type it out would add
      // hundreds of milliseconds of pure theatre.
      if (backlog > burstChars) {
        state.shown = state.target;
        state.paintedAt = now;
        return state.target;
      }

      // The deadline is anchored to when the text ARRIVED, not to the current
      // backlog. Deriving the catch-up rate from the backlog alone decays
      // exponentially — always draining a fraction, never finishing — so the
      // guarantee it was supposed to make was never actually kept.
      const remaining = Math.max(0, (state.arrivedAt + lagBudget(state)) - now) / 1000;
      if (remaining <= 0) {
        state.shown = state.target;
        state.paintedAt = now;
        return state.target;
      }

      const seconds = Math.max(0, now - (state.paintedAt || now)) / 1000;
      state.paintedAt = now;
      // The lag guarantee overrides the estimate, and is deliberately not
      // capped by `maxCps`: a smooth pace is worth having only as long as it
      // never becomes the reason text is late.
      const paced = clamp(state.cps || minCps, minCps, maxCps);
      const required = backlog / remaining;
      state.shown = Math.min(state.target, state.shown + Math.max(paced, required) * seconds);
      return Math.floor(state.shown);
    },

    /** True while anything is still being walked toward its target. */
    pending(key) {
      const state = states.get(key);
      return Boolean(state) && !state.done && state.shown < state.target;
    },

    anyPending() {
      for (const state of states.values()) {
        if (!state.done && state.shown < state.target) return true;
      }
      return false;
    },

    /**
     * Stop pacing `key` and show all of it.
     *
     * Called the moment a block closes or the turn ends. A finished turn must
     * never leave text trickling in behind it — the run is over, and the reader
     * waiting on an animation for text the server already sent is exactly the
     * kind of invented latency this module exists to remove.
     */
    flush(key) {
      const state = stateFor(key);
      state.done = true;
      state.shown = state.target;
      return state.target;
    },

    flushAll() {
      for (const state of states.values()) {
        state.done = true;
        state.shown = state.target;
      }
    },

    reset() {
      states.clear();
    },
  };
}
