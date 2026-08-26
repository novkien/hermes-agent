#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

PY_RE = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$")
JS_RE = re.compile(r"(?:\.test|\.spec)\.(?:[cm]?[jt]sx?)$")
FORK_ROOTS = ("fork_tests/", "apps/mission-control/")


def run(repo: Path, *args: str, check: bool = True, binary: bool = False):
    proc = subprocess.run(
        list(args), cwd=repo, check=False, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=not binary,
    )
    if check and proc.returncode:
        out = proc.stdout.decode("utf-8", "replace") if binary else proc.stdout
        err = proc.stderr.decode("utf-8", "replace") if binary else proc.stderr
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(args)}\n{out}\n{err}")
    return proc


def git(repo: Path, *args: str, check: bool = True, binary: bool = False):
    return run(repo, "git", *args, check=check, binary=binary)


def test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    name = PurePosixPath(p).name
    return any((
        p.startswith(("tests/", "tests-js/")),
        "/tests/" in f"/{p}", "/__tests__/" in f"/{p}",
        "/e2e/" in f"/{p}", name == "conftest.py",
        bool(PY_RE.search(p)), bool(JS_RE.search(p)),
        "__snapshots__" in PurePosixPath(p).parts,
    ))


def tree(repo: Path, ref: str):
    raw = git(repo, "ls-tree", "-r", "-z", "-l", "--full-tree", ref, binary=True).stdout
    rows = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, raw_path = record.split(b"\t", 1)
        mode, typ, sha, size = meta.decode().split()[:4]
        rows[raw_path.decode("utf-8", "surrogateescape")] = {
            "mode": mode, "type": typ, "sha": sha,
            "size": None if size == "-" else int(size),
        }
    return rows


def blob(repo: Path, sha: str) -> bytes:
    return git(repo, "cat-file", "blob", sha, binary=True).stdout


def write_blob(repo: Path, target: Path, row) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob(repo, row["sha"]))
    target.chmod(0o755 if int(row["mode"], 8) & stat.S_IXUSR else 0o644)


def remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def units(source: bytes, path: str):
    try:
        root = ast.parse(source.decode("utf-8"), filename=path)
    except Exception:
        return {}
    found = {}
    for node in root.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            found[f"{path}::{node.name}"] = ast.dump(node, include_attributes=False)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test_"):
                    found[f"{path}::{node.name}::{child.name}"] = ast.dump(child, include_attributes=False)
    return found


def domain(path: str) -> str:
    for value in ("agent", "gateway", "hermes_cli", "run_agent", "tools", "plugins", "tui_gateway", "desktop", "web"):
        if value in PurePosixPath(path).parts:
            return value.replace("_", "-")
    if "kanban" in path:
        return "kanban"
    return "misc"


def dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


VERIFY = r'''#!/usr/bin/env python3
import json, re, subprocess
from pathlib import Path, PurePosixPath
R=Path(__file__).resolve().parents[2]
P=re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$")
J=re.compile(r"(?:\.test|\.spec)\.(?:[cm]?[jt]sx?)$")
def run(*a, check=True):
 p=subprocess.run(a,cwd=R,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 if check and p.returncode: raise RuntimeError(p.stderr)
 return p
def ist(p):
 n=PurePosixPath(p).name
 return p.startswith(("tests/","tests-js/")) or "/tests/" in f"/{p}" or "/__tests__/" in f"/{p}" or "/e2e/" in f"/{p}" or n=="conftest.py" or bool(P.search(p)) or bool(J.search(p)) or "__snapshots__" in PurePosixPath(p).parts
def main():
 lock=json.loads((R/"fork/upstream-lock.json").read_text())
 expected=json.loads((R/"fork/manifests/upstream-tests.json").read_text())["tests"]
 tracked={x for x in run("git","ls-files","-z").stdout.split("\0") if x}
 current={p for p in tracked if ist(p) and not p.startswith(("fork_tests/","apps/mission-control/"))}
 errors=[]
 for p in sorted(set(expected)-current): errors.append(f"missing upstream test: {p}")
 for p in sorted(current-set(expected)): errors.append(f"local test leaked into upstream namespace: {p}")
 for p,row in expected.items():
  if (R/p).is_file() and run("git","hash-object","--",p).stdout.strip()!=row["sha"]: errors.append(f"upstream test drift: {p}")
 sha=lock["sha"]
 if run("git","merge-base","--is-ancestor",sha,"HEAD",check=False).returncode: errors.append(f"upstream SHA is not ancestor: {sha}")
 parents=run("git","rev-list","--merges","--parents","HEAD").stdout.splitlines()
 if not any(sha in line.split()[2:] for line in parents): errors.append(f"upstream SHA is not an exact merge parent: {sha}")
 report={"status":"PASS" if not errors else "FAIL","upstream_sha":sha,"upstream_test_paths":len(expected),"errors":errors}
 out=R/".fork-test-reports/upstream-integrity.json"; out.parent.mkdir(exist_ok=True); out.write_text(json.dumps(report,indent=2)+"\n")
 print(json.dumps(report,indent=2)); return 1 if errors else 0
if __name__=="__main__": raise SystemExit(main())
'''

RUN_PY = r'''#!/usr/bin/env python3
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
R=Path(__file__).resolve().parents[2]
def call(*a,cwd=R,env=None): return subprocess.call(a,cwd=cwd,env=env)
def main():
 m=json.loads((R/"fork/manifests/fork-test-snapshot.json").read_text())
 entries=m["entries"]; targets=[]
 for e in entries:
  if e["kind"]=="python": targets += e.get("selected_nodeids") or ([e["original_path"]] if e["original_path"].endswith(".py") and not e["original_path"].endswith("conftest.py") else [])
 targets=sorted(dict.fromkeys(targets))
 if not targets: print("No fork Python regressions."); return 0
 w=Path(tempfile.mkdtemp(prefix="fork-py-"))
 try:
  if call("git","worktree","add","--detach",str(w),"HEAD"): return 2
  for e in entries:
   if e["kind"] not in ("python","support"): continue
   d=w/e["original_path"]; d.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(R/e["snapshot_path"],d); d.chmod(int(e["mode"],8)&0o777)
  env=os.environ.copy(); env.setdefault("HERMES_TEST_WORKERS","4"); env["UV_PROJECT_ENVIRONMENT"]=str(R/".venv"); env["PYTHONPATH"]=str(w)
  rc=call("bash","scripts/run_tests.sh",*targets,cwd=w,env=env)
  out=R/".fork-test-reports"; out.mkdir(exist_ok=True); (out/"fork-python.json").write_text(json.dumps({"classification":"PASS" if rc==0 else "FORK_REGRESSION","exit_code":rc,"targets":targets},indent=2)+"\n")
  return rc
 finally:
  call("git","worktree","remove","--force",str(w)); shutil.rmtree(w,ignore_errors=True)
if __name__=="__main__": raise SystemExit(main())
'''

RUN_JS = r'''#!/usr/bin/env python3
import json, os, shutil, subprocess, tempfile
from pathlib import Path
R=Path(__file__).resolve().parents[2]
def call(*a,cwd=R): return subprocess.call(a,cwd=cwd)
def package(w,p):
 c=(w/p).parent
 while True:
  if (c/"package.json").is_file(): return c
  if c==w: return w
  c=c.parent
def main():
 m=json.loads((R/"fork/manifests/fork-test-snapshot.json").read_text()); entries=m["entries"]
 files=sorted({e["original_path"] for e in entries if e["kind"]=="js"})
 if not files: print("No fork JavaScript regressions."); return 0
 w=Path(tempfile.mkdtemp(prefix="fork-js-")); failures=[]
 try:
  if call("git","worktree","add","--detach",str(w),"HEAD"): return 2
  for e in entries:
   if e["kind"] not in ("js","support"): continue
   d=w/e["original_path"]; d.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(R/e["snapshot_path"],d); d.chmod(int(e["mode"],8)&0o777)
  if (R/"node_modules").exists(): os.symlink(R/"node_modules",w/"node_modules",target_is_directory=True)
  for p in files:
   pkg=package(w,p); rel=os.path.relpath(w/p,pkg); rc=call("npm","exec","--","vitest","run",rel,cwd=pkg)
   if rc: failures.append({"path":p,"exit_code":rc})
  out=R/".fork-test-reports"; out.mkdir(exist_ok=True); (out/"fork-js.json").write_text(json.dumps({"classification":"PASS" if not failures else "FORK_REGRESSION","failures":failures},indent=2)+"\n")
  return 1 if failures else 0
 finally:
  call("git","worktree","remove","--force",str(w)); shutil.rmtree(w,ignore_errors=True)
if __name__=="__main__": raise SystemExit(main())
'''

INVENTORY = r'''#!/usr/bin/env python3
import json
from pathlib import Path
R=Path(__file__).resolve().parents[2]
m=json.loads((R/"fork/manifests/fork-test-snapshot.json").read_text()); u=json.loads((R/"fork/manifests/upstream-tests.json").read_text()); c=json.loads((R/"fork/manifests/regression-contracts.json").read_text())
print(json.dumps({"upstream_test_paths":len(u["tests"]),"snapshot_entries":len(m["entries"]),"python_nodes":sum(len(e.get("selected_nodeids") or []) for e in m["entries"]),"contracts":len(c["contracts"])},indent=2))
'''

CLASSIFY = r'''#!/usr/bin/env python3
import argparse,json,xml.etree.ElementTree as ET
from pathlib import Path
def fail(p):
 if not p or not Path(p).exists(): return set()
 r=set()
 for c in ET.parse(p).getroot().iter("testcase"):
  if c.find("failure") is not None or c.find("error") is not None: r.add((c.get("file") or c.get("classname","")+"::"+c.get("name","")))
 return r
a=argparse.ArgumentParser();a.add_argument("--candidate",required=True);a.add_argument("--baseline");a.add_argument("--lane",choices=("fork","upstream"),required=True);a.add_argument("--output",required=True);x=a.parse_args();c=fail(x.candidate);b=fail(x.baseline);k="PASS" if not c else "FORK_REGRESSION" if x.lane=="fork" else "UPSTREAM_BASELINE_FAILURE" if c<=b else "INTEGRATION_REGRESSION";Path(x.output).write_text(json.dumps({"classification":k,"candidate":sorted(c),"baseline":sorted(b)},indent=2)+"\n");print(k)
'''

WORKFLOW = r'''name: Fork regressions
on:
  pull_request:
  push:
    branches: [master]
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: fork-regressions-${{ github.ref }}
  cancel-in-progress: true
jobs:
  integrity:
    name: Upstream test integrity
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: {fetch-depth: 0}
      - run: python scripts/fork_ci/verify_upstream_tests_untouched.py
      - run: python scripts/fork_ci/inventory_tests.py
      - uses: actions/upload-artifact@v4
        if: always()
        with: {name: fork-integrity, path: .fork-test-reports/, if-no-files-found: warn}
  python:
    name: Fork Python regressions
    needs: integrity
    runs-on: ubuntu-latest
    timeout-minutes: 60
    env: {HERMES_TEST_WORKERS: "4", OPENAI_API_KEY: "", ANTHROPIC_API_KEY: "", OPENROUTER_API_KEY: ""}
    steps:
      - uses: actions/checkout@v4
        with: {fetch-depth: 0}
      - uses: actions/setup-python@v5
        with: {python-version: "3.11"}
      - uses: astral-sh/setup-uv@v6
        with: {enable-cache: true}
      - run: uv sync --locked
      - run: uv run python scripts/fork_ci/run_fork_python.py
      - uses: actions/upload-artifact@v4
        if: always()
        with: {name: fork-python, path: .fork-test-reports/, if-no-files-found: warn}
  javascript:
    name: Fork JavaScript regressions
    needs: integrity
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v4
        with: {fetch-depth: 0}
      - uses: actions/setup-node@v4
        with: {node-version-file: .nvmrc, cache: npm}
      - run: npm ci --ignore-scripts
      - run: python3 scripts/fork_ci/run_fork_js.py
      - uses: actions/upload-artifact@v4
        if: always()
        with: {name: fork-javascript, path: .fork-test-reports/, if-no-files-found: warn}
  mission-control:
    name: Mission Control contracts
    needs: integrity
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.11"}
      - run: python apps/mission-control/tests/test_runtime_contracts.py
      - run: python apps/mission-control/tests/test_static_repair_surface.py
      - run: node apps/mission-control/tests/frontend_contracts.mjs
      - run: node apps/mission-control/tests/skills_surface.mjs
      - run: python -m compileall -q apps/mission-control/agent_mission_control
  gate:
    name: All fork contracts pass
    if: always()
    needs: [integrity, python, javascript, mission-control]
    runs-on: ubuntu-latest
    steps:
      - env: {I: "${{ needs.integrity.result }}", P: "${{ needs.python.result }}", J: "${{ needs.javascript.result }}", M: "${{ needs.mission-control.result }}"}
        run: test "$I" = success && test "$P" = success && test "$J" = success && test "$M" = success
'''

README = """# Fork regression layer\n\nUpstream-owned tests are byte-locked to `fork/upstream-lock.json`. Owner regressions are exact snapshots under `fork_tests/snapshots/` and run only in a disposable Git worktree, overlaid at their historical paths. This preserves path-sensitive fixtures without editing upstream test files.\n\nCommands:\n\n```bash\npython scripts/fork_ci/verify_upstream_tests_untouched.py\npython scripts/fork_ci/inventory_tests.py\npython scripts/fork_ci/run_fork_python.py\npython scripts/fork_ci/run_fork_js.py\n```\n"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--owner-base", required=True)
    ap.add_argument("--upstream", required=True)
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    base = git(repo, "merge-base", args.owner_base, args.upstream).stdout.strip()
    bt, ot, ut = tree(repo, base), tree(repo, args.owner_base), tree(repo, args.upstream)
    base_tests = {p for p in bt if test_path(p) and not p.startswith(FORK_ROOTS)}
    owner_tests = {p for p in ot if test_path(p) and not p.startswith(FORK_ROOTS)}
    upstream_tests = {p for p in ut if test_path(p) and not p.startswith(FORK_ROOTS)}
    modified = sorted(p for p in base_tests if ot.get(p, {}).get("sha") != bt[p]["sha"] or ot.get(p, {}).get("mode") != bt[p]["mode"])
    local_only = sorted(owner_tests - base_tests)
    remove(repo / "fork_tests/snapshots")
    entries = []
    for ownership, paths in (("modified-upstream", modified), ("local-only", local_only)):
        for p in paths:
            owner = ot.get(p); original = repo / p
            if owner and owner["type"] == "blob":
                snap = f"fork_tests/snapshots/{p}"
                write_blob(repo, repo / snap, owner)
                kind = "python" if p.endswith(".py") else "js" if re.search(r"\.[cm]?[jt]sx?$", p) else "support"
                selected = []
                if kind == "python" and PurePosixPath(p).name != "conftest.py":
                    ou = units(blob(repo, owner["sha"]), p)
                    bu = units(blob(repo, bt[p]["sha"]), p) if p in bt else {}
                    selected = sorted(ou if ownership == "local-only" else [n for n, f in ou.items() if bu.get(n) != f] or ou)
                entries.append({"original_path": p, "snapshot_path": snap, "ownership": ownership, "kind": kind, "mode": owner["mode"], "owner_blob_sha": owner["sha"], "base_blob_sha": bt.get(p, {}).get("sha"), "selected_nodeids": selected, "domain": domain(p)})
            if p in bt and bt[p]["type"] == "blob":
                write_blob(repo, original, bt[p])
            else:
                remove(original)
    lock = {"version": 1, "repository": "NousResearch/hermes-agent", "ref": "main", "sha": args.upstream, "owner_base_sha": args.owner_base, "merge_base_sha": base, "generated_at": datetime.now(timezone.utc).isoformat()}
    up = {"version": 1, "repository": "NousResearch/hermes-agent", "sha": args.upstream, "tests": {p: ut[p] for p in sorted(upstream_tests) if ut[p]["type"] == "blob"}}
    snapshot = {"version": 1, "owner_base_sha": args.owner_base, "upstream_target_sha": args.upstream, "merge_base_sha": base, "entries": sorted(entries, key=lambda e: e["original_path"]), "summary": {"modified_upstream_paths": len(modified), "local_only_paths": len(local_only), "python_nodes": sum(len(e["selected_nodeids"]) for e in entries), "js_files": sum(e["kind"] == "js" for e in entries)}}
    groups = defaultdict(list)
    for e in entries:
        if e["kind"] in ("python", "js"):
            groups[e["domain"]].append(e)
    contracts = []
    for d, rows in sorted(groups.items()):
        seed = d + "\n" + "\n".join(sorted(r["snapshot_path"] for r in rows))
        contracts.append({"id": f"HERMES-FORK-{d.upper().replace('-', '_')}-{hashlib.sha256(seed.encode()).hexdigest()[:8].upper()}", "name": f"{d} owner regression contract", "domain": d, "owner_snapshot_sha": args.owner_base, "test_targets": sorted({x for r in rows for x in (r["selected_nodeids"] or [r["original_path"]])}), "snapshot_paths": sorted(r["snapshot_path"] for r in rows), "expected_behavior": "Preserve owner behavior while canonical upstream tests remain byte-identical."})
    dump(repo / "fork/upstream-lock.json", lock)
    dump(repo / "fork/manifests/upstream-tests.json", up)
    dump(repo / "fork/manifests/fork-test-snapshot.json", snapshot)
    dump(repo / "fork/manifests/regression-contracts.json", {"version": 1, "contracts": contracts})
    dump(repo / "fork/manifests/build-report.json", {"owner_base_sha": args.owner_base, "upstream_sha": args.upstream, "merge_base_sha": base, "modified_upstream_test_paths": modified, "local_only_test_paths": local_only, "already_isolated_mission_control_tests": sorted(p for p in ot if p.startswith("apps/mission-control/tests/") and test_path(p)), "summary": snapshot["summary"]})
    files = {"scripts/fork_ci/verify_upstream_tests_untouched.py": VERIFY, "scripts/fork_ci/run_fork_python.py": RUN_PY, "scripts/fork_ci/run_fork_js.py": RUN_JS, "scripts/fork_ci/inventory_tests.py": INVENTORY, "scripts/fork_ci/classify_failures.py": CLASSIFY, ".github/workflows/fork-regressions.yml": WORKFLOW, "fork_tests/README.md": README}
    for p, content in files.items():
        target = repo / p; target.parent.mkdir(parents=True, exist_ok=True); target.write_text(content.rstrip() + "\n", encoding="utf-8"); target.chmod(0o755 if p.startswith("scripts/") else 0o644)
    stage = sorted(set(modified + local_only + ["fork", "fork_tests", "scripts/fork_ci", ".github/workflows/fork-regressions.yml"]))
    git(repo, "add", "--", *stage)
    git(repo, "diff", "--cached", "--check")
    git(repo, "commit", "-m", "test(fork): isolate owner regressions from upstream suite")
    print(json.dumps({"base": base, "modified": len(modified), "local_only": len(local_only), "contracts": len(contracts)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
