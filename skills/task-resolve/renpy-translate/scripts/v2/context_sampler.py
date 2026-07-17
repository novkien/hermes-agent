#!/usr/bin/env python3
"""
Phase 0-1 — context_sampler.py: Agent scene selection scaffold.

CLI tool: takes game root + inventory output, selects 3-5 scenes.

Priority: high-signal state changes -> intro scenes -> high co-occurrence -> longest unresolved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional


def select_scenes(
    inventory: Dict[str, Any],
    count: int = 3,
) -> List[Dict[str, Any]]:
    """
    Select scenes for agent reading based on analysis priorities.

    Priority:
    1. High-signal state changes (labels with $assignments, menus, renpy.notify)
    2. Introduction/early scenes for main characters
    3. High co-occurrence (most dialogue in area)
    4. Longest unresolved scene

    Args:
        inventory: Output from inventory_rpy_files().
        count: Number of scenes to select (default 3, max 5).

    Returns:
        List of {file, label, line_start, line_end, priority_reason}
    """
    count = min(max(count, 1), 5)

    labels = inventory.get("labels", [])
    signals = inventory.get("state_signals", [])

    if not labels:
        return []

    # Build signal-rich labels
    signal_count: Dict[str, int] = {}
    for sig in signals:
        sig_file = sig.get("file", "")
        # Find which label this signal belongs to
        matching_label = _find_enclosing_label(labels, sig_file, sig.get("line", 0))
        if matching_label:
            signal_count[matching_label] = signal_count.get(matching_label, 0) + 1

    # Score each label for selection priority
    scored: List[Dict[str, Any]] = []
    for lbl in labels:
        label_name = lbl["name"]
        file_name = lbl["file"]
        line = lbl["line"]
        sigs = signal_count.get(label_name, 0)

        # Priority boost from signals
        priority = sigs * 10

        # Boost for labels with menus
        menus = inventory.get("menus", [])
        for menu in menus:
            if menu.get("file") == file_name:
                # Check if menu line is near this label
                if abs(menu.get("line", 0) - line) < 200:
                    priority += 5

        # Boost for early scenes (introductions)
        if any(keyword in label_name.lower() for keyword in ["start", "intro", "begin"]):
            priority += 3

        scored.append({
            "file": file_name,
            "label": label_name,
            "line_start": line,
            "line_end": line + 200,  # Estimate scene span
            "priority": priority,
            "priority_reason": _reason(priority, signal_count.get(label_name, 0)),
        })

    # Sort by priority descending, take top `count`
    scored.sort(key=lambda x: -x["priority"])
    selected = scored[:count]

    return selected


def agent_read(
    inventory: Dict[str, Any],
    scenes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Produce source excerpts for selected scenes.

    Args:
        inventory: Output from inventory_rpy_files().
        scenes: List from select_scenes().

    Returns:
        List of {file, label, line_start, line_end, content, character_count}
    """
    game_dir = Path(inventory.get("_game_dir", "."))

    excerpts: List[Dict[str, Any]] = []
    for scene in scenes:
        file_name = scene.get("file", "")
        game_path = game_dir / "game" / file_name

        if not game_path.exists():
            # Try relative to game root directly
            game_path = game_dir / file_name

        if not game_path.exists():
            excerpts.append({
                "file": file_name,
                "label": scene.get("label", ""),
                "content": f"# File not found: {game_path}",
                "character_count": 0,
                "line_start": scene.get("line_start"),
                "line_end": scene.get("line_end"),
            })
            continue

        try:
            lines = game_path.read_text(encoding="utf-8").split("\n")
            start = max(0, scene.get("line_start", 1) - 1)
            end = min(len(lines), scene.get("line_end", len(lines)))
            content_lines = lines[start:end]
            content = "\n".join(content_lines)

            excerpts.append({
                "file": file_name,
                "label": scene.get("label", ""),
                "content": content,
                "character_count": len(content),
                "line_start": start + 1,
                "line_end": end,
            })
        except (UnicodeDecodeError, OSError) as e:
            excerpts.append({
                "file": file_name,
                "label": scene.get("label", ""),
                "content": f"# Error reading file: {e}",
                "character_count": 0,
                "line_start": scene.get("line_start"),
                "line_end": scene.get("line_end"),
            })

    return excerpts


def _find_enclosing_label(
    labels: List[Dict[str, Any]],
    file_name: str,
    line: int,
) -> Optional[str]:
    """Find the enclosing label for a given line."""
    valid = [
        l for l in labels
        if l.get("file") == file_name and l.get("line", 0) <= line
    ]
    if not valid:
        return None
    # Most recent label before this line
    valid.sort(key=lambda x: -x.get("line", 0))
    return valid[0]["name"]


def _reason(priority: int, signal_count: int) -> str:
    """Generate human-readable selection reason."""
    if priority >= 20:
        return f"high-signal ({signal_count} state changes, menu)"
    elif priority >= 10:
        return f"state_change ({signal_count} signals)"
    elif priority >= 5:
        return "intro/early_scene"
    else:
        return "default_priority"
