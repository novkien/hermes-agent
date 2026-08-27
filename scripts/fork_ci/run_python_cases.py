#!/usr/bin/env python3
"""Run Python fork regression cases at their original shared paths."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from ownership import FORK_MANIFEST, ensure_repository, git, read_json
from prepare_overlay import prepare_overlay


def selected_python_nodeids(manifest: dict) -> list[str]:
    """Return generated and explicitly preserved Python regression nodes."""
    return sorted(
        {
            str(nodeid)
            for row in manifest.get("cases", [])
            if row.get("runner") == "python"
            for field in ("nodeids", "semantic_nodeids")
            for nodeid in row.get(field, [])
        }
    )


def python_command_path(repo: Path, requested: Path) -> Path:
    """Make the interpreter path absolute without dereferencing venv symlinks."""
    return requested if requested.is_absolute() else repo / requested


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--python", type=Path, default=Path(__import__("sys").executable))
    parser.add_argument("--junit")
    args = parser.parse_args()
    repo = ensure_repository(args.repo)
    manifest = read_json(repo / FORK_MANIFEST)
    if not isinstance(manifest, dict):
        raise RuntimeError("fork_tests/manifest.json is missing")
    nodeids = selected_python_nodeids(manifest)
    if not nodeids:
        print(json.dumps({"status": "PASS", "python_case_nodeids": 0}, indent=2))
        return 0

    temporary_root = Path(tempfile.mkdtemp(prefix="hermes-fork-regressions-"))
    overlay = temporary_root / "worktree"
    try:
        prepare_overlay(repo, overlay)
        python = python_command_path(repo, args.python)
        command = [str(python), "-m", "pytest", "-q", *nodeids]
        if args.junit:
            command.append(f"--junitxml={Path(args.junit).resolve()}")
        result = subprocess.run(command, cwd=overlay, check=False)
        return result.returncode
    finally:
        git(repo, "worktree", "remove", "--force", str(overlay), check=False)
        shutil.rmtree(temporary_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
