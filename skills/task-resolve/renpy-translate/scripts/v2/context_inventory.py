#!/usr/bin/env python3
"""
Phase 0-1 — context_inventory.py: Recursive .rpy discovery + structural analysis.

Scans a Ren'Py game root for:
  - All .rpy files in game/
  - define Character() declarations
  - Labels, menus, menu items with branches
  - State-change signals ($var +=, $var =, unlock_, set_, flag, renpy.notify)
  - Architecture classification (flat/chapter/archive)
  - File-scope mapping

No hardcoded game-specific patterns.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── Compiled regex patterns ──────────────────────────────────

# define char_var = Character(_("Display Name"), ...)
CHARACTER_RE = re.compile(
    r"^define\s+(\w+)\s*=\s*Character\(\s*_\s*\(\s*\"([^\"]*)\"\s*\)",
    re.MULTILINE,
)

# label name:
LABEL_RE = re.compile(r"^\s*label\s+(\w[\w\-]*)\s*:", re.MULTILINE)

# menu:
MENU_START_RE = re.compile(r"^\s*menu\s*:", re.MULTILINE)

# Menu item: "Text":
MENU_ITEM_RE = re.compile(r'^\s+\"([^\"]+)\"\s*:', re.MULTILINE)

# State-change signals
STATE_SIGNAL_RE = re.compile(
    r"""
    (?:
        \$\s*\w+\s*\+=\s*\w+     |  # $var += value
        \$\s*\w+\s*=\s*[^=]      |  # $var = ... (not ==)
        \bunlock_\w+\s*\(        |  # unlock_xxx()
        \bset_\w+\s*\(           |  # set_xxx()
        \bflag\b                  |  # flag
        renpy\.notify\s*\(       |  # renpy.notify()
        \binit\s+\d+\s*:          |  # init blocks
        \bpython\s*:              |  # python blocks
        \bjump\s+\w+               # jump label
    )
    """,
    re.VERBOSE | re.MULTILINE | re.DOTALL,
)

# jump label
JUMP_RE = re.compile(r"\bjump\s+(\w[\w\-]*)", re.MULTILINE)

# label start line range
LABEL_LINE_RE = re.compile(r"^label\s+(\w[\w\-]*)\s*:", re.MULTILINE)


# ═══════════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════════

def inventory_rpy_files(game_root: str | Path) -> Dict[str, Any]:
    """
    Scan a Ren'Py game root for source structure.

    Returns structured dict with:
      - files: list of {filename, path, size, line_count}
      - characters: {var: {name, aliases, role, dialogue_lines, first_evidence}}
      - labels: [{name, file, line}]
      - menus: [{file, line, items: [{text, line}]}]
      - state_signals: [{file, line, text}]
      - architecture: "flat" | "chapter" | "archive" | "unknown"
      - file_scopes: {filepath: {labels: [...], scenes: [...], menus: [...], signals: [...]}}
    """
    game_root = Path(game_root).resolve()
    game_dir = game_root / "game"

    result: Dict[str, Any] = {
        "files": [],
        "characters": {},
        "labels": [],
        "menus": [],
        "state_signals": [],
        "architecture": "unknown",
        "file_scopes": {},
    }

    if not game_dir.is_dir():
        # Maybe game_root IS the game dir
        game_dir = game_root
        if not game_dir.is_dir():
            return result

    # ── Discover .rpy files recursively ──
    rpy_files: List[Path] = []
    for fpath in sorted(game_dir.rglob("*.rpy")):
        # Skip cache, saves, tl directories
        rel = fpath.relative_to(game_dir)
        if (
            "cache" in rel.parts
            or "saves" in rel.parts
            or "tl" in rel.parts
        ):
            continue
        rpy_files.append(fpath)

    for fpath in rpy_files:
        try:
            content = fpath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        rel_path = fpath.relative_to(game_dir)
        filename = str(rel_path)
        lines = content.split("\n")
        file_info = {
            "filename": filename,
            "path": str(fpath),
            "size": fpath.stat().st_size,
            "line_count": len(lines),
        }
        result["files"].append(file_info)

        # ── Extract characters ──
        for match in CHARACTER_RE.finditer(content):
            var = match.group(1)
            display_name = match.group(2)
            line_no = content[: match.start()].count("\n") + 1
            if var not in result["characters"]:
                result["characters"][var] = {
                    "name": display_name,
                    "aliases": [],
                    "role": "extra",
                    "dialogue_lines": 0,
                    "first_evidence": {
                        "file": filename,
                        "line": line_no,
                        "label": None,
                    },
                }
            else:
                # Update name if not already set
                existing = result["characters"][var]
                if not existing["name"] and display_name:
                    existing["name"] = display_name

        # ── Extract labels ──
        file_labels = []
        for match in LABEL_RE.finditer(content):
            label_name = match.group(1)
            line_no = content[: match.start()].count("\n") + 1
            result["labels"].append({
                "name": label_name,
                "file": filename,
                "line": line_no,
            })
            file_labels.append(label_name)

        # ── Extract menus ──
        file_menus = []
        # Find menu: lines and their items
        for menu_match in MENU_START_RE.finditer(content):
            menu_line = content[: menu_match.start()].count("\n") + 1
            items = []
            # Collect items following the menu:
            after_text = content[menu_match.end():]
            for item_match in MENU_ITEM_RE.finditer(after_text):
                item_text = item_match.group(1)
                item_line = menu_line + after_text[: item_match.start()].count("\n")
                items.append({"text": item_text, "line": item_line})
            menu_entry = {
                "file": filename,
                "line": menu_line,
                "items": items,
            }
            result["menus"].append(menu_entry)
            file_menus.append(menu_entry)

        # ── Extract state signals ──
        file_signals = []
        for match in STATE_SIGNAL_RE.finditer(content):
            signal_text = match.group(0).strip()
            line_no = content[: match.start()].count("\n") + 1
            result["state_signals"].append({
                "file": filename,
                "line": line_no,
                "text": signal_text,
            })
            file_signals.append(signal_text)

        # ── File scope mapping ──
        result["file_scopes"][filename] = {
            "labels": file_labels,
            "menus": file_menus,
            "signals": file_signals,
        }

    # ── Architecture classification ──
    num_files = len(rpy_files)
    num_labels = len(result["labels"])

    # Check if labels are distributed across files (chapter structure)
    files_with_labels = sum(1 for fs in result["file_scopes"].values() if fs.get("labels"))
    labels_per_file = []
    for fs in result["file_scopes"].values():
        labels_per_file.extend(fs.get("labels", []))

    if num_files == 0:
        result["architecture"] = "unknown"
    elif num_files > 3 or (num_files >= 2 and files_with_labels >= 2 and num_files == num_labels):
        # Multiple files each with distinct labels/minimal overlap = chapter
        result["architecture"] = "chapter"
    elif 1 <= num_files <= 3 and num_labels >= 2:
        result["architecture"] = "flat"
    else:
        result["architecture"] = "flat"

    # ── Dialogue line counts ──
    # Count rough character usage across files
    char_usage: Dict[str, int] = {}
    for fpath in rpy_files:
        try:
            content = fpath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for var in result["characters"]:
            # Count lines that start with var followed by space
            pattern = re.compile(rf"^\s+{re.escape(var)}\s+\"", re.MULTILINE)
            count = len(pattern.findall(content))
            char_usage[var] = char_usage.get(var, 0) + count

    for var, count in char_usage.items():
        if var in result["characters"]:
            result["characters"][var]["dialogue_lines"] = count
            # Mark main characters by dialogue volume
            if count > 100:
                result["characters"][var]["role"] = "main"

    return result


def find_rpy_files(game_root: str | Path) -> List[Path]:
    """Find all .rpy files in game/ recursively, skipping cache/saves/tl."""
    game_root = Path(game_root).resolve()
    game_dir = game_root / "game"
    if not game_dir.is_dir():
        game_dir = game_root

    rpy_files: List[Path] = []
    for fpath in sorted(game_dir.rglob("*.rpy")):
        rel = fpath.relative_to(game_dir)
        if "cache" in rel.parts or "saves" in rel.parts or "tl" in rel.parts:
            continue
        rpy_files.append(fpath)
    return rpy_files
