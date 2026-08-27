from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_tests_parallel",
    ROOT / "scripts/run_tests_parallel.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_manifest_replacements_are_loaded_for_the_shared_runner(tmp_path: Path) -> None:
    manifest = tmp_path / "fork_tests" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        """{
  "cases": [{
    "runner": "python",
    "shared_path": "tests/test_policy.py",
    "replaced_upstream_nodeids": ["tests/test_policy.py::test_upstream_policy"]
  }]
}\n""",
        encoding="utf-8",
    )

    assert MODULE._fork_replaced_nodeids(tmp_path) == {
        "tests/test_policy.py": ["tests/test_policy.py::test_upstream_policy"]
    }
