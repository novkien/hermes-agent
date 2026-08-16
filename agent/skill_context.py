"""Skill-index visibility policy shared by CLI, gateway, and prompt assembly."""
from __future__ import annotations

from typing import Any, Mapping


SKILLS_MODE_VISIBLE = "visible"
SKILLS_MODE_PRUNE = "prune"
SKILLS_MODE_INVISIBLE = "invisible"
VALID_SKILLS_MODES = frozenset({
    SKILLS_MODE_VISIBLE,
    SKILLS_MODE_PRUNE,
    SKILLS_MODE_INVISIBLE,
})
PRUNED_SKILL_DESCRIPTION_CHARS = 60


def validate_skills_mode(value: Any, *, field: str = "skills.mode") -> str:
    """Return a canonical mode or raise a user-facing configuration error."""
    if not isinstance(value, str) or value.strip() != value or value not in VALID_SKILLS_MODES:
        allowed = ", ".join(sorted(VALID_SKILLS_MODES))
        raise ValueError(f"{field} must be one of: {allowed}")
    return value


def resolve_profile_skills_mode(config: Mapping[str, Any] | None) -> str:
    """Resolve the profile-wide skill-index mode, preserving legacy visibility."""
    if not isinstance(config, Mapping):
        return SKILLS_MODE_VISIBLE
    skills = config.get("skills")
    if skills is None:
        return SKILLS_MODE_VISIBLE
    if not isinstance(skills, Mapping):
        raise ValueError("skills must be a mapping")
    return validate_skills_mode(skills.get("mode", SKILLS_MODE_VISIBLE))


def prune_skill_description(description: str) -> str:
    """Clamp one rendered description to 60 Unicode code points."""
    text = str(description or "")
    if len(text) <= PRUNED_SKILL_DESCRIPTION_CHARS:
        return text
    return text[: PRUNED_SKILL_DESCRIPTION_CHARS - 1] + "…"
