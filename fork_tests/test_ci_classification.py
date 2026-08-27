from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "classify_changes",
    ROOT / "scripts/ci/classify_changes.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_python_fork_case_runs_python_without_product_jobs() -> None:
    lanes = MODULE.classify(["fork_tests/cases/tests/test_owner.py"])
    assert lanes["python"] is True
    assert lanes["python_prod"] is False
    assert lanes["frontend"] is False


def test_javascript_fork_case_runs_frontend_without_python_product_jobs() -> None:
    lanes = MODULE.classify(["fork_tests/cases/web/src/owner.test.ts"])
    assert lanes["frontend"] is True
    assert lanes["python"] is False
    assert lanes["python_prod"] is False


def test_manifest_change_fails_open_to_python_validation() -> None:
    lanes = MODULE.classify(["fork_tests/manifest.json"])
    assert lanes["python"] is True
