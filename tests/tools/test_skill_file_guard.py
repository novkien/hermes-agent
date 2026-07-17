"""Tests for the skill file guard (_check_skill_file_guard) preventing
write_file/patch from modifying skill SKILL.md and references/*.md files.

The guard ensures only skill_manage tool can modify these files.
"""

from __future__ import annotations

import pytest


class TestCheckSkillFileGuard:
    """Direct unit tests for the _check_skill_file_guard function."""

    # --- Should block: SKILL.md files under /skills/ ---

    def test_blocks_skill_md_in_default_profile(self):
        from tools.file_tools import _check_skill_file_guard

        result = _check_skill_file_guard(
            "/home/user/.hermes/skills/creative/comfyui/SKILL.md"
        )
        assert result is not None
        assert "only skill_manager accepted for skill edit" in result

    def test_blocks_skill_md_in_profile_dir(self):
        from tools.file_tools import _check_skill_file_guard

        result = _check_skill_file_guard(
            "/home/user/.hermes/profiles/worker/skills/foo/SKILL.md"
        )
        assert result is not None
        assert "only skill_manager accepted for skill edit" in result

    # --- Should block: references/*.md files under /skills/ ---

    def test_blocks_references_md_in_skill(self):
        from tools.file_tools import _check_skill_file_guard

        result = _check_skill_file_guard(
            "/home/user/.hermes/skills/creative/comfyui/references/prompt-engineering.md"
        )
        assert result is not None
        assert "only skill_manager accepted for skill edit" in result

    # --- Should allow: reference files that are NOT .md ---

    def test_allows_references_json_in_skill(self):
        from tools.file_tools import _check_skill_file_guard

        result = _check_skill_file_guard(
            "/home/user/.hermes/skills/creative/comfyui/references/data.json"
        )
        assert result is None

    def test_allows_references_txt_in_skill(self):
        from tools.file_tools import _check_skill_file_guard

        result = _check_skill_file_guard(
            "/home/user/.hermes/skills/creative/comfyui/references/notes.txt"
        )
        assert result is None

    # --- Should allow: files outside /skills/ ---

    def test_allows_config_yaml(self):
        from tools.file_tools import _check_skill_file_guard

        result = _check_skill_file_guard(
            "/home/user/.hermes/config.yaml"
        )
        assert result is None

    def test_allows_regular_file(self):
        from tools.file_tools import _check_skill_file_guard

        result = _check_skill_file_guard(
            "/home/user/projects/myapp/main.py"
        )
        assert result is None

    def test_allows_file_with_skills_in_name_but_not_in_path(self):
        """Verify 'skills' as a directory component, not a substring match."""
        from tools.file_tools import _check_skill_file_guard

        result = _check_skill_file_guard(
            "/home/user/projects/skills-manager-app/docs/README.md"
        )
        # This path has "skills-" as a dir name, not "/skills/" — should be allowed
        assert result is None

    # --- Edge cases ---

    def test_allows_skill_file_outside_skills_dir(self):
        """A file called SKILL.md but not under /skills/ should be allowed."""
        from tools.file_tools import _check_skill_file_guard

        result = _check_skill_file_guard(
            "/home/user/projects/docs/SKILL.md"
        )
        assert result is None

    def test_allows_emoji_path(self):
        """Unicode/emoji paths should not crash the guard."""
        from tools.file_tools import _check_skill_file_guard

        result = _check_skill_file_guard(
            "/home/user/.hermes/skills/🎨/SKILL.md"
        )
        assert result is not None
        assert "only skill_manager accepted for skill edit" in result

    def test_allows_non_md_reference_with_emoji_path(self):
        from tools.file_tools import _check_skill_file_guard

        result = _check_skill_file_guard(
            "/home/user/.hermes/skills/🎨/references/data.json"
        )
        assert result is None

    def test_empty_path_does_not_crash(self):
        from tools.file_tools import _check_skill_file_guard

        # Empty string — should not crash
        result = _check_skill_file_guard("")
        # If it resolves, it won't match; if it errors, it catches and returns None
        assert result is None or "skill_manager" in result

    def test_skill_md_not_under_skills(self):
        """SKILL.md without /skills/ in path should be allowed."""
        from tools.file_tools import _check_skill_file_guard

        result = _check_skill_file_guard(
            "/tmp/SKILL.md"
        )
        assert result is None
