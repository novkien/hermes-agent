#!/usr/bin/env python3
"""Lock the shared test surface to one immutable upstream commit."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from ownership import (
    UPSTREAM_LOCK,
    UPSTREAM_TEST_MANIFEST,
    ensure_repository,
    git_stdout,
    is_shared_test_path,
    parse_tree,
    write_json,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--upstream-ref", required=True)
    args = parser.parse_args()

    repo = ensure_repository(args.repo)
    upstream_sha = git_stdout(repo, "rev-parse", f"{args.upstream_ref}^{{commit}}")
    tree = parse_tree(repo, upstream_sha)
    files = {
        path: row
        for path, row in sorted(tree.items())
        if is_shared_test_path(path)
    }
    now = datetime.now(timezone.utc).isoformat()
    lock = {
        "version": 1,
        "repository": "NousResearch/hermes-agent",
        "ref": "main",
        "sha": upstream_sha,
        "generated_at": now,
        "shared_test_manifest": UPSTREAM_TEST_MANIFEST.as_posix(),
    }
    manifest = {
        "version": 1,
        "upstream_sha": upstream_sha,
        "generated_at": now,
        "description": (
            "Exact Git blobs and modes for the shared upstream test surface. "
            "Fork-specific behavior lives under fork_tests/cases."
        ),
        "files": files,
    }
    write_json(repo / UPSTREAM_LOCK, lock)
    write_json(repo / UPSTREAM_TEST_MANIFEST, manifest)
    print(
        json.dumps(
            {"upstream_sha": upstream_sha, "shared_test_paths": len(files)},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
