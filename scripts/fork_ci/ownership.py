#!/usr/bin/env python3
"""Shared definitions for fork regression isolation.

The owner fork deliberately keeps tests in two different surfaces:

* the *shared upstream test surface* keeps the exact blobs from the locked
  ``NousResearch/hermes-agent`` commit; and
* ``fork_tests/cases`` contains maintained, executable fork regression cases.

"Ownership" here describes repository layout, not authorship.  A test can be
written by the fork owner and still live in the shared surface temporarily;
the isolation command moves that fork delta into a regression case before an
upstream merge.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

FORK_CASE_ROOT = Path("fork_tests/cases")
FORK_MANIFEST = Path("fork_tests/manifest.json")
UPSTREAM_LOCK = Path("fork/upstream-lock.json")
UPSTREAM_TEST_MANIFEST = Path("fork/manifests/upstream-tests.json")

_FORK_ONLY_PREFIXES = (
    "apps/mission-control/tests/",
    "fork_tests/",
)
_JAVASCRIPT_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")


def run(
    args: Iterable[str | Path],
    *,
    cwd: Path,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    """Run a command without a shell and include output in raised errors."""
    command = [str(item) for item in args]
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )
    if check and result.returncode:
        stdout = result.stdout if text else result.stdout.decode("utf-8", "replace")
        stderr = result.stderr if text else result.stderr.decode("utf-8", "replace")
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )
    return result


def git(
    repo: Path,
    *args: str,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    return run(("git", *args), cwd=repo, check=check, text=text)


def git_stdout(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


def ensure_repository(repo: Path) -> Path:
    """Return the physical repository root after validating the owner fork."""
    resolved = repo.resolve()
    root = Path(git_stdout(resolved, "rev-parse", "--show-toplevel")).resolve()
    if root != resolved:
        raise RuntimeError(f"expected repository root {resolved}, got {root}")
    origin = git_stdout(resolved, "remote", "get-url", "origin")
    # actions/checkout records the canonical HTTPS URL without the optional
    # ``.git`` suffix, while native clones commonly retain it.  They identify
    # the same owner repository and must pass the same fail-closed boundary.
    if origin not in {
        "https://github.com/novkien/hermes-agent",
        "https://github.com/novkien/hermes-agent.git",
    }:
        raise RuntimeError(f"unexpected origin: {origin}")
    return resolved


def safe_relative_path(value: str) -> str:
    """Normalize a repository path and reject path traversal."""
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"unsafe repository path: {value!r}")
    return path.as_posix()


def is_shared_test_path(value: str) -> bool:
    """Return whether a path belongs to the upstream-shared test surface."""
    path = safe_relative_path(value)
    if path.startswith(_FORK_ONLY_PREFIXES):
        return False
    base = path.rsplit("/", 1)[-1]
    wrapped = f"/{path}"
    return any(
        (
            path.startswith("tests/"),
            path.startswith("tests-js/"),
            "/tests/" in wrapped,
            "/__tests__/" in wrapped,
            "/e2e/" in wrapped,
            base == "conftest.py",
            bool(re.fullmatch(r"test_.*\.py", base)),
            bool(re.fullmatch(r".*_test\.py", base)),
            ".test." in base,
            ".spec." in base,
            "__snapshots__" in path,
            "/fixtures/" in wrapped
            and (
                path.startswith(("tests/", "tests-js/"))
                or "/tests/" in wrapped
            ),
        )
    )


def runner_for_path(path: str) -> str:
    """Classify a case as Python, JavaScript, or support-only."""
    base = path.rsplit("/", 1)[-1]
    if path.endswith(".py") and base != "conftest.py" and (
        base.startswith("test_") or base.endswith("_test.py")
    ):
        return "python"
    if any(token in base for token in (".test.", ".spec.")) and base.endswith(
        _JAVASCRIPT_SUFFIXES
    ):
        return "javascript"
    return "support"


def parse_tree(repo: Path, ref: str) -> dict[str, dict[str, Any]]:
    """Read a complete Git tree without checking it out."""
    raw = git(
        repo,
        "ls-tree",
        "-r",
        "-z",
        "-l",
        "--full-tree",
        ref,
        text=False,
    ).stdout
    tree: dict[str, dict[str, Any]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, path_bytes = record.split(b"\t", 1)
        fields = metadata.decode("ascii").split()
        path = path_bytes.decode("utf-8", "surrogateescape")
        tree[path] = {
            "mode": fields[0],
            "type": fields[1],
            "sha": fields[2],
            "size": None if fields[3] == "-" else int(fields[3]),
        }
    return tree


def changed_paths(repo: Path, base: str, head: str) -> set[str]:
    raw = git(
        repo,
        "diff",
        "--name-only",
        "-z",
        "--find-renames",
        base,
        head,
        text=False,
    ).stdout
    return {
        item.decode("utf-8", "surrogateescape")
        for item in raw.split(b"\0")
        if item
    }


def read_blob(repo: Path, ref: str, path: str) -> bytes:
    return git(
        repo,
        "show",
        f"{ref}:{safe_relative_path(path)}",
        text=False,
    ).stdout


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _python_test_nodes(source: bytes) -> dict[str, str] | None:
    try:
        module = ast.parse(source.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    found: dict[str, str] = {}
    for node in module.body:
        if isinstance(node, functions) and node.name.startswith("test_"):
            found[node.name] = ast.dump(node, include_attributes=False)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for child in node.body:
                if isinstance(child, functions) and child.name.startswith("test_"):
                    key = f"{node.name}::{child.name}"
                    found[key] = ast.dump(child, include_attributes=False)
    return found


def _python_support_digest(source: bytes) -> str | None:
    """Return the AST for everything that can shape multiple test nodes.

    Imports, module constants, fixtures, helpers, and non-test members on
    ``Test*`` classes are shared execution context.  If the fork changes that
    context, an unchanged test function AST is not evidence that the pristine
    upstream node still expresses the fork's semantics.
    """
    try:
        module = ast.parse(source.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    support: list[object] = []
    for node in module.body:
        if isinstance(node, functions) and node.name.startswith("test_"):
            continue
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            support.append(
                (
                    "test-class-support",
                    node.name,
                    [ast.dump(item, include_attributes=False) for item in node.bases],
                    [ast.dump(item, include_attributes=False) for item in node.keywords],
                    [
                        ast.dump(item, include_attributes=False)
                        for item in node.decorator_list
                    ],
                    [
                        ast.dump(child, include_attributes=False)
                        for child in node.body
                        if not (
                            isinstance(child, functions)
                            and child.name.startswith("test_")
                        )
                    ],
                )
            )
            continue
        support.append(ast.dump(node, include_attributes=False))
    return repr(support)


def changed_python_nodeids(
    shared_path: str,
    owner_source: bytes,
    base_source: bytes | None,
) -> list[str]:
    """Select changed test nodes, falling back to the complete test file."""
    if base_source is None:
        return [shared_path]
    owner_nodes = _python_test_nodes(owner_source)
    base_nodes = _python_test_nodes(base_source)
    if owner_nodes is None or base_nodes is None:
        return [shared_path]
    support_changed = _python_support_digest(owner_source) != _python_support_digest(
        base_source
    )
    if support_changed:
        # Use concrete nodes instead of the whole-file selector.  The owner
        # case then supplies fork fixtures/helpers for every inherited node,
        # while genuinely new upstream nodes in the same file remain active in
        # the pristine suite.
        return [f"{shared_path}::{name}" for name in sorted(owner_nodes)] or [
            shared_path
        ]
    changed = [
        f"{shared_path}::{name}"
        for name, digest in sorted(owner_nodes.items())
        if base_nodes.get(name) != digest
    ]
    # A non-Python or dynamically generated test surface cannot be classified
    # at node granularity.
    return changed or [shared_path]


def python_case_nodeids(
    shared_path: str,
    owner_source: bytes,
    base_source: bytes | None,
    upstream_source: bytes | None,
) -> tuple[list[str], list[str]]:
    """Select fork nodes and the upstream nodes that those cases replace.

    Both sides are necessary. A fork test may rename an upstream test while
    changing its policy; selecting only the new owner name leaves the old,
    contradictory upstream name active in the pristine shared suite.
    """
    owner_selected = changed_python_nodeids(shared_path, owner_source, base_source)
    if owner_selected == [shared_path]:
        return owner_selected, [shared_path] if upstream_source is not None else []

    owner_nodes = _python_test_nodes(owner_source)
    base_nodes = _python_test_nodes(base_source) if base_source is not None else None
    upstream_nodes = (
        _python_test_nodes(upstream_source) if upstream_source is not None else None
    )
    if owner_nodes is None or base_nodes is None:
        return [shared_path], [shared_path] if upstream_source is not None else []
    if upstream_source is None:
        return owner_selected, []
    if upstream_nodes is None:
        return owner_selected, [shared_path]

    support_changed = _python_support_digest(owner_source) != _python_support_digest(
        base_source
    )
    if support_changed:
        replaced = [
            f"{shared_path}::{name}"
            for name in sorted(upstream_nodes)
            if name in base_nodes
        ]
        return owner_selected, replaced

    replacement_names = {
        name
        for name, digest in owner_nodes.items()
        if base_nodes.get(name) != digest
    }
    replacement_names.update(
        name
        for name, digest in base_nodes.items()
        if owner_nodes.get(name) != digest
    )
    replaced = [
        f"{shared_path}::{name}"
        for name in sorted(upstream_nodes)
        if name in replacement_names
    ]
    return owner_selected, replaced


def python_source_nodeids(shared_path: str, source: bytes) -> set[str]:
    """Return concrete node IDs defined by a parseable Python test source."""
    nodes = _python_test_nodes(source)
    if nodes is None:
        return set()
    return {f"{shared_path}::{name}" for name in nodes}


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
