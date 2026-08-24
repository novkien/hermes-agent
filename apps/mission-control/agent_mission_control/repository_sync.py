"""Owner-only repository control for AgentOS Mission Control.

The registry is the single source of truth. A canonical-mode entry may keep a
separate common Git directory for compatibility, but deployed configuration
marks only the root Hermes superproject as locally managed. Every other entry
is remote-only.

The registry explicitly distinguishes the one locally managed Hermes
superproject from remote-only GitHub repositories. Remote-only cards never
inspect or mutate a local worktree. Their PR controls act on GitHub only. A
managed child merge creates or advances an aggregate GitHub PR containing the
exact parent gitlink. The Hermes card delegates local convergence to the
superproject's checked-in sync script only after that parent PR is merged.
"""

from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor
import dataclasses
import datetime as dt
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
from typing import Any, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request as UrlRequest, urlopen


GITHUB_API_VERSION = "2022-11-28"
DEFAULT_TIMEOUT_SECONDS = 90
INITIALIZE_FETCH_TIMEOUT_SECONDS = 300
SUPERPROJECT_BRANCH_PREFIX = "mission-control/hermes-gitlinks-"
_CODEX_LOGIN = "chatgpt-codex-connector"
_CODEX_REVIEW_RE = re.compile(r"Reviewed commit:\*{0,2}\s*`([0-9a-fA-F]{7,64})`")
_FAILED_CONCLUSIONS = {
    "failure",
    "timed_out",
    "cancelled",
    "action_required",
    "startup_failure",
    "stale",
}


@dataclasses.dataclass(frozen=True)
class HostSpec:
    name: str
    transport: str
    ssh_target: str | None
    hermes_home: str


@dataclasses.dataclass(frozen=True)
class RepoSpec:
    name: str
    repo_full_name: str
    branch: str
    host: str
    transport: str
    ssh_target: str | None
    hermes_home: str
    git_dir: str
    work_tree: str
    origin_url: str
    local_mode: str = "canonical"
    sync_script: str | None = None
    superproject_path: str | None = None
    scope_paths: tuple[str, ...] = ()
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
    def __init__(
        self, code: str, message: str, *, details: dict[str, Any] | None = None
    ):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class GitHubApiError(RepositorySyncError):
    pass


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _registry_path() -> Path:
    configured = (os.getenv("HERMES_REPOSITORY_REGISTRY") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return _repo_root() / "apps" / "mission-control" / "config" / "repositories.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - Hermes runtime ships PyYAML
        raise RepositorySyncError(
            "registry_dependency_missing",
            "PyYAML is required to load repositories.yaml",
        ) from exc
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RepositorySyncError(
            "registry_missing", f"repository registry not found: {path}"
        ) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise RepositorySyncError("registry_invalid", str(exc)) from exc
    if not isinstance(payload, dict):
        raise RepositorySyncError(
            "registry_invalid", "repository registry must be a mapping"
        )
    return payload


def _clean_name(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text or not re.fullmatch(r"[A-Za-z0-9._-]+", text):
        raise RepositorySyncError("registry_invalid", f"invalid {field}: {value!r}")
    return text


def _clean_repo(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text):
        raise RepositorySyncError(
            "registry_invalid", f"invalid GitHub repository: {value!r}"
        )
    return text


def _join_home(home: str, template: str, repository: str) -> str:
    relative = template.format(repository=repository).strip().lstrip("/")
    if ".." in PurePosixPath(relative).parts:
        raise RepositorySyncError(
            "registry_invalid", f"unsafe registry layout: {template!r}"
        )
    return f"{home.rstrip('/')}/{relative}"


def _live_path(home: str, value: Any, *, repository: str) -> str:
    relative = str(value or "").strip()
    if not relative:
        raise RepositorySyncError(
            "registry_invalid", f"repository {repository} has no work_tree"
        )
    if relative in {".", "./"}:
        return home.rstrip("/")
    if relative == "~":
        relative = ""
    elif relative.startswith("~/"):
        relative = relative[2:]
    elif relative.startswith("$HOME/"):
        relative = relative[6:]
    path = PurePosixPath(relative)
    if ".." in path.parts:
        raise RepositorySyncError(
            "registry_invalid", f"unsafe work_tree for {repository}: {relative!r}"
        )
    if path.is_absolute():
        return str(path)
    if not str(path):
        return home.rstrip("/")
    return f"{home.rstrip('/')}/{str(path).lstrip('/')}"


def _scope_paths(value: Any, *, repository: str) -> tuple[str, ...]:
    if value in (None, []):
        return ()
    if not isinstance(value, list):
        raise RepositorySyncError(
            "registry_invalid", f"repository {repository} paths must be a list"
        )
    rows: list[str] = []
    for raw in value:
        item = str(raw or "").strip().strip("/")
        path = PurePosixPath(item)
        if not item or path.is_absolute() or ".." in path.parts or item == ".":
            raise RepositorySyncError(
                "registry_invalid", f"unsafe scoped path for {repository}: {raw!r}"
            )
        rows.append(str(path))
    return tuple(dict.fromkeys(rows))


def _superproject_path(value: Any, *, repository: str) -> str | None:
    if value in (None, ""):
        return None
    item = str(value).strip().strip("/")
    path = PurePosixPath(item)
    if not item or path.is_absolute() or ".." in path.parts or item == ".":
        raise RepositorySyncError(
            "registry_invalid", f"unsafe superproject_path for {repository}: {value!r}"
        )
    return str(path)


def load_repository_registry(path: str | Path | None = None) -> dict[str, RepoSpec]:
    source = Path(path).expanduser() if path is not None else _registry_path()
    payload = _load_yaml(source)
    if int(payload.get("version") or 0) != 1:
        raise RepositorySyncError(
            "registry_invalid", "repositories.yaml version must be 1"
        )

    layout = payload.get("layout")
    hosts_raw = payload.get("hosts")
    repos_raw = payload.get("repositories")
    if (
        not isinstance(layout, dict)
        or not isinstance(hosts_raw, dict)
        or not isinstance(repos_raw, dict)
    ):
        raise RepositorySyncError(
            "registry_invalid",
            "registry requires layout, hosts, and repositories mappings",
        )

    git_template = str(layout.get("git_dir") or "repos/{repository}.git")
    hosts: dict[str, HostSpec] = {}
    for raw_name, raw_spec in hosts_raw.items():
        name = _clean_name(raw_name, field="host name")
        if not isinstance(raw_spec, dict):
            raise RepositorySyncError(
                "registry_invalid", f"host {name} must be a mapping"
            )
        transport = str(raw_spec.get("transport") or "local").strip().lower()
        if transport not in {"local", "ssh"}:
            raise RepositorySyncError(
                "registry_invalid", f"unsupported transport for {name}"
            )
        ssh_target = str(raw_spec.get("ssh_target") or "").strip() or None
        env_name = f"HERMES_REPOSITORY_HOST_{name.upper().replace('-', '_')}_SSH"
        ssh_target = (os.getenv(env_name) or ssh_target or "").strip() or None
        if transport == "ssh" and not ssh_target:
            raise RepositorySyncError(
                "registry_invalid", f"SSH host {name} has no ssh_target"
            )
        home = str(raw_spec.get("hermes_home") or "~/.hermes").strip()
        if not home:
            raise RepositorySyncError(
                "registry_invalid", f"host {name} has no hermes_home"
            )
        hosts[name] = HostSpec(name, transport, ssh_target, home)

    registry: dict[str, RepoSpec] = {}
    for raw_name, raw_spec in repos_raw.items():
        name = _clean_name(raw_name, field="repository name")
        if not isinstance(raw_spec, dict):
            raise RepositorySyncError(
                "registry_invalid", f"repository {name} must be a mapping"
            )
        repo_full_name = _clean_repo(raw_spec.get("github"))
        branch = _clean_name(raw_spec.get("branch"), field=f"{name} branch")
        host_name = _clean_name(raw_spec.get("host"), field=f"{name} host")
        if host_name not in hosts:
            raise RepositorySyncError(
                "registry_invalid", f"unknown host {host_name!r} for {name}"
            )
        host = hosts[host_name]
        visibility = str(raw_spec.get("visibility") or "private").strip().lower()
        work_tree = _live_path(
            host.hermes_home, raw_spec.get("work_tree"), repository=name
        )
        local_mode = str(raw_spec.get("local_mode") or "canonical").strip().lower()
        if local_mode not in {"canonical", "superproject", "remote_only"}:
            raise RepositorySyncError(
                "registry_invalid", f"unsupported local_mode for {name}: {local_mode!r}"
            )
        sync_script = None
        if raw_spec.get("sync_script") is not None:
            sync_script = _live_path(
                host.hermes_home, raw_spec.get("sync_script"), repository=name
            )
        if local_mode == "superproject" and not sync_script:
            raise RepositorySyncError(
                "registry_invalid", f"superproject repository {name} has no sync_script"
            )
        scope_paths = _scope_paths(raw_spec.get("paths"), repository=name)
        superproject_path = _superproject_path(
            raw_spec.get("superproject_path"), repository=name
        )
        if superproject_path and local_mode != "remote_only":
            raise RepositorySyncError(
                "registry_invalid",
                f"superproject_path is valid only for remote-only repository {name}",
            )
        upstream = str(raw_spec.get("upstream") or "").strip() or None
        if upstream:
            upstream = _clean_repo(upstream)
        # Repository operations can run on SSH-managed hosts that have HTTPS
        # credentials (or a public repository) but no GitHub SSH key.  Keep
        # host transport and GitHub transport independent: HTTPS is the
        # portable default, while an explicit origin_url can still opt into
        # SSH or another Git transport.
        origin_url = str(
            raw_spec.get("origin_url") or f"https://github.com/{repo_full_name}.git"
        ).strip()
        if not origin_url or any(ch in origin_url for ch in "\r\n\x00"):
            raise RepositorySyncError(
                "registry_invalid", f"invalid origin URL for {name}"
            )
        registry[name] = RepoSpec(
            name=name,
            repo_full_name=repo_full_name,
            branch=branch,
            host=host_name,
            transport=host.transport,
            ssh_target=host.ssh_target,
            hermes_home=host.hermes_home,
            git_dir=_join_home(
                host.hermes_home, str(raw_spec.get("git_dir") or git_template), name
            ),
            work_tree=work_tree,
            origin_url=origin_url,
            local_mode=local_mode,
            sync_script=sync_script,
            superproject_path=superproject_path,
            scope_paths=scope_paths,
            upstream_repo=upstream,
            private=visibility != "public",
        )
    if not registry:
        raise RepositorySyncError("registry_invalid", "repository registry is empty")
    superprojects = [
        spec for spec in registry.values() if spec.local_mode == "superproject"
    ]
    if len(superprojects) != 1:
        raise RepositorySyncError(
            "registry_invalid", "registry requires exactly one superproject repository"
        )
    return registry


def default_repository_registry() -> dict[str, RepoSpec]:
    return load_repository_registry()


class OperationStore:
    """One bounded append-only owner-operation log under HERMES_HOME/state."""

    def __init__(
        self, root: str | Path | None = None, *, rotate_bytes: int = 2_000_000
    ):
        configured = root or os.getenv("HERMES_REPOSITORY_STATE_DIR")
        self.root = Path(
            configured or "~/.hermes/state/repository-control"
        ).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "operations.jsonl"
        self.rotate_bytes = max(100_000, int(rotate_bytes))

    def append(self, payload: dict[str, Any]) -> None:
        if self.path.exists() and self.path.stat().st_size >= self.rotate_bytes:
            previous = self.root / "operations.jsonl.1"
            with contextlib.suppress(OSError):
                previous.unlink()
            self.path.replace(previous)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            )

    def recent(self, repo: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item_path in (self.root / "operations.jsonl.1", self.path):
            if not item_path.exists():
                continue
            with contextlib.suppress(OSError):
                for raw in item_path.read_text(encoding="utf-8").splitlines():
                    try:
                        item = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(item, dict) and (
                        not repo or item.get("repo") == repo
                    ):
                        rows.append(item)
        return rows[-max(1, min(int(limit), 200)) :][::-1]

    def last(self, repo: str) -> dict[str, Any] | None:
        rows = self.recent(repo, 1)
        return rows[0] if rows else None

    @contextlib.contextmanager
    def lock(self, repo: str, *, wait_seconds: float = 0.0) -> Iterator[None]:
        lock_dir = self.root / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        with (lock_dir / f"{repo}.lock").open("a+", encoding="utf-8") as handle:
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
        self.token = (
            token or os.getenv("AGENTOS_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
        )
        self.timeout = max(5, int(timeout))

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        url = f"https://api.github.com{path}"
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "AgentOS-Repository-Control",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = UrlRequest(url, method=method.upper(), headers=headers, data=data)
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
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
                "github_unavailable",
                f"GitHub API unavailable: {exc.reason}",
                details={"path": path},
            ) from exc

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = self.request(
            "POST", "/graphql", {"query": query, "variables": variables}
        )
        if not isinstance(payload, dict) or payload.get("errors"):
            message = "GitHub GraphQL request failed"
            if isinstance(payload, dict) and payload.get("errors"):
                message = str(payload["errors"][0].get("message") or message)
            raise GitHubApiError(
                "github_graphql_error", message, details={"response": payload}
            )
        return payload

    @staticmethod
    def _owner_repo(spec: RepoSpec) -> tuple[str, str]:
        return tuple(spec.repo_full_name.split("/", 1))  # type: ignore[return-value]

    def pull_detail(self, spec: RepoSpec, number: int) -> dict[str, Any]:
        payload = self.request(
            "GET", f"/repos/{spec.repo_full_name}/pulls/{int(number)}"
        )
        if not isinstance(payload, dict):
            raise GitHubApiError(
                "github_response_invalid", "invalid pull request response"
            )
        return payload

    def _checks(self, spec: RepoSpec, head_sha: str) -> dict[str, Any]:
        runs = self.request(
            "GET",
            f"/repos/{spec.repo_full_name}/commits/{head_sha}/check-runs?per_page=100",
            accept="application/vnd.github+json",
        )
        statuses = self.request(
            "GET",
            f"/repos/{spec.repo_full_name}/commits/{head_sha}/status?per_page=100",
        )
        check_rows = runs.get("check_runs", []) if isinstance(runs, dict) else []
        status_rows = statuses.get("statuses", []) if isinstance(statuses, dict) else []
        pending = 0
        failed = 0
        passed = 0
        normalized: list[dict[str, Any]] = []
        for row in check_rows if isinstance(check_rows, list) else []:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "")
            conclusion = str(row.get("conclusion") or "") or None
            if status != "completed":
                pending += 1
            elif conclusion in _FAILED_CONCLUSIONS:
                failed += 1
            elif conclusion in {"success", "neutral", "skipped"}:
                passed += 1
            normalized.append({
                "name": row.get("name"),
                "status": status,
                "conclusion": conclusion,
                "url": row.get("html_url"),
                "completed_at": row.get("completed_at"),
            })
        for row in status_rows if isinstance(status_rows, list) else []:
            if not isinstance(row, dict):
                continue
            state = str(row.get("state") or "")
            if state == "pending":
                pending += 1
            elif state in {"failure", "error"}:
                failed += 1
            elif state == "success":
                passed += 1
            normalized.append({
                "name": row.get("context"),
                "status": state,
                "conclusion": state,
                "url": row.get("target_url"),
                "completed_at": row.get("updated_at"),
            })
        if failed:
            state = "failed"
        elif pending:
            state = "pending"
        elif normalized:
            state = "passed"
        else:
            state = "none"
        return {
            "state": state,
            "total": len(normalized),
            "passed": passed,
            "pending": pending,
            "failed": failed,
            "items": normalized,
        }

    def _review_threads(self, spec: RepoSpec, number: int) -> list[dict[str, Any]]:
        owner, repo = self._owner_repo(spec)
        query = """
        query PullReviewThreads($owner: String!, $repo: String!, $number: Int!) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $number) {
              reviewThreads(first: 100) {
                nodes {
                  id
                  isResolved
                  comments(first: 30) {
                    nodes { author { login } body createdAt url commit { oid } }
                  }
                }
              }
            }
          }
        }
        """
        try:
            payload = self.graphql(
                query, {"owner": owner, "repo": repo, "number": int(number)}
            )
        except RepositorySyncError:
            return []
        nodes = (
            (
                ((payload.get("data") or {}).get("repository") or {}).get("pullRequest")
                or {}
            )
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        rows: list[dict[str, Any]] = []
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, dict):
                continue
            comments = (node.get("comments") or {}).get("nodes") or []
            rows.append({
                "id": node.get("id"),
                "resolved": bool(node.get("isResolved")),
                "comments": [
                    {
                        "author": ((comment.get("author") or {}).get("login")),
                        "body": comment.get("body"),
                        "created_at": comment.get("createdAt"),
                        "url": comment.get("url"),
                        "commit_sha": ((comment.get("commit") or {}).get("oid")),
                    }
                    for comment in comments
                    if isinstance(comment, dict)
                ],
            })
        return rows

    def _codex_state(
        self, spec: RepoSpec, number: int, head_sha: str, updated_at: str | None
    ) -> dict[str, Any]:
        reviews = self.request(
            "GET",
            f"/repos/{spec.repo_full_name}/pulls/{int(number)}/reviews?per_page=100",
        )
        comments = self.request(
            "GET",
            f"/repos/{spec.repo_full_name}/issues/{int(number)}/comments?per_page=100",
        )
        threads = self._review_threads(spec, number)
        codex_reviews = (
            [
                row
                for row in reviews
                if isinstance(row, dict)
                and ((row.get("user") or {}).get("login") == _CODEX_LOGIN)
            ]
            if isinstance(reviews, list)
            else []
        )
        latest = codex_reviews[-1] if codex_reviews else None
        reviewed_sha = None
        if latest:
            body = str(latest.get("body") or "")
            match = _CODEX_REVIEW_RE.search(body)
            reviewed_sha = (
                match.group(1) if match else str(latest.get("commit_id") or "") or None
            )
        current = bool(
            reviewed_sha
            and (head_sha.startswith(reviewed_sha) or reviewed_sha.startswith(head_sha))
        )
        codex_threads = [
            row
            for row in threads
            if any(
                comment.get("author") == _CODEX_LOGIN
                for comment in row.get("comments", [])
            )
        ]
        unresolved = [row for row in codex_threads if not row.get("resolved")]
        requests = (
            [
                row
                for row in comments
                if isinstance(row, dict)
                and "@codex review" in str(row.get("body") or "").lower()
            ]
            if isinstance(comments, list)
            else []
        )
        latest_review_at = str(latest.get("submitted_at") or "") if latest else ""
        latest_request_at = (
            str(requests[-1].get("created_at") or "") if requests else ""
        )
        review_requested_after_latest = bool(
            requests and (not latest or latest_request_at > latest_review_at)
        )
        if latest and current and unresolved:
            state = "has_findings"
        elif latest and current:
            state = "reviewed"
        elif review_requested_after_latest:
            state = "requested"
        elif latest:
            state = "stale"
        else:
            state = "not_requested"
        return {
            "state": state,
            "reviewed_sha": reviewed_sha,
            "current_head": current,
            "reviewed_at": latest.get("submitted_at") if latest else None,
            "requested_at": requests[-1].get("created_at") if requests else None,
            "unresolved_threads": len(unresolved),
            "threads": codex_threads,
            "pr_updated_at": updated_at,
        }

    def pull_control_state(self, spec: RepoSpec, number: int) -> dict[str, Any]:
        row = self.pull_detail(spec, number)
        head_sha = str((row.get("head") or {}).get("sha") or "")
        return {
            "number": int(row.get("number") or number),
            "title": row.get("title"),
            "state": row.get("state"),
            "draft": bool(row.get("draft")),
            "merged": bool(row.get("merged")),
            "merged_at": row.get("merged_at"),
            "mergeable": row.get("mergeable"),
            "mergeable_state": row.get("mergeable_state"),
            "head": (row.get("head") or {}).get("ref"),
            "head_sha": head_sha,
            "base": (row.get("base") or {}).get("ref"),
            "user": ((row.get("user") or {}).get("login")),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "html_url": row.get("html_url"),
            "checks": self._checks(spec, head_sha)
            if head_sha
            else {"state": "none", "items": []},
            "codex": self._codex_state(spec, number, head_sha, row.get("updated_at")),
        }

    def open_pulls(self, spec: RepoSpec, *, limit: int = 20) -> list[dict[str, Any]]:
        query = urlencode({
            "state": "open",
            "base": spec.branch,
            "sort": "updated",
            "direction": "desc",
            "per_page": min(max(1, limit), 30),
        })
        rows = self.request("GET", f"/repos/{spec.repo_full_name}/pulls?{query}")
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            number = row.get("number")
            if isinstance(number, int):
                try:
                    out.append(self.pull_control_state(spec, number))
                except RepositorySyncError as exc:
                    out.append({
                        "number": number,
                        "title": row.get("title"),
                        "draft": bool(row.get("draft")),
                        "head": (row.get("head") or {}).get("ref"),
                        "head_sha": (row.get("head") or {}).get("sha"),
                        "base": (row.get("base") or {}).get("ref"),
                        "html_url": row.get("html_url"),
                        "error": {"code": exc.code, "message": str(exc)},
                    })
        return out

    def request_codex_review(
        self, spec: RepoSpec, number: int, *, expected_head_sha: str | None = None
    ) -> dict[str, Any]:
        pull = self.pull_detail(spec, number)
        head_sha = str((pull.get("head") or {}).get("sha") or "")
        if expected_head_sha and head_sha != expected_head_sha:
            raise GitHubApiError(
                "pull_head_moved",
                "pull request head changed before review request",
                details={"expected": expected_head_sha, "actual": head_sha},
            )
        comment = self.request(
            "POST",
            f"/repos/{spec.repo_full_name}/issues/{int(number)}/comments",
            {"body": "@codex review"},
        )
        return {"requested": True, "head_sha": head_sha, "comment": comment}

    def _draft_mutation(
        self, spec: RepoSpec, number: int, *, ready: bool
    ) -> dict[str, Any]:
        pull = self.pull_detail(spec, number)
        node_id = str(pull.get("node_id") or "")
        if not node_id:
            raise GitHubApiError(
                "github_draft_change_failed", "GitHub did not return a PR node id"
            )
        field = (
            "markPullRequestReadyForReview" if ready else "convertPullRequestToDraft"
        )
        query = f"""
        mutation ChangeDraft($id: ID!) {{
          {field}(input: {{pullRequestId: $id}}) {{ pullRequest {{ id isDraft }} }}
        }}
        """
        payload = self.graphql(query, {"id": node_id})
        changed = ((payload.get("data") or {}).get(field) or {}).get("pullRequest")
        if not isinstance(changed, dict):
            raise GitHubApiError(
                "github_draft_change_failed", "GitHub did not change PR draft state"
            )
        return changed

    def mark_ready(self, spec: RepoSpec, number: int) -> dict[str, Any]:
        return self._draft_mutation(spec, number, ready=True)

    def mark_draft(self, spec: RepoSpec, number: int) -> dict[str, Any]:
        return self._draft_mutation(spec, number, ready=False)

    def merge_pr_rebase(
        self, spec: RepoSpec, number: int, *, expected_head_sha: str | None = None
    ) -> dict[str, Any]:
        pull = self.pull_detail(spec, number)
        actual_head = str((pull.get("head") or {}).get("sha") or "")
        if expected_head_sha and actual_head != expected_head_sha:
            raise GitHubApiError(
                "pull_head_moved",
                "pull request head changed before merge",
                details={"expected": expected_head_sha, "actual": actual_head},
            )
        body: dict[str, Any] = {"merge_method": "rebase"}
        if actual_head:
            body["sha"] = actual_head
        payload = self.request(
            "PUT", f"/repos/{spec.repo_full_name}/pulls/{int(number)}/merge", body
        )
        if not isinstance(payload, dict) or not payload.get("merged"):
            raise GitHubApiError(
                "github_merge_rejected",
                str((payload or {}).get("message") or "pull request was not merged"),
                details={"response": payload, "pull_number": int(number)},
            )
        confirmed = self.pull_detail(spec, number)
        if not confirmed.get("merged"):
            raise GitHubApiError(
                "github_merge_unconfirmed",
                "GitHub returned merged=true but PR confirmation is not merged",
                details={"response": confirmed},
            )
        return {
            "merged": True,
            "merge_sha": payload.get("sha"),
            "message": payload.get("message"),
            "head_sha": actual_head,
            "merged_at": confirmed.get("merged_at"),
            "html_url": confirmed.get("html_url"),
        }

    def _branch_sha(self, spec: RepoSpec, branch: str) -> str:
        encoded = quote(branch, safe="")
        payload = self.request(
            "GET", f"/repos/{spec.repo_full_name}/git/ref/heads/{encoded}"
        )
        sha = str(((payload or {}).get("object") or {}).get("sha") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            raise GitHubApiError(
                "github_response_invalid",
                f"GitHub did not return a valid branch SHA for {spec.repo_full_name}:{branch}",
            )
        return sha.lower()

    def branch_sha(self, spec: RepoSpec) -> str:
        return self._branch_sha(spec, spec.branch)

    def _commit_tree_sha(self, spec: RepoSpec, commit_sha: str) -> str:
        payload = self.request(
            "GET", f"/repos/{spec.repo_full_name}/git/commits/{commit_sha}"
        )
        tree_sha = str(((payload or {}).get("tree") or {}).get("sha") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", tree_sha):
            raise GitHubApiError(
                "github_response_invalid",
                f"GitHub did not return a tree for commit {commit_sha}",
            )
        return tree_sha.lower()

    def _content_sha(self, spec: RepoSpec, path: str, *, ref: str) -> str:
        encoded_path = quote(path, safe="/")
        query = urlencode({"ref": ref})
        payload = self.request(
            "GET", f"/repos/{spec.repo_full_name}/contents/{encoded_path}?{query}"
        )
        sha = str((payload or {}).get("sha") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            raise GitHubApiError(
                "github_response_invalid",
                f"GitHub did not return a gitlink SHA for {path} at {ref}",
            )
        return sha.lower()

    def _open_gitlink_pull(self, spec: RepoSpec) -> dict[str, Any] | None:
        query = urlencode({
            "state": "open",
            "base": spec.branch,
            "sort": "created",
            "direction": "asc",
            "per_page": 100,
        })
        rows = self.request("GET", f"/repos/{spec.repo_full_name}/pulls?{query}")
        owner = spec.repo_full_name.split("/", 1)[0]
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            head = row.get("head") or {}
            branch = str(head.get("ref") or "")
            head_repo = str(((head.get("repo") or {}).get("full_name")) or "")
            label = str(head.get("label") or "")
            if branch.startswith(SUPERPROJECT_BRANCH_PREFIX) and (
                head_repo == spec.repo_full_name or label.startswith(f"{owner}:")
            ):
                return row
        return None

    def _gitlink_pull_entries(
        self, spec: RepoSpec, pull_number: int, *, ref: str
    ) -> list[dict[str, str]]:
        """Return the exact gitlinks already accumulated on a parent PR."""
        rows = self.request(
            "GET",
            f"/repos/{spec.repo_full_name}/pulls/{int(pull_number)}/files?per_page=100",
        )
        entries: list[dict[str, str]] = []
        for row in rows if isinstance(rows, list) else []:
            path = _superproject_path(
                (row or {}).get("filename") if isinstance(row, dict) else None,
                repository=spec.name,
            )
            if not path:
                raise GitHubApiError(
                    "superproject_pin_invalid",
                    "aggregate Hermes PR contains a non-gitlink path",
                    details={"pull_number": int(pull_number), "path": path},
                )
            encoded_path = quote(path, safe="/")
            query = urlencode({"ref": ref})
            payload = self.request(
                "GET",
                f"/repos/{spec.repo_full_name}/contents/{encoded_path}?{query}",
            )
            if not isinstance(payload, dict) or payload.get("type") != "submodule":
                raise GitHubApiError(
                    "superproject_pin_invalid",
                    "aggregate Hermes PR contains a non-gitlink entry",
                    details={"pull_number": int(pull_number), "path": path},
                )
            sha = str(payload.get("sha") or "").lower()
            if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
                raise GitHubApiError(
                    "github_response_invalid",
                    f"GitHub did not return a valid gitlink SHA for {path} at {ref}",
                )
            entries.append({
                "path": path,
                "mode": "160000",
                "type": "commit",
                "sha": sha,
            })
        return entries

    def ensure_superproject_gitlink_pr(
        self,
        superproject: RepoSpec,
        child: RepoSpec,
        *,
        target_sha: str,
    ) -> dict[str, Any]:
        """Create or advance one aggregate parent PR to an exact child commit.

        This uses GitHub's Git database only. It never reads or mutates the
        deployed superproject worktree; production convergence remains a
        separate Sync Hermes action after the parent PR is merged.
        """
        if not child.superproject_path:
            return {
                "managed": False,
                "state": "not_projected",
                "repository": child.name,
            }
        target = str(target_sha or "").lower()
        if not re.fullmatch(r"[0-9a-f]{40,64}", target):
            raise GitHubApiError(
                "superproject_pin_invalid",
                "GitHub merge did not return a valid child commit SHA",
                details={"repository": child.name, "target_sha": target_sha},
            )

        current = self._open_gitlink_pull(superproject)
        parent_sha = self._branch_sha(superproject, superproject.branch)
        branch_behind_parent = False
        if current:
            branch = str((current.get("head") or {}).get("ref") or "")
            branch_sha = self._branch_sha(superproject, branch)
            pull_number = int(current.get("number") or 0)
            pull_url = current.get("html_url")
            state = "updated"
            comparison = self.request(
                "GET",
                f"/repos/{superproject.repo_full_name}/compare/{parent_sha}...{branch_sha}",
            )
            if not isinstance(comparison, dict):
                raise GitHubApiError(
                    "github_response_invalid",
                    "GitHub did not return parent branch ancestry",
                )
            branch_behind_parent = int(comparison.get("behind_by") or 0) > 0
        else:
            branch = f"{SUPERPROJECT_BRANCH_PREFIX}{child.name}-{target[:12]}-{time.time_ns()}"
            branch_sha = parent_sha
            pull_number = 0
            pull_url = None
            state = "created"

        current_pin = self._content_sha(
            superproject,
            child.superproject_path,
            ref=branch if current else superproject.branch,
        )
        if current_pin == target and not branch_behind_parent:
            return {
                "managed": True,
                "changed": False,
                "state": "pull_current" if current else "already_pinned",
                "superproject": superproject.name,
                "repository": child.name,
                "path": child.superproject_path,
                "target_sha": target,
                "branch": branch if current else None,
                "pull_number": pull_number or None,
                "html_url": pull_url,
            }

        entries_by_path: dict[str, dict[str, str]] = {}
        parents = [branch_sha]
        if current and branch_behind_parent:
            for entry in self._gitlink_pull_entries(
                superproject, pull_number, ref=branch
            ):
                entries_by_path[entry["path"]] = entry
            base_tree = self._commit_tree_sha(superproject, parent_sha)
            parents.append(parent_sha)
        else:
            base_tree = self._commit_tree_sha(superproject, branch_sha)
        entries_by_path[child.superproject_path] = {
            "path": child.superproject_path,
            "mode": "160000",
            "type": "commit",
            "sha": target,
        }
        tree = self.request(
            "POST",
            f"/repos/{superproject.repo_full_name}/git/trees",
            {
                "base_tree": base_tree,
                "tree": list(entries_by_path.values()),
            },
        )
        tree_sha = str((tree or {}).get("sha") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", tree_sha):
            raise GitHubApiError(
                "github_response_invalid", "GitHub did not create the superproject tree"
            )
        commit = self.request(
            "POST",
            f"/repos/{superproject.repo_full_name}/git/commits",
            {
                "message": f"chore: update {child.name} gitlink to {target[:12]}",
                "tree": tree_sha,
                "parents": parents,
            },
        )
        commit_sha = str((commit or {}).get("sha") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit_sha):
            raise GitHubApiError(
                "github_response_invalid",
                "GitHub did not create the superproject commit",
            )
        commit_sha = commit_sha.lower()

        if current:
            encoded = quote(branch, safe="")
            self.request(
                "PATCH",
                f"/repos/{superproject.repo_full_name}/git/refs/heads/{encoded}",
                {"sha": commit_sha, "force": False},
            )
        else:
            self.request(
                "POST",
                f"/repos/{superproject.repo_full_name}/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": commit_sha},
            )
            pull = self.request(
                "POST",
                f"/repos/{superproject.repo_full_name}/pulls",
                {
                    "title": "chore: update Hermes submodule pins",
                    "head": branch,
                    "base": superproject.branch,
                    "body": (
                        "Mission Control generated this PR after merging a managed child "
                        "repository. Merge it from the Hermes card, then use Sync Hermes "
                        "to converge the local superproject to the committed gitlinks."
                    ),
                },
            )
            pull_number = int((pull or {}).get("number") or 0)
            pull_url = (pull or {}).get("html_url")
            if not pull_number:
                raise GitHubApiError(
                    "github_response_invalid",
                    "GitHub did not create the gitlink pull request",
                )

        verified = self._content_sha(superproject, child.superproject_path, ref=branch)
        if verified != target:
            raise GitHubApiError(
                "superproject_pin_unverified",
                "parent branch does not contain the expected child gitlink",
                details={
                    "path": child.superproject_path,
                    "expected": target,
                    "actual": verified,
                },
            )
        return {
            "managed": True,
            "changed": True,
            "state": state,
            "superproject": superproject.name,
            "repository": child.name,
            "path": child.superproject_path,
            "target_sha": target,
            "branch": branch,
            "parent_commit_sha": commit_sha,
            "pull_number": pull_number,
            "html_url": pull_url,
        }

    def sync_fork(self, _spec: RepoSpec) -> dict[str, Any]:
        raise RepositorySyncError(
            "upstream_disabled", "upstream synchronization is disabled"
        )

    def fork_drift(self, _spec: RepoSpec) -> None:
        return None


class GitRunner:
    """Host-aware exact command runner. Browser input never supplies paths or argv."""

    def __init__(self, *, timeout: int = DEFAULT_TIMEOUT_SECONDS):
        self.timeout = max(5, int(timeout))
        self._homes: dict[str, str] = {}

    def _run_process(
        self, argv: list[str], *, timeout: int | None = None
    ) -> CommandResult:
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
        except subprocess.TimeoutExpired as exc:
            raise RepositorySyncError(
                "command_timeout",
                f"command timed out after {timeout or self.timeout}s",
                details={"argv": argv, "stdout": exc.stdout, "stderr": exc.stderr},
            ) from exc
        except OSError as exc:
            raise RepositorySyncError(
                "command_unavailable",
                f"unable to execute {argv[0]}: {exc}",
                details={"argv": argv},
            ) from exc
        return CommandResult(
            argv=argv,
            returncode=proc.returncode,
            stdout=(proc.stdout or "").strip(),
            stderr=(proc.stderr or "").strip(),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def _host_process(
        self, spec: RepoSpec, argv: list[str], *, timeout: int | None = None
    ) -> CommandResult:
        if spec.transport != "local":
            raise RepositorySyncError(
                "ssh_runner_required", f"SSH runner required for repository {spec.name}"
            )
        return self._run_process(argv, timeout=timeout)

    def host_home(self, spec: RepoSpec) -> str:
        if spec.host in self._homes:
            return self._homes[spec.host]
        if spec.transport == "local":
            home = str(Path.home())
        else:
            result = self._host_process(spec, ["sh", "-lc", "printf '%s' \"$HOME\""])
            if result.returncode != 0 or not result.stdout:
                raise RepositorySyncError(
                    "host_home_unavailable",
                    f"could not resolve HOME on {spec.host}",
                    details={"stderr": result.stderr},
                )
            home = result.stdout.splitlines()[-1].strip()
        self._homes[spec.host] = home
        return home

    def materialize_path(self, spec: RepoSpec, value: str) -> str:
        home = self.host_home(spec)
        if value == "~":
            return home
        if value.startswith("~/"):
            return f"{home}/{value[2:]}"
        if value == "$HOME":
            return home
        if value.startswith("$HOME/"):
            return f"{home}/{value[6:]}"
        return (
            os.path.expandvars(os.path.expanduser(value))
            if spec.transport == "local"
            else value
        )

    def git_dir(self, spec: RepoSpec) -> str:
        return self.materialize_path(spec, spec.git_dir)

    def work_tree(self, spec: RepoSpec) -> str:
        return self.materialize_path(spec, spec.work_tree)

    def resolve_path(self, spec: RepoSpec) -> str:
        return self.work_tree(spec)

    def host(
        self, spec: RepoSpec, *args: str, timeout: int | None = None
    ) -> CommandResult:
        return self._host_process(spec, list(args), timeout=timeout)

    def exists(self, spec: RepoSpec, path: str, *, directory: bool = False) -> bool:
        flag = "-d" if directory else "-e"
        result = self.host(spec, "test", flag, self.materialize_path(spec, path))
        return result.returncode == 0

    def mkdir(self, spec: RepoSpec, path: str) -> None:
        result = self.host(spec, "mkdir", "-p", self.materialize_path(spec, path))
        if result.returncode != 0:
            raise RepositorySyncError("mkdir_failed", result.stderr or "mkdir failed")

    def git(
        self, spec: RepoSpec, *args: str, timeout: int | None = None
    ) -> CommandResult:
        return self.host(
            spec,
            "git",
            f"--git-dir={self.git_dir(spec)}",
            f"--work-tree={self.work_tree(spec)}",
            *args,
            timeout=timeout,
        )

    def git_common(
        self, spec: RepoSpec, *args: str, timeout: int | None = None
    ) -> CommandResult:
        return self.host(
            spec, "git", f"--git-dir={self.git_dir(spec)}", *args, timeout=timeout
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
        self.timeout = max(5, int(timeout))

    def spec(self, name: str) -> RepoSpec:
        try:
            return self.registry[name]
        except KeyError as exc:
            raise RepositorySyncError(
                "repo_unknown", f"unknown repository: {name}"
            ) from exc

    def _superproject_spec(self) -> RepoSpec:
        rows = [
            spec for spec in self.registry.values() if spec.local_mode == "superproject"
        ]
        if len(rows) != 1:
            raise RepositorySyncError(
                "superproject_unavailable",
                "exactly one Hermes superproject is required for gitlink projection",
            )
        return rows[0]

    @staticmethod
    def _origin_ref(spec: RepoSpec) -> str:
        return f"refs/remotes/origin/{spec.branch}"

    @staticmethod
    def _dirty_summary(porcelain: str) -> dict[str, Any]:
        rows = [line for line in porcelain.splitlines() if line]
        modified = staged = untracked = deleted = conflicts = 0
        for row in rows:
            if row.startswith("??"):
                untracked += 1
                continue
            x = row[0] if row else " "
            y = row[1] if len(row) > 1 else " "
            staged += int(x not in {" ", "?"})
            modified += int(y not in {" ", "?"})
            deleted += int("D" in (x, y))
            conflicts += int(x == "U" or y == "U" or (x, y) in {("A", "A"), ("D", "D")})
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

    def _run_ok(self, spec: RepoSpec, *args: str) -> str:
        result = self.runner.git(spec, *args)
        if result.returncode != 0:
            raise RepositorySyncError(
                "git_command_failed",
                result.stderr or result.stdout or "git command failed",
                details={"argv": result.argv, "returncode": result.returncode},
            )
        return result.stdout

    def _layout(self, spec: RepoSpec) -> dict[str, Any]:
        if spec.local_mode == "remote_only":
            return {
                "ready": False,
                "managed": False,
                "git_dir": None,
                "work_tree": None,
                "git_dir_exists": False,
                "work_tree_exists": False,
                "bound_source": False,
                "scope_paths": [],
            }
        git_dir = self.runner.git_dir(spec)
        work_tree = self.runner.work_tree(spec)
        git_exists = self.runner.exists(spec, git_dir, directory=True)
        work_exists = self.runner.exists(spec, work_tree, directory=True)
        bound = False
        if git_exists and work_exists:
            check = self.runner.git(spec, "rev-parse", "--verify", "HEAD")
            bound = check.returncode == 0 and bool(check.stdout)
        return {
            "ready": bool(git_exists and work_exists and bound),
            "managed": True,
            "git_dir": git_dir,
            "work_tree": work_tree,
            "git_dir_exists": git_exists,
            "work_tree_exists": work_exists,
            "bound_source": bound,
            "scope_paths": list(spec.scope_paths),
        }

    def _event_base(self, spec: RepoSpec, action: str, trigger: str) -> dict[str, Any]:
        return {
            "repo": spec.name,
            "repo_full_name": spec.repo_full_name,
            "branch": spec.branch,
            "host": spec.host,
            "action": action,
            "trigger": trigger,
            "started_at": _now_iso(),
        }

    def _finish_event(
        self, event: dict[str, Any], *, ok: bool, **extra: Any
    ) -> dict[str, Any]:
        event.update(extra)
        event["ok"] = ok
        event["finished_at"] = _now_iso()
        started = dt.datetime.fromisoformat(event["started_at"].replace("Z", "+00:00"))
        finished = dt.datetime.fromisoformat(
            event["finished_at"].replace("Z", "+00:00")
        )
        event["duration_ms"] = int((finished - started).total_seconds() * 1000)
        self.store.append(event)
        return event

    def _scope_args(self, spec: RepoSpec) -> tuple[str, ...]:
        return ("--", *spec.scope_paths) if spec.scope_paths else ()

    def _tracked_status(self, spec: RepoSpec) -> str:
        return self._run_ok(
            spec, "status", "--porcelain=v1", "-uno", *self._scope_args(spec)
        )

    def _working_status(self, spec: RepoSpec) -> str:
        """Return tracked and untracked local work for merge preservation."""
        return self._run_ok(
            spec,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            *self._scope_args(spec),
        )

    def _stash_local_changes(
        self, spec: RepoSpec, *, message: str
    ) -> dict[str, Any] | None:
        """Temporarily stash local work, including untracked files.

        This is deliberately limited to the merge-and-pull transaction. A
        normal production pull remains fail-closed when local work exists.
        The stash SHA is retained in the operation receipt so a failed restore
        can be recovered without guessing which pre-existing stash was used.
        """
        before = self._dirty_summary(self._working_status(spec))
        if not before["dirty"]:
            return None
        pushed = self.runner.git(
            spec,
            "stash",
            "push",
            "--include-untracked",
            "--message",
            message,
            *self._scope_args(spec),
        )
        if pushed.returncode != 0:
            raise RepositorySyncError(
                "local_stash_failed",
                pushed.stderr or pushed.stdout or "could not stash local changes",
                details={"working_tree": before},
            )
        stash_sha = self._run_ok(spec, "rev-parse", "--verify", "refs/stash")
        stash_shas = [stash_sha]
        after = self._dirty_summary(self._working_status(spec))
        if after["dirty"]:
            # Git deliberately does not recurse into some directories that
            # look like broken/nested worktrees when using -u. Stage only the
            # residual paths and store them in a supplemental stash. This
            # keeps the primary stash's original staged/unstaged semantics
            # while still preserving every local file.
            staged = self.runner.git(spec, "add", "-A", *self._scope_args(spec))
            if staged.returncode != 0:
                raise RepositorySyncError(
                    "local_stash_failed",
                    staged.stderr
                    or staged.stdout
                    or "could not stage residual local changes",
                    details={
                        "working_tree_before": before,
                        "working_tree_after": after,
                        "stash_sha": stash_sha,
                    },
                )
            supplemental = self.runner.git(
                spec,
                "stash",
                "push",
                "--staged",
                "--message",
                f"{message} residual",
            )
            if supplemental.returncode != 0:
                raise RepositorySyncError(
                    "local_stash_failed",
                    supplemental.stderr
                    or supplemental.stdout
                    or "could not stash residual local changes",
                    details={
                        "working_tree_before": before,
                        "working_tree_after": after,
                        "stash_sha": stash_sha,
                    },
                )
            supplemental_sha = self._run_ok(spec, "rev-parse", "--verify", "refs/stash")
            stash_shas.append(supplemental_sha)
            after = self._dirty_summary(self._working_status(spec))
            if after["dirty"]:
                raise RepositorySyncError(
                    "local_stash_incomplete",
                    "local changes remained after staging them for merge-and-pull",
                    details={
                        "working_tree_before": before,
                        "working_tree_after": after,
                        "stash_shas": stash_shas,
                    },
                )
        return {
            "stashed": True,
            "restored": False,
            "stash_sha": stash_sha,
            "stash_shas": stash_shas,
            "working_tree_before": before,
        }

    def _restore_local_changes(
        self, spec: RepoSpec, snapshot: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if snapshot is None:
            return None
        stash_shas = list(snapshot.get("stash_shas") or [snapshot["stash_sha"]])
        stash_list = self._run_ok(spec, "stash", "list", "--format=%H").splitlines()
        refs: dict[str, str] = {}
        for sha in stash_shas:
            try:
                index = stash_list.index(sha)
            except ValueError as exc:
                raise RepositorySyncError(
                    "local_stash_moved",
                    "a merge-and-pull stash is no longer available",
                    details={"stash_sha": sha, "available_stashes": stash_list[:10]},
                ) from exc
            refs[sha] = f"stash@{{{index}}}"

        # The supplemental residual stash is newer than the primary stash.
        # Apply the older primary stash first, then the residual files, and
        # only drop either stash after both applications succeed.
        for sha in stash_shas:
            stash_ref = refs[sha]
            restore_args = ["stash", "apply"]
            if sha == stash_shas[0]:
                restore_args.append("--index")
            restore_args.append(stash_ref)
            restored = self.runner.git(spec, *restore_args)
            if restored.returncode != 0:
                current = self._dirty_summary(self._working_status(spec))
                conflicts = self._conflict_files(spec)
                code = "local_restore_conflict" if conflicts else "local_restore_failed"
                raise RepositorySyncError(
                    code,
                    restored.stderr
                    or restored.stdout
                    or "could not restore local changes",
                    details={
                        "stash_sha": sha,
                        "stash_shas": stash_shas,
                        "working_tree": current,
                        "conflict_files": conflicts,
                    },
                )
            if sha != stash_shas[0]:
                # ``stash push --staged`` is used only for residual paths that
                # survived the primary -u stash.  Applying that stash makes
                # newly-added paths staged on some Git versions, even without
                # ``--index``.  Return just those paths to their preflight
                # untracked/unstaged state; never reset the primary stash's
                # index because its staged changes are part of the contract.
                residual_paths = self._run_ok(
                    spec,
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    f"{sha}^2",
                ).splitlines()
                if residual_paths:
                    unstaged = self.runner.git(
                        spec,
                        "reset",
                        "--mixed",
                        "HEAD",
                        "--",
                        *residual_paths,
                    )
                    if unstaged.returncode != 0:
                        raise RepositorySyncError(
                            "local_restore_failed",
                            unstaged.stderr
                            or unstaged.stdout
                            or "could not restore residual index state",
                            details={
                                "stash_sha": sha,
                                "stash_shas": stash_shas,
                                "residual_paths": residual_paths[:100],
                            },
                        )

        # Drop only the stashes created by this transaction. Resolve the
        # current top ref each time because dropping one changes stash indexes.
        for _sha in reversed(stash_shas):
            current_stashes = self._run_ok(
                spec, "stash", "list", "--format=%H"
            ).splitlines()
            try:
                index = current_stashes.index(_sha)
            except ValueError as exc:
                raise RepositorySyncError(
                    "local_stash_drop_failed",
                    "restored local work but could not locate its stash for cleanup",
                    details={
                        "stash_sha": _sha,
                        "available_stashes": current_stashes[:10],
                    },
                ) from exc
            dropped = self.runner.git(spec, "stash", "drop", f"stash@{{{index}}}")
            if dropped.returncode != 0:
                raise RepositorySyncError(
                    "local_stash_drop_failed",
                    dropped.stderr or dropped.stdout or "could not drop restored stash",
                    details={"stash_sha": _sha, "stash_ref": f"stash@{{{index}}}"},
                )
        snapshot["restored"] = True
        snapshot["working_tree_after"] = self._dirty_summary(self._working_status(spec))
        return snapshot

    def _conflict_files(self, spec: RepoSpec) -> list[str]:
        result = self.runner.git(
            spec, "diff", "--name-only", "--diff-filter=U", *self._scope_args(spec)
        )
        if result.returncode != 0:
            return []
        return [row for row in result.stdout.splitlines() if row.strip()]

    def _apply_scope(self, spec: RepoSpec) -> None:
        if not spec.scope_paths:
            return
        listed = self.runner.git(spec, "ls-files", "-z")
        if listed.returncode != 0:
            raise RepositorySyncError(
                "scope_list_failed", listed.stderr or "could not list repository files"
            )
        files = [row for row in listed.stdout.split("\0") if row]
        if files:
            for start in range(0, len(files), 200):
                chunk = files[start : start + 200]
                reset = self.runner.git(
                    spec, "update-index", "--no-skip-worktree", "--", *chunk
                )
                if reset.returncode != 0:
                    raise RepositorySyncError(
                        "scope_index_failed",
                        reset.stderr or "could not reset scope bits",
                    )
        prefixes = tuple(path.rstrip("/") + "/" for path in spec.scope_paths)
        outside = [
            row
            for row in files
            if row not in spec.scope_paths
            and not any(row.startswith(prefix) for prefix in prefixes)
        ]
        for start in range(0, len(outside), 200):
            chunk = outside[start : start + 200]
            marked = self.runner.git(
                spec, "update-index", "--skip-worktree", "--", *chunk
            )
            if marked.returncode != 0:
                raise RepositorySyncError(
                    "scope_index_failed",
                    marked.stderr or "could not apply source scope",
                )

    def initialize_layout(
        self, name: str, *, trigger: str = "dashboard", wait_seconds: float = 0.0
    ) -> dict[str, Any]:
        spec = self.spec(name)
        event = self._event_base(spec, "initialize_layout", trigger)
        if spec.local_mode != "canonical":
            return self._finish_event(
                event,
                ok=False,
                status="error",
                error={
                    "code": "local_layout_unmanaged",
                    "message": (
                        "local layout initialization is unavailable for this repository"
                    ),
                    "details": {"local_mode": spec.local_mode},
                },
            )
        try:
            with self.store.lock(name, wait_seconds=wait_seconds):
                layout = self._layout(spec)
                if layout["ready"]:
                    return self._finish_event(
                        event, ok=True, status="noop", layout=layout
                    )
                work_existed = layout["work_tree_exists"]
                existing_worktree = None
                if work_existed:
                    candidate = self.runner.host(
                        spec,
                        "git",
                        "-C",
                        layout["work_tree"],
                        "rev-parse",
                        "--is-inside-work-tree",
                    )
                    if candidate.returncode == 0 and candidate.stdout.strip() == "true":
                        existing_worktree = layout["work_tree"]
                self.runner.mkdir(spec, str(Path(layout["git_dir"]).parent))
                self.runner.mkdir(spec, layout["work_tree"])
                if not layout["git_dir_exists"]:
                    init = self.runner.host(
                        spec, "git", "init", "--bare", layout["git_dir"]
                    )
                    if init.returncode != 0:
                        raise RepositorySyncError(
                            "git_init_failed",
                            init.stderr or init.stdout or "git init --bare failed",
                        )

                for key, value in (
                    ("core.bare", "false"),
                    ("core.worktree", layout["work_tree"]),
                ):
                    cfg = self.runner.git_common(spec, "config", key, value)
                    if cfg.returncode != 0:
                        raise RepositorySyncError(
                            "git_config_failed", cfg.stderr or f"failed to set {key}"
                        )

                remote = self.runner.git_common(spec, "remote", "get-url", "origin")
                if remote.returncode != 0:
                    add = self.runner.git_common(
                        spec, "remote", "add", "origin", spec.origin_url
                    )
                    if add.returncode != 0:
                        raise RepositorySyncError(
                            "remote_add_failed", add.stderr or "remote add failed"
                        )
                elif remote.stdout.strip() != spec.origin_url:
                    # A failed earlier initialization can leave a bare Git
                    # directory with only the old origin configured.  The
                    # registry is authoritative, and this branch is reached
                    # only while the layout is still unbound, so repair that
                    # partial state and let the normal fetch/verification path
                    # complete it.
                    changed = self.runner.git_common(
                        spec, "remote", "set-url", "origin", spec.origin_url
                    )
                    if changed.returncode != 0:
                        raise RepositorySyncError(
                            "remote_update_failed",
                            changed.stderr or "remote set-url failed",
                            details={
                                "expected": spec.origin_url,
                                "actual": remote.stdout.strip(),
                            },
                        )
                configured = self.runner.git_common(
                    spec,
                    "config",
                    "remote.origin.fetch",
                    "+refs/heads/*:refs/remotes/origin/*",
                )
                if configured.returncode != 0:
                    raise RepositorySyncError(
                        "remote_config_failed",
                        configured.stderr or "remote fetch configuration failed",
                    )
                remote_ref = self._origin_ref(spec)
                if existing_worktree:
                    # Existing live checkouts already contain the complete
                    # object graph. Seed the new common directory locally so
                    # Initialize remains a layout operation instead of a
                    # full-history network download. The following status
                    # refresh fetches origin and reports any real drift.
                    shallow = self.runner.host(
                        spec,
                        "git",
                        "-C",
                        existing_worktree,
                        "rev-parse",
                        "--absolute-git-dir",
                    )
                    shallow_path = f"{shallow.stdout.rstrip('/')}/shallow"
                    if (
                        shallow.returncode == 0
                        and shallow.stdout
                        and self.runner.exists(spec, shallow_path)
                    ):
                        copied = self.runner.host(
                            spec,
                            "cp",
                            "--",
                            shallow_path,
                            f"{layout['git_dir']}/shallow",
                        )
                        if copied.returncode != 0:
                            raise RepositorySyncError(
                                "shallow_state_copy_failed",
                                copied.stderr or "could not preserve shallow boundary",
                            )
                    fetched = self.runner.git_common(
                        spec,
                        "fetch",
                        "--no-tags",
                        existing_worktree,
                        f"+refs/heads/{spec.branch}:{remote_ref}",
                    )
                else:
                    fetched = self.runner.git_common(
                        spec,
                        "fetch",
                        "--prune",
                        "origin",
                        spec.branch,
                        timeout=max(self.timeout, INITIALIZE_FETCH_TIMEOUT_SECONDS),
                    )
                if fetched.returncode != 0:
                    raise RepositorySyncError(
                        "fetch_failed",
                        fetched.stderr or fetched.stdout or "fetch failed",
                    )

                remote_sha = self.runner.git_common(
                    spec, "rev-parse", "--verify", remote_ref
                )
                if remote_sha.returncode != 0:
                    raise RepositorySyncError(
                        "remote_branch_missing",
                        remote_sha.stderr or "remote branch missing",
                    )
                local = self.runner.git_common(
                    spec, "rev-parse", "--verify", f"refs/heads/{spec.branch}"
                )
                if local.returncode == 0 and local.stdout != remote_sha.stdout:
                    raise RepositorySyncError(
                        "production_branch_diverged",
                        "canonical Git directory contains a different branch tip",
                        details={
                            "branch": spec.branch,
                            "local_sha": local.stdout,
                            "remote_sha": remote_sha.stdout,
                        },
                    )
                if local.returncode != 0:
                    updated = self.runner.git_common(
                        spec, "update-ref", f"refs/heads/{spec.branch}", remote_ref
                    )
                    if updated.returncode != 0:
                        raise RepositorySyncError(
                            "branch_init_failed", updated.stderr or "branch init failed"
                        )
                head = self.runner.git_common(
                    spec, "symbolic-ref", "HEAD", f"refs/heads/{spec.branch}"
                )
                if head.returncode != 0:
                    raise RepositorySyncError(
                        "head_config_failed", head.stderr or "HEAD configuration failed"
                    )

                loaded = self.runner.git(spec, "read-tree", f"refs/heads/{spec.branch}")
                if loaded.returncode != 0:
                    raise RepositorySyncError(
                        "index_init_failed",
                        loaded.stderr or loaded.stdout or "read-tree failed",
                    )
                self._apply_scope(spec)
                if not work_existed:
                    checkout = self.runner.git(spec, "checkout-index", "-a")
                    if checkout.returncode != 0:
                        raise RepositorySyncError(
                            "live_source_init_failed",
                            checkout.stderr
                            or checkout.stdout
                            or "checkout-index failed",
                        )

                after = self._layout(spec)
                if not after["ready"]:
                    raise RepositorySyncError(
                        "layout_unverified",
                        "canonical repository layout was not verified",
                        details=after,
                    )
                return self._finish_event(event, ok=True, status="ok", layout=after)
        except RepositorySyncError as exc:
            return self._finish_event(
                event,
                ok=False,
                status="error",
                error={"code": exc.code, "message": str(exc), "details": exc.details},
            )

    def status(
        self,
        name: str,
        *,
        fetch: bool = True,
        include_github: bool = True,
        include_last_operation: bool = True,
    ) -> dict[str, Any]:
        spec = self.spec(name)
        started = time.monotonic()
        layout = self._layout(spec)
        superproject_name = next(
            (
                row.name
                for row in self.registry.values()
                if row.local_mode == "superproject"
            ),
            None,
        )
        base: dict[str, Any] = {
            "name": spec.name,
            "repo_full_name": spec.repo_full_name,
            "branch": spec.branch,
            "host": spec.host,
            "transport": spec.transport,
            "ssh_target": spec.ssh_target,
            "private": spec.private,
            "fork": spec.is_fork,
            "upstream_repo": spec.upstream_repo,
            "origin_url": spec.origin_url,
            "layout": layout,
            "path": layout["work_tree"],
            "git_dir": layout["git_dir"],
            "local_mode": spec.local_mode,
            "capabilities": {
                "merge_pr": True,
                "sync_local": spec.local_mode != "remote_only",
                "initialize_local": spec.local_mode == "canonical",
                "project_to_superproject": bool(spec.superproject_path),
            },
            "superproject": {
                "managed": bool(spec.superproject_path),
                "name": superproject_name if spec.superproject_path else None,
                "path": spec.superproject_path,
            },
        }
        if include_last_operation:
            base["last_operation"] = self.store.last(name)
        if spec.local_mode == "remote_only":
            base.update({
                "ok": True,
                "state": "remote_only",
                "current_branch": None,
                "local_sha": None,
                "remote_sha": None,
                "ahead": 0,
                "behind": 0,
                "working_tree": self._dirty_summary(""),
                "conflict_files": [],
                "last_commit_at": None,
                "last_commit_subject": None,
                "origin_actual": spec.origin_url,
            })
        elif not layout["ready"]:
            base.update({
                "ok": False,
                "state": "layout_missing",
                "current_branch": None,
                "local_sha": None,
                "remote_sha": None,
                "ahead": 0,
                "behind": 0,
                "working_tree": self._dirty_summary(""),
                "conflict_files": [],
                "error": {
                    "code": "layout_missing",
                    "message": "canonical live source is not initialized",
                    "details": layout,
                },
            })
        else:
            try:
                if fetch:
                    fetched = self.runner.git(
                        spec, "fetch", "--prune", "origin", spec.branch
                    )
                    if fetched.returncode != 0:
                        raise RepositorySyncError(
                            "fetch_failed",
                            fetched.stderr or fetched.stdout or "git fetch failed",
                        )
                branch = self._run_ok(spec, "symbolic-ref", "--short", "HEAD")
                local_sha = self._run_ok(spec, "rev-parse", "HEAD")
                remote_sha = self._run_ok(spec, "rev-parse", self._origin_ref(spec))
                counts = self._run_ok(
                    spec,
                    "rev-list",
                    "--left-right",
                    "--count",
                    f"HEAD...{self._origin_ref(spec)}",
                ).split()
                ahead = int(counts[0]) if counts else 0
                behind = int(counts[1]) if len(counts) > 1 else 0
                dirty = self._dirty_summary(self._tracked_status(spec))
                conflicts = self._conflict_files(spec)
                last = self._run_ok(spec, "log", "-1", "--format=%cI%x00%s").split(
                    "\x00", 1
                )
                actual_origin = self._run_ok(
                    spec, "config", "--get", "remote.origin.url"
                )
                if conflicts:
                    state = "conflict"
                elif actual_origin != spec.origin_url:
                    state = "origin_mismatch"
                elif branch != spec.branch:
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
                base.update({
                    "ok": True,
                    "state": state,
                    "current_branch": branch,
                    "local_sha": local_sha,
                    "remote_sha": remote_sha,
                    "ahead": ahead,
                    "behind": behind,
                    "working_tree": dirty,
                    "conflict_files": conflicts,
                    "last_commit_at": last[0] if last else None,
                    "last_commit_subject": last[1] if len(last) > 1 else None,
                    "origin_actual": actual_origin,
                })
            except RepositorySyncError as exc:
                base.update({
                    "ok": False,
                    "state": "error",
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        "details": exc.details,
                    },
                })
        if include_github:
            try:
                base["pull_requests"] = self.github.open_pulls(spec)
                base["github_available"] = True
            except RepositorySyncError as exc:
                base["pull_requests"] = []
                base["github_available"] = False
                base["github_error"] = {"code": exc.code, "message": str(exc)}
        base["duration_ms"] = int((time.monotonic() - started) * 1000)
        return base

    def status_all(
        self, *, fetch: bool = True, include_github: bool = True
    ) -> list[dict[str, Any]]:
        names = list(self.registry)

        def one(name: str) -> dict[str, Any]:
            try:
                return self.status(name, fetch=fetch, include_github=include_github)
            except Exception as exc:  # noqa: BLE001
                spec = self.spec(name)
                try:
                    if spec.local_mode == "remote_only":
                        raise RepositorySyncError(
                            "remote_only",
                            "local repository status is intentionally disabled",
                        )
                    git_dir = self.runner.git_dir(spec)
                    work_tree = self.runner.work_tree(spec)
                except Exception:  # noqa: BLE001 — an unreachable host must not break the list
                    git_dir, work_tree = spec.git_dir, spec.work_tree
                return {
                    "name": name,
                    "repo_full_name": spec.repo_full_name,
                    "branch": spec.branch,
                    "host": spec.host,
                    "state": "error",
                    "ok": False,
                    "error": {"code": "status_failed", "message": str(exc)},
                    "layout": {
                        "git_dir": git_dir,
                        "work_tree": work_tree,
                        "ready": False,
                    },
                }

        with ThreadPoolExecutor(max_workers=max(1, min(len(names), 6))) as executor:
            return list(executor.map(one, names))

    def _production_preflight(
        self, spec: RepoSpec, *, fetch: bool, allow_dirty: bool = False
    ) -> dict[str, Any]:
        status = self.status(
            spec.name, fetch=fetch, include_github=False, include_last_operation=False
        )
        if not status.get("ok"):
            error = status.get("error") or {}
            raise RepositorySyncError(
                str(error.get("code") or "status_failed"),
                str(error.get("message") or "repository status failed"),
                details=error.get("details") or {},
            )
        if status.get("origin_actual") != spec.origin_url:
            raise RepositorySyncError(
                "origin_mismatch",
                "production origin does not match the registry",
                details={
                    "expected": spec.origin_url,
                    "actual": status.get("origin_actual"),
                },
            )
        if status.get("current_branch") != spec.branch:
            raise RepositorySyncError(
                "wrong_branch",
                f"production is on {status.get('current_branch')!r}; expected {spec.branch!r}",
            )
        if status.get("conflict_files"):
            raise RepositorySyncError(
                "preexisting_conflict",
                "production contains unresolved conflicts",
                details={"conflict_files": status.get("conflict_files")},
            )
        if not allow_dirty and (status.get("working_tree") or {}).get("dirty"):
            raise RepositorySyncError(
                "production_dirty",
                "live source has local tracked changes; pull refused",
                details={"working_tree": status.get("working_tree")},
            )
        if int(status.get("ahead") or 0) > 0:
            raise RepositorySyncError(
                "production_ahead",
                "production contains local commits; pull refused",
                details={
                    "ahead": status.get("ahead"),
                    "local_sha": status.get("local_sha"),
                },
            )
        return status

    def _pull_production_locked(self, spec: RepoSpec) -> dict[str, Any]:
        before = self._production_preflight(spec, fetch=True)
        merged = self.runner.git(
            spec,
            "merge",
            "--ff-only",
            self._origin_ref(spec),
            timeout=max(self.timeout, 180),
        )
        if merged.returncode != 0:
            raise RepositorySyncError(
                "fast_forward_failed",
                merged.stderr or merged.stdout or "git merge --ff-only failed",
                details={"returncode": merged.returncode},
            )
        after = self.status(
            spec.name, fetch=False, include_github=False, include_last_operation=False
        )
        if not after.get("ok") or after.get("local_sha") != after.get("remote_sha"):
            raise RepositorySyncError(
                "pull_unverified",
                "production HEAD does not match origin after pull",
                details={"before": before, "after": after},
            )
        return {
            "ok": True,
            "host": spec.host,
            "git_dir": self.runner.git_dir(spec),
            "work_tree": self.runner.work_tree(spec),
            "before_sha": before.get("local_sha"),
            "after_sha": after.get("local_sha"),
            "remote_sha": after.get("remote_sha"),
            "changed": before.get("local_sha") != after.get("local_sha"),
        }

    def pull_production(
        self, name: str, *, trigger: str = "dashboard", wait_seconds: float = 0.0
    ) -> dict[str, Any]:
        spec = self.spec(name)
        event = self._event_base(spec, "pull_production", trigger)
        try:
            with self.store.lock(name, wait_seconds=wait_seconds):
                production = self._pull_production_locked(spec)
                return self._finish_event(
                    event, ok=True, status="ok", production=production
                )
        except RepositorySyncError as exc:
            return self._finish_event(
                event,
                ok=False,
                status="error",
                error={"code": exc.code, "message": str(exc), "details": exc.details},
            )

    def request_codex_review(
        self,
        name: str,
        number: int,
        *,
        expected_head_sha: str | None = None,
        trigger: str = "dashboard",
    ) -> dict[str, Any]:
        spec = self.spec(name)
        event = self._event_base(spec, "codex_review", trigger)
        event["pull_number"] = int(number)
        try:
            result = self.github.request_codex_review(
                spec, int(number), expected_head_sha=expected_head_sha
            )
            return self._finish_event(event, ok=True, status="requested", github=result)
        except RepositorySyncError as exc:
            return self._finish_event(
                event,
                ok=False,
                status="error",
                error={"code": exc.code, "message": str(exc), "details": exc.details},
            )

    def change_draft_state(
        self, name: str, number: int, *, ready: bool, trigger: str = "dashboard"
    ) -> dict[str, Any]:
        spec = self.spec(name)
        action = "mark_ready" if ready else "mark_draft"
        event = self._event_base(spec, action, trigger)
        event["pull_number"] = int(number)
        try:
            result = (
                self.github.mark_ready(spec, number)
                if ready
                else self.github.mark_draft(spec, number)
            )
            return self._finish_event(event, ok=True, status="ok", github=result)
        except RepositorySyncError as exc:
            return self._finish_event(
                event,
                ok=False,
                status="error",
                error={"code": exc.code, "message": str(exc), "details": exc.details},
            )

    def merge_and_pull(
        self,
        name: str,
        number: int,
        *,
        expected_head_sha: str | None = None,
        trigger: str = "dashboard",
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        spec = self.spec(name)
        event = self._event_base(spec, "merge_and_pull", trigger)
        event["pull_number"] = int(number)
        github_phase: dict[str, Any] | None = None
        local_snapshot: dict[str, Any] | None = None
        try:
            with self.store.lock(name, wait_seconds=wait_seconds):
                # Refuse known production drift before mutating GitHub. This avoids
                # an avoidable partial success while still reporting a real partial
                # result if the host becomes unavailable after the merge. Local
                # work is handled transactionally below; local commits are still
                # refused by the preflight because they cannot be fast-forwarded.
                self._production_preflight(spec, fetch=True, allow_dirty=True)
                pull = self.github.pull_detail(spec, int(number))
                if pull.get("draft"):
                    raise RepositorySyncError(
                        "pull_is_draft",
                        "draft pull request must be marked ready before merge",
                    )
                local_snapshot = self._stash_local_changes(
                    spec,
                    message=(
                        f"mission-control merge-and-pull {spec.name} "
                        f"{number} {event['started_at']}"
                    ),
                )
                github_phase = self.github.merge_pr_rebase(
                    spec, int(number), expected_head_sha=expected_head_sha
                )
                try:
                    production = self._pull_production_locked(spec)
                except RepositorySyncError as exc:
                    restore_error: RepositorySyncError | None = None
                    if local_snapshot is not None:
                        try:
                            self._restore_local_changes(spec, local_snapshot)
                        except RepositorySyncError as restore_exc:
                            restore_error = restore_exc
                    details = dict(exc.details)
                    if restore_error is not None:
                        details["local_restore"] = {
                            "code": restore_error.code,
                            "message": str(restore_error),
                            "details": restore_error.details,
                        }
                    return self._finish_event(
                        event,
                        ok=False,
                        status="partial_success",
                        partial_success=True,
                        completed_phase="github_merge",
                        github=github_phase,
                        local_changes=local_snapshot,
                        error={
                            "code": exc.code,
                            "message": str(exc),
                            "details": details,
                        },
                    )
                try:
                    self._restore_local_changes(spec, local_snapshot)
                except RepositorySyncError as exc:
                    return self._finish_event(
                        event,
                        ok=False,
                        status="partial_success",
                        partial_success=True,
                        completed_phase="production_pull",
                        github=github_phase,
                        production=production,
                        local_changes=local_snapshot,
                        error={
                            "code": exc.code,
                            "message": str(exc),
                            "details": exc.details,
                        },
                    )
                return self._finish_event(
                    event,
                    ok=True,
                    status="ok",
                    github=github_phase,
                    production=production,
                    local_changes=local_snapshot,
                )
        except RepositorySyncError as exc:
            restore_error: RepositorySyncError | None = None
            if local_snapshot is not None and not local_snapshot.get("restored"):
                try:
                    self._restore_local_changes(spec, local_snapshot)
                except RepositorySyncError as restore_exc:
                    restore_error = restore_exc
            details = dict(exc.details)
            if restore_error is not None:
                details["local_restore"] = {
                    "code": restore_error.code,
                    "message": str(restore_error),
                    "details": restore_error.details,
                }
            return self._finish_event(
                event,
                ok=False,
                status="error",
                github=github_phase,
                local_changes=local_snapshot,
                error={"code": exc.code, "message": str(exc), "details": details},
            )

    def _commit_tracked_changes(
        self, spec: RepoSpec, *, message: str
    ) -> dict[str, Any]:
        """Commit tracked work while deliberately leaving untracked files alone."""
        before = self._dirty_summary(self._tracked_status(spec))
        if not before["dirty"]:
            return {
                "committed": False,
                "working_tree_before": before,
                "untracked_ignored": True,
            }

        staged = self.runner.git(spec, "add", "-u", *self._scope_args(spec))
        if staged.returncode != 0:
            raise RepositorySyncError(
                "local_commit_failed",
                staged.stderr
                or staged.stdout
                or "could not stage tracked local changes",
                details={"working_tree": before},
            )
        committed = self.runner.git(
            spec, "commit", "--message", message, *self._scope_args(spec)
        )
        if committed.returncode != 0:
            raise RepositorySyncError(
                "local_commit_failed",
                committed.stderr
                or committed.stdout
                or "could not commit tracked local changes",
                details={"working_tree": before},
            )
        sha = self._run_ok(spec, "rev-parse", "HEAD")
        return {
            "committed": True,
            "sha": sha,
            "message": message,
            "working_tree_before": before,
            "untracked_ignored": True,
        }

    def _sync_identity_preflight(
        self, spec: RepoSpec, *, fetch: bool
    ) -> dict[str, Any]:
        status = self.status(
            spec.name, fetch=fetch, include_github=False, include_last_operation=False
        )
        if not status.get("ok"):
            error = status.get("error") or {}
            raise RepositorySyncError(
                str(error.get("code") or "status_failed"),
                str(error.get("message") or "repository status failed"),
                details=error.get("details") or {},
            )
        if status.get("origin_actual") != spec.origin_url:
            raise RepositorySyncError(
                "origin_mismatch",
                "repository origin does not match the registry",
                details={
                    "expected": spec.origin_url,
                    "actual": status.get("origin_actual"),
                },
            )
        if status.get("current_branch") != spec.branch:
            raise RepositorySyncError(
                "wrong_branch",
                f"repository is on {status.get('current_branch')!r}; expected {spec.branch!r}",
            )
        if status.get("conflict_files"):
            raise RepositorySyncError(
                "preexisting_conflict",
                "repository contains unresolved conflicts",
                details={"conflict_files": status.get("conflict_files")},
            )
        return status

    def _sync_locked(
        self, spec: RepoSpec, *, commit_message: str | None
    ) -> dict[str, Any]:
        before = self._sync_identity_preflight(spec, fetch=True)
        local_commit = self._commit_tracked_changes(
            spec,
            message=(commit_message or f"sync: save tracked changes for {spec.name}"),
        )

        # Refresh after the local commit so a remote update that arrived while
        # committing is included in the same sync transaction.
        refreshed = self._sync_identity_preflight(spec, fetch=True)
        merge: dict[str, Any] = {"changed": False}
        if refreshed.get("ahead") and refreshed.get("behind"):
            merged = self.runner.git(
                spec,
                "merge",
                "--no-edit",
                self._origin_ref(spec),
                timeout=max(self.timeout, 180),
            )
            if merged.returncode != 0:
                conflicts = self._conflict_files(spec)
                aborted = self.runner.git(spec, "merge", "--abort")
                code = "sync_conflict" if conflicts else "sync_merge_failed"
                details = {
                    "conflict_files": conflicts,
                    "abort_returncode": aborted.returncode,
                }
                raise RepositorySyncError(
                    code,
                    merged.stderr
                    or merged.stdout
                    or "could not merge origin into local commits",
                    details=details,
                )
            merge = {
                "changed": True,
                "sha": self._run_ok(spec, "rev-parse", "HEAD"),
            }
        elif refreshed.get("behind"):
            merged = self.runner.git(
                spec,
                "merge",
                "--ff-only",
                self._origin_ref(spec),
                timeout=max(self.timeout, 180),
            )
            if merged.returncode != 0:
                raise RepositorySyncError(
                    "sync_pull_failed",
                    merged.stderr
                    or merged.stdout
                    or "could not fast-forward from origin",
                    details={"returncode": merged.returncode},
                )
            merge = {
                "changed": True,
                "sha": self._run_ok(spec, "rev-parse", "HEAD"),
            }

        current = self._sync_identity_preflight(spec, fetch=False)
        pushed = False
        if current.get("ahead"):
            pushed_result = self.runner.git(
                spec,
                "push",
                "origin",
                f"HEAD:refs/heads/{spec.branch}",
                timeout=max(self.timeout, 180),
            )
            if pushed_result.returncode != 0:
                raise RepositorySyncError(
                    "sync_push_failed",
                    pushed_result.stderr
                    or pushed_result.stdout
                    or "could not push local commits to origin",
                    details={"returncode": pushed_result.returncode},
                )
            pushed = True

        after = self._sync_identity_preflight(spec, fetch=True)
        if (
            after.get("ahead")
            or after.get("behind")
            or (after.get("working_tree") or {}).get("dirty")
        ):
            raise RepositorySyncError(
                "sync_unverified",
                "sync completed but local and origin state is not clean and equal",
                details={"before": before, "after": after},
            )
        return {
            "before": before,
            "after": after,
            "local_commit": local_commit,
            "merge": merge,
            "push": {"changed": pushed},
            "untracked_ignored": True,
        }

    def _sync_superproject_locked(self, spec: RepoSpec) -> dict[str, Any]:
        if not spec.sync_script:
            raise RepositorySyncError(
                "sync_script_missing", "superproject sync script is not configured"
            )
        before = self.status(
            spec.name, fetch=False, include_github=False, include_last_operation=False
        )
        if not before.get("ok"):
            error = before.get("error") or {}
            raise RepositorySyncError(
                str(error.get("code") or "status_failed"),
                str(error.get("message") or "superproject status failed"),
                details=error.get("details") or {},
            )
        result = self.runner.host(
            spec,
            self.runner.materialize_path(spec, spec.sync_script),
            timeout=max(self.timeout, 300),
        )
        if result.returncode != 0:
            raise RepositorySyncError(
                "superproject_sync_failed",
                result.stderr or result.stdout or "Hermes superproject sync failed",
                details={"returncode": result.returncode, "stdout": result.stdout},
            )
        after = self.status(
            spec.name, fetch=False, include_github=False, include_last_operation=False
        )
        if (
            not after.get("ok")
            or after.get("ahead")
            or after.get("behind")
            or (after.get("working_tree") or {}).get("dirty")
        ):
            raise RepositorySyncError(
                "superproject_sync_unverified",
                "Hermes sync finished but the superproject is not clean and equal to origin",
                details={"before": before, "after": after, "stdout": result.stdout},
            )
        return {
            "before": before,
            "after": after,
            "sync_script": spec.sync_script,
            "script": {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "duration_ms": result.duration_ms,
            },
        }

    def sync(
        self,
        name: str,
        *,
        auto_commit: bool = False,
        commit_message: str | None = None,
        trigger: str = "manual",
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        del auto_commit
        spec = self.spec(name)
        event = self._event_base(spec, "sync", trigger)
        try:
            if spec.local_mode == "remote_only":
                raise RepositorySyncError(
                    "remote_only",
                    "local sync is available only on the Hermes superproject card",
                    details={"local_mode": spec.local_mode},
                )
            with self.store.lock(name, wait_seconds=wait_seconds):
                result = (
                    self._sync_superproject_locked(spec)
                    if spec.local_mode == "superproject"
                    else self._sync_locked(spec, commit_message=commit_message)
                )
                return self._finish_event(event, ok=True, status="ok", **result)
        except RepositorySyncError as exc:
            return self._finish_event(
                event,
                ok=False,
                status="error",
                error={"code": exc.code, "message": str(exc), "details": exc.details},
            )

    def safe_sync(self, name: str, **kwargs: Any) -> dict[str, Any]:
        return self.sync(name, **kwargs)

    def commit_local(self, name: str, **_: Any) -> dict[str, Any]:
        spec = self.spec(name)
        event = self._event_base(spec, "commit_local", "dashboard")
        return self._finish_event(
            event,
            ok=False,
            status="retired",
            error={
                "code": "operation_retired",
                "message": "production source cannot be committed from Mission Control",
                "details": {},
            },
        )

    def merge_pull_request_rebase(
        self,
        name: str,
        number: int,
        *,
        expected_head_sha: str | None = None,
        trigger: str = "manual",
        pull_after: bool = True,
        auto_commit: bool = False,
    ) -> dict[str, Any]:
        del auto_commit, pull_after
        return self.merge_pr(
            name, int(number), expected_head_sha=expected_head_sha, trigger=trigger
        )

    def merge_pr(
        self,
        name: str,
        number: int,
        *,
        expected_head_sha: str | None = None,
        trigger: str = "manual",
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        """Merge on GitHub and prepare an exact parent gitlink PR when managed."""
        spec = self.spec(name)
        event = self._event_base(spec, "merge_pr", trigger)
        event["pull_number"] = int(number)
        github: dict[str, Any] | None = None
        try:
            with self.store.lock(name, wait_seconds=wait_seconds):
                pull = self.github.pull_detail(spec, int(number))
                if pull.get("draft"):
                    raise RepositorySyncError(
                        "pull_is_draft",
                        "draft pull request must be marked ready before merge",
                    )
                github = self.github.merge_pr_rebase(
                    spec, int(number), expected_head_sha=expected_head_sha
                )
                pin: dict[str, Any] = {
                    "managed": False,
                    "state": "not_projected",
                    "repository": spec.name,
                }
                if spec.superproject_path:
                    target_sha = str(github.get("merge_sha") or "").lower()
                    if not re.fullmatch(r"[0-9a-f]{40,64}", target_sha):
                        raise RepositorySyncError(
                            "superproject_pin_invalid",
                            "GitHub merge did not return a valid child commit SHA",
                            details={
                                "repository": spec.name,
                                "target_sha": github.get("merge_sha"),
                            },
                        )
                    superproject = self._superproject_spec()
                    with self.store.lock(
                        "hermes-superproject-gitlinks", wait_seconds=wait_seconds
                    ):
                        pin = self.github.ensure_superproject_gitlink_pr(
                            superproject, spec, target_sha=target_sha
                        )
                return self._finish_event(
                    event,
                    ok=True,
                    status="ok",
                    github=github,
                    superproject_pin=pin,
                    local_mutation=False,
                )
        except RepositorySyncError as exc:
            partial = github is not None
            return self._finish_event(
                event,
                ok=False,
                status="partial_success" if partial else "error",
                partial_success=partial,
                completed_phase="github_merge" if partial else None,
                github=github,
                local_mutation=False,
                error={"code": exc.code, "message": str(exc), "details": exc.details},
            )

    def prepare_superproject_pin(
        self,
        name: str,
        *,
        trigger: str = "manual",
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        """Reconcile a managed child branch into the aggregate Hermes pin PR."""
        spec = self.spec(name)
        event = self._event_base(spec, "prepare_superproject_pin", trigger)
        if not spec.superproject_path:
            return self._finish_event(
                event,
                ok=False,
                status="error",
                local_mutation=False,
                error={
                    "code": "superproject_projection_unavailable",
                    "message": "repository is not projected into the Hermes superproject",
                    "details": {},
                },
            )
        try:
            with self.store.lock(
                "hermes-superproject-gitlinks", wait_seconds=wait_seconds
            ):
                target_sha = self.github.branch_sha(spec)
                pin = self.github.ensure_superproject_gitlink_pr(
                    self._superproject_spec(), spec, target_sha=target_sha
                )
            return self._finish_event(
                event,
                ok=True,
                status="ok" if pin.get("changed") else "noop",
                superproject_pin=pin,
                local_mutation=False,
            )
        except RepositorySyncError as exc:
            return self._finish_event(
                event,
                ok=False,
                status="error",
                local_mutation=False,
                error={"code": exc.code, "message": str(exc), "details": exc.details},
            )

    def sync_upstream(self, name: str, **_: Any) -> dict[str, Any]:
        spec = self.spec(name)
        event = self._event_base(spec, "sync_upstream", "dashboard")
        return self._finish_event(
            event,
            ok=False,
            status="retired",
            error={
                "code": "upstream_disabled",
                "message": "upstream sync is not part of owner PR control",
            },
        )

    def automation_commands(self) -> dict[str, str]:
        script = _repo_root() / "apps" / "mission-control" / "tools" / "repo_sync.py"
        return {
            "status": f"/usr/bin/python3 {script} --all --status --json",
            "sync": f"/usr/bin/python3 {script} --repo <repo> --sync --json",
        }
