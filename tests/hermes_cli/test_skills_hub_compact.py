"""Tests for ``hermes skills compact`` CLI subcommand."""
from __future__ import annotations

import io
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

from hermes_cli.skills_hub import do_compact


def _make_test_skill(skills_dir: Path, name: str, frontmatter: str = "") -> Path:
    """Create a test skill directory with SKILL.md."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    sk_path = skill_dir / "SKILL.md"
    fm = frontmatter or f"""---
name: {name}
description: A test skill
---
# {name}
"""
    sk_path.write_text(fm, encoding="utf-8")
    return sk_path


class TestCompactParser:
    """Parser smoke tests for the compact subcommand."""

    def test_compact_parser_registered(self):
        """Compact subcommand is registered and parses arguments correctly."""
        from hermes_cli.subcommands.skills import build_skills_parser

        import argparse

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()

        def handler(args):
            pass

        build_skills_parser(subparsers, cmd_skills=handler)

        # Basic parse
        args = parser.parse_args(["skills", "compact", "my-skill"])
        assert args.name == "my-skill"
        assert getattr(args, "thread", "") == ""
        assert args.unhide is False
        assert args.status is False

        # With all flags
        args = parser.parse_args(
            ["skills", "compact", "my-skill", "--thread", "12345", "--unhide"]
        )
        assert args.name == "my-skill"
        assert args.thread == "12345"
        assert args.unhide is True
        assert args.status is False

        # Status flag
        args = parser.parse_args(["skills", "compact", "my-skill", "--status"])
        assert args.status is True


class TestDoCompact:
    """Tests for do_compact() function."""

    # ── Error handling ──────────────────────────────────────────────────────

    def test_skill_not_found(self):
        """Error message when skill name doesn't match any installed skill."""
        console = Console(file=io.StringIO())
        with patch("hermes_constants.get_skills_dir") as mock_get:
            mock_get.return_value = Path("/tmp/nonexistent")
            do_compact("nonexistent", thread_id="123", console=console)
        output = console.file.getvalue()
        assert "Error" in output
        assert "nonexistent" in output

    def test_error_no_thread_id_unless_status(self):
        """Error when no thread_id is provided and not in status mode."""
        console = Console(file=io.StringIO())
        with patch("hermes_constants.get_skills_dir") as mock_get:
            mock_get.return_value = Path("/tmp/nonexistent")
            with patch.dict(
                os.environ,
                {"HERMES_SESSION_THREAD_ID": "", "HERMES_THREAD_ID": ""},
            ):
                do_compact("test-skill", thread_id="", console=console)
        output = console.file.getvalue()
        assert "thread" in output.lower() or "error" in output.lower()

    # ── Compact (add thread) ────────────────────────────────────────────────

    def test_compact_adds_thread_id(self, tmp_path):
        """Compact adds a new thread ID to compact_threads list."""
        console = Console(file=io.StringIO())
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _make_test_skill(skills_dir, "test-skill")

        with patch("hermes_constants.get_skills_dir", return_value=skills_dir):
            do_compact("test-skill", thread_id="12345", console=console)

        content = (skills_dir / "test-skill" / "SKILL.md").read_text(encoding="utf-8")
        assert "12345" in content
        assert "compact_threads" in content
        output = console.file.getvalue()
        assert "compact" in output.lower() or "hid" in output.lower()

    def test_compact_appends_to_existing_list(self, tmp_path):
        """Compact appends to an existing compact_threads list."""
        console = Console(file=io.StringIO())
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        frontmatter = """\
---
name: test-skill
description: A test skill
metadata:
  hermes:
    compact_threads:
      - old-thread
---
"""
        _make_test_skill(skills_dir, "test-skill", frontmatter)

        with patch("hermes_constants.get_skills_dir", return_value=skills_dir):
            do_compact("test-skill", thread_id="new-thread", console=console)

        content = (skills_dir / "test-skill" / "SKILL.md").read_text(encoding="utf-8")
        assert "old-thread" in content
        assert "new-thread" in content

    def test_uses_env_var_thread_id(self, tmp_path):
        """Uses HERMES_SESSION_THREAD_ID when no --thread is given."""
        console = Console(file=io.StringIO())
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _make_test_skill(skills_dir, "test-skill")

        with patch("hermes_constants.get_skills_dir", return_value=skills_dir), (
            patch.dict(os.environ, {"HERMES_SESSION_THREAD_ID": "env-123"})
        ):
            do_compact("test-skill", thread_id="", console=console)

        content = (skills_dir / "test-skill" / "SKILL.md").read_text(encoding="utf-8")
        assert "env-123" in content

    # ── Unhide ──────────────────────────────────────────────────────────────

    def test_unhide_removes_thread_id(self, tmp_path):
        """Unhide removes the specified thread ID from the list."""
        console = Console(file=io.StringIO())
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        frontmatter = """\
---
name: test-skill
description: A test skill
metadata:
  hermes:
    compact_threads:
      - 12345
      - 67890
---
"""
        _make_test_skill(skills_dir, "test-skill", frontmatter)

        with patch("hermes_constants.get_skills_dir", return_value=skills_dir):
            do_compact("test-skill", thread_id="12345", unhide=True, console=console)

        content = (skills_dir / "test-skill" / "SKILL.md").read_text(encoding="utf-8")
        assert "67890" in content
        output = console.file.getvalue()
        assert "restored" in output.lower()

    def test_unhide_removes_key_when_empty(self, tmp_path):
        """Unhide removes compact_threads key when the list becomes empty."""
        console = Console(file=io.StringIO())
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        frontmatter = """\
---
name: test-skill
description: A test skill
metadata:
  hermes:
    compact_threads:
      - 12345
---
"""
        _make_test_skill(skills_dir, "test-skill", frontmatter)

        with patch("hermes_constants.get_skills_dir", return_value=skills_dir):
            do_compact("test-skill", thread_id="12345", unhide=True, console=console)

        content = (skills_dir / "test-skill" / "SKILL.md").read_text(encoding="utf-8")
        assert "compact_threads" not in content

    # ── Status ──────────────────────────────────────────────────────────────

    def test_status_shows_active_threads(self, tmp_path):
        """Status lists the thread IDs where the skill is compact."""
        console = Console(file=io.StringIO())
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        frontmatter = """\
---
name: test-skill
description: A test skill
metadata:
  hermes:
    compact_threads:
      - 111
      - 222
---
"""
        _make_test_skill(skills_dir, "test-skill", frontmatter)

        with patch("hermes_constants.get_skills_dir", return_value=skills_dir):
            do_compact("test-skill", status=True, console=console)

        output = console.file.getvalue()
        assert "111" in output
        assert "222" in output

    def test_status_no_compact_threads(self, tmp_path):
        """Status says 'showing on all threads' when compact_threads is absent."""
        console = Console(file=io.StringIO())
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _make_test_skill(skills_dir, "test-skill")

        with patch("hermes_constants.get_skills_dir", return_value=skills_dir):
            do_compact("test-skill", status=True, console=console)

        output = console.file.getvalue()
        assert "all threads" in output.lower()
