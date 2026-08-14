from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from hermes_cli import mission_control


def _context(tmp_path: Path) -> mission_control.MissionControlUnitContext:
    app_root = tmp_path / "repo" / "apps" / "mission-control"
    (app_root / "agent_mission_control").mkdir(parents=True)
    (app_root / "agent_mission_control" / "main.py").write_text("app = None\n")
    hermes_root = tmp_path / ".hermes"
    return mission_control.MissionControlUnitContext(
        python_path=tmp_path / ".venv" / "bin" / "python",
        app_root=app_root,
        hermes_root=hermes_root,
        data_root=hermes_root / "mission-control",
        env_file=hermes_root / "mission-control.env",
    )


def _gateway_stub(commands: list[list[str]]) -> ModuleType:
    module = ModuleType("hermes_cli.gateway")

    def fake_run(args, **kwargs):
        commands.append(list(args))
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    module._run_systemctl = fake_run  # type: ignore[attr-defined]
    return module


def test_generated_unit_is_independent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mission_control, "_resolve_unit_context", lambda **_: _context(tmp_path))
    unit = mission_control.generate_systemd_unit()

    assert "ExecStart=" in unit
    assert "-m hermes_cli.mission_control" in unit
    assert "Restart=on-failure" in unit
    assert "hermes-gateway" not in unit
    assert "PartOf=" not in unit
    assert "BindsTo=" not in unit
    assert "Requires=" not in unit


def test_gateway_coupling_uses_start_not_restart(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(mission_control, "_configured_gateway_user", lambda _system: None)
    monkeypatch.setattr(mission_control, "systemd_install", lambda **_: False)
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway", _gateway_stub(commands))

    assert mission_control.start_for_gateway(system=False) is True
    assert commands == [["start", mission_control.SERVICE_NAME]]


def test_mission_control_restart_targets_only_its_service(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(mission_control, "_configured_gateway_user", lambda _system: None)
    monkeypatch.setattr(mission_control, "systemd_install", lambda **_: False)
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway", _gateway_stub(commands))

    mission_control.systemd_restart(system=False)

    assert commands == [["restart", mission_control.SERVICE_NAME]]
    assert all("hermes-gateway" not in " ".join(command) for command in commands)


def test_gateway_unit_source_declares_only_one_way_wants() -> None:
    source = Path("hermes_cli/gateway.py").read_text(encoding="utf-8")
    assert source.count("Wants=hermes-mission-control.service") == 2
    assert "PartOf=hermes-mission-control.service" not in source
    assert "BindsTo=hermes-mission-control.service" not in source
    assert "Requires=hermes-mission-control.service" not in source
