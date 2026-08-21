"""Alerts/Pulse derivation (Stage 5 item 6) — deterministic rules per 11.5.

Rule engine evaluates on a tick against cached source data + local store
alert_rules/alert_acknowledgements:

R1 upstream source unavailable (health fail)
R2 schema fingerprint changed (adapter /capabilities vs store)
R3 stale source beyond policy (freshness meta)
R4 failed cron run (jobs.json last_status error)
R5 failed run/task (kanban status failed/timed_out/crashed or run outcome)
R6 stale running-task heartbeat (>5 min)
R7 expiring/long-pending permit (expires_at < 24h or created > 7d)
R8 unresolved high-severity issue (status open, severity critical/high)
R9 event-stream disconnected — client-side only; server emits source.health
R10 token/cost spike vs local threshold (from analytics meta, per-rule config)
R11 repeated authenticated mutation failure (audit rows, >=3 in 10 min)

Each alert: {rule_id, severity, source_id, entity_type, entity_id,
first_seen_at, last_seen_at, state: active|acknowledged|snoozed|resolved,
reason}. GET /api/alerts, POST /api/alerts/{id}/ack + /snooze
(CSRF + local-store mutation only).
"""
from __future__ import annotations

import time
from typing import Any, Optional

from .store import Store

RULES = {
    "R1": {"severity": "critical", "title": "upstream source unavailable"},
    "R2": {"severity": "warning", "title": "schema fingerprint changed"},
    "R3": {"severity": "warning", "title": "stale source beyond policy"},
    "R4": {"severity": "warning", "title": "failed cron run"},
    "R5": {"severity": "warning", "title": "failed run/task"},
    "R6": {"severity": "warning", "title": "stale running-task heartbeat"},
    "R7": {"severity": "warning", "title": "expiring/long-pending permit"},
    "R8": {"severity": "critical", "title": "unresolved high-severity issue"},
    "R10": {"severity": "warning", "title": "token/cost spike vs threshold"},
    "R11": {"severity": "warning", "title": "repeated authenticated mutation failure"},
}

ACK = "acknowledged"
SNOOZE = "snoozed"
ACTIVE = "active"
# Only ever carried on the alert.changed event for a key that just cleared —
# a resolved alert is removed from the engine, never listed in this state.
RESOLVED = "resolved"


def _parse_iso_to_epoch(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value)
    try:
        import datetime as _dt

        s2 = s.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


class Alert:
    def __init__(self, rule_id: str, severity: str, source_id: str,
                 entity_type: str, entity_id: str, reason: str) -> None:
        self.rule_id = rule_id
        self.severity = severity
        self.source_id = source_id
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.reason = reason
        now = int(time.time())
        self.first_seen_at = now
        self.last_seen_at = now

    def key(self) -> str:
        return f"{self.rule_id}:{self.source_id}:{self.entity_type}:{self.entity_id}"

    def to_dict(self) -> dict:
        return {
            "id": self.key(),
            "rule_id": self.rule_id,
            "severity": self.severity,
            "source_id": self.source_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "state": ACTIVE,
            "reason": self.reason,
            "title": RULES.get(self.rule_id, {}).get("title", self.rule_id),
        }


class AlertEngine:
    def __init__(
        self,
        store: Store,
        cfg: Any,
        source_data: Optional[dict] = None,
        bus: Optional[Any] = None,
    ) -> None:
        self.store = store
        self.cfg = cfg
        # source_data: {provider: data} injected by workers/cache (tests inject fixtures)
        self.source_data: dict = source_data or {}
        self.alerts: dict[str, dict] = {}
        self.ack_cache: dict[str, dict] = {}
        # Keys resolved by the last evaluate(), waiting for publish_resolved().
        self.resolved_pending: list[str] = []
        # Optional event bus: acknowledge/snooze publish alert.changed so the
        # SPA shell reloads its alert strip. None => no-op (tests, standalone).
        self.bus = bus

    def set_source_data(self, source_id: str, data: Any) -> None:
        self.source_data[source_id] = data

    def _state_for(self, alert: Alert) -> str:
        ack = self.store.get_acknowledgement(alert.key())
        if not ack:
            return ACTIVE
        if ack["action"] == SNOOZE and ack.get("expires_at") and ack["expires_at"] > int(time.time()):
            return SNOOZE
        if ack["action"] == ACK:
            return ACK
        return ACTIVE

    def evaluate(self) -> list[dict]:
        """Run all rules against source_data; returns active/acked alert list."""
        now = int(time.time())
        found: dict[str, Alert] = {}

        # R1 — upstream source unavailable
        health = self.source_data.get("health", {})
        for src, ok in health.items():
            if ok is False:
                a = Alert("R1", "critical", "health", "source", src,
                          f"upstream source {src} unavailable")
                found[a.key()] = a

        # R2 — schema fingerprint changed
        caps = self.source_data.get("capabilities", {})
        if isinstance(caps, dict):
            fp = caps.get("schema_fingerprint") or caps.get("fingerprint") or ""
            if isinstance(fp, dict):
                fp = fp.get("sha256_ddl", "") or fp.get("global", "")
            if fp:
                recorded = self.store.get_fingerprint("adapter")
                if recorded and recorded != str(fp):
                    a = Alert("R2", "warning", "adapter", "schema", "adapter",
                              f"schema fingerprint changed: {recorded} -> {fp}")
                    found[a.key()] = a

        # R3 — stale source beyond policy
        freshness = self.source_data.get("freshness", {})
        for src, meta in freshness.items():
            fetched = meta.get("fetched_at") if isinstance(meta, dict) else None
            if isinstance(fetched, (int, float)) and now - int(fetched) > self.cfg.alert_stale_seconds:
                a = Alert("R3", "warning", src, "source", src,
                          f"source {src} stale beyond policy")
                found[a.key()] = a

        # R4 — failed cron run
        cron_jobs = self.source_data.get("cron", [])
        for j in cron_jobs:
            if j.get("last_status") == "error":
                a = Alert("R4", "warning", "cron", "cron_job", j.get("id", ""),
                          f"cron job {j.get('name') or j.get('id')} last run failed")
                found[a.key()] = a

        # R5 — failed run/task
        tasks = self.source_data.get("tasks", [])
        for t in tasks:
            if t.get("status") in ("failed", "timed_out", "crashed", "gave_up"):
                a = Alert("R5", "warning", "kanban", "task", t.get("id", ""),
                          f"task {t.get('id')} status {t.get('status')}")
                found[a.key()] = a
        runs = self.source_data.get("runs", [])
        for r in runs:
            if r.get("outcome") in ("crashed", "timed_out", "spawn_failed", "gave_up"):
                a = Alert("R5", "warning", "kanban", "run", str(r.get("id", "")),
                          f"run {r.get('id')} outcome {r.get('outcome')}")
                found[a.key()] = a

        # R6 — stale running-task heartbeat
        for t in tasks:
            if t.get("status") == "running":
                hb = t.get("last_heartbeat_at")
                if isinstance(hb, (int, float)) and now - int(hb) > self.cfg.alert_heartbeat_stale_seconds:
                    a = Alert("R6", "warning", "kanban", "task", t.get("id", ""),
                              f"running task {t.get('id')} heartbeat stale")
                    found[a.key()] = a

        # R7 — expiring/long-pending permit
        permits = self.source_data.get("permits", [])
        for p in permits:
            if p.get("status") not in ("pending_approval",):
                continue
            exp = _parse_iso_to_epoch(p.get("expires_at"))
            created = _parse_iso_to_epoch(p.get("created_at"))
            if exp and exp - now < self.cfg.alert_permit_expiry_hours * 3600:
                a = Alert("R7", "warning", "permits", "permit", p.get("permit_id", ""),
                          f"permit {p.get('permit_id')} expiring <24h")
                found[a.key()] = a
            elif created and now - created > self.cfg.alert_permit_pending_days * 86400:
                a = Alert("R7", "warning", "permits", "permit", p.get("permit_id", ""),
                          f"permit {p.get('permit_id')} pending >7d")
                found[a.key()] = a

        # R8 — unresolved high-severity issue
        issues = self.source_data.get("issues", [])
        for i in issues:
            if i.get("status") in ("open",) and i.get("severity") in ("critical", "high"):
                a = Alert("R8", "critical", "issues", "issue", str(i.get("id", "")),
                          f"issue {i.get('id')} {i.get('severity')} open")
                found[a.key()] = a

        # R10 — token/cost spike vs local threshold
        analytics = self.source_data.get("analytics", {})
        threshold = float(analytics.get("token_threshold", 0) or 0)
        observed = float(analytics.get("tokens", 0) or 0)
        if threshold > 0 and observed > threshold:
            a = Alert("R10", "warning", "analytics", "source", "analytics",
                      f"token usage {observed} exceeds threshold {threshold}")
            found[a.key()] = a

        # R11 — repeated authenticated mutation failure
        if self.store.audit_failures_since(
            self.cfg.alert_mutation_fail_window_seconds, self.cfg.alert_mutation_fail_min
        ):
            a = Alert("R11", "warning", "audit", "source", "audit",
                      f">={self.cfg.alert_mutation_fail_min} mutation failures in "
                      f"{self.cfg.alert_mutation_fail_window_seconds}s")
            found[a.key()] = a

        # merge into persistent state
        for key, alert in found.items():
            prev = self.alerts.get(key)
            if prev:
                prev["last_seen_at"] = alert.last_seen_at
                prev["reason"] = alert.reason
            else:
                self.alerts[key] = alert.to_dict()

        # Resolve: a key that no longer matches any rule is gone, not merely
        # old. Before this, `self.alerts` only ever grew, so a source that
        # blipped once stayed "critical / active" until the process restarted
        # — the alert strip showed outages that had recovered hours earlier.
        #
        # Dropping a key on a missing feed would be wrong, but cannot happen
        # here: `set_source_data` only ever overwrites, so once a worker has
        # fed a source the key stays present with its last value, and a rule
        # that stops matching has genuinely stopped matching.
        for key in [k for k in self.alerts if k not in found]:
            del self.alerts[key]
            try:
                self.store.clear_acknowledgements(key)
            except Exception:  # noqa: BLE001 - eval must not die on ack cleanup
                pass
            self.ack_cache.pop(key, None)
            self.resolved_pending.append(key)

        return self.list_active()

    async def publish_resolved(self) -> None:
        """Emit `alert.changed` for keys resolved since the last call.

        `evaluate()` stays synchronous (tests and the read route call it
        directly), so the async tick loop drains the queue instead.
        """
        if self.bus is None:
            self.resolved_pending.clear()
            return
        while self.resolved_pending:
            key = self.resolved_pending.pop(0)
            try:
                await self.bus.publish(
                    "alert.changed", "alert-engine", "alert", key,
                    {"state": RESOLVED}, coverage="derived",
                )
            except Exception:  # noqa: BLE001
                pass

    def list_active(self) -> list[dict]:
        out = []
        for key, a in self.alerts.items():
            d = dict(a)
            d["state"] = self._state_for(Alert(
                d["rule_id"], d["severity"], d["source_id"],
                d["entity_type"], d["entity_id"], d["reason"],
            ))
            out.append(d)
        return out

    async def acknowledge(self, alert_id: str, action: str = ACK, snooze_seconds: Optional[int] = None) -> dict:
        if alert_id not in self.alerts:
            raise KeyError(alert_id)
        expires_at = None
        if action == SNOOZE:
            expires_at = int(time.time()) + (snooze_seconds or 3600)
        self.store.acknowledge_alert(alert_id, action, expires_at)
        self.ack_cache[alert_id] = {"action": action, "expires_at": expires_at}
        d = dict(self.alerts[alert_id])
        d["state"] = action
        if self.bus is not None:
            try:
                await self.bus.publish(
                    "alert.changed", "alert-engine", "alert", alert_id,
                    {"state": action}, coverage="native",
                )
            except Exception:
                pass
        return d
