from pathlib import Path
import re

p = Path('apps/mission-control/agent_mission_control/repository_sync.py')
s = p.read_text(encoding='utf-8')

def sub(pattern, repl, label):
    global s
    s2, n = re.subn(pattern, repl, s, count=1, flags=re.S)
    if n != 1:
        raise SystemExit(f'{label}: {n}')
    s = s2

def one(old, new, label):
    global s
    if s.count(old) != 1:
        raise SystemExit(f'{label}: {s.count(old)}')
    s = s.replace(old, new, 1)

layout = '''    def _layout(self, spec: RepoSpec) -> dict[str, Any]:
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
            "git_dir": git_dir,
            "work_tree": work_tree,
            "git_dir_exists": git_exists,
            "work_tree_exists": work_exists,
            "bound_source": bound,
            "scope_paths": list(spec.scope_paths),
        }
'''
sub(r'    def _layout\(self, spec: RepoSpec\) -> dict\[str, Any\]:\n.*?\n    def _event_base\(', layout + '\n    def _event_base(', 'layout')

initialize = '''    def _scope_args(self, spec: RepoSpec) -> tuple[str, ...]:
        return ("--", *spec.scope_paths) if spec.scope_paths else ()

    def _tracked_status(self, spec: RepoSpec) -> str:
        return self._run_ok(spec, "status", "--porcelain=v1", "-uno", *self._scope_args(spec))

    def _conflict_files(self, spec: RepoSpec) -> list[str]:
        result = self.runner.git(spec, "diff", "--name-only", "--diff-filter=U", *self._scope_args(spec))
        if result.returncode != 0:
            return []
        return [row for row in result.stdout.splitlines() if row.strip()]

    def _apply_scope(self, spec: RepoSpec) -> None:
        if not spec.scope_paths:
            return
        listed = self.runner.git(spec, "ls-files", "-z")
        if listed.returncode != 0:
            raise RepositorySyncError("scope_list_failed", listed.stderr or "could not list repository files")
        files = [row for row in listed.stdout.split("\0") if row]
        if files:
            for start in range(0, len(files), 200):
                chunk = files[start:start + 200]
                reset = self.runner.git(spec, "update-index", "--no-skip-worktree", "--", *chunk)
                if reset.returncode != 0:
                    raise RepositorySyncError("scope_index_failed", reset.stderr or "could not reset scope bits")
        prefixes = tuple(path.rstrip("/") + "/" for path in spec.scope_paths)
        outside = [
            row for row in files
            if row not in spec.scope_paths and not any(row.startswith(prefix) for prefix in prefixes)
        ]
        for start in range(0, len(outside), 200):
            chunk = outside[start:start + 200]
            marked = self.runner.git(spec, "update-index", "--skip-worktree", "--", *chunk)
            if marked.returncode != 0:
                raise RepositorySyncError("scope_index_failed", marked.stderr or "could not apply source scope")

    def initialize_layout(
        self, name: str, *, trigger: str = "dashboard", wait_seconds: float = 0.0
    ) -> dict[str, Any]:
        spec = self.spec(name)
        event = self._event_base(spec, "initialize_layout", trigger)
        try:
            with self.store.lock(name, wait_seconds=wait_seconds):
                layout = self._layout(spec)
                if layout["ready"]:
                    return self._finish_event(event, ok=True, status="noop", layout=layout)
                work_existed = layout["work_tree_exists"]
                self.runner.mkdir(spec, str(Path(layout["git_dir"]).parent))
                self.runner.mkdir(spec, layout["work_tree"])
                if not layout["git_dir_exists"]:
                    init = self.runner.host(spec, "git", "init", "--bare", layout["git_dir"])
                    if init.returncode != 0:
                        raise RepositorySyncError("git_init_failed", init.stderr or init.stdout or "git init --bare failed")

                for key, value in (("core.bare", "false"), ("core.worktree", layout["work_tree"])):
                    cfg = self.runner.git_common(spec, "config", key, value)
                    if cfg.returncode != 0:
                        raise RepositorySyncError("git_config_failed", cfg.stderr or f"failed to set {key}")

                remote = self.runner.git_common(spec, "remote", "get-url", "origin")
                if remote.returncode != 0:
                    add = self.runner.git_common(spec, "remote", "add", "origin", spec.origin_url)
                    if add.returncode != 0:
                        raise RepositorySyncError("remote_add_failed", add.stderr or "remote add failed")
                elif remote.stdout.strip() != spec.origin_url:
                    raise RepositorySyncError(
                        "origin_mismatch", "canonical Git directory has an unexpected origin",
                        details={"expected": spec.origin_url, "actual": remote.stdout.strip()},
                    )
                configured = self.runner.git_common(
                    spec, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"
                )
                if configured.returncode != 0:
                    raise RepositorySyncError("remote_config_failed", configured.stderr or "remote fetch configuration failed")
                fetched = self.runner.git_common(spec, "fetch", "--prune", "origin", spec.branch)
                if fetched.returncode != 0:
                    raise RepositorySyncError("fetch_failed", fetched.stderr or fetched.stdout or "fetch failed")

                remote_ref = self._origin_ref(spec)
                remote_sha = self.runner.git_common(spec, "rev-parse", "--verify", remote_ref)
                if remote_sha.returncode != 0:
                    raise RepositorySyncError("remote_branch_missing", remote_sha.stderr or "remote branch missing")
                local = self.runner.git_common(spec, "rev-parse", "--verify", f"refs/heads/{spec.branch}")
                if local.returncode == 0 and local.stdout != remote_sha.stdout:
                    raise RepositorySyncError(
                        "production_branch_diverged", "canonical Git directory contains a different branch tip",
                        details={"branch": spec.branch, "local_sha": local.stdout, "remote_sha": remote_sha.stdout},
                    )
                if local.returncode != 0:
                    updated = self.runner.git_common(spec, "update-ref", f"refs/heads/{spec.branch}", remote_ref)
                    if updated.returncode != 0:
                        raise RepositorySyncError("branch_init_failed", updated.stderr or "branch init failed")
                head = self.runner.git_common(spec, "symbolic-ref", "HEAD", f"refs/heads/{spec.branch}")
                if head.returncode != 0:
                    raise RepositorySyncError("head_config_failed", head.stderr or "HEAD configuration failed")

                loaded = self.runner.git(spec, "read-tree", f"refs/heads/{spec.branch}")
                if loaded.returncode != 0:
                    raise RepositorySyncError("index_init_failed", loaded.stderr or loaded.stdout or "read-tree failed")
                self._apply_scope(spec)
                if not work_existed:
                    checkout = self.runner.git(spec, "checkout-index", "-a")
                    if checkout.returncode != 0:
                        raise RepositorySyncError("live_source_init_failed", checkout.stderr or checkout.stdout or "checkout-index failed")

                after = self._layout(spec)
                if not after["ready"]:
                    raise RepositorySyncError("layout_unverified", "canonical repository layout was not verified", details=after)
                return self._finish_event(event, ok=True, status="ok", layout=after)
        except RepositorySyncError as exc:
            return self._finish_event(
                event, ok=False, status="error",
                error={"code": exc.code, "message": str(exc), "details": exc.details},
            )
'''
sub(r'    def initialize_layout\(\n.*?\n    def status\(', initialize + '\n    def status(', 'initialize')
one('''                dirty = self._dirty_summary(
                    self._run_ok(spec, "status", "--porcelain=v1", "-uall")
                )
                conflict_text = self._run_ok(spec, "diff", "--name-only", "--diff-filter=U")
                conflicts = [row for row in conflict_text.splitlines() if row.strip()]''', '''                dirty = self._dirty_summary(self._tracked_status(spec))
                conflicts = self._conflict_files(spec)''', 'status-scope')
s = s.replace('"linked_worktree"', '"bound_source"')
s = s.replace('canonical production checkout is not initialized', 'canonical live source is not initialized')
s = s.replace('production worktree has local changes; pull refused', 'live source has local tracked changes; pull refused')
p.write_text(s, encoding='utf-8')
