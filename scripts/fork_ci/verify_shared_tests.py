#!/usr/bin/env python3
"""Verify that shared test paths exactly match the locked upstream commit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ownership import (
    UPSTREAM_LOCK,
    UPSTREAM_TEST_MANIFEST,
    ensure_repository,
    git,
    is_shared_test_path,
    parse_tree,
    read_json,
)


def verify(repo: Path, ref: str) -> dict[str, Any]:
    lock = read_json(repo / UPSTREAM_LOCK)
    manifest = read_json(repo / UPSTREAM_TEST_MANIFEST)
    if not isinstance(lock, dict) or not isinstance(manifest, dict):
        raise RuntimeError("run update_upstream_lock.py before verification")
    expected = manifest.get("files")
    if not isinstance(expected, dict):
        raise RuntimeError("upstream shared-test manifest has no files mapping")
    if manifest.get("upstream_sha") != lock.get("sha"):
        raise RuntimeError("upstream lock and shared-test manifest disagree")

    failures: list[dict[str, str]] = []
    if git(
        repo,
        "merge-base",
        "--is-ancestor",
        str(lock["sha"]),
        ref,
        check=False,
    ).returncode:
        failures.append(
            {
                "path": "<git-graph>",
                "reason": "locked upstream SHA is not an ancestor of the checked ref",
            }
        )

    current = parse_tree(repo, ref)
    for path, expected_row in expected.items():
        actual = current.get(path)
        if actual is None:
            failures.append({"path": path, "reason": "missing shared upstream test"})
            continue
        if (
            actual.get("sha") != expected_row.get("sha")
            or actual.get("mode") != expected_row.get("mode")
        ):
            failures.append(
                {
                    "path": path,
                    "reason": (
                        "shared test differs from upstream; move the fork delta "
                        "to fork_tests/cases"
                    ),
                    "expected_sha": str(expected_row.get("sha")),
                    "actual_sha": str(actual.get("sha")),
                }
            )

    for path in sorted(current):
        if is_shared_test_path(path) and path not in expected:
            failures.append(
                {
                    "path": path,
                    "reason": (
                        "fork-only test is in the shared upstream test surface; "
                        "move it to fork_tests/cases"
                    ),
                }
            )

    return {
        "status": "PASS" if not failures else "FAIL",
        "checked_ref": ref,
        "locked_upstream_sha": lock["sha"],
        "shared_test_paths": len(expected),
        "failure_count": len(failures),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--ref", default="HEAD")
    args = parser.parse_args()
    repo = ensure_repository(args.repo)
    result = verify(repo, args.ref)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
