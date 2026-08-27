#!/usr/bin/env python3
"""Create a disposable worktree with fork regression cases overlaid."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ownership import (
    FORK_MANIFEST,
    ensure_repository,
    git,
    git_stdout,
    read_json,
    safe_relative_path,
    sha256_bytes,
)


def load_cases(repo: Path) -> list[dict[str, Any]]:
    manifest = read_json(repo / FORK_MANIFEST)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("cases"), list):
        raise RuntimeError("fork_tests/manifest.json is missing or invalid")
    return manifest["cases"]


def prepare_overlay(repo: Path, destination: Path, ref: str = "HEAD") -> dict[str, Any]:
    destination = destination.resolve()
    if destination.exists():
        raise RuntimeError(f"overlay destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--detach", str(destination), ref)
    copied: list[str] = []
    try:
        for row in load_cases(repo):
            shared_path = safe_relative_path(str(row["shared_path"]))
            case_path = safe_relative_path(str(row["case_path"]))
            source = repo / case_path
            if not source.is_file():
                raise RuntimeError(f"missing fork regression case: {case_path}")
            payload = source.read_bytes()
            if sha256_bytes(payload) != row.get("sha256"):
                raise RuntimeError(f"case checksum does not match manifest: {case_path}")
            target = destination / shared_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            copied.append(shared_path)
    except Exception:
        git(repo, "worktree", "remove", "--force", str(destination), check=False)
        raise
    metadata = {
        "base_ref": ref,
        "base_sha": git_stdout(repo, "rev-parse", f"{ref}^{{commit}}"),
        "destination": str(destination),
        "case_count": len(copied),
        "overlaid_shared_paths": copied,
    }
    (destination / ".fork-regression-overlay.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--ref", default="HEAD")
    args = parser.parse_args()
    repo = ensure_repository(args.repo)
    result = prepare_overlay(repo, args.destination, args.ref)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
