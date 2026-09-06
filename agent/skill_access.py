"""Profile skill access, shared by every agent construction and skill entrypoint.

Visibility (``skills.mode``) is deliberately separate from access. An absent
policy preserves legacy behavior; an explicit empty allowlist permits nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


def canonical_skill_identity(value: object) -> str:
    from agent.skill_utils import normalize_skill_lookup_name

    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"unsupported enabled_skills identity: {value!r}")
    if any(char in value for char in ("/", ":", "\\")):
        raise ValueError(f"enabled_skills must use canonical frontmatter names: {value!r}")
    if normalize_skill_lookup_name(value) != value:
        raise ValueError(f"enabled_skills must use canonical frontmatter names: {value!r}")
    return value


def skill_names(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of canonical skill names")
    names = tuple(canonical_skill_identity(item) for item in value)
    if len(set(names)) != len(names):
        raise ValueError(f"{field} contains duplicate canonical identities")
    return names


@dataclass(frozen=True)
class SkillAccess:
    enabled: tuple[str, ...] | None = None
    preload: tuple[str, ...] = ()


def resolve_skill_access(
    config: Mapping | None,
    *,
    enabled: Iterable[str] | None = None,
    known: Iterable[str] | None = None,
) -> SkillAccess:
    """Intersect a caller/topic restriction with the profile policy.

    Validate the entire configured set, not just the intersection: a typo must
    not be hidden by a narrower topic. Required preloads cannot be narrowed out.
    """
    section = config.get("skills", {}) if isinstance(config, Mapping) else {}
    if not isinstance(section, Mapping):
        raise ValueError("skills must be a mapping")
    profile = skill_names(section["enabled"], "skills.enabled") if "enabled" in section else None
    preload = skill_names(section.get("preload", []), "skills.preload")
    caller = tuple(enabled) if enabled is not None else None
    if known is not None:
        unknown = set((profile or ()) + preload) - set(known)
        if unknown:
            raise ValueError("Unknown configured skill(s): " + ", ".join(sorted(unknown)))
    effective = profile if caller is None else caller
    if caller is not None and profile is not None:
        effective = tuple(name for name in caller if name in profile)
    if effective is not None and not set(preload) <= set(effective):
        raise ValueError("skills.preload must be a subset of the effective enabled skills")
    return SkillAccess(effective, preload)


def configured_skill_access(config: Mapping | None) -> bool:
    section = config.get("skills", {}) if isinstance(config, Mapping) else {}
    return isinstance(section, Mapping) and any(key in section for key in ("enabled", "preload"))


def resolve_configured_skill_access(config: Mapping | None, *, enabled=None) -> SkillAccess:
    """Resolve against installed, enabled canonical skills only when configured."""
    known = None
    if configured_skill_access(config):
        from agent.skill_commands import get_skill_commands

        known = {info["name"] for info in get_skill_commands().values() if info.get("name")}
    return resolve_skill_access(config, enabled=enabled, known=known)


def materialize_profile_preloads(agent, config: Mapping | None, already_loaded=()) -> None:
    """Freeze access and materialize required profile skills once per agent."""
    from agent.skill_commands import build_preloaded_skills_prompt
    from agent.skill_policy_context import bind_enabled_skills

    policy = resolve_configured_skill_access(config, enabled=agent.enabled_skills)
    agent.enabled_skills = policy.enabled
    loaded = set(already_loaded)
    if policy.enabled is not None and not loaded <= set(policy.enabled):
        raise ValueError("Preloaded skills are outside the effective skill allowlist")
    pending = [name for name in policy.preload if name not in loaded]
    if pending:
        with bind_enabled_skills(policy.enabled):
            prompt, names, missing = build_preloaded_skills_prompt(pending)
        if missing or set(names) != set(pending):
            raise ValueError("Required skill preload failed: " + ", ".join(missing or pending))
        agent._auto_loaded_skill_prompt = "\n\n".join(
            part for part in (agent._auto_loaded_skill_prompt, prompt) if part
        )
        loaded.update(names)
    agent.preloaded_skill_names = tuple(sorted(loaded))
