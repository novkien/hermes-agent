#!/usr/bin/env python3
"""Move fork-specific shared-test deltas into executable case files.

Run this command on the owner side *before* merging a frozen upstream commit.
It copies every owner-modified test/support file from the shared upstream test
surface into ``fork_tests/cases/<shared-path>``.  The original shared path is
then restored to the common-base blob (or removed when it was owner-only), so
the subsequent upstream merge can update that surface without test conflicts.

The default mode is a read-only plan.  ``--apply`` requires a clean worktree
and writes the cases plus ``fork_tests/manifest.json``.  The command never
commits, merges, pushes, or rewrites history.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ownership import (
    FORK_CASE_ROOT,
    FORK_MANIFEST,
    changed_paths,
    ensure_repository,
    git,
    git_stdout,
    is_shared_test_path,
    parse_tree,
    python_case_nodeids,
    read_blob,
    read_json,
    runner_for_path,
    sha256_bytes,
    write_json,
)


def _javascript_workspace(
    repo: Path,
    owner_ref: str,
    owner_tree: dict[str, dict[str, Any]],
    shared_path: str,
) -> tuple[str, str] | None:
    parent = PurePosixPath(shared_path).parent
    for candidate in (parent, *parent.parents):
        root = "" if candidate == PurePosixPath(".") else candidate.as_posix()
        package_path = f"{root}/package.json" if root else "package.json"
        row = owner_tree.get(package_path)
        if not row or row["type"] != "blob":
            continue
        package = json.loads(read_blob(repo, owner_ref, package_path))
        name = package.get("name")
        if isinstance(name, str) and name:
            return root or ".", name
    return None


def _existing_cases(repo: Path) -> dict[str, dict[str, Any]]:
    manifest = read_json(repo / FORK_MANIFEST, default={}) or {}
    rows = manifest.get("cases", [])
    if not isinstance(rows, list):
        raise RuntimeError("fork_tests/manifest.json: cases must be a list")
    by_shared_path: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("shared_path"), str):
            raise RuntimeError("fork_tests/manifest.json contains an invalid case")
        case_path = row.get("case_path")
        if not isinstance(case_path, str) or not (repo / case_path).is_file():
            raise RuntimeError(f"missing fork regression case: {case_path!r}")
        by_shared_path[row["shared_path"]] = row
    return by_shared_path


def build_plan(repo: Path, owner_ref: str, upstream_ref: str) -> dict[str, Any]:
    owner_sha = git_stdout(repo, "rev-parse", f"{owner_ref}^{{commit}}")
    upstream_sha = git_stdout(repo, "rev-parse", f"{upstream_ref}^{{commit}}")
    common_base = git_stdout(repo, "merge-base", owner_sha, upstream_sha)
    if not common_base:
        raise RuntimeError("owner and upstream refs have no common ancestor")
    if git(
        repo,
        "merge-base",
        "--is-ancestor",
        owner_sha,
        "HEAD",
        check=False,
    ).returncode:
        raise RuntimeError(f"owner ref {owner_sha} is not an ancestor of HEAD")

    base_tree = parse_tree(repo, common_base)
    owner_tree = parse_tree(repo, owner_sha)
    upstream_tree = parse_tree(repo, upstream_sha)
    owner_delta = changed_paths(repo, common_base, owner_sha)

    modified_shared = sorted(
        path
        for path in owner_delta
        if path in base_tree and is_shared_test_path(path)
    )
    owner_only_shared = sorted(
        path
        for path, row in owner_tree.items()
        if path not in base_tree
        and row["type"] == "blob"
        and is_shared_test_path(path)
    )

    existing = _existing_cases(repo)
    generated: list[dict[str, Any]] = []
    deleted_owner_tests: list[str] = []
    for shared_path in sorted(set(modified_shared) | set(owner_only_shared)):
        owner_row = owner_tree.get(shared_path)
        if not owner_row or owner_row["type"] != "blob":
            deleted_owner_tests.append(shared_path)
            continue
        source = read_blob(repo, owner_sha, shared_path)
        base_source = None
        if shared_path in base_tree and base_tree[shared_path]["type"] == "blob":
            base_source = read_blob(repo, common_base, shared_path)
        upstream_source = None
        if shared_path in upstream_tree:
            upstream_source = read_blob(repo, upstream_sha, shared_path)
        runner = runner_for_path(shared_path)
        nodeids: list[str] = []
        replaced_upstream_nodeids: list[str] = []
        workspace: dict[str, str] | None = None
        if runner == "python":
            nodeids, replaced_upstream_nodeids = python_case_nodeids(
                shared_path, source, base_source, upstream_source
            )
        elif runner == "javascript":
            nodeids = [shared_path]
            if upstream_source is not None:
                replaced_upstream_nodeids = [shared_path]
            resolved = _javascript_workspace(repo, owner_sha, owner_tree, shared_path)
            if resolved is None:
                raise RuntimeError(
                    f"cannot locate package.json for JavaScript case {shared_path}"
                )
            workspace = {"root": resolved[0], "package": resolved[1]}

        row: dict[str, Any] = {
            "shared_path": shared_path,
            "case_path": (FORK_CASE_ROOT / shared_path).as_posix(),
            "kind": (
                "owner-modified-shared-test"
                if shared_path in base_tree
                else "owner-only-shared-test"
            ),
            "runner": runner,
            "nodeids": nodeids,
            "replaced_upstream_nodeids": replaced_upstream_nodeids,
            "sha256": sha256_bytes(source),
            "lineage": {
                "owner_sha": owner_sha,
                "common_base_sha": common_base,
                "upstream_target_sha": upstream_sha,
            },
        }
        if workspace:
            row["javascript_workspace"] = workspace
        generated.append(row)
        existing[shared_path] = row

    return {
        "owner_ref": owner_ref,
        "owner_sha": owner_sha,
        "upstream_ref": upstream_ref,
        "upstream_sha": upstream_sha,
        "common_base_sha": common_base,
        "modified_shared_paths": modified_shared,
        "owner_only_shared_paths": owner_only_shared,
        "owner_deleted_shared_paths": deleted_owner_tests,
        "new_or_updated_cases": generated,
        "all_cases": [existing[path] for path in sorted(existing)],
    }


def apply_plan(repo: Path, plan: dict[str, Any]) -> None:
    status = git_stdout(repo, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise RuntimeError(f"--apply requires a clean worktree:\n{status}")

    owner_sha = plan["owner_sha"]
    common_base = plan["common_base_sha"]
    for row in plan["new_or_updated_cases"]:
        case_path = repo / row["case_path"]
        case_path.parent.mkdir(parents=True, exist_ok=True)
        case_path.write_bytes(read_blob(repo, owner_sha, row["shared_path"]))

    deleted_shared_paths = set(plan["owner_deleted_shared_paths"])
    for shared_path in plan["modified_shared_paths"]:
        if shared_path in deleted_shared_paths:
            continue
        target = repo / shared_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(read_blob(repo, common_base, shared_path))

    for shared_path in plan["owner_only_shared_paths"]:
        target = repo / shared_path
        if target.exists():
            if not target.is_file() and not target.is_symlink():
                raise RuntimeError(f"refusing to remove non-file test path: {target}")
            target.unlink()

    manifest = {
        "version": 1,
        "case_root": FORK_CASE_ROOT.as_posix(),
        "description": (
            "Maintained fork regression cases isolated from the shared upstream "
            "test surface. These are executable sources, not snapshots."
        ),
        "last_isolation": {
            "owner_sha": owner_sha,
            "upstream_target_sha": plan["upstream_sha"],
            "common_base_sha": common_base,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "cases": plan["all_cases"],
    }
    write_json(repo / FORK_MANIFEST, manifest)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--owner-ref", required=True)
    parser.add_argument("--upstream-ref", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    repo = ensure_repository(args.repo)
    plan = build_plan(repo, args.owner_ref, args.upstream_ref)
    if args.apply:
        apply_plan(repo, plan)
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
