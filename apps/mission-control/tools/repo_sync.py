#!/usr/bin/env python3
"""Manual/cron CLI for the canonical repository registry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

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


def build_parser() -> argparse.ArgumentParser:
    registry = default_repository_registry()
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or fast-forward production worktrees declared by "
            "apps/mission-control/config/repositories.yaml."
        )
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true")
    scope.add_argument("--repo", action="append", choices=tuple(registry), metavar="NAME")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true", help="inspect state (default)")
    action.add_argument("--initialize", action="store_true", help="create canonical Git/worktree layout")
    action.add_argument("--sync", action="store_true", help="clean fast-forward production")
    action.add_argument("--merge-pr", type=int, metavar="NUMBER", help="rebase-merge one PR and pull production")
    parser.add_argument("--expected-head-sha")
    parser.add_argument("--trigger", default="manual", choices=("manual", "cron", "hook", "dashboard"))
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--lock-wait", type=float, default=0.0)
    parser.add_argument("--no-github", action="store_true")
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--json", action="store_true")
    # Retained as accepted no-ops so existing cron lines do not fail during rollout.
    parser.add_argument("--auto-commit", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--commit-message", help=argparse.SUPPRESS)
    return parser


def _selected(args: argparse.Namespace, service: RepositorySyncService) -> list[str]:
    return list(dict.fromkeys(args.repo)) if args.repo else list(service.registry)


def _action(args: argparse.Namespace) -> str:
    if args.initialize:
        return "initialize"
    if args.sync:
        return "pull"
    if args.merge_pr is not None:
        return "merge_and_pull"
    return "status"


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    service = RepositorySyncService(
        default_repository_registry(),
        runner=RepositoryGitRunner(timeout=args.timeout),
        store=OperationStore(),
        github=GitHubRestClient(timeout=min(args.timeout, 30)),
        timeout=args.timeout,
    )
    names = _selected(args, service)
    action = _action(args)
    if action == "merge_and_pull" and len(names) != 1:
        raise RepositorySyncError("scope_invalid", "--merge-pr requires exactly one --repo")

    results: list[dict[str, Any]] = []
    for name in names:
        if action == "status":
            results.append(service.status(
                name, fetch=not args.no_fetch, include_github=not args.no_github
            ))
        elif action == "initialize":
            results.append(service.initialize_layout(
                name, trigger=args.trigger, wait_seconds=args.lock_wait
            ))
        elif action == "pull":
            results.append(service.pull_production(
                name, trigger=args.trigger, wait_seconds=args.lock_wait
            ))
        else:
            results.append(service.merge_and_pull(
                name,
                args.merge_pr,
                expected_head_sha=args.expected_head_sha,
                trigger=args.trigger,
                wait_seconds=args.lock_wait,
            ))

    ok = all(bool(row.get("ok")) for row in results)
    return (0 if ok else 1), {
        "ok": ok,
        "action": action,
        "trigger": args.trigger,
        "results": results,
        "automation": service.automation_commands(),
    }


def _print_human(payload: dict[str, Any]) -> None:
    for row in payload.get("results", []):
        repo = row.get("repo") or row.get("name") or "repo"
        state = row.get("state") or row.get("status") or ("ok" if row.get("ok") else "error")
        print(f"[{'OK' if row.get('ok') else 'ERROR'}] {repo}: {state}")
        if row.get("error"):
            print(f"  {row['error'].get('code')}: {row['error'].get('message')}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code, payload = run(args)
    except RepositorySyncError as exc:
        code = 2
        payload = {
            "ok": False,
            "action": _action(args),
            "trigger": args.trigger,
            "error": {"code": exc.code, "message": str(exc), "details": exc.details},
            "results": [],
        }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_human(payload)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
