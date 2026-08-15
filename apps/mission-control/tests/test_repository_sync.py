#!/usr/bin/env python3
"""Regression tests for AgentOS repository synchronization.

No network access is required. The integration cases build temporary local Git
repositories and verify preservation of dirty files and divergent local commits.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from agent_mission_control.repository_runner import RepositoryGitRunner  # noqa: E402
from agent_mission_control.repository_sync import (  # noqa: E402
    OperationStore,
    RepoSpec,
    RepositorySyncService,
    default_repository_registry,
)


class OfflineGithub:
    def open_pulls(self, _spec, *, limit=10):
        return []

    def fork_drift(self, _spec):
        return None


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({proc.returncode})\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return (proc.stdout or "").strip()


def configure_identity(repo: Path) -> None:
    git(repo, "config", "user.email", "repo-sync-test@example.invalid")
    git(repo, "config", "user.name", "Repo Sync Test")


class RepositoryRegistryTests(unittest.TestCase):
    def test_owner_registry_is_exactly_six_repositories(self):
        registry = default_repository_registry()
        self.assertEqual(
            list(registry),
            ["9router", "hermes-agent", "hermes-skills", "hermes-plugins", "agents", "llama-proxy"],
        )
        self.assertNotIn("agent-mission-control", registry)
        self.assertEqual(registry["9router"].branch, "master")
        self.assertEqual(registry["9router"].upstream_repo, "decolua/9router")
        self.assertEqual(registry["hermes-agent"].upstream_repo, "NousResearch/hermes-agent")
        self.assertEqual(registry["llama-proxy"].transport, "ssh")
        self.assertEqual(registry["hermes-skills"].branch, "main")

    def test_dirty_summary_reports_modified_staged_and_untracked(self):
        summary = RepositorySyncService._dirty_summary(
            " M tracked.txt\nM  staged.txt\n?? new.txt\n"
        )
        self.assertTrue(summary["dirty"])
        self.assertEqual(summary["modified"], 1)
        self.assertEqual(summary["staged"], 1)
        self.assertEqual(summary["untracked"], 1)


class RepositorySyncIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agentos-repo-sync-")
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.seed = root / "seed"
        self.prod = root / "prod"
        self.writer = root / "writer"
        self.state = root / "state"

        subprocess.run(["git", "init", "--bare", str(self.origin)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.origin), str(self.seed)], check=True, capture_output=True)
        configure_identity(self.seed)
        git(self.seed, "checkout", "-b", "main")
        (self.seed / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.seed, "add", "base.txt")
        git(self.seed, "commit", "-m", "initial")
        git(self.seed, "push", "-u", "origin", "main")
        subprocess.run(
            ["git", "--git-dir", str(self.origin), "symbolic-ref", "HEAD", "refs/heads/main"],
            check=True,
            capture_output=True,
        )

        subprocess.run(["git", "clone", str(self.origin), str(self.prod)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.origin), str(self.writer)], check=True, capture_output=True)
        configure_identity(self.prod)
        configure_identity(self.writer)

        self.spec = RepoSpec(
            name="demo",
            repo_full_name="example/demo",
            branch="main",
            path_candidates=(str(self.prod),),
            private=True,
        )
        self.service = RepositorySyncService(
            {"demo": self.spec},
            runner=RepositoryGitRunner(timeout=20),
            store=OperationStore(self.state),
            github=OfflineGithub(),
            timeout=20,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def push_remote_file(self, name: str, content: str, message: str) -> str:
        git(self.writer, "pull", "--ff-only", "origin", "main")
        (self.writer / name).write_text(content, encoding="utf-8")
        git(self.writer, "add", name)
        git(self.writer, "commit", "-m", message)
        git(self.writer, "push", "origin", "main")
        return git(self.writer, "rev-parse", "HEAD")

    def test_safe_sync_preserves_uncommitted_modified_and_untracked_files(self):
        self.push_remote_file("remote.txt", "remote\n", "remote update")
        (self.prod / "base.txt").write_text("base\nlocal work\n", encoding="utf-8")
        (self.prod / "untracked.txt").write_text("untracked\n", encoding="utf-8")

        result = self.service.sync("demo", trigger="cron", auto_commit=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual((self.prod / "base.txt").read_text(encoding="utf-8"), "base\nlocal work\n")
        self.assertEqual((self.prod / "untracked.txt").read_text(encoding="utf-8"), "untracked\n")
        self.assertTrue((self.prod / "remote.txt").exists())
        status = self.service.status("demo", fetch=False, include_github=False)
        self.assertTrue(status["working_tree"]["dirty"])
        self.assertEqual(status["behind"], 0)

    def test_diverged_local_commit_gets_recovery_branch_before_rebase(self):
        (self.prod / "local.txt").write_text("local commit\n", encoding="utf-8")
        git(self.prod, "add", "local.txt")
        git(self.prod, "commit", "-m", "local only")
        local_before = git(self.prod, "rev-parse", "HEAD")
        remote_head = self.push_remote_file("remote.txt", "remote\n", "remote only")

        result = self.service.sync("demo", trigger="cron", auto_commit=False)
        self.assertTrue(result["ok"], result)
        backup = result.get("backup_branch")
        self.assertTrue(backup, result)
        self.assertEqual(git(self.prod, "rev-parse", backup), local_before)
        self.assertTrue((self.prod / "local.txt").exists())
        self.assertTrue((self.prod / "remote.txt").exists())
        status = self.service.status("demo", fetch=False, include_github=False)
        self.assertEqual(status["behind"], 0)
        self.assertEqual(status["ahead"], 1)
        self.assertNotEqual(git(self.prod, "rev-parse", "HEAD"), remote_head)

    def test_stash_restore_conflict_is_reported_without_clean_or_reset(self):
        # Same tracked line changes remotely and locally: pull itself is clean after
        # the stash, but restoring local M must conflict and the stash must remain.
        (self.prod / "base.txt").write_text("local replacement\n", encoding="utf-8")
        git(self.writer, "pull", "--ff-only", "origin", "main")
        (self.writer / "base.txt").write_text("remote replacement\n", encoding="utf-8")
        git(self.writer, "add", "base.txt")
        git(self.writer, "commit", "-m", "remote conflicting update")
        git(self.writer, "push", "origin", "main")

        result = self.service.sync("demo", trigger="cron", auto_commit=False)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "stash_restore_conflict")
        details = result["error"]["details"]
        self.assertIn("base.txt", details["conflict_files"])
        self.assertTrue(details.get("stash_sha"))
        self.assertTrue(git(self.prod, "stash", "list"))
        self.assertTrue(git(self.prod, "diff", "--name-only", "--diff-filter=U"))

    def test_automation_commands_share_one_entrypoint_and_trigger_labels(self):
        commands = self.service.automation_commands()
        self.assertIn("--all --sync --auto-commit --json --trigger cron", commands["cron"])
        self.assertIn("--trigger hook", commands["hook_template"])
        self.assertIn("--status --json --trigger manual", commands["status"])


if __name__ == "__main__":
    unittest.main()
