#!/usr/bin/env python3
"""Contracts for the Phase 0 recovery and value-free Adapter baseline."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "tests" / "fixtures" / "adapter_baseline_manifest.json"
PROBE_PATH = ROOT / "tools" / "capture_adapter_baseline.py"


def load_probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("capture_adapter_baseline", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_manifest_contract() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    probe = load_probe()

    expected_routes = {
        (route.method, route.path_template, route.name)
        for route in probe.ROUTE_INVENTORY
    }
    captured_routes = {
        (route["method"], route["path"], route["name"])
        for route in manifest["route_inventory"]
    }
    assert len(captured_routes) == 26
    assert captured_routes == expected_routes

    corpus = manifest["golden_corpus"]
    for route in manifest["route_inventory"]:
        name = route["name"]
        if name == "source-fingerprint":
            assert all(
                f"source-fingerprint:{source}" in corpus
                for source in ("kanban", "permits", "issues", "state")
            )
            continue
        assert name in corpus, f"missing golden result for {name}"
        if route["classification"] == "mutation":
            assert corpus[name]["status"] in {400, 404}

    assert corpus["memory-read"]["body"] == "discarded-before-shaping"
    assert manifest["capture_policy"]["chat_transcripts"] == "not-collected"
    assert manifest["capture_policy"]["credentials"] == "not-collected"
    assert manifest["negative_mutation_probe"] == {
        "permit_db_unchanged": True,
        "issues_db_unchanged": True,
        "memory_directory_unchanged": True,
        "scope": (
            "Only fail-closed validation paths were requested; no valid entity "
            "or file target was used."
        ),
    }

    for source in manifest["source_databases"].values():
        assert source["query_only"] == 1
        assert source["main_wal_shm_unchanged_during_probe"] is True

    recovery = manifest["recovery"]
    assert recovery["tracked_dirty_snapshot_ref"].startswith("refs/codex/recovery/")
    assert len(recovery["tracked_dirty_snapshot_commit"]) == 40
    assert len(recovery["untracked_blob_sha1"]) == 40
    assert len(manifest["adapter_runtime"]["python_source_sha256"]) == 10
    assert manifest["browser_baseline"]["manual_refresh_probe"]["result"] == (
        "fails-zero-flicker-baseline"
    )


def test_shape_redaction_contract() -> None:
    probe = load_probe()
    dynamic_id = "20260816_214805_fc7eefeb"
    secret_value = "must-never-survive-shaping"
    shape = probe._shape(
        {
            "tips": {
                dynamic_id: {
                    "authorization": secret_value,
                    "session_id": "value-is-never-emitted",
                }
            }
        }
    )
    serialized = json.dumps(shape, sort_keys=True)
    assert dynamic_id not in serialized
    assert secret_value not in serialized
    assert "value-is-never-emitted" not in serialized
    assert "<dynamic-key>" in serialized
    assert "<redacted-field>" in serialized


def main() -> None:
    test_manifest_contract()
    test_shape_redaction_contract()
    print("PHASE0_BASELINE_TESTS=PASS")


if __name__ == "__main__":
    main()
