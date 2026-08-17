"""Per-skill index visibility policy shared across Hermes surfaces."""
from __future__ import annotations

from typing import Any, Mapping


SKILLS_MODE_VISIBLE = "visible"
SKILLS_MODE_PRUNE = "prune"
SKILLS_MODE_INVISIBLE = "invisible"
CONFIGURED_SKILLS_MODES = frozenset({
    SKILLS_MODE_PRUNE,
    SKILLS_MODE_INVISIBLE,
})
PRUNED_SKILL_DESCRIPTION_CHARS = 60


def empty_skills_mode() -> dict[str, tuple[str, ...]]:
    """Return the canonical policy where every skill remains visible."""
    return {
        SKILLS_MODE_PRUNE: (),
        SKILLS_MODE_INVISIBLE: (),
    }


def validate_skills_mode(
    value: Any, *, field: str = "skills.mode"
) -> dict[str, tuple[str, ...]]:
    """Validate and canonicalize ``{prune: [...], invisible: [...]}``.

    Skills absent from both lists remain visible. The same skill cannot occur
    in both lists because an ambiguous policy must fail closed rather than
    depend on an undocumented precedence rule.
    """
    if not isinstance(value, Mapping):
        raise ValueError(
            f"{field} must be a mapping with optional prune and invisible lists"
        )
    unknown_keys = sorted(str(key) for key in value if key not in CONFIGURED_SKILLS_MODES)
    if unknown_keys:
        raise ValueError(
            f"{field} contains unsupported keys: {', '.join(unknown_keys)}; "
            "allowed keys: invisible, prune"
        )

    canonical = empty_skills_mode()
    for mode in sorted(CONFIGURED_SKILLS_MODES):
        names = value.get(mode, [])
        if not isinstance(names, (list, tuple)):
            raise ValueError(f"{field}.{mode} must be a list of skill names")
        normalized: list[str] = []
        for name in names:
            if not isinstance(name, str) or not name or name.strip() != name:
                raise ValueError(
                    f"{field}.{mode} must contain non-empty, trimmed skill names"
                )
            normalized.append(name)
        canonical[mode] = tuple(sorted(set(normalized)))

    overlap = sorted(
        set(canonical[SKILLS_MODE_PRUNE])
        & set(canonical[SKILLS_MODE_INVISIBLE])
    )
    if overlap:
        raise ValueError(
            f"{field} assigns skill(s) to both prune and invisible: "
            + ", ".join(overlap)
        )
    return canonical


def resolve_profile_skills_mode(
    config: Mapping[str, Any] | None,
) -> dict[str, tuple[str, ...]]:
    """Resolve the profile policy; omission leaves every skill visible."""
    if not isinstance(config, Mapping):
        return empty_skills_mode()
    skills = config.get("skills")
    if skills is None:
        return empty_skills_mode()
    if not isinstance(skills, Mapping):
        raise ValueError("skills must be a mapping")
    return validate_skills_mode(skills.get("mode", {}))


def skill_mode_for_name(policy: Mapping[str, Any], name: str) -> str:
    """Return the rendered mode for one skill under a validated policy."""
    if name in policy.get(SKILLS_MODE_INVISIBLE, ()):
        return SKILLS_MODE_INVISIBLE
    if name in policy.get(SKILLS_MODE_PRUNE, ()):
        return SKILLS_MODE_PRUNE
    return SKILLS_MODE_VISIBLE


def serialize_skills_mode(policy: Any) -> dict[str, list[str]]:
    """Return the stable JSON/YAML shape used by session snapshots and APIs."""
    canonical = validate_skills_mode(policy)
    return {
        SKILLS_MODE_PRUNE: list(canonical[SKILLS_MODE_PRUNE]),
        SKILLS_MODE_INVISIBLE: list(canonical[SKILLS_MODE_INVISIBLE]),
    }


def skills_mode_cache_key(policy: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return a deterministic hashable representation of a policy."""
    canonical = validate_skills_mode(policy)
    return (
        canonical[SKILLS_MODE_PRUNE],
        canonical[SKILLS_MODE_INVISIBLE],
    )


def prune_skill_description(description: str) -> str:
    """Clamp one rendered description to 60 Unicode code points."""
    text = str(description or "")
    if len(text) <= PRUNED_SKILL_DESCRIPTION_CHARS:
        return text
    return text[: PRUNED_SKILL_DESCRIPTION_CHARS - 1] + "…"
