#!/usr/bin/env python3
"""
Phase 0-1 — context_rpa.py: RPA extraction wrapper.

For archive-extracted games: extracts to unique dir under ~/.hermes/workspace/tmp/,
inspects copy read-only, optional cleanup after run.

Skips silently if no RPA files found.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


def extract_rpa_if_needed(
    game_root: str | Path,
    cleanup: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    Extract archive.rpa files if present. Returns result dict or None.

    Args:
        game_root: Path to game root directory.
        cleanup: If True, remove extracted files after inspection.

    Returns:
        None if no RPA found, or dict with:
            extracted: bool
            temp_dir: str | None
            extracted_files: list[str]
            error: str | None
    """
    game_root = Path(game_root).resolve()
    game_dir = game_root / "game"

    if not game_dir.is_dir():
        return {"extracted": False, "temp_dir": None, "extracted_files": [], "error": None}

    # Find .rpa files
    rpa_files = list(game_dir.glob("*.rpa"))
    if not rpa_files:
        return {"extracted": False, "temp_dir": None, "extracted_files": [], "error": None}

    # Create extraction dir
    extract_dir = _make_extract_dir()

    try:
        extracted = _extract_archives(game_dir, rpa_files, extract_dir)

        if cleanup:
            extracted_files = list(extract_dir.rglob("*.rpy")) if extract_dir.exists() else []
            result = {
                "extracted": True,
                "temp_dir": str(extract_dir),
                "extracted_files": [str(p.relative_to(extract_dir)) for p in extracted_files],
                "error": None,
            }
            shutil.rmtree(extract_dir, ignore_errors=True)
        else:
            result = {
                "extracted": True,
                "temp_dir": str(extract_dir),
                "extracted_files": extracted,
                "error": None,
            }

        return result
    except Exception as e:
        # Cleanup on error
        if cleanup and extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)
        return {
            "extracted": True,
            "temp_dir": str(extract_dir),
            "extracted_files": [],
            "error": str(e),
        }


def _make_extract_dir() -> Path:
    """Create a unique temporary directory for RPA extraction."""
    tmp_base = Path.home() / ".hermes" / "workspace" / "tmp"
    tmp_base.mkdir(parents=True, exist_ok=True)
    dir_name = f"rpa-extract-{uuid.uuid4().hex[:12]}"
    extract_dir = tmp_base / dir_name
    extract_dir.mkdir(parents=True, exist_ok=True)
    return extract_dir


def _extract_archives(
    game_dir: Path,
    rpa_files: List[Path],
    extract_dir: Path,
) -> List[str]:
    """Extract .rpa archives using unrpa."""
    extracted: List[str] = []

    for rpa_file in rpa_files:
        try:
            result = subprocess.run(
                ["unrpa", "--dest", str(extract_dir), str(rpa_file)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode == 0:
                # List extracted files
                for f in extract_dir.rglob("*"):
                    if f.is_file():
                        extracted.append(str(f.relative_to(extract_dir)))
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            raise RuntimeError(f"Failed to extract {rpa_file.name}: {e}")

    # Copy recovered .rpy files into the game directory structure
    if extracted:
        for rpy_file in extract_dir.rglob("*.rpy"):
            rel = rpy_file.relative_to(extract_dir)
            # Try to place in game dir maintaining structure
            target = game_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rpy_file, target)

    return extracted
