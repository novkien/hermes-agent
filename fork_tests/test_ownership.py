from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/fork_ci"))

import ownership  # noqa: E402
from ownership import (  # noqa: E402
    changed_python_nodeids,
    is_shared_test_path,
    python_case_nodeids,
    runner_for_path,
)


def test_repository_validation_accepts_checkout_origin_without_git_suffix(
    monkeypatch, tmp_path
) -> None:
    repo = tmp_path.resolve()

    def fake_git_stdout(_repo: Path, *args: str) -> str:
        if args == ("rev-parse", "--show-toplevel"):
            return str(repo)
        if args == ("remote", "get-url", "origin"):
            return "https://github.com/novkien/hermes-agent"
        raise AssertionError(args)

    monkeypatch.setattr(ownership, "git_stdout", fake_git_stdout)

    assert ownership.ensure_repository(repo) == repo


def test_shared_surface_wording_matches_layout() -> None:
    assert is_shared_test_path("tests/agent/test_prompt_builder.py")
    assert is_shared_test_path("web/src/lib/parser.test.ts")
    assert is_shared_test_path("plugins/example/tests/test_contract.py")
    assert not is_shared_test_path("fork_tests/cases/tests/test_owner.py")
    assert not is_shared_test_path("apps/mission-control/tests/test_runtime.py")


def test_runner_classification_is_explicit() -> None:
    assert runner_for_path("tests/test_owner.py") == "python"
    assert runner_for_path("web/src/parser.spec.ts") == "javascript"
    assert runner_for_path("tests/conftest.py") == "support"


def test_changed_python_nodeids_selects_only_changed_tests() -> None:
    base = b"def test_same():\n    assert True\n\ndef test_changed():\n    assert 1 == 1\n"
    owner = b"def test_same():\n    assert True\n\ndef test_changed():\n    assert 2 == 2\n"
    assert changed_python_nodeids("tests/test_sample.py", owner, base) == [
        "tests/test_sample.py::test_changed"
    ]


def test_module_support_change_selects_every_concrete_owner_node() -> None:
    base = b"VALUE = 1\n\ndef test_value():\n    assert VALUE\n\ndef test_other():\n    assert True\n"
    owner = b"VALUE = 2\n\ndef test_value():\n    assert VALUE\n\ndef test_other():\n    assert True\n"
    assert changed_python_nodeids("tests/test_sample.py", owner, base) == [
        "tests/test_sample.py::test_other",
        "tests/test_sample.py::test_value",
    ]


def test_support_change_replaces_only_base_nodes_not_new_upstream_nodes() -> None:
    base = b"VALUE = 1\n\ndef test_existing():\n    assert VALUE\n"
    owner = b"VALUE = 2\n\ndef test_existing():\n    assert VALUE\n"
    upstream = (
        b"VALUE = 1\n\ndef test_existing():\n    assert VALUE\n"
        b"\ndef test_new_upstream():\n    assert True\n"
    )

    selected, replaced = python_case_nodeids(
        "tests/test_sample.py", owner, base, upstream
    )

    assert selected == ["tests/test_sample.py::test_existing"]
    assert replaced == ["tests/test_sample.py::test_existing"]


def test_renamed_fork_test_replaces_the_original_upstream_node() -> None:
    base = b"def test_old_policy():\n    assert 1 == 1\n"
    owner = b"def test_new_policy():\n    assert 2 == 2\n"
    upstream = b"def test_old_policy():\n    assert 3 == 3\n"

    selected, replaced = python_case_nodeids(
        "tests/test_sample.py", owner, base, upstream
    )

    assert selected == ["tests/test_sample.py::test_new_policy"]
    assert replaced == ["tests/test_sample.py::test_old_policy"]
