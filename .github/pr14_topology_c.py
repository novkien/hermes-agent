from pathlib import Path
import re

# Frontend wording only; behavior already calls direct pull APIs.
p = Path('apps/mission-control/frontend/dist/tabs/repositories.js')
s = p.read_text(encoding='utf-8')
for old, new in {
    'Canonical production worktree': 'Canonical live source',
    'canonical production worktree': 'canonical live source',
    'Production worktree': 'Live source',
    'production worktree': 'live source',
    'Production ready': 'Live source ready',
    'canonical worktrees': 'canonical live trees',
    "el('span', { text: 'Production' })": "el('span', { text: 'Live source' })",
    "kv('Production', repo.layout?.work_tree, { mono: true })": "kv('Live source', repo.layout?.work_tree, { mono: true })",
    'Initialize production layout': 'Initialize repository layout',
    'Canonical checkout not initialized': 'Canonical live source not initialized',
}.items():
    s = s.replace(old, new)
p.write_text(s, encoding='utf-8')

# Surface contract.
p = Path('apps/mission-control/tests/repository_surface.mjs')
s = p.read_text(encoding='utf-8')
s = s.replace(
    "assert.match(registry, /production_worktree: worktrees\\/\\{repository\\}\\/production/);\nassert.match(registry, /llama-proxy:[\\s\\S]*host: jarvis-pi/);",
    "assert.doesNotMatch(registry, /production_worktree|worktrees\\/\\{repository\\}\\/production/);\nassert.match(registry, /hermes-skills:[\\s\\S]*work_tree: \\.\\n[\\s\\S]*paths:[\\s\\S]*- skills[\\s\\S]*- workspace\\/skills-pack/);\nassert.match(registry, /hermes-plugins:[\\s\\S]*work_tree: plugins/);\nassert.match(registry, /agents:[\\s\\S]*work_tree: profiles/);\nassert.match(registry, /llama-proxy:[\\s\\S]*host: jarvis-pi[\\s\\S]*work_tree: llama-proxy/);",
)
s = s.replace('assert.match(tab, /Initialize production layout/);', 'assert.match(tab, /Initialize repository layout/);')
s = s.replace('assert.match(servicePy, /git merge --ff-only/);', 'assert.match(servicePy, /git merge --ff-only/);\nassert.match(servicePy, /--git-dir=/);\nassert.match(servicePy, /--work-tree=/);')
s = s.replace('assert.doesNotMatch(servicePy, /path_candidates/);', 'assert.doesNotMatch(servicePy, /path_candidates/);\nassert.doesNotMatch(servicePy, /worktrees\\/<repo>|worktrees\\/\\{repository\\}\\/production/);')
p.write_text(s, encoding='utf-8')

# Focused Python tests.
p = Path('apps/mission-control/tests/test_repository_sync.py')
s = p.read_text(encoding='utf-8')
def replace_once(label, old, new):
    if s.count(old) != 1:
        if s.count(old) == 0:
            return s
        raise SystemExit(f'{label} {s.count(old)}')
    return s.replace(old, new, 1)


def regex_once(label, pattern, repl):
    global s
    s2, n = re.subn(pattern, repl, s, count=1, flags=re.M | re.S)
    if n == 0:
        return s
    if n != 1:
        raise SystemExit(f'{label} {n}')
    return s2


s = replace_once(
    "registry test block mismatch",
'''        for name, spec in registry.items():
            self.assertEqual(spec.git_dir, f"~/.hermes/repos/{name}.git")
            self.assertEqual(spec.work_tree, f"~/.hermes/worktrees/{name}/production")
            self.assertNotIn("/tmp/", spec.work_tree)
            self.assertNotIn("deployment", spec.work_tree)''',
'''        expected_live = {
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
        self.assertEqual(registry["hermes-skills"].scope_paths, ("skills", "workspace/skills-pack"))''',
)

s = replace_once(
    "demo work tree default mismatch",
    'self.work_tree = self.hermes_home / "worktrees" / "demo" / "production"',
    'self.work_tree = self.hermes_home / "demo"',
)

pattern = r'''    def test_initialize_creates_one_common_dir_and_one_production_worktree\(self\):
.*?
    def test_pull_fast_forwards_the_live_production_worktree\(self\):'''
replacement = '''    def test_initialize_creates_one_common_dir_and_direct_live_source(self):
        result = self.service.initialize_layout("demo")
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.git_dir.is_dir())
        self.assertTrue(self.work_tree.is_dir())
        self.assertFalse((self.work_tree / ".git").exists())
        self.assertEqual(self.service.runner.git_dir(self.spec), str(self.git_dir))
        self.assertEqual(self.service.runner.git(self.spec, "branch", "--show-current").stdout, "main")
        self.assertEqual((self.work_tree / "base.txt").read_text(), "base\\n")

    def test_pull_fast_forwards_the_live_production_worktree(self):'''
s = regex_once("initialize test block mismatch", pattern, replacement)

s = s.replace(
    'work_tree="/tmp/.hermes/worktrees/demo/production"',
    'work_tree="/tmp/.hermes/demo"',
)

p.write_text(s, encoding='utf-8')
