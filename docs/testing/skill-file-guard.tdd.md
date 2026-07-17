# TDD Evidence Report: Skill-file guard for write_file/patch

## Summary
Added a guard blocking `write_file` and `patch` tools from modifying `**/skills/**/SKILL.md` and `**/skills/**/references/*.md` files. Only `skill_manage` should be allowed to edit these files.

## RED evidence
All 13 tests failed with `ImportError: cannot import name '_check_skill_file_guard'` — proving the function didn't exist yet.

```
$ scripts/run_tests.sh tests/tools/test_skill_file_guard.py
FAILED tests/tools/test_skill_file_guard.py::TestCheckSkillFileGuard::test_blocks_skill_md_in_default_profile
... (13 failed, all ImportError)
```

## GREEN evidence
After implementing `_check_skill_file_guard()` and wiring it into `write_file_tool()` and `patch_tool()`:

```
$ scripts/run_tests.sh tests/tools/test_skill_file_guard.py tests/tools/test_cross_profile_guard.py
✓ tests/tools/test_skill_file_guard.py (13✓)
✓ tests/tools/test_cross_profile_guard.py (18✓)
31 tests passed
```

## Test Specification Table

| # | What is guaranteed | Test | Type | Result |
|---|-------------------|------|------|--------|
| 1 | Blocks SKILL.md in default ~/.hermes/skills/ | `test_blocks_skill_md_in_default_profile` | unit | PASS |
| 2 | Blocks SKILL.md in profile dir | `test_blocks_skill_md_in_profile_dir` | unit | PASS |
| 3 | Blocks references/*.md in skill dir | `test_blocks_references_md_in_skill` | unit | PASS |
| 4 | Allows references/*.json in skill dir | `test_allows_references_json_in_skill` | unit | PASS |
| 5 | Allows references/*.txt in skill dir | `test_allows_references_txt_in_skill` | unit | PASS |
| 6 | Allows non-skill config.yaml | `test_allows_config_yaml` | unit | PASS |
| 7 | Allows regular project files | `test_allows_regular_file` | unit | PASS |
| 8 | Allows path with "skills-" substring | `test_allows_file_with_skills_in_name_but_not_in_path` | unit | PASS |
| 9 | Allows SKILL.md outside /skills/ | `test_allows_skill_file_outside_skills_dir` | unit | PASS |
| 10 | Handles emoji paths | `test_allows_emoji_path` | unit | PASS |
| 11 | Handles emoji paths with non-md reference | `test_allows_non_md_reference_with_emoji_path` | unit | PASS |
| 12 | Empty path doesn't crash | `test_empty_path_does_not_crash` | unit | PASS |
| 13 | SKILL.md outside /skills/ allowed | `test_skill_md_not_under_skills` | unit | PASS |
| 14 | In-profile write allowed (non-protected) | `test_in_profile_write_allowed` | integration | PASS |
| 15 | In-profile SKILL.md blocked by skill guard | `test_in_profile_skill_md_now_blocked_by_skill_guard` | integration | PASS |
| 16 | Cross-profile write blocked (non-protected) | `test_cross_profile_write_blocked_by_default` | integration | PASS |
| 17 | Cross-profile SKILL.md blocked by skill guard | `test_cross_profile_skill_md_blocked_by_skill_guard` | integration | PASS |
| 18 | cross_profile=True bypass works (non-protected) | `test_cross_profile_True_bypass` | integration | PASS |
| 19 | cross_profile=True does NOT bypass skill guard | `test_cross_profile_True_does_not_bypass_skill_guard` | integration | PASS |
| 20 | Patch: cross-profile blocked (non-protected) | `test_cross_profile_patch_blocked` | integration | PASS |
| 21 | Patch: cross-profile bypass (non-protected) | `test_cross_profile_patch_bypass` | integration | PASS |
| 22 | Patch: SKILL.md blocked by skill guard | `test_skill_md_patch_blocked_by_skill_guard` | integration | PASS |
| 23 | Patch: cross_profile=True cannot bypass skill guard | `test_skill_md_patch_bypass_blocked_by_skill_guard` | integration | PASS |
| 24 | V4A patch extracts path for cross-profile guard | `test_v4a_patch_extracts_path_for_guard` | integration | PASS |
| 25 | V4A patch SKILL.md blocked by skill guard | `test_v4a_skill_md_patch_blocked_by_skill_guard` | integration | PASS |

## Coverage
Unit tests cover: SKILL.md paths, references/*.md paths, non-.md references, non-skill paths, edge-case paths with emoji, empty paths, paths with "skills" as substring but not under /skills/. Integration tests verify that write_file and patch both correctly invoke the guard in real tool dispatch.

## Design decisions
- Guard fires BEFORE the cross-profile guard → even in-profile write_file/patch on SKILL.md is blocked
- cross_profile=True does NOT bypass the skill file guard
- Uses simple `in` + `endswith` checks (not regex), matching existing `_check_sensitive_path` pattern
