#!/usr/bin/env python3
"""Cron/manual/hook CLI for AgentOS repository synchronization."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

# Make the Mission Control package importable when this file is executed directly.
MISSION_CONTROL_ROOT = Path(__file__).resolve().parents[1]
if str(MISSION_CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(MISSION_CONTROL_ROOT))

from agent_mission_control.repository_runner import RepositoryGitRunner  # noqa: E402
from agent_mission_control.repository_sync import (  # noqa: E402
    GitHubRestClient,
    OperationStore,
    RepositorySyncError,
    RepositorySyncService,
    default_repository_registry,
)


TRIGGERS = ("manual", "cron", "hook", "dashboard")


def build_parser() -> argparse.ArgumentParser:
    registry = default_repository_registry()
    parser = argparse.ArgumentParser(
        description=(
            "Safely inspect/sync the AgentOS repository registry. Local commits are "
            "recovery-branched before a divergent rebase; modified/staged/untracked "
            "files are stashed with -u and restored after pull. Conflicts stop the "
            "operation and return non-zero."
        )
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--all", action="store_true",
        help="operate on all configured repositories (default when --repo is omitted)",
    )
    scope.add_argument(
        "--repo", action="append", choices=tuple(registry), metavar="NAME",
        help="operate on one or more named repositories; repeat this flag",
    )

    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true", help="inspect state only (default action)")
    action.add_argument("--sync", action="store_true", help="safe fetch + pull --rebase")
    action.add_argument("--commit-only", action="store_true", help="commit current local changes without pulling")
    action.add_argument("--sync-upstream", action="store_true", help="sync configured fork from upstream and then safe-pull")
    action.add_argument("--merge-pr", type=int, metavar="NUMBER", help="rebase-merge one GitHub pull request then safe-pull")

    parser.add_argument(
        "--auto-commit", action="store_true",
        help="after a successful sync/stash restore, git add -A and commit remaining local changes",
    )
    parser.add_argument(
        "--commit-message", default=None,
        help="override the automatic local commit message",
    )
    parser.add_argument(
        "--expected-head-sha", default=None,
        help="for --merge-pr, reject the GitHub merge if the PR head moved",
    )
    parser.add_argument(
        "--trigger", choices=TRIGGERS, default="manual",
        help="record why the run started; cron and a future hook use the same executable",
    )
    parser.add_argument(
        "--timeout", type=int, default=90,
        help="Git/SSH command timeout in seconds (pull/rebase may use a larger internal ceiling)",
    )
    parser.add_argument(
        "--lock-wait", type=float, default=0.0,
        help="seconds to wait for a repository lock before reporting repo_busy",
    )
    parser.add_argument(
        "--no-github", action="store_true",
        help="status mode: skip GitHub PR/upstream enrichment",
    )
    parser.add_argument(
        "--no-fetch", action="store_true",
        help="status mode only: report current local refs without git fetch",
    )
    parser.add_argument(
        "--no-pull-after", action="store_true",
        help="for upstream/PR actions, do not safe-pull production after the GitHub mutation",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="show current state and planned action without mutating Git or GitHub",
    )
    parser.add_argument("--json", action="store_true", help="emit one machine-readable JSON document")
    return parser


def _selected(args: argparse.Namespace, service: RepositorySyncService) -> list[str]:
    if args.repo:
        return list(dict.fromkeys(args.repo))
    return list(service.registry)


def _planned_action(args: argparse.Namespace) -> str:
    if args.sync:
        return "sync"
    if args.commit_only:
        return "commit"
    if args.sync_upstream:
        return "sync_upstream"
    if args.merge_pr is not None:
        return "rebase_merge_pr"
    return "status"


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    registry = default_repository_registry()
    service = RepositorySyncService(
        registry,
        runner=RepositoryGitRunner(timeout=args.timeout),
        store=OperationStore(),
        github=GitHubRestClient(timeout=min(args.timeout, 30)),
        timeout=args.timeout,
    )
    names = _selected(args, service)
    action = _planned_action(args)

    if args.merge_pr is not None and len(names) != 1:
        raise RepositorySyncError("scope_invalid", "--merge-pr requires exactly one --repo")
    if args.sync_upstream and any(not service.spec(name).is_fork for name in names):
        bad = [name for name in names if not service.spec(name).is_fork]
        raise RepositorySyncError(
            "scope_invalid", f"--sync-upstream only supports configured forks: {', '.join(bad)}"
        )

    if args.dry_run:
        rows = [
            {
                "repo": name,
                "planned_action": action,
                "auto_commit": bool(args.auto_commit),
                "status": service.status(
                    name,
                    fetch=not args.no_fetch,
                    include_github=not args.no_github,
                ),
            }
            for name in names
        ]
        ok = all(row["status"].get("ok") for row in rows)
        return (0 if ok else 1), {
            "ok": ok,
            "dry_run": True,
            "action": action,
            "trigger": args.trigger,
            "results": rows,
        }

    results: list[dict[str, Any]] = []
    if action == "status":
        for name in names:
            results.append(
                service.status(
                    name,
                    fetch=not args.no_fetch,
                    include_github=not args.no_github,
                )
            )
    elif action == "sync":
        for name in names:
            results.append(
                service.sync(
                    name,
                    auto_commit=args.auto_commit,
                    commit_message=args.commit_message,
                    trigger=args.trigger,
                    wait_seconds=args.lock_wait,
                )
            )
    elif action == "commit":
        for name in names:
            results.append(
                service.commit_local(
                    name,
                    message=args.commit_message,
                    trigger=args.trigger,
                    wait_seconds=args.lock_wait,
                )
            )
    elif action == "sync_upstream":
        for name in names:
            results.append(
                service.sync_upstream(
                    name,
                    trigger=args.trigger,
                    pull_after=not args.no_pull_after,
                    auto_commit=args.auto_commit,
                )
            )
    else:
        name = names[0]
        results.append(
            service.merge_pull_request_rebase(
                name,
                args.merge_pr,
                expected_head_sha=args.expected_head_sha,
                trigger=args.trigger,
                pull_after=not args.no_pull_after,
                auto_commit=args.auto_commit,
            )
        )

    ok = all(bool(row.get("ok")) for row in results)
    return (0 if ok else 1), {
        "ok": ok,
        "action": action,
        "trigger": args.trigger,
        "auto_commit": bool(args.auto_commit),
        "results": results,
        "automation": service.automation_commands(),
    }


def _print_human(payload: dict[str, Any]) -> None:
    for row in payload.get("results", []):
        repo = row.get("repo") or row.get("name") or "repo"
        ok = bool(row.get("ok"))
        state = row.get("state") or row.get("status") or ("ok" if ok else "error")
        print(f"[{'OK' if ok else 'ERROR'}] {repo}: {state}")
        error = row.get("error")
        if error:
            print(f"  {error.get('code')}: {error.get('message')}")
            details = error.get("details") or {}
            conflicts = details.get("conflict_files") or []
            if conflicts:
                print("  conflicts:")
                for path in conflicts:
                    print(f"    - {path}")
            if details.get("stash_sha"):
                print(f"  preserved stash: {details['stash_sha']}")
            if details.get("backup_branch"):
                print(f"  backup branch: {details['backup_branch']}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code, payload = run(args)
    except RepositorySyncError as exc:
        payload = {
            "ok": False,
            "action": _planned_action(args),
            "trigger": args.trigger,
            "error": {"code": exc.code, "message": str(exc), "details": exc.details},
            "results": [],
        }
        code = 2
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_human(payload)
        if payload.get("error"):
            error = payload["error"]
            print(f"[ERROR] {error.get('code')}: {error.get('message')}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
