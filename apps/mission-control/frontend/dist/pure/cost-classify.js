// Cost-origin classification (AC-30 / §11.9).
// Every cost figure must be labeled with exactly one of these classes.
// Estimated values must never be presented as actual.

export const COST_CLASS = Object.freeze({
  PROVIDER_REPORTED: 'provider-reported',
  HERMES_CALCULATED: 'Hermes-calculated',
  ESTIMATED_FROM_VERIFIED_RATE: 'estimated-from-verified-rate',
  UNAVAILABLE: 'unavailable',
});

const KNOWN = new Set(Object.values(COST_CLASS));

export function classifyCost(cls) {
  return KNOWN.has(cls) ? cls : COST_CLASS.UNAVAILABLE;
}

export const COST_LABELS = Object.freeze({
  [COST_CLASS.PROVIDER_REPORTED]: 'provider-reported',
  [COST_CLASS.HERMES_CALCULATED]: 'Hermes-calculated (estimated)',
  [COST_CLASS.ESTIMATED_FROM_VERIFIED_RATE]: 'estimated-from-verified-rate',
  [COST_CLASS.UNAVAILABLE]: 'unavailable',
});

export function costLabel(cls) {
  return COST_LABELS[classifyCost(cls)];
}
