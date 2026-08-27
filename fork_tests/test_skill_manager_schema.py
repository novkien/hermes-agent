from __future__ import annotations

from tools.skill_manager_tool import SKILL_MANAGE_SCHEMA, SKILL_PROMPT_DESC_LIMIT


def test_public_skill_manage_schema_matches_composed_merge_semantics() -> None:
    properties = SKILL_MANAGE_SCHEMA["parameters"]["properties"]
    actions = properties["action"]["enum"]

    assert actions == ["create", "patch", "delete", "write_file", "remove_file"]
    assert "absorbed_into" not in properties
    assert "patch/edit" not in properties["name"]["description"]
    assert "on 'patch' it performs a full rewrite" in properties["content"]["description"]
    assert str(SKILL_PROMPT_DESC_LIMIT - 3) in SKILL_MANAGE_SCHEMA["description"]
    assert "Pinned skills can still be patched" in SKILL_MANAGE_SCHEMA["description"]
    assert "hermes curator unpin <name>" in SKILL_MANAGE_SCHEMA["description"]
