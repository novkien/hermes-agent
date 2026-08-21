#!/usr/bin/env python3
"""Focused regression tests for registry-driven repository owner control."""

from __future__ import annotations

import dataclasses
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
    CommandResult,
    GitHubRestClient,
    OperationStore,
    RepoSpec,
    RepositorySyncError,
    RepositorySyncService,
    default_repository_registry,
)


class CapturingSshRunner(RepositoryGitRunner):
    def _run_process(self, argv, *, timeout=None):
        self.last_argv = argv
        return CommandResult(argv, 0, "", "", 0)


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
        for spec in registry.values():
            self.assertEqual(
                spec.origin_url,
                f"https://github.com/{spec.repo_full_name}.git",
            )
        self.assertEqual(registry["godot-mcp"].branch, "main")
        expected_live = {
            "hermes-agent": "~/.hermes/hermes-agent",
            "hermes-skills": "~/.hermes",
            "hermes-plugins": "~/.hermes/plugins",
            "agents": "~/.hermes/profiles",
            "llama-proxy": "/home/pi/llama-proxy",
            "9router": "/home/pi/9router",
            "godot-mcp": "/home/novkien/godot-mcp",
        }
        for name, spec in registry.items():
            self.assertEqual(spec.git_dir, f"~/.hermes/repos/{name}.git")
            self.assertEqual(spec.work_tree, expected_live[name])
            self.assertNotIn("/worktrees/", spec.work_tree)
        self.assertEqual(registry["hermes-skills"].scope_paths, ("skills", "workspace/skills-pack"))

    def test_ssh_runner_avoids_login_profile_output(self):
        spec = default_repository_registry()["9router"]
        runner = CapturingSshRunner()

        runner.host(spec, "git", "status", "--short")

        self.assertEqual(runner.last_argv[-3:-1], ["bash", "-c"])
        self.assertNotIn("-lc", runner.last_argv)

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

    def test_initialize_repairs_origin_left_by_failed_partial_layout(self):
        subprocess.run(
            ["git", "init", "--bare", str(self.git_dir)],
            check=True,
            capture_output=True,
        )
        git(
            self.git_dir,
            "remote",
            "add",
            "origin",
            "git@github.com:example/demo.git",
        )

        result = self.service.initialize_layout("demo")

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            git(self.git_dir, "remote", "get-url", "origin"),
            str(self.origin),
        )
        self.assertEqual((self.work_tree / "base.txt").read_text(), "base\n")

    def test_initialize_seeds_common_dir_from_existing_live_checkout(self):
        subprocess.run(
            [
                "git",
                "clone",
                "--depth=1",
                f"file://{self.origin}",
                str(self.work_tree),
            ],
            check=True,
            capture_output=True,
        )
        self.assertEqual(git(self.work_tree, "rev-parse", "--is-shallow-repository"), "true")
        offline_spec = dataclasses.replace(
            self.spec,
            origin_url="file:///repository-not-reachable-during-layout-init.git",
        )
        service = RepositorySyncService(
            {"demo": offline_spec},
            runner=RepositoryGitRunner(timeout=30),
            store=OperationStore(self.state),
            github=OfflineGithub(),
            timeout=30,
        )

        result = service.initialize_layout("demo")

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            service.runner.git_common(
                offline_spec, "rev-parse", "refs/remotes/origin/main"
            ).stdout,
            git(self.work_tree, "rev-parse", "main"),
        )
        self.assertEqual(
            service.runner.git_common(offline_spec, "remote", "get-url", "origin").stdout,
            offline_spec.origin_url,
        )

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

    def test_merge_and_pull_stashes_and_restores_local_work(self):
        self.assertTrue(self.service.initialize_layout("demo")["ok"])
        (self.work_tree / "staged.txt").write_text("staged\n", encoding="utf-8")
        self.assertEqual(
            self.service.runner.git(self.spec, "add", "staged.txt").returncode,
            0,
        )
        (self.work_tree / "unstaged.txt").write_text("unstaged\n", encoding="utf-8")
        (self.work_tree / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        github = MergeGithub()
        self.service.github = github
        result = self.service.merge_and_pull(
            "demo", 7, expected_head_sha="abc1234"
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(github.merge_calls, [(7, "abc1234")])
        self.assertEqual(result["local_changes"]["stashed"], True)
        self.assertEqual(result["local_changes"]["restored"], True)
        self.assertEqual(
            self.service.runner.git(self.spec, "diff", "--cached", "--name-only").stdout,
            "staged.txt",
        )
        self.assertEqual((self.work_tree / "staged.txt").read_text(), "staged\n")
        self.assertEqual((self.work_tree / "unstaged.txt").read_text(), "unstaged\n")
        self.assertEqual((self.work_tree / "untracked.txt").read_text(), "untracked\n")
        self.assertEqual(self.service.runner.git(self.spec, "stash", "list").stdout, "")

    def test_merge_and_pull_stashes_broken_nested_worktree_residual(self):
        self.assertTrue(self.service.initialize_layout("demo")["ok"])
        nested = self.work_tree / ".claude" / "worktrees" / "broken"
        nested.mkdir(parents=True)
        (nested / ".git").write_text(
            "gitdir: /nonexistent/repository/worktree\n", encoding="utf-8"
        )
        (nested / "initial.txt").write_text("initial nested work\n", encoding="utf-8")
        class ResidualRunner(RepositoryGitRunner):
            def __init__(self):
                super().__init__(timeout=30)
                self.reintroduced = False

            def git(self, spec, *args, timeout=None):
                result = super().git(spec, *args, timeout=timeout)
                if (
                    not self.reintroduced
                    and args[:2] == ("stash", "push")
                    and "--include-untracked" in args
                ):
                    nested.mkdir(parents=True, exist_ok=True)
                    (nested / ".git").write_text(
                        "gitdir: /nonexistent/repository/worktree\n", encoding="utf-8"
                    )
                    (nested / "generated.txt").write_text(
                        "nested local work\n", encoding="utf-8"
                    )
                    self.reintroduced = True
                return result

        service = RepositorySyncService(
            {"demo": self.spec},
            runner=ResidualRunner(),
            store=self.service.store,
            github=MergeGithub(),
            timeout=30,
        )

        result = service.merge_and_pull(
            "demo", 7, expected_head_sha="abc1234"
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["local_changes"]["stash_shas"]), 2)
        self.assertTrue(result["local_changes"]["restored"])
        self.assertEqual((nested / "initial.txt").read_text(), "initial nested work\n")
        self.assertEqual((nested / "generated.txt").read_text(), "nested local work\n")
        self.assertIn(
            "?? .claude/worktrees/broken/generated.txt",
            service.runner.git(
                self.spec, "status", "--porcelain=v1", "--untracked-files=all"
            ).stdout,
        )
        self.assertEqual(service.runner.git(self.spec, "stash", "list").stdout, "")

    def test_merge_and_pull_restores_local_work_when_github_merge_fails(self):
        self.assertTrue(self.service.initialize_layout("demo")["ok"])
        (self.work_tree / "base.txt").write_text("dirty\n", encoding="utf-8")

        class FailingGithub(MergeGithub):
            def merge_pr_rebase(self, *_args, **_kwargs):
                raise RepositorySyncError("github_merge_failed", "merge rejected")

        github = FailingGithub()
        self.service.github = github
        result = self.service.merge_and_pull(
            "demo", 7, expected_head_sha="abc1234"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "github_merge_failed")
        self.assertTrue(result["local_changes"]["restored"])
        self.assertEqual((self.work_tree / "base.txt").read_text(), "dirty\n")
        self.assertEqual(self.service.runner.git(self.spec, "stash", "list").stdout, "")

    def test_merge_and_pull_keeps_stash_when_origin_pull_conflicts_on_restore(self):
        self.assertTrue(self.service.initialize_layout("demo")["ok"])
        (self.work_tree / "base.txt").write_text("local change\n", encoding="utf-8")

        def push_conflicting_origin_change():
            self.push("base.txt", "origin change\n", "origin conflicting update")

        github = MergeGithub(callback=push_conflicting_origin_change)
        self.service.github = github
        result = self.service.merge_and_pull(
            "demo", 7, expected_head_sha="abc1234"
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["partial_success"])
        self.assertEqual(result["completed_phase"], "production_pull")
        self.assertEqual(result["error"]["code"], "local_restore_conflict")
        self.assertFalse(result["local_changes"]["restored"])
        self.assertIn("base.txt", result["error"]["details"]["conflict_files"])
        self.assertIn("mission-control merge-and-pull", self.service.runner.git(
            self.spec, "stash", "list"
        ).stdout)

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


class UnreachableHostRunner(RepositoryGitRunner):
    """Simulate an SSH host whose HOME lookup fails (offline machine)."""

    def _host_process(self, spec, argv, *, timeout=None):
        if spec.transport == "ssh":
            raise RepositorySyncError("ssh_unreachable", f"could not reach {spec.ssh_target}")
        return super()._host_process(spec, argv, timeout=timeout)


class StatusAllContainmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="repository-control-")
        root = Path(self.tmp.name)
        self.hermes_home = root / ".hermes"
        self.state = self.hermes_home / "state" / "repository-control"
        local = RepoSpec(
            name="good", repo_full_name="example/good", branch="main", host="local-test",
            transport="local", ssh_target=None, hermes_home=str(self.hermes_home),
            git_dir=str(self.hermes_home / "repos" / "good.git"),
            work_tree=str(self.hermes_home / "good"),
            origin_url="file:///nonexistent.git", private=True,
        )
        bad = RepoSpec(
            name="bad", repo_full_name="example/bad", branch="main", host="far-host",
            transport="ssh", ssh_target="far@host.invalid", hermes_home="~/.hermes",
            git_dir="~/.hermes/repos/bad.git", work_tree="/home/far/bad",
            origin_url="git@example.invalid:example/bad.git", private=True,
        )
        self.service = RepositorySyncService(
            {"good": local, "bad": bad},
            runner=UnreachableHostRunner(timeout=30),
            store=OperationStore(self.state),
            github=OfflineGithub(),
            timeout=30,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_unreachable_host_degrades_to_error_row_instead_of_breaking_list(self):
        rows = self.service.status_all(fetch=False, include_github=False)
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(set(by_name), {"good", "bad"})
        bad_row = by_name["bad"]
        self.assertFalse(bad_row["ok"])
        self.assertEqual(bad_row["state"], "error")
        self.assertIn("far@host.invalid", bad_row["error"]["message"])
        # Layout paths fall back to registry spec values when the host cannot resolve them.
        self.assertEqual(bad_row["layout"]["git_dir"], "~/.hermes/repos/bad.git")
        self.assertEqual(bad_row["layout"]["work_tree"], "/home/far/bad")
        self.assertFalse(bad_row["layout"]["ready"])


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
