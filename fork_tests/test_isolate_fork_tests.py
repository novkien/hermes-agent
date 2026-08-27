from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/fork_ci"))

from isolate_fork_tests import apply_plan, build_plan  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "--", "tests")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_apply_moves_owner_delta_to_cases_and_restores_shared_surface(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "owner")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "remote", "add", "origin", "https://github.com/novkien/hermes-agent.git")
    shared = repo / "tests/test_shared.py"
    shared.parent.mkdir(parents=True)
    shared.write_text("def test_value():\n    assert 1 == 1\n", encoding="utf-8")
    base = _commit(repo, "base")

    _git(repo, "switch", "-c", "upstream")
    shared.write_text("def test_value():\n    assert 3 == 3\n", encoding="utf-8")
    _commit(repo, "upstream")

    _git(repo, "switch", "owner")
    assert _git(repo, "rev-parse", "HEAD") == base
    owner_source = "def test_value():\n    assert 2 == 2\n"
    shared.write_text(owner_source, encoding="utf-8")
    extra = repo / "tests/test_owner_only.py"
    extra.write_text("def test_owner():\n    assert True\n", encoding="utf-8")
    owner = _commit(repo, "owner")

    plan = build_plan(repo, owner, "upstream")
    assert plan["modified_shared_paths"] == ["tests/test_shared.py"]
    assert plan["owner_only_shared_paths"] == ["tests/test_owner_only.py"]
    apply_plan(repo, plan)

    case = repo / "fork_tests/cases/tests/test_shared.py"
    assert case.read_text(encoding="utf-8") == owner_source
    assert shared.read_text(encoding="utf-8") == (
        "def test_value():\n    assert 1 == 1\n"
    )
    assert not extra.exists()
    manifest = json.loads((repo / "fork_tests/manifest.json").read_text())
    assert manifest["description"].endswith("not snapshots.")
    assert {row["shared_path"] for row in manifest["cases"]} == {
        "tests/test_shared.py",
        "tests/test_owner_only.py",
    }
