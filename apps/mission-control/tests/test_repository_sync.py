#!/usr/bin/env python3
"""Focused regression tests for registry-driven repository owner control."""

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
    GitHubRestClient,
    OperationStore,
    RepoSpec,
    RepositorySyncError,
    RepositorySyncService,
    default_repository_registry,
)


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({proc.returncode})\n{proc.stdout}\n{proc.stderr}"
        )
    return (proc.stdout or "").strip()


def identity(repo: Path) -> None:
    git(repo, "config", "user.email", "repo-control-test@example.invalid")
    git(repo, "config", "user.name", "Repository Control Test")


class OfflineGithub:
    def open_pulls(self, _spec, *, limit=20):
        return []


class MergeGithub(OfflineGithub):
    def __init__(self, callback=None):
        self.callback = callback
        self.merge_calls = []

    def pull_detail(self, _spec, number):
        return {"number": number, "draft": False, "head": {"sha": "abc1234"}}

    def merge_pr_rebase(self, _spec, number, *, expected_head_sha=None):
        self.merge_calls.append((number, expected_head_sha))
        if self.callback:
            self.callback()
        return {"merged": True, "merge_sha": "merged123", "head_sha": "abc1234"}


class RepositoryRegistryTests(unittest.TestCase):
    def test_registry_is_yaml_driven_and_contains_seven_repositories(self):
        registry = default_repository_registry()
        self.assertEqual(
            list(registry),
            [
                "hermes-agent", "hermes-skills", "hermes-plugins", "agents",
                "llama-proxy", "9router", "godot-mcp",
            ],
        )
        self.assertEqual(registry["llama-proxy"].host, "jarvis-pi")
        self.assertEqual(registry["llama-proxy"].ssh_target, "jarvis-pi")
        self.assertEqual(registry["godot-mcp"].branch, "main")
        expected_live = {
            "hermes-agent": "~/.hermes/hermes-agent",
            "hermes-skills": "~/.hermes",
            "hermes-plugins": "~/.hermes/plugins",
            "agents": "~/.hermes/profiles",
            "llama-proxy": "~/.hermes/llama-proxy",
            "9router": "~/.hermes/9router",
            "godot-mcp": "~/.hermes/godot-mcp",
        }
        for name, spec in registry.items():
            self.assertEqual(spec.git_dir, f"~/.hermes/repos/{name}.git")
            self.assertEqual(spec.work_tree, expected_live[name])
            self.assertNotIn("/worktrees/", spec.work_tree)
        self.assertEqual(registry["hermes-skills"].scope_paths, ("skills", "workspace/skills-pack"))

    def test_dirty_summary_is_compact_and_deterministic(self):
        summary = RepositorySyncService._dirty_summary(
            " M tracked.txt\nM  staged.txt\n?? new.txt\n"
        )
        self.assertTrue(summary["dirty"])
        self.assertEqual(summary["modified"], 1)
        self.assertEqual(summary["staged"], 1)
        self.assertEqual(summary["untracked"], 1)


class RepositoryProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="repository-control-")
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.seed = root / "seed"
        self.writer = root / "writer"
        self.hermes_home = root / ".hermes"
        self.git_dir = self.hermes_home / "repos" / "demo.git"
        self.work_tree = self.hermes_home / "demo"
        self.state = self.hermes_home / "state" / "repository-control"

        subprocess.run(["git", "init", "--bare", str(self.origin)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.origin), str(self.seed)], check=True, capture_output=True)
        identity(self.seed)
        git(self.seed, "checkout", "-b", "main")
        (self.seed / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.seed, "add", "base.txt")
        git(self.seed, "commit", "-m", "initial")
        git(self.seed, "push", "-u", "origin", "main")
        subprocess.run(
            ["git", "--git-dir", str(self.origin), "symbolic-ref", "HEAD", "refs/heads/main"],
            check=True, capture_output=True,
        )
        subprocess.run(["git", "clone", str(self.origin), str(self.writer)], check=True, capture_output=True)
        identity(self.writer)

        self.spec = RepoSpec(
            name="demo",
            repo_full_name="example/demo",
            branch="main",
            host="local-test",
            transport="local",
            ssh_target=None,
            hermes_home=str(self.hermes_home),
            git_dir=str(self.git_dir),
            work_tree=str(self.work_tree),
            origin_url=str(self.origin),
            private=True,
        )
        self.service = RepositorySyncService(
            {"demo": self.spec},
            runner=RepositoryGitRunner(timeout=30),
            store=OperationStore(self.state),
            github=OfflineGithub(),
            timeout=30,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def push(self, filename: str, content: str, message: str) -> str:
        git(self.writer, "pull", "--ff-only", "origin", "main")
        (self.writer / filename).write_text(content, encoding="utf-8")
        git(self.writer, "add", filename)
        git(self.writer, "commit", "-m", message)
        git(self.writer, "push", "origin", "main")
        return git(self.writer, "rev-parse", "HEAD")

    def test_initialize_creates_one_common_dir_and_direct_live_source(self):
        result = self.service.initialize_layout("demo")
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.git_dir.is_dir())
        self.assertTrue(self.work_tree.is_dir())
        self.assertFalse((self.work_tree / ".git").exists())
        self.assertEqual(self.service.runner.git_dir(self.spec), str(self.git_dir))
        self.assertEqual(self.service.runner.git(self.spec, "branch", "--show-current").stdout, "main")
        self.assertEqual((self.work_tree / "base.txt").read_text(), "base\n")

    def test_pull_fast_forwards_the_live_production_worktree(self):
        self.assertTrue(self.service.initialize_layout("demo")["ok"])
        remote = self.push("remote.txt", "remote\n", "remote update")
        result = self.service.pull_production("demo")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["production"]["after_sha"], remote)
        self.assertEqual(self.service.runner.git(self.spec, "rev-parse", "HEAD").stdout, remote)
        self.assertTrue((self.work_tree / "remote.txt").exists())

    def test_pull_refuses_dirty_production_without_stash_or_commit(self):
        self.assertTrue(self.service.initialize_layout("demo")["ok"])
        (self.work_tree / "base.txt").write_text("dirty\n", encoding="utf-8")
        result = self.service.pull_production("demo")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "production_dirty")
        self.assertEqual((self.work_tree / "base.txt").read_text(), "dirty\n")
        self.assertEqual(self.service.runner.git(self.spec, "stash", "list").stdout, "")

    def test_merge_and_pull_blocks_known_dirty_production_before_github_merge(self):
        self.assertTrue(self.service.initialize_layout("demo")["ok"])
        (self.work_tree / "base.txt").write_text("dirty\n", encoding="utf-8")
        github = MergeGithub()
        self.service.github = github
        result = self.service.merge_and_pull(
            "demo", 7, expected_head_sha="abc1234"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "production_dirty")
        self.assertEqual(github.merge_calls, [])

    def test_merge_and_pull_reports_partial_success_if_host_drifts_after_merge(self):
        self.assertTrue(self.service.initialize_layout("demo")["ok"])

        def dirty_after_merge():
            (self.work_tree / "base.txt").write_text("dirty after merge\n", encoding="utf-8")

        github = MergeGithub(callback=dirty_after_merge)
        self.service.github = github
        result = self.service.merge_and_pull(
            "demo", 7, expected_head_sha="abc1234"
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["partial_success"])
        self.assertEqual(result["completed_phase"], "github_merge")
        self.assertEqual(result["error"]["code"], "production_dirty")
        self.assertEqual(github.merge_calls, [(7, "abc1234")])

    def test_legacy_sync_uses_production_pull_semantics(self):
        self.assertTrue(self.service.initialize_layout("demo")["ok"])
        remote = self.push("remote.txt", "remote\n", "remote update")
        result = self.service.sync("demo", auto_commit=True, commit_message="ignored")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["production"]["after_sha"], remote)
        self.assertEqual(
            self.service.runner.git(self.spec, "log", "-1", "--format=%s").stdout,
            "remote update",
        )


class CodexStateTests(unittest.TestCase):
    class Client(GitHubRestClient):
        def request(self, method, path, body=None, *, accept="application/vnd.github+json"):
            del method, body, accept
            if path.endswith("/reviews?per_page=100"):
                return [{
                    "user": {"login": "chatgpt-codex-connector"},
                    "body": "**Reviewed commit:** `abcdef1234`",
                    "commit_id": "abcdef1234",
                    "submitted_at": "2026-08-20T00:00:00Z",
                }]
            if path.endswith("/comments?per_page=100"):
                return []
            raise AssertionError(path)

        def _review_threads(self, spec, number):
            del spec, number
            return [{
                "id": "thread-1", "resolved": False,
                "comments": [{"author": "chatgpt-codex-connector", "body": "Fix this"}],
            }]

    def test_current_codex_review_with_unresolved_thread_is_has_findings(self):
        spec = RepoSpec(
            name="demo", repo_full_name="example/demo", branch="main", host="local",
            transport="local", ssh_target=None, hermes_home="/tmp/.hermes",
            git_dir="/tmp/.hermes/repos/demo.git",
            work_tree="/tmp/.hermes/demo",
            origin_url="git@example.invalid:example/demo.git",
        )
        state = self.Client()._codex_state(
            spec, 3, "abcdef1234567890", "2026-08-20T00:00:00Z"
        )
        self.assertEqual(state["state"], "has_findings")
        self.assertTrue(state["current_head"])
        self.assertEqual(state["unresolved_threads"], 1)

    class ReRequestedClient(Client):
        def request(self, method, path, body=None, *, accept="application/vnd.github+json"):
            if path.endswith("/comments?per_page=100"):
                return [{
                    "body": "@codex review",
                    "created_at": "2026-08-20T00:10:00Z",
                }]
            return super().request(method, path, body, accept=accept)

    def test_new_request_after_stale_review_is_requested(self):
        spec = RepoSpec(
            name="demo", repo_full_name="example/demo", branch="main", host="local",
            transport="local", ssh_target=None, hermes_home="/tmp/.hermes",
            git_dir="/tmp/.hermes/repos/demo.git",
            work_tree="/tmp/.hermes/demo",
            origin_url="git@example.invalid:example/demo.git",
        )
        state = self.ReRequestedClient()._codex_state(
            spec, 3, "feedface12345678", "2026-08-20T00:10:00Z"
        )
        self.assertEqual(state["state"], "requested")
        self.assertFalse(state["current_head"])



if __name__ == "__main__":
    unittest.main()
