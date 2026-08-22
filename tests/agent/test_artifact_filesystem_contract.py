from __future__ import annotations

from pathlib import Path

from agent.runtime_invariants import ARTIFACT_FILESYSTEM_CONTRACT


def test_artifact_filesystem_contract_is_bounded_and_independent() -> None:
    assert len(ARTIFACT_FILESYSTEM_CONTRACT.splitlines()) <= 50
    assert "SOUL.md" not in ARTIFACT_FILESYSTEM_CONTRACT
    assert "normalize" not in ARTIFACT_FILESYSTEM_CONTRACT.lower()
    assert "HERMES_ARTIFACT_FILESYSTEM_CONTRACT" not in ARTIFACT_FILESYSTEM_CONTRACT


def test_artifact_filesystem_contract_rejects_remote_storage_as_inferred_project_root() -> None:
    assert "~/.hermes/workspace/remote-data/**" in ARTIFACT_FILESYSTEM_CONTRACT
    assert "Never infer a canonical project" in ARTIFACT_FILESYSTEM_CONTRACT
    assert "A remembered workspace inventory is not project-location authority" in ARTIFACT_FILESYSTEM_CONTRACT


def test_artifact_filesystem_contract_rejects_checksum_identity_ceremony() -> None:
    assert "Do not compute or request SHA" in ARTIFACT_FILESYSTEM_CONTRACT
    assert "Do not spend an agent turn on checksum ceremony" in ARTIFACT_FILESYSTEM_CONTRACT
    assert "Exact path/name" in ARTIFACT_FILESYSTEM_CONTRACT


def test_system_prompt_injects_contract_before_active_skill() -> None:
    source = Path("agent/system_prompt.py").read_text(encoding="utf-8")
    contract_append = "stable_parts.append(ARTIFACT_FILESYSTEM_CONTRACT)"
    active_skill_read = "_auto_skill_prompt = getattr(agent, \"_auto_loaded_skill_prompt\""
    assert source.count(contract_append) == 1
    assert source.index(contract_append) < source.index(active_skill_read)
