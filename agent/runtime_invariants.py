"""Compatibility exports for always-on Hermes runtime invariants."""

from __future__ import annotations

from agent.artifact_filesystem_contract import ARTIFACT_FILESYSTEM_CONTRACT

if len(ARTIFACT_FILESYSTEM_CONTRACT.splitlines()) > 50:
    raise RuntimeError("Artifact Filesystem Contract must not exceed 50 lines")

__all__ = ["ARTIFACT_FILESYSTEM_CONTRACT"]
