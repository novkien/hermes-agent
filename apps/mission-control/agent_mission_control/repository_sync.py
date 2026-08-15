"""Safe Git repository synchronization for AgentOS Mission Control.

This module is intentionally deterministic. It owns a fixed repository registry and
never accepts an arbitrary filesystem path or shell command from the browser.

The same engine is used by:
- the Mission Control Repositories tab;
- ``tools/repo_sync.py`` for cron/manual execution;
- a future hook that can invoke the CLI early with ``--trigger hook``.

Safety contract for ``sync``:
1. fetch the configured default branch;
2. refuse pre-existing merge conflicts or the wrong checked-out branch;
3. create a recovery branch when local commits may be rebased;
4. stash tracked/staged/untracked work with ``git stash push -u``;
5. ``git pull --rebase --no-autostash``;
6. abort a failed rebase and restore the original stash;
7. restore the stash after a successful pull;
8. if stash restoration conflicts, stop and keep the stash entry;
9. optionally commit restored local work after a clean synchronization.

No reset --hard, clean, force push, stash drop, or automatic conflict resolution is
performed.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
from typing import Any, Iterable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request as UrlRequest, urlopen


GITHUB_API_VERSION = "2026-03-10"
DEFAULT_REMOTE_SSH = "pi@192.168.1.140"
DEFAULT_TIMEOUT_SECONDS = 90


@dataclasses.dataclass(frozen=True)
class RepoSpec:
    name: str
    repo_full_name: str
    branch: str
    transport: str = "local"
    path_candidates: tuple[str, ...] = ()
    git_dir: str | None = None
    work_tree: str | None = None
    ssh_target: str | None = None
    upstream_repo: str | None = None
    private: bool = True

    @property
    def is_fork(self) -> bool:
        return bool(self.upstream_repo)


@dataclasses.dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int


class RepositorySyncError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class GitHubApiError(RepositorySyncError):
    pass


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _repo_root() -> Path:
    # .../apps/mission-control/agent_mission_control/repository_sync.py -> repo root
    return Path(__file__).resolve().parents[3]


def _env_path(name: str, defaults: Iterable[str]) -> tuple[str, ...]:
    explicit = (os.getenv(name) or "").strip()
    if explicit:
        return (os.path.expanduser(explicit),)
    return tuple(os.path.expanduser(value) for value in defaults)


def _ssh_arg_list() -> list[str]:
    # Avoid host config files with overly permissive permissions (e.g.
    # /etc/ssh/ssh_config.d/*.conf) from breaking non-interactive checks, while
    # keeping the behavior deterministic for controlled internal hosts.
    return [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-F",
        "/dev/null",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
    ]


def default_repository_registry() -> dict[str, RepoSpec]:
    """Return the owner-approved six-repository registry.

    Paths that are not a stable source contract are configurable by environment and
    have conservative discovery candidates. A missing checkout is reported as a
    repository status error instead of guessed or created automatically.
    """

    home = str(Path.home())
    remote_ssh = (os.getenv("REPO_SYNC_REMOTE_SSH") or DEFAULT_REMOTE_SSH).strip()
    router_ssh = (os.getenv("REPO_SYNC_9ROUTER_SSH") or remote_ssh).strip()
    proxy_ssh = (os.getenv("REPO_SYNC_LLAMA_PROXY_SSH") or remote_ssh).strip()
    hermes_agent_branch = (
        (os.getenv("REPO_SYNC_HERMES_AGENT_BRANCH") or "master").strip() or "master"
    )

    hermes_root = _repo_root()
    hermes_agent_paths = _env_path("REPO_SYNC_HERMES_AGENT_PATH", (str(hermes_root),))
    plugins_paths = _env_path(
        "REPO_SYNC_HERMES_PLUGINS_PATH",
        (
            f"{home}/hermes-plugins",
            f"{home}/.hermes/hermes-plugins",
            f"{home}/.hermes/repos/hermes-plugins",
        ),
    )
    agents_paths = _env_path(
        "REPO_SYNC_AGENTS_PATH",
        (
            f"{home}/agents",
            f"{home}/.hermes/agents",
            f"{home}/.hermes/repos/agents",
        ),
    )
    proxy_paths = _env_path(
        "REPO_SYNC_LLAMA_PROXY_PATH",
        ("/home/jarvis/llama-proxy", "/opt/llama-proxy", "/home/pi/llama-proxy"),
    )
    router_paths = _env_path(
        "REPO_SYNC_9ROUTER_PATH",
        ("/home/jarvis/9router", "/opt/9router", "/home/pi/9router"),
    )

    return {
        "9router": RepoSpec(
            name="9router",
            repo_full_name="novkien/9router",
            branch="master",
            transport="ssh",
            path_candidates=router_paths,
            ssh_target=router_ssh,
            upstream_repo="decolua/9router",
            private=False,
        ),
        "hermes-agent": RepoSpec(
            name="hermes-agent",
            repo_full_name="novkien/hermes-agent",
            branch=hermes_agent_branch,
            path_candidates=hermes_agent_paths,
            upstream_repo="NousResearch/hermes-agent",
            private=False,
        ),
        "hermes-skills": RepoSpec(
            name="hermes-skills",
            repo_full_name="novkien/hermes-skills",
            branch="master",
            # Canonical split git-dir/work-tree layout.
            git_dir=os.path.expanduser(
                os.getenv("REPO_SYNC_HERMES_SKILLS_GIT_DIR", "~/.hermes/repos/hermes-skills.git")
            ),
            work_tree=os.path.expanduser(
                os.getenv("REPO_SYNC_HERMES_SKILLS_WORK_TREE", "~/.hermes")
            ),
            private=True,
        ),
        "hermes-plugins": RepoSpec(
            name="hermes-plugins",
            repo_full_name="novkien/hermes-plugins",
            branch="master",
            path_candidates=plugins_paths,
            private=True,
        ),
        "agents": RepoSpec(
            name="agents",
            repo_full_name="novkien/agents",
            branch="master",
            path_candidates=agents_paths,
            private=True,
        ),
        "llama-proxy": RepoSpec(
            name="llama-proxy",
            repo_full_name="novkien/llama-proxy",
            branch="master",
            transport="ssh",
            path_candidates=proxy_paths,
            ssh_target=proxy_ssh,
            private=True,
        ),
    }


class OperationStore:
    """Small append-only operation history shared by CLI and dashboard."""

    def __init__(self, root: str | Path | None = None, *, rotate_bytes: int = 2_000_000):
        configured = root or os.getenv("REPO_SYNC_STATE_DIR")
        self.root = Path(configured or "~/.hermes/mission-control/repository-sync").expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "operations.jsonl"
        self.rotate_bytes = max(100_000, int(rotate_bytes))

    def append(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size >= self.rotate_bytes:
            previous = self.root / "operations.jsonl.1"
            with contextlib.suppress(OSError):
                previous.unlink()
            self.path.replace(previous)
        line = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def recent(self, repo: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        rows: list[dict[str, Any]] = []
        for path in (self.root / "operations.jsonl.1", self.path):
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for raw in handle:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            item = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if repo and item.get("repo") != repo:
                            continue
                        rows.append(item)
            except OSError:
                continue
        return rows[-limit:][::-1]

    def last(self, repo: str) -> dict[str, Any] | None:
        rows = self.recent(repo, 1)
        return rows[0] if rows else None

    @contextlib.contextmanager
    def lock(self, repo: str, *, wait_seconds: float = 0.0) -> Iterator[None]:
        lock_dir = self.root / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        path = lock_dir / f"{repo}.lock"
        with path.open("a+", encoding="utf-8") as handle:
            deadline = time.monotonic() + max(0.0, wait_seconds)
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise RepositorySyncError(
                            "repo_busy", f"repository operation already running: {repo}"
                        ) from exc
                    time.sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class GitHubRestClient:
    def __init__(self, token: str | None = None, *, timeout: int = 30):
        self.token = token or os.getenv("AGENTOS_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
        self.timeout = max(5, int(timeout))

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"https://api.github.com{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "AgentOS-Repository-Sync",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = UrlRequest(url, method=method.upper(), headers=headers, data=data)
        try:
            with urlopen(req, timeout=self.timeout) as response:  # noqa: S310 - fixed GitHub host
                raw = response.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"message": raw}
            raise GitHubApiError(
                "github_api_error",
                str(payload.get("message") or f"GitHub API HTTP {exc.code}"),
                details={"status": exc.code, "path": path, "response": payload},
            ) from exc
        except URLError as exc:
            raise GitHubApiError(
                "github_unavailable", f"GitHub API unavailable: {exc.reason}", details={"path": path}
            ) from exc

    def open_pulls(self, spec: RepoSpec, *, limit: int = 10) -> list[dict[str, Any]]:
        query = urlencode({"state": "open", "base": spec.branch, "per_page": min(limit, 30)})
        rows = self.request("GET", f"/repos/{spec.repo_full_name}/pulls?{query}")
        if not isinstance(rows, list):
            return []
        out = []
        for row in rows[:limit]:
            out.append(
                {
                    "number": row.get("number"),
                    "title": row.get("title"),
                    "draft": bool(row.get("draft")),
                    "state": row.get("state"),
                    "head": (row.get("head") or {}).get("ref"),
                    "head_sha": (row.get("head") or {}).get("sha"),
                    "base": (row.get("base") or {}).get("ref"),
                    "user": ((row.get("user") or {}).get("login")),
                    "updated_at": row.get("updated_at"),
                    "html_url": row.get("html_url"),
                }
            )
        return out

    def merge_pr_rebase(self, spec: RepoSpec, number: int, *, expected_head_sha: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"merge_method": "rebase"}
        if expected_head_sha:
            body["sha"] = expected_head_sha
        payload = self.request(
            "PUT", f"/repos/{spec.repo_full_name}/pulls/{int(number)}/merge", body
        )
        if not payload.get("merged"):
            raise GitHubApiError(
                "github_merge_rejected",
                str(payload.get("message") or "pull request was not merged"),
                details={"response": payload, "pull_number": int(number)},
            )
        return payload

    def sync_fork(self, spec: RepoSpec) -> dict[str, Any]:
        if not spec.upstream_repo:
            raise RepositorySyncError("not_a_fork", f"{spec.name} has no configured upstream")
        payload = self.request(
            "POST", f"/repos/{spec.repo_full_name}/merge-upstream", {"branch": spec.branch}
        )
        return payload if isinstance(payload, dict) else {"result": payload}

    def fork_drift(self, spec: RepoSpec) -> dict[str, Any] | None:
        if not spec.upstream_repo:
            return None
        upstream_owner, _ = spec.upstream_repo.split("/", 1)
        base = quote(spec.branch, safe="")
        head = quote(f"{spec.repo_full_name.split('/', 1)[0]}:{spec.branch}", safe=":")
        try:
            payload = self.request(
                "GET", f"/repos/{spec.upstream_repo}/compare/{base}...{head}"
            )
        except GitHubApiError as exc:
            return {"status": "unavailable", "error": exc.code, "message": str(exc)}
        return {
            "status": payload.get("status"),
            "ahead_by": payload.get("ahead_by"),
            "behind_by": payload.get("behind_by"),
            "total_commits": payload.get("total_commits"),
        }


class GitRunner:
    def __init__(self, *, timeout: int = DEFAULT_TIMEOUT_SECONDS):
        self.timeout = max(5, int(timeout))
        self._resolved_paths: dict[str, str] = {}

    def _run_process(self, argv: list[str], *, timeout: int | None = None) -> CommandResult:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                check=False,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            return CommandResult(
                argv=argv,
                returncode=proc.returncode,
                stdout=(proc.stdout or "").strip(),
                stderr=(proc.stderr or "").strip(),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except subprocess.TimeoutExpired as exc:
            raise RepositorySyncError(
                "command_timeout", f"command timed out after {timeout or self.timeout}s",
                details={"argv": argv, "stdout": exc.stdout, "stderr": exc.stderr},
            ) from exc
        except OSError as exc:
            raise RepositorySyncError(
                "command_unavailable", f"unable to execute {argv[0]}: {exc}", details={"argv": argv}
            ) from exc

    def resolve_path(self, spec: RepoSpec) -> str:
        if spec.git_dir and spec.work_tree:
            return spec.work_tree
        if spec.name in self._resolved_paths:
            return self._resolved_paths[spec.name]
        if not spec.path_candidates:
            raise RepositorySyncError("repo_path_missing", f"no checkout path configured for {spec.name}")

        if spec.transport == "ssh":
            if not spec.ssh_target:
                raise RepositorySyncError("ssh_target_missing", f"no SSH target configured for {spec.name}")
            checks = []
            for candidate in spec.path_candidates:
                q = shlex.quote(candidate)
                checks.append(
                    f"if git -C {q} rev-parse --is-inside-work-tree >/dev/null 2>&1; "
                    f"then printf '%s\\n' {q}; exit 0; fi"
                )
            remote = shlex.quote("; ".join(checks) + "; exit 4")
            result = self._run_process(["ssh", *_ssh_arg_list(), spec.ssh_target, "bash", "-c", remote])
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
        else:
            path = ""
            for candidate in spec.path_candidates:
                check = self._run_process(["git", "-C", candidate, "rev-parse", "--is-inside-work-tree"])
                if check.returncode == 0 and check.stdout == "true":
                    path = candidate
                    break
            if not path:
                raise RepositorySyncError(
                    "repo_not_found", f"no Git checkout found for {spec.name}",
                    details={"candidates": list(spec.path_candidates)},
                )

        self._resolved_paths[spec.name] = path
        return path

    def git(self, spec: RepoSpec, *args: str, timeout: int | None = None) -> CommandResult:
        if spec.git_dir and spec.work_tree:
            argv = [
                "git",
                f"--git-dir={spec.git_dir}",
                f"--work-tree={spec.work_tree}",
                *args,
            ]
        else:
            path = self.resolve_path(spec)
            argv = ["git", "-C", path, *args]

        if spec.transport != "ssh":
            return self._run_process(argv, timeout=timeout)
        if not spec.ssh_target:
            raise RepositorySyncError("ssh_target_missing", f"no SSH target configured for {spec.name}")
        remote = " ".join(shlex.quote(part) for part in argv)
        return self._run_process(
            ["ssh", *_ssh_arg_list(), spec.ssh_target, "bash", "-c", remote],
            timeout=timeout,
        )


class RepositorySyncService:
    def __init__(
        self,
        registry: dict[str, RepoSpec] | None = None,
        *,
        runner: GitRunner | None = None,
        store: OperationStore | None = None,
        github: GitHubRestClient | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.registry = registry or default_repository_registry()
        self.runner = runner or GitRunner(timeout=timeout)
        self.store = store or OperationStore()
        self.github = github or GitHubRestClient(timeout=min(timeout, 30))
        self.timeout = timeout

    def spec(self, name: str) -> RepoSpec:
        try:
            return self.registry[name]
        except KeyError as exc:
            raise RepositorySyncError("repo_unknown", f"unknown repository: {name}") from exc

    def _run_ok(self, spec: RepoSpec, *args: str) -> str:
        result = self.runner.git(spec, *args)
        if result.returncode != 0:
            raise RepositorySyncError(
                "git_command_failed",
                result.stderr or result.stdout or f"git {' '.join(args)} failed",
                details={"argv": result.argv, "returncode": result.returncode},
            )
        return result.stdout

    def _conflicts(self, spec: RepoSpec) -> list[str]:
        result = self.runner.git(spec, "diff", "--name-only", "--diff-filter=U")
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    def _dirty_summary(porcelain: str) -> dict[str, Any]:
        rows = [line for line in porcelain.splitlines() if line]
        modified = 0
        staged = 0
        untracked = 0
        deleted = 0
        conflicts = 0
        for row in rows:
            if row.startswith("??"):
                untracked += 1
                continue
            x = row[0] if len(row) > 0 else " "
            y = row[1] if len(row) > 1 else " "
            if x not in {" ", "?"}:
                staged += 1
            if y not in {" ", "?"}:
                modified += 1
            if "D" in (x, y):
                deleted += 1
            if x == "U" or y == "U" or (x, y) in {("A", "A"), ("D", "D")}:
                conflicts += 1
        return {
            "dirty": bool(rows),
            "entries": len(rows),
            "modified": modified,
            "staged": staged,
            "untracked": untracked,
            "deleted": deleted,
            "conflicts": conflicts,
            "porcelain": rows[:100],
        }

    def status(self, name: str, *, fetch: bool = True, include_github: bool = True) -> dict[str, Any]:
        spec = self.spec(name)
        started = time.monotonic()
        base: dict[str, Any] = {
            "name": spec.name,
            "repo_full_name": spec.repo_full_name,
            "branch": spec.branch,
            "transport": spec.transport,
            "host": spec.ssh_target if spec.transport == "ssh" else "local",
            "fork": spec.is_fork,
            "upstream_repo": spec.upstream_repo,
            "private": spec.private,
        }
        try:
            base["path"] = self.runner.resolve_path(spec)
            if fetch:
                fetched = self.runner.git(spec, "fetch", "--prune", "origin", spec.branch)
                if fetched.returncode != 0:
                    raise RepositorySyncError(
                        "fetch_failed", fetched.stderr or fetched.stdout or "git fetch failed",
                        details={"returncode": fetched.returncode},
                    )
            current_branch = self._run_ok(spec, "symbolic-ref", "--short", "HEAD")
            head = self._run_ok(spec, "rev-parse", "HEAD")
            remote = self._run_ok(spec, "rev-parse", f"origin/{spec.branch}")
            counts = self._run_ok(spec, "rev-list", "--left-right", "--count", f"HEAD...origin/{spec.branch}")
            parts = counts.split()
            ahead = int(parts[0]) if parts else 0
            behind = int(parts[1]) if len(parts) > 1 else 0
            porcelain = self._run_ok(spec, "status", "--porcelain=v1", "-uall")
            dirty = self._dirty_summary(porcelain)
            conflicts = self._conflicts(spec)
            origin_url = self._run_ok(spec, "config", "--get", "remote.origin.url")
            last_commit = self._run_ok(spec, "log", "-1", "--format=%cI%x00%s").split("\x00", 1)

            if conflicts:
                state = "conflict"
            elif current_branch != spec.branch:
                state = "wrong_branch"
            elif ahead and behind:
                state = "diverged"
            elif dirty["dirty"]:
                state = "dirty"
            elif behind:
                state = "behind"
            elif ahead:
                state = "ahead"
            else:
                state = "synced"

            base.update(
                {
                    "ok": True,
                    "state": state,
                    "current_branch": current_branch,
                    "local_sha": head,
                    "remote_sha": remote,
                    "ahead": ahead,
                    "behind": behind,
                    "origin_url": origin_url,
                    "working_tree": dirty,
                    "conflict_files": conflicts,
                    "last_commit_at": last_commit[0] if last_commit else None,
                    "last_commit_subject": last_commit[1] if len(last_commit) > 1 else None,
                }
            )
        except RepositorySyncError as exc:
            base.update(
                {
                    "ok": False,
                    "state": "error",
                    "error": {"code": exc.code, "message": str(exc), "details": exc.details},
                }
            )
        except Exception as exc:  # noqa: BLE001 - status must report, not crash the dashboard
            base.update(
                {
                    "ok": False,
                    "state": "error",
                    "error": {"code": "unexpected_error", "message": f"{type(exc).__name__}: {exc}"},
                }
            )

        base["last_operation"] = self.store.last(name)
        if include_github:
            try:
                base["pull_requests"] = self.github.open_pulls(spec)
                base["github_available"] = True
                base["upstream_drift"] = self.github.fork_drift(spec)
            except RepositorySyncError as exc:
                base["github_available"] = False
                base["github_error"] = {"code": exc.code, "message": str(exc)}
                base["pull_requests"] = []
                base["upstream_drift"] = None
        base["duration_ms"] = int((time.monotonic() - started) * 1000)
        return base

    def status_all(self, *, fetch: bool = True, include_github: bool = True) -> list[dict[str, Any]]:
        # Keep execution deterministic and low-pressure on SSH/GitHub. Six repos are
        # small enough that sequential status is easier to diagnose than interleaved
        # subprocess output.
        return [self.status(name, fetch=fetch, include_github=include_github) for name in self.registry]

    def _event_base(self, spec: RepoSpec, action: str, trigger: str) -> dict[str, Any]:
        return {
            "repo": spec.name,
            "repo_full_name": spec.repo_full_name,
            "branch": spec.branch,
            "host": spec.ssh_target if spec.transport == "ssh" else "local",
            "action": action,
            "trigger": trigger,
            "started_at": _now_iso(),
        }

    def _finish_event(self, event: dict[str, Any], *, ok: bool, **extra: Any) -> dict[str, Any]:
        event.update(extra)
        event["ok"] = ok
        event["finished_at"] = _now_iso()
        started = dt.datetime.fromisoformat(event["started_at"].replace("Z", "+00:00"))
        finished = dt.datetime.fromisoformat(event["finished_at"].replace("Z", "+00:00"))
        event["duration_ms"] = int((finished - started).total_seconds() * 1000)
        self.store.append(event)
        return event

    def _backup_branch(self, spec: RepoSpec, head: str) -> str:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"autosync-backup/{spec.name}/{stamp}-{head[:7]}"
        result = self.runner.git(spec, "branch", name, head)
        if result.returncode != 0:
            raise RepositorySyncError(
                "backup_branch_failed",
                result.stderr or result.stdout or "failed to create backup branch",
                details={"backup_branch": name, "returncode": result.returncode},
            )
        return name

    def _stash(self, spec: RepoSpec, trigger: str) -> tuple[str | None, str | None]:
        before = self.runner.git(spec, "rev-parse", "--verify", "stash@{0}")
        before_sha = before.stdout if before.returncode == 0 else None
        message = f"repo-sync:{trigger}:{spec.name}:{_now_iso()}"
        pushed = self.runner.git(spec, "stash", "push", "-u", "-m", message)
        if pushed.returncode != 0:
            raise RepositorySyncError(
                "stash_failed", pushed.stderr or pushed.stdout or "git stash failed",
                details={"returncode": pushed.returncode},
            )
        after = self.runner.git(spec, "rev-parse", "--verify", "stash@{0}")
        after_sha = after.stdout if after.returncode == 0 else None
        if not after_sha or after_sha == before_sha:
            return None, None
        return "stash@{0}", after_sha

    def _restore_stash(self, spec: RepoSpec, stash_ref: str | None) -> tuple[bool, list[str], str | None]:
        if not stash_ref:
            return True, [], None
        restored = self.runner.git(spec, "stash", "pop", "--index", stash_ref)
        if restored.returncode == 0:
            return True, [], None
        conflicts = self._conflicts(spec)
        current = self.runner.git(spec, "rev-parse", "--verify", "stash@{0}")
        # Git keeps the stash entry when pop fails. Report the concrete SHA so the
        # operator can recover even if newer stashes are added later.
        preserved = current.stdout if current.returncode == 0 else None
        return False, conflicts, preserved

    def sync(
        self,
        name: str,
        *,
        auto_commit: bool = False,
        commit_message: str | None = None,
        trigger: str = "manual",
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        spec = self.spec(name)
        event = self._event_base(spec, "sync", trigger)
        backup_branch: str | None = None
        stash_ref: str | None = None
        stash_sha: str | None = None
        try:
            with self.store.lock(name, wait_seconds=wait_seconds):
                before = self.status(name, fetch=True, include_github=False)
                event["before"] = before
                if not before.get("ok"):
                    error = before.get("error") or {}
                    raise RepositorySyncError(
                        str(error.get("code") or "status_failed"),
                        str(error.get("message") or "repository status failed"),
                        details=error.get("details") or {},
                    )
                if before.get("current_branch") != spec.branch:
                    raise RepositorySyncError(
                        "wrong_branch",
                        f"{spec.name} is on {before.get('current_branch')!r}; expected {spec.branch!r}",
                    )
                if before.get("conflict_files"):
                    raise RepositorySyncError(
                        "preexisting_conflict",
                        "repository already contains unresolved conflicts",
                        details={"conflict_files": before.get("conflict_files")},
                    )

                head = str(before.get("local_sha") or "")
                if int(before.get("ahead") or 0) > 0 and int(before.get("behind") or 0) > 0:
                    backup_branch = self._backup_branch(spec, head)

                dirty = bool((before.get("working_tree") or {}).get("dirty"))
                if dirty:
                    stash_ref, stash_sha = self._stash(spec, trigger)

                pulled = self.runner.git(
                    spec, "pull", "--rebase", "--no-autostash", "origin", spec.branch,
                    timeout=max(self.timeout, 180),
                )
                if pulled.returncode != 0:
                    conflicts = self._conflicts(spec)
                    self.runner.git(spec, "rebase", "--abort")
                    restore_ok, restore_conflicts, preserved = self._restore_stash(spec, stash_ref)
                    code = "rebase_conflict" if conflicts else "pull_failed"
                    raise RepositorySyncError(
                        code,
                        pulled.stderr or pulled.stdout or "git pull --rebase failed",
                        details={
                            "conflict_files": conflicts,
                            "stash_restore_ok": restore_ok,
                            "stash_restore_conflicts": restore_conflicts,
                            "stash_sha": preserved or stash_sha,
                            "backup_branch": backup_branch,
                            "returncode": pulled.returncode,
                        },
                    )

                restore_ok, restore_conflicts, preserved = self._restore_stash(spec, stash_ref)
                if not restore_ok:
                    raise RepositorySyncError(
                        "stash_restore_conflict",
                        "remote update succeeded but restoring local working changes conflicted",
                        details={
                            "conflict_files": restore_conflicts,
                            "stash_sha": preserved or stash_sha,
                            "backup_branch": backup_branch,
                            "recovery_note": "stash entry was preserved; no reset/clean was performed",
                        },
                    )

                committed_sha = None
                if auto_commit:
                    after_restore = self._run_ok(spec, "status", "--porcelain=v1", "-uall")
                    if after_restore.strip():
                        added = self.runner.git(spec, "add", "-A")
                        if added.returncode != 0:
                            raise RepositorySyncError(
                                "git_add_failed", added.stderr or added.stdout or "git add failed"
                            )
                        message = (
                            commit_message
                            or f"chore: preserve local {spec.name} changes after auto-sync"
                        )
                        committed = self.runner.git(spec, "commit", "-m", message)
                        if committed.returncode != 0:
                            raise RepositorySyncError(
                                "commit_failed",
                                committed.stderr or committed.stdout or "git commit failed",
                                details={"returncode": committed.returncode},
                            )
                        committed_sha = self._run_ok(spec, "rev-parse", "HEAD")

                after = self.status(name, fetch=False, include_github=False)
                return self._finish_event(
                    event,
                    ok=True,
                    status="ok",
                    backup_branch=backup_branch,
                    stash_sha=None,
                    auto_commit=auto_commit,
                    committed_sha=committed_sha,
                    after=after,
                )
        except RepositorySyncError as exc:
            return self._finish_event(
                event,
                ok=False,
                status="error",
                backup_branch=backup_branch,
                stash_sha=stash_sha,
                error={"code": exc.code, "message": str(exc), "details": exc.details},
            )
        except Exception as exc:  # noqa: BLE001
            return self._finish_event(
                event,
                ok=False,
                status="error",
                backup_branch=backup_branch,
                stash_sha=stash_sha,
                error={"code": "unexpected_error", "message": f"{type(exc).__name__}: {exc}"},
            )

    def commit_local(
        self,
        name: str,
        *,
        message: str | None = None,
        trigger: str = "manual",
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        spec = self.spec(name)
        event = self._event_base(spec, "commit", trigger)
        try:
            with self.store.lock(name, wait_seconds=wait_seconds):
                before = self.status(name, fetch=False, include_github=False)
                event["before"] = before
                if not before.get("ok"):
                    raise RepositorySyncError("status_failed", "repository status failed", details=before)
                if before.get("conflict_files"):
                    raise RepositorySyncError(
                        "preexisting_conflict",
                        "cannot commit automatically while conflicts are unresolved",
                        details={"conflict_files": before.get("conflict_files")},
                    )
                if not (before.get("working_tree") or {}).get("dirty"):
                    return self._finish_event(event, ok=True, status="noop", message="working tree clean")
                added = self.runner.git(spec, "add", "-A")
                if added.returncode != 0:
                    raise RepositorySyncError("git_add_failed", added.stderr or added.stdout or "git add failed")
                commit = self.runner.git(
                    spec, "commit", "-m", message or f"chore: save local {spec.name} changes"
                )
                if commit.returncode != 0:
                    raise RepositorySyncError("commit_failed", commit.stderr or commit.stdout or "git commit failed")
                sha = self._run_ok(spec, "rev-parse", "HEAD")
                after = self.status(name, fetch=False, include_github=False)
                return self._finish_event(event, ok=True, status="ok", committed_sha=sha, after=after)
        except RepositorySyncError as exc:
            return self._finish_event(
                event, ok=False, status="error",
                error={"code": exc.code, "message": str(exc), "details": exc.details},
            )

    def sync_upstream(
        self,
        name: str,
        *,
        trigger: str = "manual",
        pull_after: bool = True,
        auto_commit: bool = True,
    ) -> dict[str, Any]:
        spec = self.spec(name)
        event = self._event_base(spec, "sync_upstream", trigger)
        try:
            payload = self.github.sync_fork(spec)
            event = self._finish_event(event, ok=True, status="ok", github=payload)
        except RepositorySyncError as exc:
            return self._finish_event(
                event, ok=False, status="error",
                error={"code": exc.code, "message": str(exc), "details": exc.details},
            )
        if pull_after:
            event["production_sync"] = self.sync(
                name, auto_commit=auto_commit, trigger=f"{trigger}:upstream-sync"
            )
            event["ok"] = bool(event["ok"] and event["production_sync"].get("ok"))
        return event

    def merge_pull_request_rebase(
        self,
        name: str,
        number: int,
        *,
        expected_head_sha: str | None = None,
        trigger: str = "manual",
        pull_after: bool = True,
        auto_commit: bool = True,
    ) -> dict[str, Any]:
        spec = self.spec(name)
        event = self._event_base(spec, "rebase_merge_pr", trigger)
        event["pull_number"] = int(number)
        try:
            payload = self.github.merge_pr_rebase(
                spec, int(number), expected_head_sha=expected_head_sha
            )
            event = self._finish_event(event, ok=True, status="ok", github=payload)
        except RepositorySyncError as exc:
            return self._finish_event(
                event, ok=False, status="error",
                error={"code": exc.code, "message": str(exc), "details": exc.details},
            )
        if pull_after:
            event["production_sync"] = self.sync(
                name, auto_commit=auto_commit, trigger=f"{trigger}:pr-merge"
            )
            event["ok"] = bool(event["ok"] and event["production_sync"].get("ok"))
        return event

    def automation_commands(self) -> dict[str, str]:
        script = _repo_root() / "apps" / "mission-control" / "tools" / "repo_sync.py"
        quoted = shlex.quote(str(script))
        return {
            "cron": (
                f"/usr/bin/python3 {quoted} --all --sync --auto-commit --json --trigger cron"
            ),
            "hook_template": (
                f"/usr/bin/python3 {quoted} --repo <repo> --sync --auto-commit --json --trigger hook"
            ),
            "status": f"/usr/bin/python3 {quoted} --all --status --json --trigger manual",
        }
