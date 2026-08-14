// Read-only audit for S7 modules — pure logic.
// Every tab declares its mutation surface; anything not explicitly allowlisted
// here is a read-only call path (AC-27: mutations are allowlisted). This module
// is the single source of truth tests assert against for "no mutation call paths".

import { S7_ROUTE_SPEC } from './s7-route-spec.js';

export function readOnlyRouteIds() {
  return S7_ROUTE_SPEC.map((r) => r.id);
}

// Verified allowlisted operations for SYSTEM command-center per u02 evidence:
// POST /api/ops/doctor, POST /api/ops/security-audit, POST /api/ops/backup are
// verified write routes (u02-dashboard-routes.md lines 114-116).
//
// `csrf` is the hard gate — every one of these goes through the BFF's mutation
// chain. `confirm` is the *UI* arming requirement, which is a separate axis:
// prompt-size only measures a prompt, so it fires on one click, while the three
// that write to disk or shell out arm first.
const VERIFIED_OPS = Object.freeze({
  'command-center': [
    { action: 'POST /api/ops/doctor', csrf: true, confirm: true },
    { action: 'POST /api/ops/security-audit', csrf: true, confirm: true },
    { action: 'POST /api/ops/backup', csrf: true, confirm: true },
    { action: 'POST /api/ops/prompt-size', csrf: true, confirm: false },
  ],
});

/** Mutation call paths for a route id; empty array = pure read-only module. */
export function mutationCalls(routeId) {
  const ops = VERIFIED_OPS[routeId];
  if (!ops) return [];
  return ops.map((o) => ({ ...o }));
}
