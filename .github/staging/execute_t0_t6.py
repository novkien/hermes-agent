#!/usr/bin/env python3
"""Build a clean T0-T6 fork-test-isolation branch and merge upstream/main.

This script is staging-only. It starts from the exact owner master SHA, creates
one isolation commit, then creates one ordinary two-parent upstream merge
commit. The staging workflow and temporary payload never enter the final
branch.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

REPO = Path.cwd().resolve()
REPORT_DIR = Path("reports/upstream-sync/2026-08-26")
SNAPSHOT_ROOT = Path("fork_tests/snapshots")
MANIFEST_PATH = Path("fork_tests/manifest.json")
NODEIDS_PATH = Path("fork_tests/nodeids-python.txt")
CONTRACTS_PATH = Path("fork/manifests/regression-contracts.json")
LOCK_PATH = Path("fork/upstream-lock.json")
UPSTREAM_TESTS_PATH = Path("fork/manifests/upstream-tests.json")


def run(
    args: Iterable[str],
    *,
    check: bool = True,
    cwd: Path | None = None,
    text: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[Any]:
    cmd = [str(x) for x in args]
    result = subprocess.run(
        cmd,
        cwd=str(cwd or REPO),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        env=env,
    )
    if check and result.returncode != 0:
        stdout = result.stdout if text else result.stdout.decode("utf-8", "replace")
        stderr = result.stderr if text else result.stderr.decode("utf-8", "replace")
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )
    return result


def git(*args: str, check: bool = True, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return run(("git", *args), check=check, text=text)


def git_stdout(*args: str) -> str:
    return git(*args).stdout.strip()


def ensure_repo() -> None:
    root = git_stdout("rev-parse", "--show-toplevel")
    if Path(root).resolve() != REPO:
        raise RuntimeError(f"wrong repository root: {root}")
    origin = git_stdout("remote", "get-url", "origin")
    if "novkien/hermes-agent" not in origin:
        raise RuntimeError(f"wrong origin: {origin}")


def safe_path(path: str) -> str:
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ValueError(f"unsafe repository path: {path!r}")
    return pure.as_posix()


def parse_ls_tree(ref: str) -> dict[str, dict[str, Any]]:
    raw = git("ls-tree", "-r", "-z", "-l", "--full-tree", ref, text=False).stdout
    tree: dict[str, dict[str, Any]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, path_raw = record.split(b"\t", 1)
        fields = meta.decode("ascii").split()
        mode, obj_type, sha = fields[:3]
        size = None if fields[3] == "-" else int(fields[3])
        path = path_raw.decode("utf-8", "surrogateescape")
        tree[path] = {"mode": mode, "type": obj_type, "sha": sha, "size": size}
    return tree


def changed_paths(base: str, head: str) -> set[str]:
    raw = git("diff", "--name-only", "-z", "--find-renames", base, head, text=False).stdout
    return {
        item.decode("utf-8", "surrogateescape")
        for item in raw.split(b"\0")
        if item
    }


def is_test_owned(path: str) -> bool:
    p = path.replace("\\", "/")
    if p.startswith(("fork_tests/", "reports/")):
        return False
    base = p.rsplit("/", 1)[-1]
    wrapped = f"/{p}"
    return any(
        (
            p.startswith("tests/"),
            p.startswith("tests-js/"),
            "/tests/" in wrapped,
            "/__tests__/" in wrapped,
            "/e2e/" in wrapped,
            base == "conftest.py",
            bool(re.fullmatch(r"test_.*\.py", base)),
            bool(re.fullmatch(r".*_test\.py", base)),
            ".test." in base,
            ".spec." in base,
            "__snapshots__" in p,
            "/fixtures/" in wrapped and (
                p.startswith(("tests/", "tests-js/")) or "/tests/" in wrapped
            ),
        )
    )


def is_upstream_test_namespace(path: str) -> bool:
    p = path.replace("\\", "/")
    if p.startswith("apps/mission-control/tests/"):
        return False
    if p.startswith("fork_tests/"):
        return False
    return is_test_owned(p)


def runnable_python(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return path.endswith(".py") and base != "conftest.py" and (
        base.startswith("test_") or base.endswith("_test.py")
    )


def runnable_javascript(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return any(token in base for token in (".test.", ".spec.")) and base.endswith(
        (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")
    )


def read_blob(ref: str, path: str) -> bytes:
    return git("show", f"{ref}:{safe_path(path)}", text=False).stdout


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def collect_python_tests(source: bytes) -> dict[str, str] | None:
    try:
        text = source.decode("utf-8")
        module = ast.parse(text)
    except (UnicodeDecodeError, SyntaxError):
        return None
    found: dict[str, str] = {}
    function_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in module.body:
        if isinstance(node, function_types) and node.name.startswith("test_"):
            found[node.name] = ast.dump(node, include_attributes=False)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for child in node.body:
                if isinstance(child, function_types) and child.name.startswith("test_"):
                    key = f"{node.name}::{child.name}"
                    found[key] = ast.dump(child, include_attributes=False)
    return found


def changed_python_nodeids(
    path: str,
    local_source: bytes,
    base_source: bytes | None,
) -> list[str]:
    if base_source is None:
        return [path]
    local_nodes = collect_python_tests(local_source)
    base_nodes = collect_python_tests(base_source)
    if local_nodes is None or base_nodes is None:
        return [path]
    changed = [
        f"{path}::{name}"
        for name, digest in sorted(local_nodes.items())
        if base_nodes.get(name) != digest
    ]
    return changed or [path]


def commit_context(base: str, owner: str, test_path: str) -> tuple[list[str], list[str]]:
    commits = [
        line.strip()
        for line in git(
            "log",
            "--format=%H",
            f"{base}..{owner}",
            "--",
            test_path,
        ).stdout.splitlines()
        if line.strip()
    ]
    sources: set[str] = set()
    for commit in commits[:40]:
        names = git("show", "--format=", "--name-only", commit).stdout.splitlines()
        for name in names:
            name = name.strip()
            if not name or is_test_owned(name):
                continue
            if name.startswith(("docs/", "website/", "contributors/")):
                continue
            sources.add(name)
            if len(sources) >= 120:
                break
        if len(sources) >= 120:
            break
    return commits, sorted(sources)


COMMON_MODULE = r'''#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parents[2]


def run(args: Iterable[str], *, check: bool = True, text: bool = True, cwd: Path | None = None):
    result = subprocess.run(
        [str(x) for x in args],
        cwd=str(cwd or REPO),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )
    if check and result.returncode:
        out = result.stdout if text else result.stdout.decode("utf-8", "replace")
        err = result.stderr if text else result.stderr.decode("utf-8", "replace")
        raise RuntimeError(f"command failed: {result.args}\n{out}\n{err}")
    return result


def git(*args: str, check: bool = True, text: bool = True):
    return run(("git", *args), check=check, text=text)


def read_tree(ref: str = "HEAD") -> dict[str, dict[str, Any]]:
    raw = git("ls-tree", "-r", "-z", "-l", "--full-tree", ref, text=False).stdout
    tree: dict[str, dict[str, Any]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, path_raw = record.split(b"\t", 1)
        fields = meta.decode("ascii").split()
        tree[path_raw.decode("utf-8", "surrogateescape")] = {
            "mode": fields[0],
            "type": fields[1],
            "sha": fields[2],
            "size": None if fields[3] == "-" else int(fields[3]),
        }
    return tree


def is_test_owned(path: str) -> bool:
    p = path.replace("\\", "/")
    if p.startswith(("fork_tests/", "reports/")):
        return False
    base = p.rsplit("/", 1)[-1]
    wrapped = f"/{p}"
    return any((
        p.startswith("tests/"),
        p.startswith("tests-js/"),
        "/tests/" in wrapped,
        "/__tests__/" in wrapped,
        "/e2e/" in wrapped,
        base == "conftest.py",
        bool(re.fullmatch(r"test_.*\.py", base)),
        bool(re.fullmatch(r".*_test\.py", base)),
        ".test." in base,
        ".spec." in base,
        "__snapshots__" in p,
        "/fixtures/" in wrapped and (
            p.startswith(("tests/", "tests-js/")) or "/tests/" in wrapped
        ),
    ))


def is_exempt_fork_test(path: str) -> bool:
    return path.startswith("apps/mission-control/tests/") or path.startswith("fork_tests/")


def load_json(path: str | Path) -> Any:
    return json.loads((REPO / path).read_text(encoding="utf-8"))
'''

VERIFY_SCRIPT = r'''#!/usr/bin/env python3
from __future__ import annotations

import json
import sys

from test_ownership import REPO, git, is_exempt_fork_test, is_test_owned, load_json, read_tree


def main() -> int:
    lock = load_json("fork/upstream-lock.json")
    manifest = load_json("fork/manifests/upstream-tests.json")
    expected = manifest["files"]
    current = read_tree("HEAD")
    failures: list[dict[str, str]] = []

    ancestor = git("merge-base", "--is-ancestor", lock["sha"], "HEAD", check=False)
    if ancestor.returncode != 0:
        failures.append({"path": "<git-graph>", "reason": "locked upstream SHA is not an ancestor of HEAD"})

    for path, row in expected.items():
        actual = current.get(path)
        if actual is None:
            failures.append({"path": path, "reason": "missing upstream-owned test path"})
            continue
        if actual["sha"] != row["sha"] or actual["mode"] != row["mode"]:
            failures.append({
                "path": path,
                "reason": "upstream-owned test blob or mode changed",
                "expected_sha": row["sha"],
                "actual_sha": actual["sha"],
            })

    extras = sorted(
        path
        for path in current
        if is_test_owned(path)
        and not is_exempt_fork_test(path)
        and path not in expected
    )
    for path in extras:
        failures.append({"path": path, "reason": "fork-only test remains in upstream test namespace"})

    result = {
        "status": "PASS" if not failures else "FAIL",
        "locked_upstream_sha": lock["sha"],
        "expected_test_paths": len(expected),
        "failure_count": len(failures),
        "failures": failures,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
'''

INVENTORY_SCRIPT = r'''#!/usr/bin/env python3
from __future__ import annotations

import json

from test_ownership import is_exempt_fork_test, is_test_owned, load_json, read_tree


def main() -> int:
    lock = load_json("fork/upstream-lock.json")
    manifest = load_json("fork/manifests/upstream-tests.json")
    snapshots = load_json("fork_tests/manifest.json")
    current = read_tree("HEAD")
    extras = sorted(
        p for p in current
        if is_test_owned(p) and not is_exempt_fork_test(p) and p not in manifest["files"]
    )
    result = {
        "locked_upstream_sha": lock["sha"],
        "upstream_owned_test_paths": len(manifest["files"]),
        "fork_snapshot_entries": len(snapshots["entries"]),
        "python_regression_nodeids": len(snapshots.get("python_nodeids", [])),
        "unexpected_upstream_namespace_paths": extras,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not extras else 1


if __name__ == "__main__":
    raise SystemExit(main())
'''

OVERLAY_SCRIPT = r'''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path, PurePosixPath

from test_ownership import REPO, git, load_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    destination = Path(args.destination).resolve()
    if destination.exists():
        shutil.rmtree(destination)
    git("worktree", "add", "--detach", str(destination), "HEAD")

    manifest = load_json("fork_tests/manifest.json")
    copied: list[str] = []
    for entry in manifest["entries"]:
        original = PurePosixPath(entry["original_path"])
        snapshot = PurePosixPath(entry["snapshot_path"])
        if original.is_absolute() or ".." in original.parts:
            raise ValueError(f"unsafe original path: {original}")
        source = REPO / snapshot
        target = destination / original
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(original.as_posix())

    metadata = {
        "head": git("rev-parse", "HEAD").stdout.strip(),
        "destination": str(destination),
        "overlay_count": len(copied),
        "paths": copied,
    }
    (destination / ".fork-test-overlay.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

REPORT_SCRIPT = r'''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from test_ownership import load_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    contracts = load_json("fork/manifests/regression-contracts.json")["contracts"]
    by_name: dict[str, list[dict]] = {}
    for contract in contracts:
        for nodeid in contract.get("nodeids", []):
            by_name.setdefault(nodeid.rsplit("::", 1)[-1], []).append(contract)

    failures: list[dict] = []
    junit = Path(args.junit)
    if junit.exists():
        root = ET.parse(junit).getroot()
        for case in root.iter("testcase"):
            if case.find("failure") is None and case.find("error") is None:
                continue
            name = case.attrib.get("name", "")
            matches = by_name.get(name, [])
            failures.append({
                "name": name,
                "classname": case.attrib.get("classname", ""),
                "classification": "FORK_REGRESSION",
                "contracts": [item["id"] for item in matches],
            })
    result = {
        "status": "PASS" if not failures else "FAIL",
        "classification": "NONE" if not failures else "FORK_REGRESSION",
        "failure_count": len(failures),
        "failures": failures,
    }
    Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

CLASSIFY_SCRIPT = r'''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from test_ownership import REPO, git


def environment_failure(text: str) -> bool:
    lower = text.lower()
    markers = (
        "address already in use",
        "no space left on device",
        "resource temporarily unavailable",
        "connection refused",
        "timed out",
        "modulenotfounderror",
    )
    return any(marker in lower for marker in markers)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nodeids-file", required=True)
    parser.add_argument("--upstream-ref", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    nodeids = [line.strip() for line in Path(args.nodeids_file).read_text().splitlines() if line.strip()]
    temp = Path(tempfile.mkdtemp(prefix="hermes-upstream-baseline-"))
    try:
        git("worktree", "add", "--detach", str(temp), args.upstream_ref)
        python = Path(os.environ.get("HERMES_TEST_PYTHON", REPO / ".venv/bin/python"))
        result = subprocess.run(
            [str(python), "-m", "pytest", "-q", *nodeids],
            cwd=temp,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if result.returncode == 0:
            classification = "INTEGRATION_REGRESSION"
        elif environment_failure(result.stdout):
            classification = "ENVIRONMENT_FAILURE"
        else:
            classification = "UPSTREAM_BASELINE_FAILURE"
        payload = {
            "classification": classification,
            "upstream_ref": args.upstream_ref,
            "nodeid_count": len(nodeids),
            "upstream_exit_code": result.returncode,
            "output_tail": result.stdout[-12000:],
        }
        Path(args.output).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    finally:
        git("worktree", "remove", "--force", str(temp), check=False)
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
'''

UPDATE_LOCK_SCRIPT = r'''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from test_ownership import git, is_test_owned, read_tree


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream-ref", required=True)
    parser.add_argument("--lock", default="fork/upstream-lock.json")
    parser.add_argument("--manifest", default="fork/manifests/upstream-tests.json")
    args = parser.parse_args()
    sha = git("rev-parse", args.upstream_ref).stdout.strip()
    tree = read_tree(sha)
    files = {p: row for p, row in sorted(tree.items()) if is_test_owned(p)}
    now = datetime.now(timezone.utc).isoformat()
    lock = {
        "repository": "NousResearch/hermes-agent",
        "ref": "main",
        "sha": sha,
        "generated_at": now,
        "test_manifest": args.manifest,
    }
    manifest = {"version": 1, "upstream_sha": sha, "generated_at": now, "files": files}
    Path(args.lock).parent.mkdir(parents=True, exist_ok=True)
    Path(args.manifest).parent.mkdir(parents=True, exist_ok=True)
    Path(args.lock).write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    Path(args.manifest).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"sha": sha, "test_paths": len(files)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

README_TEXT = '''# Fork Regression Tests

This directory is owned by `novkien/hermes-agent`, not by
`NousResearch/hermes-agent`.

- `snapshots/` preserves the exact owner-side test/support files that differed
  from the upstream merge base.
- `manifest.json` maps each snapshot to its original execution path.
- `nodeids-python.txt` contains only owner-added or owner-modified Python test
  nodes when AST extraction is possible.
- `.github/workflows/fork-regressions.yml` builds a disposable worktree,
  overlays these files at their original paths, and runs them independently.

The tracked upstream test tree remains byte-identical to the SHA locked in
`fork/upstream-lock.json`. Do not add owner-only tests to `tests/`, `tests-js/`,
or upstream colocated test directories.
'''

FORK_WORKFLOW = r'''name: Fork regressions

on:
  pull_request:
    branches: [master]
  push:
    branches: [master]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: fork-regressions-${{ github.ref }}
  cancel-in-progress: true

jobs:
  upstream-test-integrity:
    name: Upstream test integrity
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Verify locked upstream test ownership
        run: python scripts/fork_ci/verify_upstream_tests_untouched.py
      - name: Inventory fork regression ownership
        run: python scripts/fork_ci/inventory_tests.py

  fork-python-regressions:
    name: Fork Python regression contracts
    needs: upstream-test-integrity
    runs-on: ubuntu-latest
    timeout-minutes: 90
    env:
      HERMES_HOME: ${{ runner.temp }}/hermes-home
      HERMES_TEST_WORKERS: "8"
      OPENAI_API_KEY: ""
      ANTHROPIC_API_KEY: ""
      OPENROUTER_API_KEY: ""
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - uses: astral-sh/setup-uv@v6
      - name: Install locked Python environment
        run: uv sync --frozen
      - name: Prepare disposable fork-test overlay
        run: python scripts/fork_ci/prepare_overlay.py --destination "${RUNNER_TEMP}/fork-overlay"
      - name: Run owner regression node IDs
        run: |
          python - <<'PY'
          from pathlib import Path
          import os
          import subprocess

          repo = Path(os.environ["GITHUB_WORKSPACE"])
          overlay = Path(os.environ["RUNNER_TEMP"]) / "fork-overlay"
          nodeids = [
              line.strip()
              for line in (repo / "fork_tests/nodeids-python.txt").read_text().splitlines()
              if line.strip()
          ]
          report = repo / "fork-regressions-python.xml"
          if not nodeids:
              report.write_text("<testsuite tests='0' failures='0' errors='0' />\n")
              raise SystemExit(0)
          python = repo / ".venv/bin/python"
          command = [str(python), "-m", "pytest", "-q", *nodeids, f"--junitxml={report}"]
          raise SystemExit(subprocess.run(command, cwd=overlay).returncode)
          PY
      - name: Classify fork regression failures
        if: always()
        run: |
          python scripts/fork_ci/report_contract_failures.py \
            --junit fork-regressions-python.xml \
            --output fork-regressions-classification.json
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: fork-python-regression-results
          path: |
            fork-regressions-python.xml
            fork-regressions-classification.json
          if-no-files-found: error

  mission-control-contracts:
    name: Mission Control fork contracts
    needs: upstream-test-integrity
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Python contracts
        run: |
          python apps/mission-control/tests/test_runtime_contracts.py
          python apps/mission-control/tests/test_static_repair_surface.py
      - name: Frontend contracts
        run: |
          node apps/mission-control/tests/frontend_contracts.mjs
          node apps/mission-control/tests/skills_surface.mjs
      - name: Compile Mission Control
        run: python -m compileall -q apps/mission-control/agent_mission_control

  fork-regression-gate:
    name: All fork contracts pass
    if: always()
    needs:
      - upstream-test-integrity
      - fork-python-regressions
      - mission-control-contracts
    runs-on: ubuntu-latest
    steps:
      - name: Enforce all results
        env:
          INTEGRITY: ${{ needs.upstream-test-integrity.result }}
          PYTHON: ${{ needs.fork-python-regressions.result }}
          MISSION_CONTROL: ${{ needs.mission-control-contracts.result }}
        run: |
          test "$INTEGRITY" = success
          test "$PYTHON" = success
          test "$MISSION_CONTROL" = success
'''


def chunked(items: list[str], size: int = 80) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def stage_paths(paths: Iterable[str]) -> None:
    existing = [path for path in paths if (REPO / path).exists()]
    for batch in chunked(existing):
        git("add", "--", *batch)


def write_final_support_files() -> list[str]:
    files = {
        "scripts/fork_ci/test_ownership.py": COMMON_MODULE,
        "scripts/fork_ci/verify_upstream_tests_untouched.py": VERIFY_SCRIPT,
        "scripts/fork_ci/inventory_tests.py": INVENTORY_SCRIPT,
        "scripts/fork_ci/prepare_overlay.py": OVERLAY_SCRIPT,
        "scripts/fork_ci/report_contract_failures.py": REPORT_SCRIPT,
        "scripts/fork_ci/classify_upstream_failures.py": CLASSIFY_SCRIPT,
        "scripts/fork_ci/update_upstream_lock.py": UPDATE_LOCK_SCRIPT,
        "fork_tests/README.md": README_TEXT,
        ".github/workflows/fork-regressions.yml": FORK_WORKFLOW,
    }
    for path, content in files.items():
        write_text(Path(path), content.rstrip() + "\n")
    return sorted(files)


def make_upstream_manifest(upstream_sha: str) -> dict[str, Any]:
    tree = parse_ls_tree(upstream_sha)
    files = {
        path: row
        for path, row in sorted(tree.items())
        if is_test_owned(path)
    }
    return {
        "version": 1,
        "upstream_sha": upstream_sha,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }


def restore_upstream_tests(upstream_sha: str, manifest: dict[str, Any]) -> None:
    paths = list(manifest["files"])
    for batch in chunked(paths, 60):
        git("checkout", upstream_sha, "--", *batch)


def build(args: argparse.Namespace) -> dict[str, Any]:
    ensure_repo()
    owner_base = args.owner_base
    final_branch = args.final_branch
    upstream_remote = "hermes-upstream"

    git("remote", "remove", upstream_remote, check=False)
    git("remote", "add", upstream_remote, args.upstream_url)
    git("fetch", "--no-tags", upstream_remote, "main")
    upstream_initial = git_stdout("rev-parse", f"refs/remotes/{upstream_remote}/main")
    merge_base = git_stdout("merge-base", owner_base, upstream_initial)

    remote_branch = git(
        "ls-remote",
        "--heads",
        "origin",
        f"refs/heads/{final_branch}",
    ).stdout.strip()
    if remote_branch:
        raise RuntimeError(f"final branch already exists remotely: {final_branch}")

    base_tree = parse_ls_tree(merge_base)
    local_tree = parse_ls_tree(owner_base)
    local_changed = changed_paths(merge_base, owner_base)
    modified_upstream_tests = sorted(
        path
        for path in local_changed
        if path in base_tree and is_test_owned(path)
    )
    local_only_tests = sorted(
        path
        for path in local_tree
        if path not in base_tree
        and is_upstream_test_namespace(path)
        and not path.startswith("apps/mission-control/tests/")
    )
    already_isolated = sorted(
        path
        for path in local_tree
        if path.startswith("apps/mission-control/tests/") and is_test_owned(path)
    )

    git("switch", "--detach", owner_base)
    git("switch", "-c", final_branch)

    if SNAPSHOT_ROOT.exists():
        shutil.rmtree(SNAPSHOT_ROOT)
    entries: list[dict[str, Any]] = []
    all_nodeids: list[str] = []
    contract_rows: list[dict[str, Any]] = []
    candidate_paths = sorted(set(modified_upstream_tests) | set(local_only_tests))

    for index, original in enumerate(candidate_paths, start=1):
        original = safe_path(original)
        local_row = local_tree.get(original)
        if local_row is None or local_row["type"] != "blob":
            continue
        local_source = read_blob(owner_base, original)
        snapshot_path = (SNAPSHOT_ROOT / original).as_posix()
        write_bytes(Path(snapshot_path), local_source)
        base_source = None
        if original in base_tree and base_tree[original]["type"] == "blob":
            base_source = read_blob(merge_base, original)

        nodeids: list[str] = []
        runner = "support"
        if runnable_python(original):
            runner = "python"
            nodeids = changed_python_nodeids(original, local_source, base_source)
            all_nodeids.extend(nodeids)
        elif runnable_javascript(original):
            runner = "javascript"
            nodeids = [original]

        commits, source_paths = commit_context(merge_base, owner_base, original)
        kind = "modified-upstream-test" if original in base_tree else "fork-only-test"
        entry = {
            "original_path": original,
            "snapshot_path": snapshot_path,
            "kind": kind,
            "runner": runner,
            "nodeids": nodeids,
            "snapshot_sha256": sha256_bytes(local_source),
            "local_commits": commits,
            "source_paths": source_paths,
        }
        entries.append(entry)
        contract_rows.append(
            {
                "id": f"HERMES-FORK-REG-{index:04d}",
                "name": f"Owner regression contract for {original}",
                "original_test_path": original,
                "snapshot_path": snapshot_path,
                "nodeids": nodeids,
                "source_paths": source_paths,
                "origin_commits": commits,
                "classification_on_failure": "FORK_REGRESSION",
            }
        )

    for path in modified_upstream_tests:
        if path in base_tree:
            git("checkout", merge_base, "--", path)
    for path in local_only_tests:
        if (REPO / path).exists():
            git("rm", "-f", "--", path)

    support_files = write_final_support_files()
    manifest = {
        "version": 1,
        "owner_base_sha": owner_base,
        "merge_base_sha": merge_base,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
        "python_nodeids": sorted(set(all_nodeids)),
        "already_isolated_fork_tests": already_isolated,
    }
    write_text(MANIFEST_PATH, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    write_text(NODEIDS_PATH, "\n".join(sorted(set(all_nodeids))) + ("\n" if all_nodeids else ""))
    contracts = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "contracts": contract_rows,
        "already_isolated_subsystems": [
            {
                "id": "HERMES-FORK-MISSION-CONTROL",
                "root": "apps/mission-control/tests",
                "test_path_count": len(already_isolated),
                "classification_on_failure": "FORK_REGRESSION",
            }
        ],
    }
    write_text(CONTRACTS_PATH, json.dumps(contracts, indent=2, sort_keys=True) + "\n")

    premerge_report = {
        "phase": "T0-T4",
        "owner_base_sha": owner_base,
        "upstream_initial_sha": upstream_initial,
        "merge_base_sha": merge_base,
        "modified_upstream_test_paths": modified_upstream_tests,
        "local_only_tests_in_upstream_namespace": local_only_tests,
        "already_isolated_fork_test_count": len(already_isolated),
        "fork_snapshot_count": len(entries),
        "python_regression_nodeid_count": len(set(all_nodeids)),
    }
    write_text(REPORT_DIR / "t0-t4-inventory.json", json.dumps(premerge_report, indent=2, sort_keys=True) + "\n")

    generated_paths = support_files + [
        MANIFEST_PATH.as_posix(),
        NODEIDS_PATH.as_posix(),
        CONTRACTS_PATH.as_posix(),
        (REPORT_DIR / "t0-t4-inventory.json").as_posix(),
    ]
    generated_paths.extend(entry["snapshot_path"] for entry in entries)
    stage_paths(generated_paths)
    stage_paths(modified_upstream_tests)
    git("diff", "--cached", "--check")
    git("commit", "-m", "test(fork): isolate owner regressions from upstream tests")
    isolation_sha = git_stdout("rev-parse", "HEAD")

    git("fetch", "--no-tags", upstream_remote, "main")
    upstream_sha = git_stdout("rev-parse", f"refs/remotes/{upstream_remote}/main")
    live_merge_base = git_stdout("merge-base", isolation_sha, upstream_sha)
    if live_merge_base != merge_base:
        raise RuntimeError(
            f"upstream merge base moved unexpectedly: {merge_base} -> {live_merge_base}"
        )

    simulation = git(
        "merge-tree",
        "--write-tree",
        "--name-only",
        isolation_sha,
        upstream_sha,
        check=False,
    )
    simulation_lines = [line for line in simulation.stdout.splitlines() if line.strip()]
    if simulation.returncode != 0:
        raise RuntimeError(
            "T6 merge simulation found conflicts:\n"
            + simulation.stdout
            + "\n"
            + simulation.stderr
        )

    git("merge", "--no-ff", "--no-commit", upstream_sha)
    unresolved = [
        line.strip()
        for line in git("diff", "--name-only", "--diff-filter=U").stdout.splitlines()
        if line.strip()
    ]
    if unresolved:
        raise RuntimeError(f"unexpected merge conflicts after clean simulation: {unresolved}")

    upstream_manifest = make_upstream_manifest(upstream_sha)
    restore_upstream_tests(upstream_sha, upstream_manifest)
    now = datetime.now(timezone.utc).isoformat()
    lock = {
        "repository": "NousResearch/hermes-agent",
        "ref": "main",
        "sha": upstream_sha,
        "owner_base_sha": owner_base,
        "merge_base_sha": merge_base,
        "generated_at": now,
        "test_manifest": UPSTREAM_TESTS_PATH.as_posix(),
    }
    upstream_manifest["generated_at"] = now
    write_text(LOCK_PATH, json.dumps(lock, indent=2, sort_keys=True) + "\n")
    write_text(UPSTREAM_TESTS_PATH, json.dumps(upstream_manifest, indent=2, sort_keys=True) + "\n")

    current_tree = parse_ls_tree(upstream_sha)
    test_conflicts = [path for path in unresolved if is_test_owned(path)]
    t6_report = {
        "phase": "T6",
        "status": "PASS",
        "owner_base_sha": owner_base,
        "isolation_parent_sha": isolation_sha,
        "upstream_parent_sha": upstream_sha,
        "merge_base_sha": merge_base,
        "upstream_commit_count": int(git_stdout("rev-list", "--count", f"{merge_base}..{upstream_sha}")),
        "owner_commit_count": int(git_stdout("rev-list", "--count", f"{merge_base}..{owner_base}")),
        "merge_simulation_exit_code": simulation.returncode,
        "merge_simulation_output": simulation_lines,
        "unresolved_conflicts": unresolved,
        "test_path_conflicts": test_conflicts,
        "upstream_owned_test_path_count": len(upstream_manifest["files"]),
        "fork_snapshot_count": len(entries),
        "python_regression_nodeid_count": len(set(all_nodeids)),
        "upstream_tree_path_count": len(current_tree),
    }
    write_text(REPORT_DIR / "t6-merge-validation.json", json.dumps(t6_report, indent=2, sort_keys=True) + "\n")
    stage_paths(
        [
            LOCK_PATH.as_posix(),
            UPSTREAM_TESTS_PATH.as_posix(),
            (REPORT_DIR / "t6-merge-validation.json").as_posix(),
        ]
    )
    git("diff", "--cached", "--check")
    git("commit", "-m", "merge: sync NousResearch/main with isolated fork regressions")
    merge_sha = git_stdout("rev-parse", "HEAD")
    parents = git_stdout("show", "-s", "--format=%P", "HEAD").split()
    if parents != [isolation_sha, upstream_sha]:
        raise RuntimeError(f"unexpected merge parents: {parents}")

    verification = run((sys.executable, "scripts/fork_ci/verify_upstream_tests_untouched.py"))
    status = git("status", "--porcelain", "--untracked-files=all").stdout.strip()
    if status:
        raise RuntimeError(f"candidate worktree is not clean after build:\n{status}")

    result = {
        "final_branch": final_branch,
        "owner_base_sha": owner_base,
        "merge_base_sha": merge_base,
        "upstream_sha": upstream_sha,
        "isolation_sha": isolation_sha,
        "merge_sha": merge_sha,
        "merge_parents": parents,
        "modified_upstream_test_count": len(modified_upstream_tests),
        "local_only_test_count": len(local_only_tests),
        "already_isolated_test_count": len(already_isolated),
        "fork_snapshot_count": len(entries),
        "python_nodeid_count": len(set(all_nodeids)),
        "integrity_output": verification.stdout,
    }
    write_text(REPORT_DIR / "candidate-build-result.json", json.dumps(result, indent=2, sort_keys=True) + "\n")
    # The result file is intentionally an Actions artifact, not a third source commit.
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-base", required=True)
    parser.add_argument("--upstream-url", required=True)
    parser.add_argument("--final-branch", required=True)
    parser.add_argument("--github-output")
    args = parser.parse_args()
    result = build(args)
    print(json.dumps(result, indent=2, sort_keys=True))
    output = args.github_output or os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            for key in ("final_branch", "upstream_sha", "isolation_sha", "merge_sha"):
                handle.write(f"{key}={result[key]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
