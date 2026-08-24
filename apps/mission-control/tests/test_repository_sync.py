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
from urllib.parse import parse_qs, unquote, urlparse

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
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
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
    MERGE_SHA = "1" * 40
    BRANCH_SHA = "6" * 40

    def __init__(self, callback=None, *, pin_error=None):
        self.callback = callback
        self.merge_calls = []
        self.pin_calls = []
        self.pin_error = pin_error

    def pull_detail(self, _spec, number):
        return {"number": number, "draft": False, "head": {"sha": "abc1234"}}

    def merge_pr_rebase(self, _spec, number, *, expected_head_sha=None):
        self.merge_calls.append((number, expected_head_sha))
        if self.callback:
            self.callback()
        return {"merged": True, "merge_sha": self.MERGE_SHA, "head_sha": "abc1234"}

    def ensure_superproject_gitlink_pr(self, parent, child, *, target_sha):
        self.pin_calls.append((parent.name, child.name, target_sha))
        if self.pin_error:
            raise self.pin_error
        return {
            "managed": True,
            "changed": True,
            "state": "created",
            "superproject": parent.name,
            "repository": child.name,
            "path": child.superproject_path,
            "target_sha": target_sha,
            "pull_number": 12,
            "html_url": "https://github.com/example/hermes/pull/12",
        }

    def branch_sha(self, _spec):
        return self.BRANCH_SHA


class RepositoryRegistryTests(unittest.TestCase):
    def test_registry_marks_only_hermes_as_locally_synced(self):
        registry = default_repository_registry()
        self.assertEqual(
            list(registry),
            [
                "hermes",
                "hermes-agent",
                "hermes-skills",
                "hermes-plugins",
                "agents",
                "llama-proxy",
                "9router",
                "godot-mcp",
            ],
        )
        self.assertEqual(registry["hermes"].local_mode, "superproject")
        self.assertEqual(registry["hermes"].git_dir, "~/.hermes/.git")
        self.assertEqual(registry["hermes"].work_tree, "~/.hermes")
        self.assertEqual(registry["hermes"].sync_script, "~/.hermes/scripts/sync.sh")
        for name, spec in registry.items():
            if name != "hermes":
                self.assertEqual(spec.local_mode, "remote_only")
        expected_gitlinks = {
            "hermes-agent": "hermes-agent",
            "hermes-skills": ".sources/hermes-skills",
            "hermes-plugins": "plugins",
            "agents": "profiles",
        }
        for name, path in expected_gitlinks.items():
            self.assertEqual(registry[name].superproject_path, path)
        for name in {"hermes", "llama-proxy", "9router", "godot-mcp"}:
            self.assertIsNone(registry[name].superproject_path)
        self.assertEqual(registry["llama-proxy"].host, "jarvis-pi")
        self.assertEqual(registry["llama-proxy"].ssh_target, "jarvis-pi")
        for spec in registry.values():
            self.assertEqual(
                spec.origin_url,
                f"https://github.com/{spec.repo_full_name}.git",
            )
        self.assertEqual(registry["godot-mcp"].branch, "main")
        expected_live = {
            "hermes": "~/.hermes",
            "hermes-agent": "~/.hermes/hermes-agent",
            "hermes-skills": "~/.hermes",
            "hermes-plugins": "~/.hermes/plugins",
            "agents": "~/.hermes/profiles",
            "llama-proxy": "/home/pi/llama-proxy",
            "9router": "/home/pi/9router",
            "godot-mcp": "/home/novkien/godot-mcp",
        }
        for name, spec in registry.items():
            expected_git_dir = (
                "~/.hermes/.git" if name == "hermes" else f"~/.hermes/repos/{name}.git"
            )
            self.assertEqual(spec.git_dir, expected_git_dir)
            self.assertEqual(spec.work_tree, expected_live[name])
            self.assertNotIn("/worktrees/", spec.work_tree)
        self.assertEqual(
            registry["hermes-skills"].scope_paths, ("skills", "workspace/skills-pack")
        )

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

        subprocess.run(
            ["git", "init", "--bare", str(self.origin)], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "clone", str(self.origin), str(self.seed)],
            check=True,
            capture_output=True,
        )
        identity(self.seed)
        git(self.seed, "checkout", "-b", "main")
        (self.seed / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.seed, "add", "base.txt")
        git(self.seed, "commit", "-m", "initial")
        git(self.seed, "push", "-u", "origin", "main")
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.origin),
                "symbolic-ref",
                "HEAD",
                "refs/heads/main",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "clone", str(self.origin), str(self.writer)],
            check=True,
            capture_output=True,
        )
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
        self.assertEqual(
            self.service.runner.git(self.spec, "branch", "--show-current").stdout,
            "main",
        )
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
        self.assertEqual(
            git(self.work_tree, "rev-parse", "--is-shallow-repository"), "true"
        )
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
            service.runner.git_common(
                offline_spec, "remote", "get-url", "origin"
            ).stdout,
            offline_spec.origin_url,
        )

    def test_pull_fast_forwards_the_live_production_worktree(self):
        self.assertTrue(self.service.initialize_layout("demo")["ok"])
        remote = self.push("remote.txt", "remote\n", "remote update")
        result = self.service.pull_production("demo")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["production"]["after_sha"], remote)
        self.assertEqual(
            self.service.runner.git(self.spec, "rev-parse", "HEAD").stdout, remote
        )
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
        result = self.service.merge_and_pull("demo", 7, expected_head_sha="abc1234")
        self.assertTrue(result["ok"], result)
        self.assertEqual(github.merge_calls, [(7, "abc1234")])
        self.assertEqual(result["local_changes"]["stashed"], True)
        self.assertEqual(result["local_changes"]["restored"], True)
        self.assertEqual(
            self.service.runner.git(
                self.spec, "diff", "--cached", "--name-only"
            ).stdout,
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

        result = service.merge_and_pull("demo", 7, expected_head_sha="abc1234")

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
        result = self.service.merge_and_pull("demo", 7, expected_head_sha="abc1234")
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
        result = self.service.merge_and_pull("demo", 7, expected_head_sha="abc1234")
        self.assertFalse(result["ok"])
        self.assertTrue(result["partial_success"])
        self.assertEqual(result["completed_phase"], "production_pull")
        self.assertEqual(result["error"]["code"], "local_restore_conflict")
        self.assertFalse(result["local_changes"]["restored"])
        self.assertIn("base.txt", result["error"]["details"]["conflict_files"])
        self.assertIn(
            "mission-control merge-and-pull",
            self.service.runner.git(self.spec, "stash", "list").stdout,
        )

    def test_merge_and_pull_reports_partial_success_if_host_drifts_after_merge(self):
        self.assertTrue(self.service.initialize_layout("demo")["ok"])

        def dirty_after_merge():
            (self.work_tree / "base.txt").write_text(
                "dirty after merge\n", encoding="utf-8"
            )

        github = MergeGithub(callback=dirty_after_merge)
        self.service.github = github
        result = self.service.merge_and_pull("demo", 7, expected_head_sha="abc1234")
        self.assertFalse(result["ok"])
        self.assertTrue(result["partial_success"])
        self.assertEqual(result["completed_phase"], "github_merge")
        self.assertEqual(result["error"]["code"], "production_dirty")
        self.assertEqual(github.merge_calls, [(7, "abc1234")])

    def test_sync_commits_tracked_changes_pushes_and_ignores_untracked(self):
        self.assertTrue(self.service.initialize_layout("demo")["ok"])
        self.service.runner.git_common(
            self.spec, "config", "user.email", "repo-control-test@example.invalid"
        )
        self.service.runner.git_common(
            self.spec, "config", "user.name", "Repository Control Test"
        )
        (self.work_tree / "base.txt").write_text("tracked sync\n", encoding="utf-8")
        (self.work_tree / "untracked.txt").write_text("leave me\n", encoding="utf-8")

        result = self.service.sync("demo", commit_message="sync tracked work")

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["local_commit"]["committed"])
        self.assertTrue(result["push"]["changed"])
        self.assertTrue(result["untracked_ignored"])
        self.assertEqual(result["after"]["ahead"], 0)
        self.assertEqual(result["after"]["behind"], 0)
        self.assertFalse(result["after"]["working_tree"]["dirty"])
        self.assertTrue((self.work_tree / "untracked.txt").exists())
        self.assertEqual(
            self.service.runner.git(self.spec, "log", "-1", "--format=%s").stdout,
            "sync tracked work",
        )

    def test_sync_does_not_count_or_remove_untracked_only_work(self):
        self.assertTrue(self.service.initialize_layout("demo")["ok"])
        (self.work_tree / "untracked.txt").write_text("leave me\n", encoding="utf-8")

        result = self.service.sync("demo")

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["local_commit"]["committed"])
        self.assertFalse(result["push"]["changed"])
        self.assertFalse(result["after"]["working_tree"]["dirty"])
        self.assertTrue((self.work_tree / "untracked.txt").exists())

    def test_sync_pulls_origin_when_local_is_behind(self):
        self.assertTrue(self.service.initialize_layout("demo")["ok"])
        remote = self.push("remote.txt", "remote\n", "remote update")

        result = self.service.sync("demo")

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["merge"]["changed"])
        self.assertFalse(result["push"]["changed"])
        self.assertEqual(result["after"]["local_sha"], remote)
        self.assertTrue((self.work_tree / "remote.txt").exists())

    def test_sync_merges_origin_then_pushes_local_commits(self):
        self.assertTrue(self.service.initialize_layout("demo")["ok"])
        self.service.runner.git_common(
            self.spec, "config", "user.email", "repo-control-test@example.invalid"
        )
        self.service.runner.git_common(
            self.spec, "config", "user.name", "Repository Control Test"
        )
        (self.work_tree / "local.txt").write_text("local\n", encoding="utf-8")
        self.service.runner.git(self.spec, "add", "local.txt")
        self.service.runner.git(self.spec, "commit", "-m", "local commit")
        self.push("remote.txt", "remote\n", "remote commit")

        result = self.service.sync("demo")

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["merge"]["changed"])
        self.assertTrue(result["push"]["changed"])
        self.assertEqual(result["after"]["ahead"], 0)
        self.assertEqual(result["after"]["behind"], 0)
        self.assertTrue((self.work_tree / "local.txt").exists())
        self.assertTrue((self.work_tree / "remote.txt").exists())

    def test_superproject_sync_runs_only_the_configured_script_and_verifies_status(
        self,
    ):
        self.assertTrue(self.service.initialize_layout("demo")["ok"])
        script = self.hermes_home / "scripts" / "sync.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "#!/bin/sh\nprintf 'superproject-sync-ok\\n'\n", encoding="utf-8"
        )
        script.chmod(0o755)
        spec = dataclasses.replace(
            self.spec, local_mode="superproject", sync_script=str(script)
        )
        service = RepositorySyncService(
            {"demo": spec},
            runner=self.service.runner,
            store=self.service.store,
            github=OfflineGithub(),
            timeout=30,
        )

        result = service.sync("demo")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["script"]["stdout"], "superproject-sync-ok")
        self.assertEqual(result["sync_script"], str(script))
        self.assertNotIn("local_commit", result)


class RemoteOnlyRepositoryTests(unittest.TestCase):
    class NoLocalRunner(RepositoryGitRunner):
        def _host_process(self, spec, argv, *, timeout=None):
            raise AssertionError(f"local command forbidden for {spec.name}: {argv}")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="repository-remote-only-")
        root = Path(self.tmp.name)
        self.spec = RepoSpec(
            name="child",
            repo_full_name="example/child",
            branch="main",
            host="jarvis",
            transport="local",
            ssh_target=None,
            hermes_home=str(root),
            git_dir=str(root / "child.git"),
            work_tree=str(root / "child"),
            origin_url="https://github.com/example/child.git",
            local_mode="remote_only",
        )
        self.github = MergeGithub()
        self.service = RepositorySyncService(
            {"child": self.spec},
            runner=self.NoLocalRunner(),
            store=OperationStore(root / "state"),
            github=self.github,
            timeout=30,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_and_sync_never_touch_local_git(self):
        status = self.service.status("child", fetch=True, include_github=True)
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["state"], "remote_only")
        self.assertFalse(status["layout"]["managed"])
        self.assertFalse(status["capabilities"]["sync_local"])

        result = self.service.sync("child")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "remote_only")

    def test_merge_pr_mutates_github_only(self):
        result = self.service.merge_pr("child", 9, expected_head_sha="abc1234")
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["local_mutation"])
        self.assertFalse(result["superproject_pin"]["managed"])
        self.assertEqual(self.github.merge_calls, [(9, "abc1234")])
        self.assertEqual(self.github.pin_calls, [])

    def _managed_service(self, github):
        parent = RepoSpec(
            name="hermes",
            repo_full_name="example/hermes",
            branch="main",
            host="jarvis",
            transport="local",
            ssh_target=None,
            hermes_home=str(Path(self.tmp.name)),
            git_dir=str(Path(self.tmp.name) / ".git"),
            work_tree=str(Path(self.tmp.name)),
            origin_url="https://github.com/example/hermes.git",
            local_mode="superproject",
            sync_script=str(Path(self.tmp.name) / "sync.sh"),
        )
        child = dataclasses.replace(self.spec, superproject_path="modules/child")
        return RepositorySyncService(
            {"hermes": parent, "child": child},
            runner=self.NoLocalRunner(),
            store=OperationStore(Path(self.tmp.name) / "managed-state"),
            github=github,
            timeout=30,
        )

    def test_merge_managed_child_creates_exact_parent_gitlink_pr(self):
        github = MergeGithub()
        service = self._managed_service(github)

        result = service.merge_pr("child", 9, expected_head_sha="abc1234")

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["local_mutation"])
        self.assertEqual(
            github.pin_calls,
            [("hermes", "child", MergeGithub.MERGE_SHA)],
        )
        self.assertEqual(result["superproject_pin"]["path"], "modules/child")
        self.assertEqual(result["superproject_pin"]["pull_number"], 12)

    def test_managed_child_status_advertises_exact_parent_projection(self):
        service = self._managed_service(MergeGithub())

        result = service.status("child", fetch=True, include_github=True)

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["capabilities"]["sync_local"])
        self.assertTrue(result["capabilities"]["project_to_superproject"])
        self.assertEqual(
            result["superproject"],
            {"managed": True, "name": "hermes", "path": "modules/child"},
        )

    def test_invalid_merge_sha_is_partial_after_child_merge(self):
        github = MergeGithub()
        github.MERGE_SHA = "not-a-commit"
        service = self._managed_service(github)

        result = service.merge_pr("child", 9, expected_head_sha="abc1234")

        self.assertFalse(result["ok"], result)
        self.assertTrue(result["partial_success"])
        self.assertEqual(result["completed_phase"], "github_merge")
        self.assertEqual(result["error"]["code"], "superproject_pin_invalid")

    def test_parent_pin_failure_is_partial_after_child_merge(self):
        github = MergeGithub(
            pin_error=RepositorySyncError("pin_failed", "could not prepare parent PR")
        )
        service = self._managed_service(github)

        result = service.merge_pr("child", 9, expected_head_sha="abc1234")

        self.assertFalse(result["ok"], result)
        self.assertTrue(result["partial_success"])
        self.assertEqual(result["completed_phase"], "github_merge")
        self.assertEqual(result["error"]["code"], "pin_failed")
        self.assertEqual(github.merge_calls, [(9, "abc1234")])

    def test_prepare_pin_reconciles_current_fork_branch_without_local_git(self):
        github = MergeGithub()
        service = self._managed_service(github)

        result = service.prepare_superproject_pin("child")

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["local_mutation"])
        self.assertEqual(
            github.pin_calls,
            [("hermes", "child", MergeGithub.BRANCH_SHA)],
        )

    def test_prepare_pin_rejects_repository_without_local_projection(self):
        result = self.service.prepare_superproject_pin("child")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "superproject_projection_unavailable")
        self.assertEqual(self.github.pin_calls, [])

    def test_merging_parent_pr_does_not_recursively_prepare_another_pin(self):
        github = MergeGithub()
        service = self._managed_service(github)

        result = service.merge_pr("hermes", 12, expected_head_sha="abc1234")

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["superproject_pin"]["managed"])
        self.assertEqual(github.pin_calls, [])


class GitlinkGithub(GitHubRestClient):
    BASE_COMMIT = "a" * 40
    BASE_TREE = "b" * 40

    def __init__(self, initial_pins):
        super().__init__(token="test-token")
        self.refs = {"main": self.BASE_COMMIT}
        self.commits = {self.BASE_COMMIT: self.BASE_TREE}
        self.trees = {self.BASE_TREE: dict(initial_pins)}
        self.commit_parents = {self.BASE_COMMIT: []}
        self.open_pull = None
        self.counter = 10
        self.tree_entries = []
        self.ref_updates = []
        self.pull_files = set()
        self.behind_by = 0
        self.content_refs = []

    def _new_sha(self):
        self.counter += 1
        return f"{self.counter:040x}"

    def request(self, method, path, body=None, *, accept="application/vnd.github+json"):
        del accept
        parsed = urlparse(path)
        endpoint = parsed.path
        if endpoint.endswith("/pulls") and method == "GET":
            return [self.open_pull] if self.open_pull else []
        if endpoint.endswith("/files") and "/pulls/" in endpoint and method == "GET":
            return [{"filename": path} for path in sorted(self.pull_files)]
        if "/compare/" in endpoint and method == "GET":
            return {"behind_by": self.behind_by}
        marker = "/git/ref/heads/"
        if marker in endpoint and method == "GET":
            branch = unquote(endpoint.split(marker, 1)[1])
            return {"object": {"sha": self.refs[branch]}}
        marker = "/git/commits/"
        if marker in endpoint and method == "GET":
            sha = endpoint.split(marker, 1)[1]
            return {"tree": {"sha": self.commits[sha]}}
        marker = "/contents/"
        if marker in endpoint and method == "GET":
            item = unquote(endpoint.split(marker, 1)[1])
            ref = parse_qs(parsed.query)["ref"][0]
            self.content_refs.append(ref)
            tree = self.commits[self.refs.get(ref, ref)]
            return {
                "sha": self.trees[tree][item],
                "type": "submodule",
            }
        if endpoint.endswith("/git/trees") and method == "POST":
            tree_sha = self._new_sha()
            pins = dict(self.trees[body["base_tree"]])
            for entry in body["tree"]:
                pins[entry["path"]] = entry["sha"]
                self.pull_files.add(entry["path"])
            self.trees[tree_sha] = pins
            self.tree_entries.extend(dict(entry) for entry in body["tree"])
            return {"sha": tree_sha}
        if endpoint.endswith("/git/commits") and method == "POST":
            commit_sha = self._new_sha()
            self.commits[commit_sha] = body["tree"]
            self.commit_parents[commit_sha] = list(body["parents"])
            self.assert_parent = body["parents"][0]
            return {"sha": commit_sha}
        if endpoint.endswith("/git/refs") and method == "POST":
            branch = body["ref"].removeprefix("refs/heads/")
            self.refs[branch] = body["sha"]
            return {"ref": body["ref"], "object": {"sha": body["sha"]}}
        marker = "/git/refs/heads/"
        if marker in endpoint and method == "PATCH":
            branch = unquote(endpoint.split(marker, 1)[1])
            self.refs[branch] = body["sha"]
            self.ref_updates.append({"branch": branch, **body})
            return {"ref": f"refs/heads/{branch}", "object": {"sha": body["sha"]}}
        if endpoint.endswith("/pulls") and method == "POST":
            branch = body["head"]
            self.open_pull = {
                "number": 12,
                "html_url": "https://github.com/example/hermes/pull/12",
                "head": {
                    "ref": branch,
                    "sha": self.refs[branch],
                    "label": f"example:{branch}",
                    "repo": {"full_name": "example/hermes"},
                },
            }
            return self.open_pull
        raise AssertionError(f"unexpected request: {method} {path} {body}")


class GitHubGitlinkWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.parent = RepoSpec(
            name="hermes",
            repo_full_name="example/hermes",
            branch="main",
            host="jarvis",
            transport="local",
            ssh_target=None,
            hermes_home="~/.hermes",
            git_dir="~/.hermes/.git",
            work_tree="~/.hermes",
            origin_url="https://github.com/example/hermes.git",
            local_mode="superproject",
            sync_script="~/.hermes/scripts/sync.sh",
        )
        self.agent = RepoSpec(
            name="hermes-agent",
            repo_full_name="example/hermes-agent",
            branch="main",
            host="jarvis",
            transport="local",
            ssh_target=None,
            hermes_home="~/.hermes",
            git_dir="unused",
            work_tree="unused",
            origin_url="https://github.com/example/hermes-agent.git",
            local_mode="remote_only",
            superproject_path="hermes-agent",
        )
        self.skills = dataclasses.replace(
            self.agent,
            name="hermes-skills",
            repo_full_name="example/hermes-skills",
            superproject_path=".sources/hermes-skills",
        )
        self.old_agent = "2" * 40
        self.old_skills = "3" * 40
        self.github = GitlinkGithub({
            "hermes-agent": self.old_agent,
            ".sources/hermes-skills": self.old_skills,
        })

    def test_create_reuse_and_verify_one_aggregate_parent_pull(self):
        agent_target = "4" * 40
        skills_target = "5" * 40

        first = self.github.ensure_superproject_gitlink_pr(
            self.parent, self.agent, target_sha=agent_target
        )
        second = self.github.ensure_superproject_gitlink_pr(
            self.parent, self.skills, target_sha=skills_target
        )
        duplicate = self.github.ensure_superproject_gitlink_pr(
            self.parent, self.skills, target_sha=skills_target
        )

        self.assertEqual(first["state"], "created")
        self.assertRegex(
            first["branch"],
            r"^mission-control/hermes-gitlinks-hermes-agent-4{12}-[0-9]+$",
        )
        self.assertEqual(second["state"], "updated")
        self.assertEqual(second["pull_number"], first["pull_number"])
        self.assertEqual(duplicate["state"], "pull_current")
        self.assertFalse(duplicate["changed"])
        self.assertEqual(
            self.github.tree_entries,
            [
                {
                    "path": "hermes-agent",
                    "mode": "160000",
                    "type": "commit",
                    "sha": agent_target,
                },
                {
                    "path": ".sources/hermes-skills",
                    "mode": "160000",
                    "type": "commit",
                    "sha": skills_target,
                },
            ],
        )
        branch = first["branch"]
        tree = self.github.commits[self.github.refs[branch]]
        self.assertEqual(self.github.trees[tree]["hermes-agent"], agent_target)
        self.assertEqual(
            self.github.trees[tree][".sources/hermes-skills"], skills_target
        )
        self.assertEqual(self.github.ref_updates[-1]["force"], False)

    def test_no_parent_pull_when_base_already_pins_target(self):
        result = self.github.ensure_superproject_gitlink_pr(
            self.parent, self.agent, target_sha=self.old_agent
        )

        self.assertEqual(result["state"], "already_pinned")
        self.assertFalse(result["changed"])
        self.assertIsNone(self.github.open_pull)
        self.assertEqual(self.github.tree_entries, [])

    def test_open_parent_pull_reconciles_new_master_without_losing_pins(self):
        agent_target = "4" * 40
        skills_target = "5" * 40
        first = self.github.ensure_superproject_gitlink_pr(
            self.parent, self.agent, target_sha=agent_target
        )

        parent_tree = self.github._new_sha()
        self.github.trees[parent_tree] = {
            **self.github.trees[self.github.BASE_TREE],
            "config.yaml": "6" * 40,
        }
        parent_commit = self.github._new_sha()
        self.github.commits[parent_commit] = parent_tree
        self.github.commit_parents[parent_commit] = [self.github.BASE_COMMIT]
        self.github.refs["main"] = parent_commit
        self.github.behind_by = 1

        second = self.github.ensure_superproject_gitlink_pr(
            self.parent, self.skills, target_sha=skills_target
        )

        branch_head = self.github.refs[first["branch"]]
        self.assertEqual(
            self.github.commit_parents[branch_head],
            [first["parent_commit_sha"], parent_commit],
        )
        merged_tree = self.github.trees[self.github.commits[branch_head]]
        self.assertEqual(merged_tree["config.yaml"], "6" * 40)
        self.assertEqual(merged_tree["hermes-agent"], agent_target)
        self.assertEqual(merged_tree[".sources/hermes-skills"], skills_target)
        self.assertEqual(second["pull_number"], first["pull_number"])
        self.assertFalse(self.github.ref_updates[-1]["force"])

    def test_created_pin_is_verified_at_immutable_parent_commit(self):
        result = self.github.ensure_superproject_gitlink_pr(
            self.parent, self.agent, target_sha="4" * 40
        )

        self.assertTrue(result["changed"])
        self.assertIn(result["parent_commit_sha"], self.github.commits)
        self.assertEqual(self.github.content_refs[-1], result["parent_commit_sha"])


class UnreachableHostRunner(RepositoryGitRunner):
    """Simulate an SSH host whose HOME lookup fails (offline machine)."""

    def _host_process(self, spec, argv, *, timeout=None):
        if spec.transport == "ssh":
            raise RepositorySyncError(
                "ssh_unreachable", f"could not reach {spec.ssh_target}"
            )
        return super()._host_process(spec, argv, timeout=timeout)


class StatusAllContainmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="repository-control-")
        root = Path(self.tmp.name)
        self.hermes_home = root / ".hermes"
        self.state = self.hermes_home / "state" / "repository-control"
        local = RepoSpec(
            name="good",
            repo_full_name="example/good",
            branch="main",
            host="local-test",
            transport="local",
            ssh_target=None,
            hermes_home=str(self.hermes_home),
            git_dir=str(self.hermes_home / "repos" / "good.git"),
            work_tree=str(self.hermes_home / "good"),
            origin_url="file:///nonexistent.git",
            private=True,
        )
        bad = RepoSpec(
            name="bad",
            repo_full_name="example/bad",
            branch="main",
            host="far-host",
            transport="ssh",
            ssh_target="far@host.invalid",
            hermes_home="~/.hermes",
            git_dir="~/.hermes/repos/bad.git",
            work_tree="/home/far/bad",
            origin_url="git@example.invalid:example/bad.git",
            private=True,
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
        def request(
            self, method, path, body=None, *, accept="application/vnd.github+json"
        ):
            del method, body, accept
            if path.endswith("/reviews?per_page=100"):
                return [
                    {
                        "user": {"login": "chatgpt-codex-connector"},
                        "body": "**Reviewed commit:** `abcdef1234`",
                        "commit_id": "abcdef1234",
                        "submitted_at": "2026-08-20T00:00:00Z",
                    }
                ]
            if path.endswith("/comments?per_page=100"):
                return []
            raise AssertionError(path)

        def _review_threads(self, spec, number):
            del spec, number
            return [
                {
                    "id": "thread-1",
                    "resolved": False,
                    "comments": [
                        {"author": "chatgpt-codex-connector", "body": "Fix this"}
                    ],
                }
            ]

    def test_current_codex_review_with_unresolved_thread_is_has_findings(self):
        spec = RepoSpec(
            name="demo",
            repo_full_name="example/demo",
            branch="main",
            host="local",
            transport="local",
            ssh_target=None,
            hermes_home="/tmp/.hermes",
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
        def request(
            self, method, path, body=None, *, accept="application/vnd.github+json"
        ):
            if path.endswith("/comments?per_page=100"):
                return [
                    {
                        "body": "@codex review",
                        "created_at": "2026-08-20T00:10:00Z",
                    }
                ]
            return super().request(method, path, body, accept=accept)

    def test_new_request_after_stale_review_is_requested(self):
        spec = RepoSpec(
            name="demo",
            repo_full_name="example/demo",
            branch="main",
            host="local",
            transport="local",
            ssh_target=None,
            hermes_home="/tmp/.hermes",
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
