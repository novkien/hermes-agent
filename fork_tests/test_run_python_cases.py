from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/fork_ci"))

from run_python_cases import selected_python_nodeids  # noqa: E402


def test_generated_and_semantic_python_nodes_are_both_executed() -> None:
    manifest = {
        "cases": [
            {
                "runner": "python",
                "nodeids": ["tests/test_owner.py::test_owner_delta"],
                "semantic_nodeids": ["tests/test_owner.py::test_preserved_behavior"],
            },
            {
                "runner": "javascript",
                "nodeids": ["web/src/owner.test.ts"],
            },
        ]
    }

    assert selected_python_nodeids(manifest) == [
        "tests/test_owner.py::test_owner_delta",
        "tests/test_owner.py::test_preserved_behavior",
    ]
