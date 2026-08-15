"""SSH-safe Git runner used by repository sync entrypoints.

OpenSSH accepts a remote command string rather than a preserved argv. Building one
fully shell-quoted command prevents ``bash -lc`` argument-boundary loss when a repo is
managed on the Pi.
"""

from __future__ import annotations

import shlex

from .repository_sync import GitRunner, RepoSpec, RepositorySyncError


class RepositoryGitRunner(GitRunner):
    """GitRunner with exact remote command serialization for SSH repositories."""

    def _ssh(self, spec: RepoSpec, command: str, *, timeout: int | None = None):
        if not spec.ssh_target:
            raise RepositorySyncError(
                "ssh_target_missing", f"no SSH target configured for {spec.name}"
            )
        return self._run_process(
            [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=10",
                spec.ssh_target,
                command,
            ],
            timeout=timeout,
        )

    def resolve_path(self, spec: RepoSpec) -> str:
        if spec.transport != "ssh":
            return super().resolve_path(spec)
        if spec.git_dir and spec.work_tree:
            return spec.work_tree
        if spec.name in self._resolved_paths:
            return self._resolved_paths[spec.name]
        if not spec.path_candidates:
            raise RepositorySyncError(
                "repo_path_missing", f"no checkout path configured for {spec.name}"
            )

        checks = []
        for candidate in spec.path_candidates:
            q = shlex.quote(candidate)
            checks.append(
                f"if git -C {q} rev-parse --is-inside-work-tree >/dev/null 2>&1; "
                f"then printf '%s\\n' {q}; exit 0; fi"
            )
        result = self._ssh(spec, "; ".join(checks) + "; exit 4")
        if result.returncode != 0 or not result.stdout:
            raise RepositorySyncError(
                "repo_not_found",
                f"no Git checkout found for {spec.name} on {spec.ssh_target}",
                details={
                    "host": spec.ssh_target,
                    "candidates": list(spec.path_candidates),
                    "stderr": result.stderr,
                },
            )
        path = result.stdout.splitlines()[-1].strip()
        self._resolved_paths[spec.name] = path
        return path

    def git(self, spec: RepoSpec, *args: str, timeout: int | None = None):
        if spec.transport != "ssh":
            return super().git(spec, *args, timeout=timeout)

        path = self.resolve_path(spec)
        argv = ["git", "-C", path, *args]
        command = " ".join(shlex.quote(part) for part in argv)
        return self._ssh(spec, command, timeout=timeout)
