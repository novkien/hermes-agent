"""Authoritative fail-closed topic ``enabled_skills`` policy resolution."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Iterable


class SkillPolicyStatus(str, Enum):
    UNCONFIGURED = "UNCONFIGURED"
    CONFIGURED_VALID = "CONFIGURED_VALID"
    CONFIGURED_INVALID = "CONFIGURED_INVALID"
    RESOLUTION_ERROR = "RESOLUTION_ERROR"


@dataclass(frozen=True)
class EnabledSkillsPolicy:
    status: SkillPolicyStatus
    identities: tuple[str, ...] = ()
    fingerprint: str = "legacy"
    error: str | None = None

    @property
    def configured(self) -> bool:
        return self.status is not SkillPolicyStatus.UNCONFIGURED

    def permits(self, identity: str) -> bool:
        return self.status is SkillPolicyStatus.CONFIGURED_VALID and identity in self.identities


def canonical_skill_identity(value: object) -> str:
    """Canonical configured identity; paths and aliases are intentionally rejected."""
    value = str(value or "").strip()
    if not value or "/" in value or ":" in value or "\\" in value:
        raise ValueError(f"unsupported enabled_skills identity: {value!r}")
    from agent.skill_utils import normalize_skill_lookup_name
    identity = normalize_skill_lookup_name(value)
    if not identity or identity != value:
        raise ValueError(f"enabled_skills must use canonical frontmatter names: {value!r}")
    return identity


def _invalid(status: SkillPolicyStatus, error: str) -> EnabledSkillsPolicy:
    return EnabledSkillsPolicy(status=status, error=error, fingerprint="invalid:" + sha256(error.encode()).hexdigest())


def resolve_enabled_skills_policy(source, config: dict | None, *, skill_names: Iterable[str] | None = None) -> EnabledSkillsPolicy:
    """Resolve policy from *physical* Telegram source before session inheritance.

    Resolution errors are a policy state, never the legacy ``None`` sentinel.
    """
    try:
        platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))
        if str(platform).lower() != "telegram" or not getattr(source, "chat_id", None) or not getattr(source, "thread_id", None):
            return EnabledSkillsPolicy(SkillPolicyStatus.UNCONFIGURED)
        from gateway.platforms.base import resolve_group_topic
        cfg = config if isinstance(config, dict) else {}
        typed = ((cfg.get("platforms") or {}).get("telegram") or {}).get("extra") or {}
        legacy = (cfg.get("telegram") or {}).get("extra") or {}
        # Both locations are supported; typed takes precedence only when it declares topics.
        extra = typed if "group_topics" in typed else legacy
        topic = resolve_group_topic(extra, str(source.chat_id), str(source.thread_id))
        if topic is None or "enabled_skills" not in topic:
            return EnabledSkillsPolicy(SkillPolicyStatus.UNCONFIGURED)
        raw = topic.get("enabled_skills")
        if not isinstance(raw, list) or not raw:
            return _invalid(SkillPolicyStatus.CONFIGURED_INVALID, "enabled_skills must be a non-empty list")
        if not all(isinstance(item, str) for item in raw):
            return _invalid(SkillPolicyStatus.CONFIGURED_INVALID, "enabled_skills entries must be strings")
        try:
            identities = [canonical_skill_identity(item) for item in raw]
        except ValueError as exc:
            return _invalid(SkillPolicyStatus.CONFIGURED_INVALID, str(exc))
        if len(set(identities)) != len(identities):
            return _invalid(SkillPolicyStatus.CONFIGURED_INVALID, "enabled_skills contains duplicate canonical identities")
        if skill_names is not None:
            known = {canonical_skill_identity(name) for name in skill_names}
            unknown = sorted(set(identities) - known)
            if unknown:
                return _invalid(SkillPolicyStatus.CONFIGURED_INVALID, "unknown enabled_skills: " + ", ".join(unknown))
        # Include inherited canonical target and registry identities in the cache partition.
        canonical = str(topic.get("thread_id", ""))
        material = "|".join([canonical, *sorted(identities), *sorted(skill_names or ())])
        return EnabledSkillsPolicy(SkillPolicyStatus.CONFIGURED_VALID, tuple(identities), sha256(material.encode()).hexdigest())
    except Exception as exc:
        return _invalid(SkillPolicyStatus.RESOLUTION_ERROR, f"could not resolve enabled_skills: {exc}")


def current_enabled_skills_policy() -> EnabledSkillsPolicy:
    """Resolve the active tool-call context without manufacturing a legacy bypass."""
    try:
        from types import SimpleNamespace
        from gateway.session_context import get_session_env
        from hermes_cli.config import load_config_readonly
        platform = get_session_env("HERMES_SESSION_PLATFORM", "") or ""
        source = SimpleNamespace(
            platform=SimpleNamespace(value=platform),
            chat_id=get_session_env("HERMES_SESSION_CHAT_ID", "") or "",
            thread_id=get_session_env("HERMES_SESSION_THREAD_ID", "") or "",
        )
        return resolve_enabled_skills_policy(source, load_config_readonly())
    except Exception as exc:
        return _invalid(SkillPolicyStatus.RESOLUTION_ERROR, f"could not resolve session enabled_skills: {exc}")
