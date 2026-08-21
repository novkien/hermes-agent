"""Exact local/SSH command transport for repository control."""

from __future__ import annotations

import shlex

from .repository_sync import CommandResult, GitRunner, RepoSpec, RepositorySyncError


_SSH_ARGS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-F", "/dev/null",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "GlobalKnownHostsFile=/dev/null",
]


class RepositoryGitRunner(GitRunner):
    """Run only backend-authored argv on the host declared by the registry."""

    def _host_process(
        self, spec: RepoSpec, argv: list[str], *, timeout: int | None = None
    ) -> CommandResult:
        if spec.transport != "ssh":
            return super()._host_process(spec, argv, timeout=timeout)
        if not spec.ssh_target:
            raise RepositorySyncError(
                "ssh_target_missing", f"no SSH target configured for {spec.name}"
            )
        command = " ".join(shlex.quote(str(part)) for part in argv)
        return self._run_process(
            # A login shell sources host profile scripts. Raspberry Pi OS can
            # print first-boot/rfkill notices there, contaminating stdout from
            # machine-readable Git commands (for example an epoch becomes
            # "Wi-Fi ... <epoch>"). Exact commands do not need a login shell.
            ["ssh", *_SSH_ARGS, spec.ssh_target, "bash", "-c", shlex.quote(command)],
            timeout=timeout,
        )
