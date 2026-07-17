#!/usr/bin/env python3
"""
Phase 0-1 — context_resolver.py: Per-batch context for API.

Loads context.yaml, selects applicable scene/relationship rules per batch.
Returns readable context dict for injection into user-role API request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


class ContextResolver:
    """
    Resolves context.yaml data into per-batch context for translation API.

    Selection rules:
    1. Prefer scene rule whose file/label scope matches current batch.
    2. Only facts involving current speaker + identified addressee.
    3. Prefer narrower scope -> higher confidence -> most recent.
    4. Cap: 3 relationship rules + 1 speaker note + 1 scene sentence.
    5. Convert to readable names, NOT internal IDs.
    """

    def __init__(self, context_data: str | Path | Dict[str, Any]):
        """
        Initialize resolver from path or dict.

        Args:
            context_data: Path to context.yaml, or parsed dict.
        """
        if isinstance(context_data, (str, Path)):
            path = Path(context_data)
            if path.exists():
                import yaml
                with open(path, "r", encoding="utf-8") as f:
                    self.data = yaml.safe_load(f) or {}
            else:
                self.data = {}
        else:
            self.data = context_data

        self._characters = self.data.get("characters", {})
        self._relationships = self.data.get("relationships", [])
        self._scenes = self.data.get("scenes", [])

        # Build readable name map
        self._name_map: Dict[str, str] = {}
        for char_id, char_def in self._characters.items():
            if isinstance(char_def, dict):
                name = char_def.get("name", "")
                if name:
                    self._name_map[char_id] = name

    def resolve(
        self,
        file: str,
        label: str,
        characters: List[str],
    ) -> Dict[str, Any]:
        """
        Resolve context for a batch.

        Args:
            file: Source file name (e.g. "game/script.rpy").
            label: Current label (e.g. "po01").
            characters: Active character IDs in this batch.

        Returns:
            Context dict: {
                scene: str,
                relationship_rules: [str],
                speaker_notes: {str: str}
            }
        """
        result: Dict[str, Any] = {
            "scene": "",
            "relationship_rules": [],
            "speaker_notes": {},
        }

        # ── Find matching scene ──
        scene_text = self._resolve_scene(file, label, characters)
        if scene_text:
            result["scene"] = scene_text

        # ── Find matching relationship rules ──
        rules = self._resolve_relationships(file, label, characters)

        # Cap at 3
        result["relationship_rules"] = rules[:3]

        # ── Speaker notes (1 cap) ──
        notes = self._resolve_speaker_notes(file, label, characters)
        if notes:
            # Take just the first
            first_speaker = next(iter(notes.keys()))
            result["speaker_notes"] = {first_speaker: notes[first_speaker]}

        return result

    def _resolve_scene(
        self,
        file: str,
        label: str,
        characters: List[str],
    ) -> str:
        """Find best matching scene summary."""
        best_scene = None
        best_score = 0

        for scene in self._scenes:
            if not isinstance(scene, dict):
                continue
            scope = scene.get("scope", {})
            summary = scene.get("summary", "")
            if not summary:
                continue

            score = 0
            # Match file
            scene_file = scope.get("file", "")
            if scene_file and scene_file in file:
                score += 2
            # Match label
            scene_label = scope.get("label", "")
            if scene_label and scene_label == label:
                score += 3
            # Match characters
            scene_chars = scene.get("characters", [])
            char_overlap = len(set(scene_chars) & set(characters))
            score += char_overlap

            if score > best_score:
                best_score = score
                best_scene = summary

        return best_scene or ""

    def _resolve_relationships(
        self,
        file: str,
        label: str,
        characters: List[str],
    ) -> List[str]:
        """Find applicable relationship rules."""
        rules: List[str] = []

        for rel in self._relationships:
            if not isinstance(rel, dict):
                continue

            rel_chars = rel.get("characters", [])

            # Must include at least one active character
            if not set(rel_chars) & set(characters):
                continue

            scope = rel.get("scope", {})
            scope_files = scope.get("files", [])
            scope_labels = scope.get("labels", [])

            # Check scope match
            scope_match = True
            if scope_files:
                if not any(sf in file for sf in scope_files):
                    scope_match = False
            if scope_labels and scope_match:
                if label not in scope_labels:
                    scope_match = False

            if not scope_match:
                continue

            # Build readable rule
            rule = self._build_relationship_rule(rel)
            if rule:
                rules.append(rule)

        return rules

    def _resolve_speaker_notes(
        self,
        file: str,
        label: str,
        characters: List[str],
    ) -> Dict[str, str]:
        """Extract speaker notes from matched scenes."""
        notes: Dict[str, str] = {}

        for scene in self._scenes:
            if not isinstance(scene, dict):
                continue
            scope = scene.get("scope", {})
            scene_label = scope.get("label", "")

            if scene_label and scene_label != label:
                continue

            tone = scene.get("tone", "")
            if tone and characters:
                # Apply tone as speaker note for first active character
                for char_id in characters:
                    readable = self._name_map.get(char_id, char_id)
                    if readable not in notes:
                        notes[readable] = self._tone_to_note(tone, readable)
                        break

        return notes

    def _build_relationship_rule(self, rel: Dict) -> Optional[str]:
        """Build readable rule from a relationship entry."""
        rel_chars = rel.get("characters", [])
        rel_type = rel.get("type", "")
        address = rel.get("address", {})

        if len(rel_chars) < 2:
            return None

        char_a = self._name_map.get(rel_chars[0], rel_chars[0])
        char_b = self._name_map.get(rel_chars[1], rel_chars[1])

        # Build address description
        addr_parts = []
        for addr_key, addr_val in address.items():
            if addr_key == "register":
                # Skip register as it's meta
                continue
            if isinstance(addr_val, str) and addr_val:
                addr_parts.append(addr_val)

        addr_str = f", address: {'; '.join(addr_parts)}" if addr_parts else ""

        # Readable rule
        rule = rel.get("rule", "")
        if rule:
            return rule

        # Auto-build fallback
        type_labels = {
            "family": "family",
            "friends": "close friends",
            "enemies": "enemies",
            "lovers": "lovers",
            "coworkers": "colleagues",
            "superior": "superior-subordinate",
            "stranger": "strangers",
        }
        type_label = type_labels.get(rel_type, rel_type)

        return f"{char_a} và {char_b} là {type_label}{addr_str}."

    @staticmethod
    def _tone_to_note(tone: str, speaker: str) -> str:
        """Convert tone label to speaker note."""
        tone_notes = {
            "tense_direct": f"{speaker} nói thẳng, cộc, có chất chuyên nghiệp/quân đội.",
            "warm": f"{speaker} nói ấm áp, thân thiện.",
            "formal": f"{speaker} nói trang trọng, lịch sự.",
            "casual": f"{speaker} nói thoải mái, thân mật.",
            "angry": f"{speaker} nói giận dữ, căng thẳng.",
            "sad": f"{speaker} nói buồn bã, suy tư.",
            "happy": f"{speaker} nói vui vẻ, phấn khởi.",
            "neutral": f"{speaker} nói trung tính, bình thường.",
        }
        return tone_notes.get(tone, f"{speaker} nói với giọng {tone}.")
