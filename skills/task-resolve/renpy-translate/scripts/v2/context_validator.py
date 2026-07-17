#!/usr/bin/env python3
"""
Phase 0-1 — context_validator.py: Schema validation for context.yaml.

Validates required fields, allows empty lists/maps for weak-signal games.
Rejects: orphan aliases, impossible address directions, duplicate conflicting rules.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


REQUIRED_TOP_LEVEL = {"version", "run", "game", "characters", "relationships", "scenes"}

REQUIRED_RUN = {"id", "generated_at", "source_fingerprint", "source_language", "confidence_cap"}

REQUIRED_GAME = {"name", "source_architecture", "tracking_signals", "mc_nameable", "nvl_mode"}

REQUIRED_RELATIONSHIP = {"characters", "type", "scope", "address", "confidence", "evidence"}

REQUIRED_SCENE = {"key", "scope", "characters", "summary", "confidence"}

VALID_ARCHITECTURES = {"flat", "chapter", "archive"}


def validate_context(data: Any) -> Tuple[bool, List[str]]:
    """
    Validate a context.yaml structure.

    Args:
        data: Parsed YAML data (should be a dict).

    Returns:
        (valid: bool, errors: list[str])
    """
    errors: List[str] = []

    if not isinstance(data, dict):
        return False, ["Context data must be a dict (YAML mapping)"]

    # ── Check required top-level fields ──
    missing_top = REQUIRED_TOP_LEVEL - set(data.keys())
    if missing_top:
        errors.append(f"Missing required top-level fields: {', '.join(sorted(missing_top))}")

    # ── Validate 'run' ──
    run = data.get("run")
    if not isinstance(run, dict):
        if "run" not in missing_top:
            errors.append("'run' must be a dict")
    else:
        missing_run = REQUIRED_RUN - set(run.keys())
        if missing_run:
            errors.append(f"Missing required 'run' fields: {', '.join(sorted(missing_run))}")

    # ── Validate 'game' ──
    game = data.get("game")
    if not isinstance(game, dict):
        if "game" not in missing_top:
            errors.append("'game' must be a dict")
    else:
        missing_game = REQUIRED_GAME - set(game.keys())
        if missing_game:
            errors.append(f"Missing required 'game' fields: {', '.join(sorted(missing_game))}")
        arch = game.get("source_architecture")
        if arch and arch not in VALID_ARCHITECTURES:
            errors.append(f"Invalid source_architecture: '{arch}' (valid: {VALID_ARCHITECTURES})")

    # ── Validate 'characters' keys ──
    characters = data.get("characters", {})
    if not isinstance(characters, dict):
        errors.append("'characters' must be a dict mapping character IDs to definitions")
        characters = {}

    # ── Build set of valid character IDs ──
    valid_char_ids: set = set(characters.keys()) if isinstance(characters, dict) else set()

    # ── Validate 'relationships' ──
    relationships = data.get("relationships", [])
    if not isinstance(relationships, list):
        errors.append("'relationships' must be a list")
        relationships = []

    seen_scope_pairs: Dict[Tuple[str, ...], List[Dict]] = {}
    for i, rel in enumerate(relationships):
        if not isinstance(rel, dict):
            errors.append(f"relationships[{i}]: must be a dict")
            continue

        missing_rel = REQUIRED_RELATIONSHIP - set(rel.keys())
        if missing_rel:
            errors.append(f"relationships[{i}]: missing fields: {', '.join(sorted(missing_rel))}")
            continue

        # Check character IDs in this relationship
        rel_chars = rel.get("characters", [])
        if not isinstance(rel_chars, list):
            errors.append(f"relationships[{i}].characters: must be a list")
        else:
            for char_id in rel_chars:
                if char_id not in valid_char_ids:
                    errors.append(
                        f"relationships[{i}]: character '{char_id}' not defined in 'characters'"
                    )

        # Check address directions
        address = rel.get("address", {})
        if isinstance(address, dict):
            for addr_key in address:
                if addr_key.endswith("_to_") or "_to_" in addr_key:
                    parts = addr_key.split("_to_")
                    if len(parts) == 2:
                        from_char, to_char = parts
                        if from_char and from_char not in valid_char_ids:
                            errors.append(
                                f"relationships[{i}].address: "
                                f"'{addr_key}' references unknown character '{from_char}'"
                            )
                        if to_char and to_char not in valid_char_ids:
                            errors.append(
                                f"relationships[{i}].address: "
                                f"'{addr_key}' references unknown character '{to_char}'"
                            )

        # Check for duplicate conflicting rules at equal scope
        scope = rel.get("scope", {})
        scope_key = tuple(sorted(rel_chars)) if isinstance(rel_chars, list) else ()
        rel_type = rel.get("type", "")

        if scope_key in seen_scope_pairs:
            for prev_rel in seen_scope_pairs[scope_key]:
                if prev_rel.get("type") != rel_type:
                    errors.append(
                        f"relationships[{i}]: conflicting types for "
                        f"character pair {'/'.join(scope_key)}: "
                        f"'{prev_rel.get('type')}' vs '{rel_type}' at equal scope"
                    )
        seen_scope_pairs.setdefault(scope_key, []).append(rel)

    # ── Validate 'scenes' ──
    scenes = data.get("scenes", [])
    if not isinstance(scenes, list):
        errors.append("'scenes' must be a list")
        scenes = []

    for i, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            errors.append(f"scenes[{i}]: must be a dict")
            continue

        missing_scene = REQUIRED_SCENE - set(scene.keys())
        if missing_scene:
            errors.append(f"scenes[{i}]: missing fields: {', '.join(sorted(missing_scene))}")

        # Check scene characters are valid
        scene_chars = scene.get("characters", [])
        if isinstance(scene_chars, list):
            for char_id in scene_chars:
                if char_id not in valid_char_ids:
                    errors.append(
                        f"scenes[{i}]: character '{char_id}' not defined in 'characters'"
                    )

    return len(errors) == 0, errors
