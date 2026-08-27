#!/usr/bin/env python3
"""Run JavaScript fork cases with a temporary in-place overlay.

The regular workspace checks run first against pristine upstream tests.  This
command then overlays all fork case/support files, invokes each affected npm
workspace's ``test`` script for only the isolated case paths, and restores the
checkout byte-for-byte in ``finally``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from ownership import (
    FORK_MANIFEST,
    ensure_repository,
    git_stdout,
    read_json,
    safe_relative_path,
    sha256_bytes,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    repo = ensure_repository(args.repo)
    manifest = read_json(repo / FORK_MANIFEST)
    if not isinstance(manifest, dict):
        raise RuntimeError("fork_tests/manifest.json is missing")
    cases: list[dict[str, Any]] = manifest.get("cases", [])
    javascript = [row for row in cases if row.get("runner") == "javascript"]
    if not javascript:
        print(json.dumps({"status": "PASS", "javascript_case_files": 0}, indent=2))
        return 0
    status = git_stdout(repo, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise RuntimeError(f"JavaScript case overlay requires a clean checkout:\n{status}")

    originals: dict[Path, tuple[bool, bytes, int]] = {}
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    try:
        for row in cases:
            shared_path = safe_relative_path(str(row["shared_path"]))
            case_path = safe_relative_path(str(row["case_path"]))
            source = repo / case_path
            payload = source.read_bytes()
            if sha256_bytes(payload) != row.get("sha256"):
                raise RuntimeError(f"case checksum does not match manifest: {case_path}")
            target = repo / shared_path
            existed = target.exists()
            originals[target] = (
                existed,
                target.read_bytes() if existed else b"",
                target.stat().st_mode if existed else 0,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

        for row in javascript:
            workspace = row.get("javascript_workspace")
            if not isinstance(workspace, dict):
                raise RuntimeError(
                    f"JavaScript case has no workspace metadata: {row['shared_path']}"
                )
            root = str(workspace["root"])
            package = str(workspace["package"])
            shared = PurePosixPath(str(row["shared_path"]))
            relative = shared.as_posix()
            if root != ".":
                relative = shared.relative_to(PurePosixPath(root)).as_posix()
            groups[(root, package)].append(relative)

        for (root, package), paths in sorted(groups.items()):
            package_json = repo / ("package.json" if root == "." else f"{root}/package.json")
            package_data = json.loads(package_json.read_text(encoding="utf-8"))
            if "test" not in package_data.get("scripts", {}):
                raise RuntimeError(f"npm workspace {package!r} has no test script")
            command = ["npm"]
            if root != ".":
                command.extend(("--workspace", package))
            command.extend(("test", "--", *sorted(paths)))
            result = subprocess.run(command, cwd=repo, check=False)
            if result.returncode:
                return result.returncode
        return 0
    finally:
        for target, (existed, payload, mode) in originals.items():
            if existed:
                target.write_bytes(payload)
                os.chmod(target, mode)
            elif target.exists():
                target.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
