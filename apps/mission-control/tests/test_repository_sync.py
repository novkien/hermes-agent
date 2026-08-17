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
    RepositorySyncError,
    RepositorySyncService,
    default_repository_registry,
)


class OfflineGithub:
    def open_pulls(self, _spec, *, limit=10):
        return []

    def fork_drift(self, _spec):
        raise AssertionError("upstream drift must never be queried")


class TimelineGithub:
    """In-memory GitHub double for the dashboard Safe sync merge sequence."""

    def __init__(self, pulls, *, reject_numbers=()):
        self.pulls = [dict(pull) for pull in pulls]
        self.reject_numbers = set(reject_numbers)
        self.merge_calls = []
        self.ready_calls = []
        self.open_calls = 0

    def open_pulls(self, _spec, *, limit=10):
        self.open_calls += 1
        return [dict(pull) for pull in self.pulls[:limit]]

    def merge_pr_rebase(self, _spec, number, *, expected_head_sha=None):
        self.merge_calls.append((number, expected_head_sha))
        if number in self.reject_numbers:
            raise RepositorySyncError(
                "github_merge_rejected",
                f"PR #{number} has merge conflicts",
                details={"pull_number": number},
            )
        self.pulls = [pull for pull in self.pulls if pull["number"] != number]
        return {"merged": True, "sha": f"merged-{number}"}

    def mark_pull_ready_for_review(self, _spec, number, *, pull_node_id=None):
        self.ready_calls.append((number, pull_node_id))
        for pull in self.pulls:
            if pull["number"] == number:
                pull["draft"] = False
                return {"id": pull_node_id or f"node-{number}", "isDraft": False}
        raise AssertionError(f"missing PR #{number}")


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
        self.assertEqual(registry["hermes-skills"].branch, "master")
        self.assertEqual(registry["hermes-plugins"].branch, "master")
        self.assertEqual(registry["agents"].branch, "master")
        self.assertEqual(registry["llama-proxy"].branch, "master")

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
        self.assertEqual(status["ahead"], 0)
        self.assertEqual(result["pushed_sha"], git(self.prod, "rev-parse", "HEAD"))
        self.assertNotEqual(git(self.prod, "rev-parse", "HEAD"), remote_head)
        self.assertEqual(git(self.prod, "rev-parse", "HEAD"), git(self.origin, "rev-parse", "main"))

    def test_auto_commit_pushes_restored_local_changes_after_safe_sync(self):
        (self.prod / "base.txt").write_text("base\nlocal work\n", encoding="utf-8")

        result = self.service.sync("demo", trigger="dashboard", auto_commit=True)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["committed_sha"])
        self.assertEqual(result["pushed_sha"], result["committed_sha"])
        self.assertEqual(git(self.prod, "rev-parse", "HEAD"), git(self.origin, "rev-parse", "main"))

    def test_dashboard_safe_sync_merges_initial_prs_including_drafts_then_pulls_local(self):
        self.push_remote_file("merged-on-origin.txt", "remote\n", "remote merge result")
        github = TimelineGithub(
            [
                {
                    "number": 22,
                    "created_at": "2026-08-16T11:00:00Z",
                    "head_sha": "head-22",
                    "draft": False,
                },
                {
                    "number": 11,
                    "created_at": "2026-08-16T10:00:00Z",
                    "head_sha": "head-11",
                    "node_id": "node-11",
                    "draft": True,
                },
            ]
        )
        self.service.github = github

        result = self.service.safe_sync("demo", trigger="dashboard", auto_commit=False)

        self.assertTrue(result["ok"], result)
        self.assertEqual([row["number"] for row in result["merged_pulls"]], [11, 22])
        self.assertEqual(github.merge_calls, [(11, "head-11"), (22, "head-22")])
        self.assertEqual(github.ready_calls, [(11, "node-11")])
        self.assertGreaterEqual(github.open_calls, 3)
        self.assertTrue(result["production_sync"]["ok"], result)
        self.assertTrue((self.prod / "merged-on-origin.txt").exists())

    def test_dashboard_safe_sync_stops_at_first_pr_merge_rejection_without_pulling_local(self):
        self.push_remote_file("not-pulled.txt", "remote\n", "remote update")
        github = TimelineGithub(
            [
                {
                    "number": 11,
                    "created_at": "2026-08-16T10:00:00Z",
                    "head_sha": "head-11",
                    "draft": False,
                },
                {
                    "number": 22,
                    "created_at": "2026-08-16T11:00:00Z",
                    "head_sha": "head-22",
                    "draft": False,
                },
                {
                    "number": 33,
                    "created_at": "2026-08-16T12:00:00Z",
                    "head_sha": "head-33",
                    "draft": False,
                },
            ],
            reject_numbers={22},
        )
        self.service.github = github

        result = self.service.safe_sync("demo", trigger="dashboard", auto_commit=False)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "github_merge_rejected")
        self.assertEqual(result["error"]["details"]["pull_number"], 22)
        self.assertEqual([row["number"] for row in result["merged_pulls"]], [11])
        self.assertEqual(github.merge_calls, [(11, "head-11"), (22, "head-22")])
        self.assertNotIn("production_sync", result)
        self.assertFalse((self.prod / "not-pulled.txt").exists())

    def test_status_does_not_query_upstream_drift(self):
        status = self.service.status("demo", fetch=False, include_github=True)
        self.assertIsNone(status["upstream_drift"])

    def test_upstream_sync_is_blocked_without_github_call(self):
        result = self.service.sync_upstream("demo")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "upstream_disabled")

    def test_auto_commit_pushes_staged_and_untracked_local_changes(self):
        (self.prod / "base.txt").write_text("base\nstaged work\n", encoding="utf-8")
        git(self.prod, "add", "base.txt")
        (self.prod / "current-model.json").write_text("local model\n", encoding="utf-8")

        result = self.service.sync("demo", trigger="dashboard", auto_commit=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pushed_sha"], result["committed_sha"])
        self.assertEqual((self.prod / "current-model.json").read_text(encoding="utf-8"), "local model\n")
        self.assertEqual(git(self.prod, "rev-parse", "HEAD"), git(self.origin, "rev-parse", "main"))

    def test_auto_commit_preserves_worktree_version_of_added_file(self):
        added = self.prod / "current-model.json"
        added.write_text("staged model\n", encoding="utf-8")
        git(self.prod, "add", "current-model.json")
        added.write_text("latest model\n", encoding="utf-8")

        result = self.service.sync("demo", trigger="dashboard", auto_commit=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(added.read_text(encoding="utf-8"), "latest model\n")
        self.assertEqual(git(self.prod, "show", "HEAD:current-model.json"), "latest model")

    def test_commit_local_pushes_to_origin(self):
        (self.prod / "local.txt").write_text("local work\n", encoding="utf-8")

        result = self.service.commit_local("demo", trigger="dashboard")

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["committed_sha"])
        self.assertEqual(result["pushed_sha"], result["committed_sha"])
        self.assertEqual(git(self.prod, "rev-parse", "HEAD"), git(self.origin, "rev-parse", "main"))

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

    def test_tracked_deployment_preserves_runtime_and_skips_fastcontext(self):
        live = Path(self.tmp.name) / "live"
        (self.prod / "skills" / "example").mkdir(parents=True)
        (self.prod / "skills" / "example" / "SKILL.md").write_text("source\n", encoding="utf-8")
        (self.prod / "skills" / "example" / ".fastcontext").mkdir()
        (self.prod / "skills" / "example" / ".fastcontext" / "trace.json").write_text(
            "ignored\n", encoding="utf-8"
        )
        git(self.prod, "add", "skills")
        git(self.prod, "commit", "-m", "add deployable source")
        deployed = RepoSpec(
            name="demo",
            repo_full_name="example/demo",
            branch="main",
            path_candidates=(str(self.prod),),
            deployment_root=str(live),
            deployment_paths=("skills",),
        )
        service = RepositorySyncService(
            {"demo": deployed},
            runner=RepositoryGitRunner(timeout=20),
            store=OperationStore(self.state),
            github=OfflineGithub(),
            timeout=20,
        )

        result = service._deploy_work_tree(deployed)

        self.assertEqual(result["copied"], 1)
        self.assertEqual((live / "skills" / "example" / "SKILL.md").read_text(encoding="utf-8"), "source\n")
        self.assertFalse((live / "skills" / "example" / ".fastcontext" / "trace.json").exists())

    def test_automation_commands_share_one_entrypoint_and_trigger_labels(self):
        commands = self.service.automation_commands()
        self.assertIn("--all --sync --auto-commit --json --trigger cron", commands["cron"])
        self.assertIn("--trigger hook", commands["hook_template"])
        self.assertIn("--status --json --trigger manual", commands["status"])


if __name__ == "__main__":
    unittest.main()
