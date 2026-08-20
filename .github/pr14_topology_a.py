from pathlib import Path

p = Path('apps/mission-control/agent_mission_control/repository_sync.py')
s = p.read_text(encoding='utf-8')

def one(old, new, label):
    global s
    if s.count(old) != 1:
        raise SystemExit(f'{label}: {s.count(old)}')
    s = s.replace(old, new, 1)

one(
'''The registry is the single source of truth. Every managed repository uses one
Git common directory and one live production worktree on its declared host:

    <HERMES_HOME>/repos/<repo>.git
    <HERMES_HOME>/worktrees/<repo>/production

There is no staging checkout, deployment copy, automatic stash, automatic
commit, production push, or implicit PR batch merge. A pull request merge is a
single owner-selected rebase merge followed by a clean fast-forward of that
repository's production worktree.''',
'''The registry is the single source of truth. Every managed repository keeps Git
metadata in one common directory on its own host and operates directly on the
canonical live source path consumed by Hermes or the service:

    <HERMES_HOME>/repos/<repo>.git
    <HERMES_HOME>/<repo-specific-live-path>

There is no staging checkout, generic production-worktree layer, deployment
copy, automatic stash, automatic commit, production push, or implicit PR batch
merge. A pull request merge is a single owner-selected rebase merge followed by
a clean fast-forward of the canonical live source tree.''',
'docstring')
one('''    work_tree: str
    origin_url: str
    upstream_repo: str | None = None''', '''    work_tree: str
    origin_url: str
    scope_paths: tuple[str, ...] = ()
    upstream_repo: str | None = None''', 'repospec')
old = '''def _join_home(home: str, template: str, repository: str) -> str:
    relative = template.format(repository=repository).strip().lstrip("/")
    if ".." in PurePosixPath(relative).parts:
        raise RepositorySyncError("registry_invalid", f"unsafe registry layout: {template!r}")
    return f"{home.rstrip('/')}/{relative}"
'''
new = old + '''\n\ndef _live_path(home: str, value: Any, *, repository: str) -> str:
    relative = str(value or "").strip()
    if not relative:
        raise RepositorySyncError("registry_invalid", f"repository {repository} has no work_tree")
    if relative in {".", "./"}:
        return home.rstrip("/")
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise RepositorySyncError("registry_invalid", f"unsafe work_tree for {repository}: {relative!r}")
    return f"{home.rstrip('/')}/{str(path).lstrip('/')}"


def _scope_paths(value: Any, *, repository: str) -> tuple[str, ...]:
    if value in (None, []):
        return ()
    if not isinstance(value, list):
        raise RepositorySyncError("registry_invalid", f"repository {repository} paths must be a list")
    rows: list[str] = []
    for raw in value:
        item = str(raw or "").strip().strip("/")
        path = PurePosixPath(item)
        if not item or path.is_absolute() or ".." in path.parts or item == ".":
            raise RepositorySyncError("registry_invalid", f"unsafe scoped path for {repository}: {raw!r}")
        rows.append(str(path))
    return tuple(dict.fromkeys(rows))
'''
one(old, new, 'helpers')
one('''    git_template = str(layout.get("git_dir") or "repos/{repository}.git")
    worktree_template = str(
        layout.get("production_worktree") or "worktrees/{repository}/production"
    )
    hosts: dict[str, HostSpec] = {}''', '''    git_template = str(layout.get("git_dir") or "repos/{repository}.git")
    hosts: dict[str, HostSpec] = {}''', 'template')
one('''        visibility = str(raw_spec.get("visibility") or "private").strip().lower()
        upstream = str(raw_spec.get("upstream") or "").strip() or None''', '''        visibility = str(raw_spec.get("visibility") or "private").strip().lower()
        work_tree = _live_path(host.hermes_home, raw_spec.get("work_tree"), repository=name)
        scope_paths = _scope_paths(raw_spec.get("paths"), repository=name)
        upstream = str(raw_spec.get("upstream") or "").strip() or None''', 'parse-live')
one('''            git_dir=_join_home(host.hermes_home, git_template, name),
            work_tree=_join_home(host.hermes_home, worktree_template, name),
            origin_url=origin_url,
            upstream_repo=upstream,''', '''            git_dir=_join_home(host.hermes_home, git_template, name),
            work_tree=work_tree,
            origin_url=origin_url,
            scope_paths=scope_paths,
            upstream_repo=upstream,''', 'assign-live')
one('''    def git(self, spec: RepoSpec, *args: str, timeout: int | None = None) -> CommandResult:
        return self.host(spec, "git", "-C", self.work_tree(spec), *args, timeout=timeout)''', '''    def git(self, spec: RepoSpec, *args: str, timeout: int | None = None) -> CommandResult:
        return self.host(
            spec,
            "git",
            f"--git-dir={self.git_dir(spec)}",
            f"--work-tree={self.work_tree(spec)}",
            *args,
            timeout=timeout,
        )''', 'git-runner')
p.write_text(s, encoding='utf-8')
