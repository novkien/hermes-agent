"""Parser for ``hermes mission-control`` lifecycle commands."""

from __future__ import annotations

import argparse
from typing import Callable


def _add_system_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--system",
        action="store_true",
        help="Target the Linux system-level Mission Control service",
    )


def build_mission_control_parser(
    subparsers, *, cmd_mission_control: Callable
) -> None:
    parser = subparsers.add_parser(
        "mission-control",
        help="Run and manage the AgentOS Mission Control service",
        description=(
            "Manage the AgentOS Mission Control app vendored in hermes-agent. "
            "It runs as an independent process from the Hermes gateway."
        ),
    )
    actions = parser.add_subparsers(dest="mission_control_command")

    run_parser = actions.add_parser("run", help="Run Mission Control in the foreground")
    run_parser.add_argument("--host", default=None, help="Bind address (default: BIND_HOST or 127.0.0.1)")
    run_parser.add_argument("--port", type=int, default=None, help="Bind port (default: 51763)")

    install_parser = actions.add_parser("install", help="Install the independent systemd service")
    install_parser.add_argument("--force", action="store_true", help="Rewrite the unit even when unchanged")
    install_parser.add_argument(
        "--run-as-user",
        dest="run_as_user",
        default=None,
        help="User account for a system-level service",
    )
    _add_system_flag(install_parser)

    for action, help_text in (
        ("start", "Start the Mission Control service"),
        ("stop", "Stop the Mission Control service without stopping the gateway"),
        ("restart", "Restart only Mission Control; leave the gateway untouched"),
        ("uninstall", "Uninstall only the Mission Control service"),
    ):
        action_parser = actions.add_parser(action, help=help_text)
        _add_system_flag(action_parser)

    status_parser = actions.add_parser("status", help="Show Mission Control service status")
    status_parser.add_argument(
        "-l",
        "--full",
        action="store_true",
        help="Show untruncated systemd status output",
    )
    _add_system_flag(status_parser)

    parser.set_defaults(func=cmd_mission_control)
