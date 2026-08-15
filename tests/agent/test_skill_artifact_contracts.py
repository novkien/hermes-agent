from __future__ import annotations

import json
from pathlib import Path

from agent.skill_preprocessing import load_artifact_contract, preprocess_skill_content


def _write_registry(root: Path, registry: dict, contracts: dict[str, str]) -> None:
    contract_root = root / ".artifact-contracts"
    contract_root.mkdir(parents=True)
    (contract_root / "registry.json").write_text(json.dumps(registry), encoding="utf-8")
    for relative, content in contracts.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_exact_registry_contract_loads(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "schedule" / "daily-report"
    skill_dir.mkdir(parents=True)
    _write_registry(
        tmp_path,
        {
            "schema_version": 1,
            "contracts": {
                "skills/schedule/daily-report": ".artifact-contracts/wave-2/daily-report.md"
            },
        },
        {".artifact-contracts/wave-2/daily-report.md": "## Artifact Contract\nDaily report output."},
    )
    rendered = preprocess_skill_content("# Skill", skill_dir, skills_cfg={"template_vars": True})
    assert rendered.count("## Artifact Contract") == 1
    assert "Daily report output." in rendered


def test_longest_prefix_contract_loads(tmp_path: Path) -> None:
    skill_dir = tmp_path / "workspace" / "skills-pack" / "coder" / "code-build"
    skill_dir.mkdir(parents=True)
    _write_registry(
        tmp_path,
        {
            "schema_version": 1,
            "prefix_contracts": {
                "workspace/skills-pack/": ".artifact-contracts/generic.md",
                "workspace/skills-pack/coder/": ".artifact-contracts/coder.md",
            },
        },
        {
            ".artifact-contracts/generic.md": "GENERIC CONTRACT",
            ".artifact-contracts/coder.md": "CODER CONTRACT",
        },
    )
    assert load_artifact_contract(skill_dir) == "CODER CONTRACT"


def test_local_sidecar_overrides_registry(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "schedule" / "daily-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "ARTIFACTS.md").write_text("LOCAL CONTRACT", encoding="utf-8")
    _write_registry(
        tmp_path,
        {
            "schema_version": 1,
            "contracts": {
                "skills/schedule/daily-report": ".artifact-contracts/registry.md"
            },
        },
        {".artifact-contracts/registry.md": "REGISTRY CONTRACT"},
    )
    assert load_artifact_contract(skill_dir) == "LOCAL CONTRACT"


def test_unrelated_skill_has_no_contract(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "research" / "web-search"
    skill_dir.mkdir(parents=True)
    _write_registry(tmp_path, {"schema_version": 1, "contracts": {}}, {})
    assert preprocess_skill_content("# Skill", skill_dir, skills_cfg={}) == "# Skill"


def test_unsafe_or_malformed_registry_is_a_noop(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "schedule" / "daily-report"
    skill_dir.mkdir(parents=True)
    _write_registry(
        tmp_path,
        {
            "schema_version": 1,
            "contracts": {"skills/schedule/daily-report": "../outside.md"},
        },
        {},
    )
    assert load_artifact_contract(skill_dir) == ""
    (tmp_path / ".artifact-contracts" / "registry.json").write_text("{broken", encoding="utf-8")
    assert load_artifact_contract(skill_dir) == ""


def test_contract_template_variables_are_resolved(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "evaluation" / "eval-agent"
    skill_dir.mkdir(parents=True)
    (skill_dir / "ARTIFACTS.md").write_text(
        "Path: ${HERMES_SKILL_DIR}; session: ${HERMES_SESSION_ID}",
        encoding="utf-8",
    )
    rendered = preprocess_skill_content(
        "# Skill",
        skill_dir,
        session_id="session-42",
        skills_cfg={"template_vars": True},
    )
    assert str(skill_dir) in rendered
    assert "session-42" in rendered


def test_contract_is_not_appended_twice(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "evaluation" / "eval-agent"
    skill_dir.mkdir(parents=True)
    (skill_dir / "ARTIFACTS.md").write_text("## Artifact Contract\nONE", encoding="utf-8")
    once = preprocess_skill_content("# Skill", skill_dir, skills_cfg={})
    twice = preprocess_skill_content(once, skill_dir, skills_cfg={})
    assert twice.count("## Artifact Contract") == 1
