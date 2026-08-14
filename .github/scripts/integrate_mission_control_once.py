from __future__ import annotations

from pathlib import Path


def replace_exact(path: str, old: str, new: str, *, count: int = 1) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    found = text.count(old)
    if found != count:
        raise SystemExit(f"{path}: expected {count} matches, found {found}: {old[:120]!r}")
    file.write_text(text.replace(old, new), encoding="utf-8")


def replace_section(path: str, start_marker: str, end_marker: str, replacement: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    start = text.find(start_marker)
    if start < 0:
        raise SystemExit(f"{path}: missing start marker {start_marker!r}")
    end = text.find(end_marker, start)
    if end < 0:
        raise SystemExit(f"{path}: missing end marker {end_marker!r}")
    file.write_text(text[:start] + replacement + text[end:], encoding="utf-8")


# Gateway: one-way systemd startup coupling. No stop/restart propagation.
replace_exact(
    "hermes_cli/gateway.py",
    "Wants=network-online.target\nStartLimitIntervalSec=0",
    "Wants=network-online.target\nWants=hermes-mission-control.service\nStartLimitIntervalSec=0",
    count=2,
)
replace_exact(
    "hermes_cli/gateway.py",
    '''    if system:\n        _require_root_for_system_service("install")\n\n    # Offer to remove legacy units''',
    '''    if system:\n        _require_root_for_system_service("install")\n\n    # Mission Control is installed as a sibling service. It is never a child\n    # process of the gateway, and its lifecycle remains independently addressable.\n    from hermes_cli.mission_control import install_for_gateway\n\n    install_for_gateway(\n        system=system,\n        run_as_user=run_as_user,\n        force=force,\n        enable_on_startup=enable_on_startup,\n    )\n\n    # Offer to remove legacy units''',
)
replace_exact(
    "hermes_cli/gateway.py",
    '''    _require_service_installed("start", system=system)\n    # HERMES_HOME sync happens inside refresh_systemd_unit_if_needed's''',
    '''    _require_service_installed("start", system=system)\n    # Start Mission Control without restarting it when it is already active.\n    # Failure is visible but cannot block the gateway's own startup.\n    from hermes_cli.mission_control import start_for_gateway\n\n    start_for_gateway(system=system)\n    # HERMES_HOME sync happens inside refresh_systemd_unit_if_needed's''',
)
replace_exact(
    "hermes_cli/gateway.py",
    '''    _require_service_installed("restart", system=system)\n    # HERMES_HOME sync happens inside refresh_systemd_unit_if_needed's''',
    '''    _require_service_installed("restart", system=system)\n    # A start job is idempotent: an active Mission Control process keeps its\n    # PID while the gateway restarts. Never issue a sibling restart here.\n    from hermes_cli.mission_control import start_for_gateway\n\n    start_for_gateway(system=system)\n    # HERMES_HOME sync happens inside refresh_systemd_unit_if_needed's''',
)

# CLI parser and handler.
replace_exact(
    "hermes_cli/main.py",
    "from hermes_cli.subcommands.gateway import build_gateway_parser\n",
    "from hermes_cli.subcommands.gateway import build_gateway_parser\nfrom hermes_cli.subcommands.mission_control import build_mission_control_parser\n",
)
replace_exact(
    "hermes_cli/main.py",
    '''def cmd_gateway(args):\n    """Gateway management commands."""''',
    '''def cmd_mission_control(args):\n    """AgentOS Mission Control lifecycle commands."""\n    from hermes_cli.mission_control import mission_control_command\n\n    return mission_control_command(args)\n\n\ndef cmd_gateway(args):\n    """Gateway management commands."""''',
)
replace_exact(
    "hermes_cli/main.py",
    '''    build_gateway_parser(\n        subparsers, cmd_gateway=cmd_gateway, cmd_proxy=cmd_proxy, cmd_gateway_enroll=cmd_gateway_enroll\n    )\n\n    # =========================================================================\n    # lsp command''',
    '''    build_gateway_parser(\n        subparsers, cmd_gateway=cmd_gateway, cmd_proxy=cmd_proxy, cmd_gateway_enroll=cmd_gateway_enroll\n    )\n    build_mission_control_parser(\n        subparsers, cmd_mission_control=cmd_mission_control\n    )\n\n    # =========================================================================\n    # lsp command''',
)
replace_exact(
    "hermes_cli/_parser.py",
    "    hermes gateway install        Install gateway background service\n",
    "    hermes gateway install        Install gateway background service\n"
    "    hermes mission-control status Show independent Mission Control status\n"
    "    hermes mission-control restart Restart dashboard without restarting gateway\n",
)

# Compact root repository authority/topology.
replace_exact(
    "AGENTS.md",
    "- CLI, TUI, web dashboard, desktop, and messaging gateway surfaces;",
    "- CLI, TUI, web dashboard, desktop, AgentOS Mission Control, and messaging gateway surfaces;",
)
replace_exact(
    "AGENTS.md",
    "    U <--> OS[AgentOS Dashboard<br/>Pi]",
    "    U <--> OS[AgentOS Mission Control<br/>Jarvis host]",
)
replace_exact(
    "AGENTS.md",
    "| AgentOS | Browser control plane for the whole Jarvis/Hermes system | `novkien/agent-mission-control`; Pi LAN route currently `192.168.1.140` |",
    "| AgentOS Mission Control | Browser control plane for the whole Jarvis/Hermes system; independent sibling service to the gateway | Native app under `apps/mission-control/`; deployed on the Jarvis/Hermes host |",
)
replace_exact(
    "AGENTS.md",
    "| `novkien/agent-mission-control` | AgentOS dashboard/BFF | Separate AgentOS code until an owner-authorized merge plan is executed |",
    "| `novkien/agent-mission-control` | Historical AgentOS source imported at commit `42a9c191fdebc66ace4aac98a1e581d9ab7a13d1` | Provenance only after merge; canonical source is `apps/mission-control/` in this repository |",
)
replace_exact(
    "AGENTS.md",
    "├── apps/                 # desktop/shared application packages when present",
    "├── apps/                 # desktop/shared packages plus native Mission Control",
)

# Human-readable topology. Keep 9router/llama-proxy on Pi and the temporary
# external adapter separate; only Mission Control moves into this repository/host.
replace_exact(
    "README.md",
    "      HDA[Hermes Dashboard API :9119]\n      CORE[Hermes Agent Core]",
    "      HDA[Hermes Dashboard API :9119]\n      AMC[AgentOS Mission Control :51763]\n      CORE[Hermes Agent Core]",
)
replace_exact(
    "README.md",
    "      AMC[AgentOS Mission Control :51763]\n      R9[9router :20128]",
    "      R9[9router :20128]",
)
replace_exact(
    "README.md",
    "      HM[(novkien/agent-mission-control)]\n",
    "",
)
replace_exact(
    "README.md",
    "    HM --> AMC\n",
    "    HA --> AMC\n",
)
replace_exact(
    "README.md",
    "| AgentOS Mission Control | Browser BFF/control plane for system state, health, governance, chat, events and proxy surfaces | [`novkien/agent-mission-control`](https://github.com/novkien/agent-mission-control); deployed on the Pi |",
    "| AgentOS Mission Control | Browser BFF/control plane for system state, health, governance, chat, events and proxy surfaces | Native `apps/mission-control/` application in this repository; independent `hermes-mission-control.service` on the Jarvis/Hermes host |",
)
replace_section(
    "README.md",
    "## AgentOS control plane\n",
    "## LLM routing\n",
    '''## AgentOS control plane\n\nAgentOS Mission Control is a native application in this repository under\n`apps/mission-control/`. It runs on the Jarvis/Hermes host as an independent\n`hermes-mission-control.service` process alongside `hermes-gateway.service`:\n\n```text\nAgentOS browser\n  → Mission Control on Jarvis/Hermes :51763\n      → Hermes Dashboard API :9119\n      → Hermes Gateway API :8642\n      → temporary external AgentOS adapter :8643\n      → direct/proxied 9router dashboard/API on Pi\n      → direct/proxied llama-proxy dashboard/API on Pi\n```\n\nThe gateway unit uses only `Wants=hermes-mission-control.service`. There is no\n`Requires=`, `PartOf=` or `BindsTo=` relationship. Consequently, Mission Control has\nits own PID, cgroup, logs and restart policy; restarting or stopping it does not affect\nthe gateway. Gateway start/restart issues an idempotent Mission Control start, so an\nalready-running dashboard keeps its PID.\n\nThe BFF keeps bounded allowlists for upstream reads and supported mutation surfaces.\nThe external adapter remains a separate transitional component until a later explicit\nowner-authorized merge. The old Pi checkout/service is retired only by a separate\npost-merge deployment and cleanup operation with runtime evidence.\n\n''',
)
replace_exact(
    "README.md",
    "AgentOS                                    → novkien/agent-mission-control",
    "AgentOS Mission Control                    → novkien/hermes-agent/apps/mission-control",
)
replace_exact(
    "README.md",
    "├── gateway/              # messaging gateway runtime\n",
    "├── gateway/              # messaging gateway runtime\n"
    "├── apps/mission-control/ # native AgentOS BFF/control-plane application\n",
)
