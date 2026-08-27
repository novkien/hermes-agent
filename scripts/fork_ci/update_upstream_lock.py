#!/usr/bin/env python3
"""Lock the shared test surface to one immutable upstream commit."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from ownership import (
    FORK_MANIFEST,
    UPSTREAM_LOCK,
    UPSTREAM_TEST_MANIFEST,
    ensure_repository,
    git_stdout,
    is_shared_test_path,
    parse_tree,
    python_case_nodeids,
    python_source_nodeids,
    read_blob,
    read_json,
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
    fork_manifest = read_json(repo / FORK_MANIFEST)
    if isinstance(fork_manifest, dict) and isinstance(fork_manifest.get("cases"), list):
        fallback_lineage = fork_manifest.get("last_isolation", {})
        lineage_trees: dict[str, dict] = {}
        for row in fork_manifest["cases"]:
            if not isinstance(row, dict) or row.get("runner") != "python":
                continue
            shared_path = str(row["shared_path"])
            lineage = row.get("lineage") or fallback_lineage
            owner_sha = str(lineage["owner_sha"])
            common_base_sha = str(lineage["common_base_sha"])
            if common_base_sha not in lineage_trees:
                lineage_trees[common_base_sha] = parse_tree(repo, common_base_sha)
            base_tree = lineage_trees[common_base_sha]
            case_source = (repo / str(row["case_path"])).read_bytes()
            base_source = (
                read_blob(repo, common_base_sha, shared_path)
                if shared_path in base_tree
                else None
            )
            upstream_source = (
                read_blob(repo, upstream_sha, shared_path)
                if shared_path in tree
                else None
            )
            selected, replaced = python_case_nodeids(
                shared_path, case_source, base_source, upstream_source
            )
            row["nodeids"] = sorted(set(selected))
            if upstream_source is not None:
                upstream_nodeids = python_source_nodeids(shared_path, upstream_source)
                replaced.extend(
                    nodeid
                    for nodeid in row.get("semantic_nodeids", [])
                    if nodeid in upstream_nodeids
                )
            row["replaced_upstream_nodeids"] = sorted(set(replaced))
            row["lineage"] = {
                "owner_sha": owner_sha,
                "common_base_sha": common_base_sha,
                "upstream_target_sha": upstream_sha,
            }
        write_json(repo / FORK_MANIFEST, fork_manifest)
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
