"""Real Git regression cases for native worktrees owned by durable assignments."""
import subprocess
from pathlib import Path

import pytest

from cli import _setup_worktree, _cleanup_worktree, _worktree_lock_is_live


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "source.txt").write_text("base\n")
    git(root, "add", "source.txt")
    git(root, "commit", "-m", "base")
    return root


def create(repo, name="worker-a", commit=None):
    return _setup_worktree(str(repo), name=name, base_commit=commit or git(repo, "rev-parse", "HEAD"), managed_owner=name)


def test_exact_candidate_isolated_writers_and_native_discovery(repo):
    base = git(repo, "rev-parse", "HEAD")
    (repo / "source.txt").write_text("later\n")
    git(repo, "commit", "-am", "later")
    first = create(repo, commit=base)
    second = create(repo, "worker-b", base)
    assert first["branch"] == "hermes/worker-a"
    assert Path(first["path"]).parent == repo / ".worktrees"
    assert git(first["path"], "rev-parse", "HEAD") == base
    (Path(first["path"]) / "source.txt").write_text("worker a\n")
    assert (Path(second["path"]) / "source.txt").read_text() == "base\n"
    assert (repo / "source.txt").read_text() == "later\n"
    assert not git(repo, "status", "--porcelain")
    assert first["path"] in git(repo, "worktree", "list", "--porcelain")


def test_no_shared_includes_and_no_tracked_ignore_mutation(repo):
    (repo / "cache").mkdir()
    (repo / "cache" / "file").write_text("owned by parent")
    (repo / ".worktreeinclude").write_text("cache\n")
    before = git(repo, "status", "--porcelain")
    info = create(repo)
    assert not (Path(info["path"]) / "cache").exists()
    assert not (repo / ".gitignore").exists()
    assert git(repo, "status", "--porcelain") == before


def test_durable_lock_prevents_session_cleanup(repo):
    info = create(repo)
    assert _worktree_lock_is_live(str(repo), info["path"]) == "live"
    _cleanup_worktree(info)
    assert Path(info["path"]).is_dir()
    assert "locked hermes managed=worker-a" in git(repo, "worktree", "list", "--porcelain")


def test_failed_duplicate_preserves_existing_branch(repo):
    git(repo, "branch", "hermes/worker-a")
    previous = git(repo, "rev-parse", "hermes/worker-a")
    assert create(repo) is None
    assert git(repo, "rev-parse", "hermes/worker-a") == previous


def test_missing_exact_sha_never_falls_back(repo):
    assert create(repo, commit="a" * 40) is None
    assert "hermes/worker-a" not in git(repo, "branch", "--list")
    with pytest.raises(ValueError, match="exact commit"):
        create(repo, commit="HEAD")
