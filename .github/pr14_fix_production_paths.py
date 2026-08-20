from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"missing patch anchor: {label}")
    return text.replace(old, new, 1)


service_path = Path("apps/mission-control/agent_mission_control/repository_sync.py")
s = service_path.read_text(encoding="utf-8")

s = replace_once(
    s,
    "The registry is the single source of truth. Every managed repository uses one\nGit common directory and one live production worktree on its declared host:\n\n    <HERMES_HOME>/repos/<repo>.git\n    <HERMES_HOME>/worktrees/<repo>/production\n",
    "The registry is the single source of truth. Git metadata is centralized on the\nrepository's own host, while production remains in its canonical live source path:\n\n    <HERMES_HOME>/repos/<repo>.git\n    <registry-declared live work tree>\n",
    "service docstring",
)

join_anchor = '''def _join_home(home: str, template: str, repository: str) -> str:\n    relative = template.format(repository=repository).strip().lstrip("/")\n    if ".." in PurePosixPath(relative).parts:\n        raise RepositorySyncError("registry_invalid", f"unsafe registry layout: {template!r}")\n    return f"{home.rstrip('/')}/{relative}"\n'''
join_replacement = join_anchor + '''\n\ndef _resolve_work_tree(home: str, value: Any, *, repository: str) -> str:\n    text = str(value or "").strip()\n    if not text:\n        raise RepositorySyncError(\n            "registry_invalid", f"repository {repository} requires work_tree"\n        )\n    if text in {"~", "$HOME"} or text.startswith("~/") or text.startswith("$HOME/"):\n        return text\n    if text.startswith("/"):\n        return text\n    if ".." in PurePosixPath(text).parts:\n        raise RepositorySyncError(\n            "registry_invalid", f"unsafe work_tree for {repository}: {text!r}"\n        )\n    if text == ".":\n        return home.rstrip("/")\n    return f"{home.rstrip('/')}/{text.lstrip('./')}"\n'''
s = replace_once(s, join_anchor, join_replacement, "work tree resolver")

s = replace_once(
    s,
    '''    git_template = str(layout.get("git_dir") or "repos/{repository}.git")\n    worktree_template = str(\n        layout.get("production_worktree") or "worktrees/{repository}/production"\n    )\n''',
    '''    git_template = str(layout.get("git_dir") or "repos/{repository}.git")\n''',
    "remove generic production worktree",
)

s = replace_once(
    s,
    '''            git_dir=_join_home(host.hermes_home, git_template, name),\n            work_tree=_join_home(host.hermes_home, worktree_template, name),\n''',
    '''            git_dir=_join_home(host.hermes_home, git_template, name),\n            work_tree=_resolve_work_tree(\n                host.hermes_home, raw_spec.get("work_tree"), repository=name\n            ),\n''',
    "registry work tree assignment",
)

s = replace_once(
    s,
    '''    def git(self, spec: RepoSpec, *args: str, timeout: int | None = None) -> CommandResult:\n        return self.host(spec, "git", "-C", self.work_tree(spec), *args, timeout=timeout)\n''',
    '''    def git(self, spec: RepoSpec, *args: str, timeout: int | None = None) -> CommandResult:\n        return self.host(\n            spec,\n            "git",\n            f"--git-dir={self.git_dir(spec)}",\n            f"--work-tree={self.work_tree(spec)}",\n            *args,\n            timeout=timeout,\n        )\n''',
    "explicit git dir/work tree",
)

s = replace_once(
    s,
    '''        linked = False\n        if work_exists:\n            check = self.runner.git(spec, "rev-parse", "--is-inside-work-tree")\n            linked = check.returncode == 0 and check.stdout == "true"\n        return {\n            "ready": bool(git_exists and work_exists and linked),\n''',
    '''        linked = False\n        if git_exists and work_exists:\n            check = self.runner.git(spec, "rev-parse", "--is-inside-work-tree")\n            linked = check.returncode == 0 and check.stdout == "true"\n        return {\n            "ready": bool(git_exists and work_exists and linked),\n''',
    "layout readiness",
)

service_path.write_text(s, encoding="utf-8")

# Registry contract tests: production is no longer a generated worktree tree.
test_path = Path("apps/mission-control/tests/test_repository_sync.py")
t = test_path.read_text(encoding="utf-8")
t = replace_once(
    t,
    '''        for name, spec in registry.items():\n            self.assertEqual(spec.git_dir, f"~/.hermes/repos/{name}.git")\n            self.assertEqual(spec.work_tree, f"~/.hermes/worktrees/{name}/production")\n            self.assertNotIn("/tmp/", spec.work_tree)\n            self.assertNotIn("deployment", spec.work_tree)\n''',
    '''        for name, spec in registry.items():\n            self.assertEqual(spec.git_dir, f"~/.hermes/repos/{name}.git")\n        self.assertEqual(registry["hermes-skills"].work_tree, "~/.hermes")\n        self.assertEqual(registry["hermes-plugins"].work_tree, "~/.hermes/plugins")\n        self.assertEqual(registry["agents"].work_tree, "~/.hermes/profiles")\n        self.assertEqual(registry["llama-proxy"].work_tree, "~/llama-proxy")\n        self.assertEqual(registry["9router"].work_tree, "~/9router")\n        self.assertEqual(registry["godot-mcp"].work_tree, "~/godot-mcp")\n''',
    "registry path assertions",
)
t = t.replace(
    'self.work_tree = self.hermes_home / "worktrees" / "demo" / "production"',
    'self.work_tree = root / "production-live"',
)
test_path.write_text(t, encoding="utf-8")

surface_path = Path("apps/mission-control/tests/repository_surface.mjs")
j = surface_path.read_text(encoding="utf-8")
j = replace_once(
    j,
    '''assert.match(registry, /git_dir: repos\\/\\{repository\\}\\.git/);\nassert.match(registry, /production_worktree: worktrees\\/\\{repository\\}\\/production/);\nassert.match(registry, /llama-proxy:[\\s\\S]*host: jarvis-pi/);\n''',
    '''assert.match(registry, /git_dir: repos\\/\\{repository\\}\\.git/);\nassert.doesNotMatch(registry, /production_worktree/);\nassert.match(registry, /llama-proxy:[\\s\\S]*host: jarvis-pi[\\s\\S]*work_tree: ~\\/llama-proxy/);\nassert.match(registry, /9router:[\\s\\S]*host: jarvis-pi[\\s\\S]*work_tree: ~\\/9router/);\nassert.match(registry, /godot-mcp:[\\s\\S]*host: workstation[\\s\\S]*work_tree: ~\\/godot-mcp/);\n''',
    "surface topology assertions",
)
surface_path.write_text(j, encoding="utf-8")
