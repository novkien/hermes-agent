"""Hermes Mission Control runtime and systemd lifecycle.

AgentOS Mission Control is vendored under ``apps/mission-control`` but runs as
its own process.  The gateway only has a one-way startup dependency on this
service; Mission Control never shares the gateway PID/cgroup and restarting it
does not restart the gateway.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SERVICE_NAME = "hermes-mission-control"
SERVICE_DESCRIPTION = "Hermes AgentOS Mission Control"
DEFAULT_PORT = 51763
_SOURCE_COMMIT = "42a9c191fdebc66ace4aac98a1e581d9ab7a13d1"


@dataclass(frozen=True)
class MissionControlUnitContext:
    python_path: Path
    app_root: Path
    hermes_root: Path
    data_root: Path
    env_file: Path
    username: str | None = None
    group_name: str | None = None


def get_app_root() -> Path:
    """Return the vendored AgentOS Mission Control repository root."""
    return Path(__file__).resolve().parents[1] / "apps" / "mission-control"


def get_default_hermes_root() -> Path:
    """Return the stable default Hermes root, independent of active profile."""
    from hermes_constants import get_default_hermes_root as _get_default_root

    return Path(_get_default_root()).expanduser().resolve()


def get_data_root(hermes_root: Path | None = None) -> Path:
    return (hermes_root or get_default_hermes_root()) / "mission-control"


def get_env_file(hermes_root: Path | None = None) -> Path:
    return (hermes_root or get_default_hermes_root()) / "mission-control.env"


def get_systemd_unit_path(system: bool = False) -> Path:
    if system:
        return Path("/etc/systemd/system") / f"{SERVICE_NAME}.service"
    return Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"


def _systemd_quote(value: str | Path) -> str:
    rendered = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{rendered}"'


def _resolve_unit_context(
    *, system: bool = False, run_as_user: str | None = None
) -> MissionControlUnitContext:
    """Resolve stable paths and identity for a Mission Control unit."""
    from hermes_cli import gateway as gateway_cli

    app_root = get_app_root().resolve()
    python_path = Path(gateway_cli.get_python_path())

    if not system:
        hermes_root = get_default_hermes_root()
        return MissionControlUnitContext(
            python_path=python_path,
            app_root=app_root,
            hermes_root=hermes_root,
            data_root=get_data_root(hermes_root),
            env_file=get_env_file(hermes_root),
        )

    username, group_name, home_dir = gateway_cli._system_service_identity(run_as_user)
    hermes_root = Path(home_dir) / ".hermes"
    app_root = Path(gateway_cli._remap_path_for_user(str(app_root), home_dir))
    python_path = Path(gateway_cli._remap_path_for_user(str(python_path), home_dir))
    return MissionControlUnitContext(
        python_path=python_path,
        app_root=app_root,
        hermes_root=hermes_root,
        data_root=get_data_root(hermes_root),
        env_file=get_env_file(hermes_root),
        username=username,
        group_name=group_name,
    )


def generate_systemd_unit(
    *, system: bool = False, run_as_user: str | None = None
) -> str:
    """Generate an independent Mission Control systemd service definition."""
    ctx = _resolve_unit_context(system=system, run_as_user=run_as_user)
    identity = ""
    wanted_by = "default.target"
    if system:
        identity = f"User={ctx.username}\nGroup={ctx.group_name}\n"
        wanted_by = "multi-user.target"

    environment = (
        f"Environment=\"HERMES_HOME={ctx.hermes_root}\"\n"
        f"Environment=\"PYTHONPATH={ctx.app_root}\"\n"
        f"Environment=\"FRONTEND_DIR={ctx.app_root / 'frontend' / 'dist'}\"\n"
        f"Environment=\"STORE_PATH={ctx.data_root / 'agentos-dashboard.db'}\"\n"
        f"Environment=\"DASHBOARD_STORE_PATH={ctx.data_root / 'store.db'}\"\n"
        "Environment=\"BIND_HOST=127.0.0.1\"\n"
        f"Environment=\"MISSION_CONTROL_PORT={DEFAULT_PORT}\"\n"
        f"Environment=\"ALLOWED_ORIGIN=http://127.0.0.1:{DEFAULT_PORT}\"\n"
        f"Environment=\"ALLOWED_HOST=127.0.0.1:{DEFAULT_PORT}\"\n"
        "Environment=\"PYTHONUNBUFFERED=1\"\n"
        f"EnvironmentFile=-{ctx.env_file}\n"
    )

    return f"""[Unit]
Description={SERVICE_DESCRIPTION}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{identity}WorkingDirectory={_systemd_quote(ctx.data_root)}
{environment}ExecStart={_systemd_quote(ctx.python_path)} -m hermes_cli.mission_control
Restart=on-failure
RestartSec=3
TimeoutStopSec=15
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={_systemd_quote(ctx.data_root)}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy={wanted_by}
"""


def _prepare_data_root(ctx: MissionControlUnitContext, *, system: bool) -> None:
    ctx.data_root.mkdir(parents=True, exist_ok=True)
    if not system or not ctx.username or not ctx.group_name:
        return
    try:
        import grp
        import pwd

        uid = pwd.getpwnam(ctx.username).pw_uid
        gid = grp.getgrnam(ctx.group_name).gr_gid
        os.chown(ctx.data_root, uid, gid)
    except (KeyError, OSError, AttributeError):
        # Unit startup will surface an actionable permission failure.  Do not
        # recursively chown any wider Hermes tree from this installer.
        pass


def systemd_install(
    *,
    force: bool = False,
    system: bool = False,
    run_as_user: str | None = None,
    enable_on_startup: bool = True,
    quiet: bool = False,
) -> bool:
    """Install or refresh the independent Mission Control systemd unit."""
    from hermes_cli import gateway as gateway_cli

    if not gateway_cli.supports_systemd_services():
        raise RuntimeError("Mission Control service installation requires systemd")
    if system:
        gateway_cli._require_root_for_system_service("Mission Control install")
    else:
        gateway_cli._preflight_user_systemd()

    ctx = _resolve_unit_context(system=system, run_as_user=run_as_user)
    if not ctx.app_root.is_dir():
        raise RuntimeError(
            f"Mission Control source is missing at {ctx.app_root}; expected import {_SOURCE_COMMIT}"
        )
    if not (ctx.app_root / "agent_mission_control" / "main.py").is_file():
        raise RuntimeError(f"Mission Control entrypoint is missing under {ctx.app_root}")

    _prepare_data_root(ctx, system=system)
    unit_path = get_systemd_unit_path(system=system)
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    expected = generate_systemd_unit(system=system, run_as_user=run_as_user)
    current = unit_path.read_text(encoding="utf-8") if unit_path.exists() else None
    changed = force or current != expected
    if changed:
        unit_path.write_text(expected, encoding="utf-8")
        gateway_cli._run_systemctl(
            ["daemon-reload"], system=system, check=True, timeout=30
        )
    if enable_on_startup:
        gateway_cli._run_systemctl(
            ["enable", SERVICE_NAME], system=system, check=True, timeout=30
        )
    if not quiet:
        action = "updated" if current is not None else "installed"
        print(f"✓ Mission Control {action}: {unit_path}")
    return changed


def _configured_gateway_user(system: bool) -> str | None:
    if not system:
        return None
    try:
        from hermes_cli import gateway as gateway_cli

        return gateway_cli._read_systemd_user_from_unit(
            gateway_cli.get_systemd_unit_path(system=True)
        )
    except Exception:
        return None


def install_for_gateway(
    *,
    system: bool,
    run_as_user: str | None = None,
    force: bool = False,
    enable_on_startup: bool = True,
) -> bool:
    """Strict install path used while installing the gateway service."""
    return systemd_install(
        force=force,
        system=system,
        run_as_user=run_as_user or _configured_gateway_user(system),
        enable_on_startup=enable_on_startup,
        quiet=True,
    )


def start_for_gateway(*, system: bool) -> bool:
    """Best-effort one-way startup coupling used by gateway start/restart.

    A Mission Control failure is reported but never blocks the gateway.  The
    command is ``start`` rather than ``restart`` so an already-running dashboard
    keeps the same PID while the gateway is restarted.
    """
    try:
        run_as_user = _configured_gateway_user(system)
        systemd_install(
            force=False,
            system=system,
            run_as_user=run_as_user,
            enable_on_startup=True,
            quiet=True,
        )
        from hermes_cli import gateway as gateway_cli

        result = gateway_cli._run_systemctl(
            ["start", SERVICE_NAME],
            system=system,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "start failed").strip()
            print(f"⚠ Mission Control did not start; gateway will continue: {detail}")
            return False
        return True
    except Exception as exc:
        print(f"⚠ Mission Control startup failed; gateway will continue: {exc}")
        return False


def systemd_start(*, system: bool = False) -> None:
    systemd_install(system=system, run_as_user=_configured_gateway_user(system), quiet=True)
    from hermes_cli import gateway as gateway_cli

    gateway_cli._run_systemctl(
        ["start", SERVICE_NAME], system=system, check=True, timeout=30
    )
    print("✓ Mission Control service started")


def systemd_stop(*, system: bool = False) -> None:
    from hermes_cli import gateway as gateway_cli

    gateway_cli._run_systemctl(
        ["stop", SERVICE_NAME], system=system, check=True, timeout=90
    )
    print("✓ Mission Control service stopped")


def systemd_restart(*, system: bool = False) -> None:
    systemd_install(system=system, run_as_user=_configured_gateway_user(system), quiet=True)
    from hermes_cli import gateway as gateway_cli

    gateway_cli._run_systemctl(
        ["restart", SERVICE_NAME], system=system, check=True, timeout=90
    )
    print("✓ Mission Control service restarted")


def systemd_status(*, system: bool = False, full: bool = False) -> bool:
    from hermes_cli import gateway as gateway_cli

    status_args = ["status", SERVICE_NAME, "--no-pager"]
    if full:
        status_args.append("-l")
    gateway_cli._run_systemctl(
        status_args, system=system, check=False, timeout=15
    )
    result = gateway_cli._run_systemctl(
        ["is-active", SERVICE_NAME],
        system=system,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    active = result.stdout.strip() == "active"
    print("✓ Mission Control service is running" if active else "✗ Mission Control service is stopped")
    return active


def systemd_uninstall(*, system: bool = False) -> None:
    from hermes_cli import gateway as gateway_cli

    if system:
        gateway_cli._require_root_for_system_service("Mission Control uninstall")
    gateway_cli._run_systemctl(
        ["stop", SERVICE_NAME], system=system, check=False, timeout=90
    )
    gateway_cli._run_systemctl(
        ["disable", SERVICE_NAME], system=system, check=False, timeout=30
    )
    unit_path = get_systemd_unit_path(system=system)
    unit_path.unlink(missing_ok=True)
    gateway_cli._run_systemctl(
        ["daemon-reload"], system=system, check=True, timeout=30
    )
    print("✓ Mission Control service uninstalled")


def run_foreground(*, host: str | None = None, port: int | None = None) -> None:
    """Run Mission Control in the foreground from the vendored source tree."""
    app_root = get_app_root().resolve()
    if not (app_root / "agent_mission_control" / "main.py").is_file():
        raise RuntimeError(f"Mission Control source is missing at {app_root}")

    data_root = get_data_root()
    data_root.mkdir(parents=True, exist_ok=True)
    selected_host = host or os.environ.get("BIND_HOST", "127.0.0.1")
    selected_port = port or int(os.environ.get("MISSION_CONTROL_PORT", str(DEFAULT_PORT)))

    os.environ.setdefault("HERMES_HOME", str(get_default_hermes_root()))
    os.environ.setdefault("FRONTEND_DIR", str(app_root / "frontend" / "dist"))
    os.environ.setdefault("STORE_PATH", str(data_root / "agentos-dashboard.db"))
    os.environ.setdefault("DASHBOARD_STORE_PATH", str(data_root / "store.db"))
    os.environ["BIND_HOST"] = selected_host
    if selected_host in {"127.0.0.1", "::1", "localhost"}:
        os.environ.setdefault("ALLOWED_ORIGIN", f"http://127.0.0.1:{selected_port}")
        os.environ.setdefault("ALLOWED_HOST", f"127.0.0.1:{selected_port}")
    os.environ["_HERMES_MISSION_CONTROL"] = "1"

    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))
    os.chdir(data_root)

    import uvicorn

    uvicorn.run(
        "agent_mission_control.main:app",
        app_dir=str(app_root),
        host=selected_host,
        port=selected_port,
        timeout_graceful_shutdown=5,
    )


def mission_control_command(args: Any) -> None:
    action = getattr(args, "mission_control_command", None) or "run"
    system = bool(getattr(args, "system", False))
    if action == "run":
        run_foreground(
            host=getattr(args, "host", None),
            port=getattr(args, "port", None),
        )
        return
    if action == "install":
        systemd_install(
            force=bool(getattr(args, "force", False)),
            system=system,
            run_as_user=getattr(args, "run_as_user", None),
        )
        return
    if action == "uninstall":
        systemd_uninstall(system=system)
        return
    if action == "start":
        systemd_start(system=system)
        return
    if action == "stop":
        systemd_stop(system=system)
        return
    if action == "restart":
        systemd_restart(system=system)
        return
    if action == "status":
        systemd_status(system=system, full=bool(getattr(args, "full", False)))
        return
    raise ValueError(f"Unsupported Mission Control action: {action}")


def main() -> None:
    """Dedicated service entrypoint; avoids coupling to gateway process state."""
    run_foreground()


if __name__ == "__main__":
    main()
