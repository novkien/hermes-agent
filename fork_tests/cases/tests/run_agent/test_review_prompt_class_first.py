"""Behavior tests for the skill review / combined review prompts.

The review prompts steer the background review agent toward conservative,
targeted patches of existing skills. Autonomous reviews cannot create, rewrite,
delete, or add/remove support files, and must exclude ``agent2agent-*`` skills.

User-preference corrections (style, format, verbosity, legibility) are
first-class skill signals, not just memory signals.

These tests assert behavioral *instructions* are present — they do NOT
snapshot the full prompt text (change-detector).
"""

from run_agent import AIAgent


# ---------------------------------------------------------------------------
# _SKILL_REVIEW_PROMPT
# ---------------------------------------------------------------------------

def test_skill_review_prompt_biases_toward_conservative_patches():
    """Prompt must prefer evidence-backed patches over write volume."""
    prompt = AIAgent._SKILL_REVIEW_PROMPT
    lower = prompt.lower()
    assert "patch conservatively" in lower
    assert "high write count is not the objective" in lower
    assert "most sessions produce" not in lower
    assert "missed learning opportunity" not in lower


def _assert_patch_only_policy(prompt: str, label: str) -> None:
    lower = prompt.lower()
    assert "strict mutation policy" in lower, f"{label}: policy must be explicit"
    assert "action=patch" in lower, f"{label}: must name the sole allowed action"
    assert "old_string/new_string" in lower, f"{label}: must require targeted patches"
    assert "never create a skill" in lower, f"{label}: autonomous create must be forbidden"
    assert "agent2agent-*" in lower, f"{label}: excluded family must be named"
    assert "regardless of whether they are local, external, bundled" in lower, (
        f"{label}: all other existing skill ownership classes must be patchable"
    )


def test_skill_review_prompt_is_patch_only():
    _assert_patch_only_policy(AIAgent._SKILL_REVIEW_PROMPT, "_SKILL_REVIEW_PROMPT")


def test_skill_review_prompt_treats_user_corrections_as_skill_signal():
    """Style/format/verbosity complaints must be FIRST-CLASS skill signals, not just memory."""
    prompt = AIAgent._SKILL_REVIEW_PROMPT
    lower = prompt.lower()
    # Must mention style/format/verbosity-family corrections
    assert any(k in lower for k in ("style", "format", "verbos", "legib", "tone")), (
        "must name style/format/verbosity/legibility as signals"
    )
    # Must frame these as first-class skill signals (not memory-only)
    assert "FIRST-CLASS" in prompt or "first-class" in prompt, (
        "must explicitly label user-preference corrections as first-class skill signals"
    )
    # Must mention the correction-type phrases to tune the model's ear
    assert "stop doing" in lower or "don't" in lower or "hate" in lower or "frustrat" in lower, (
        "must give concrete phrasing examples so the model recognizes corrections"
    )
















# ---------------------------------------------------------------------------
# _COMBINED_REVIEW_PROMPT
# ---------------------------------------------------------------------------

def test_combined_review_prompt_has_memory_section():
    """Memory half must still cover user facts and preferences."""
    prompt = AIAgent._COMBINED_REVIEW_PROMPT
    assert "**Memory**" in prompt
    assert "memory tool" in prompt


def test_combined_review_prompt_is_patch_only():
    _assert_patch_only_policy(AIAgent._COMBINED_REVIEW_PROMPT, "_COMBINED_REVIEW_PROMPT")














# ---------------------------------------------------------------------------
# Anti-pattern guidance — see issue #6051. The reviewer was learning transient
# environment failures (e.g. "browser tools do not work" from a fresh-install
# Playwright miss) as durable skill rules, then citing them against itself for
# weeks after the environment was fixed. Both review prompts must explicitly
# tell the reviewer not to capture environment-dependent or negative-framing
# content as skills.
# ---------------------------------------------------------------------------


def _assert_anti_pattern_guidance(prompt: str, label: str) -> None:
    """Both review prompts must carry the same anti-pattern section."""
    lower = prompt.lower()
    assert "do not capture" in lower, (
        f"{label}: must have an explicit 'Do NOT capture' section"
    )
    # Environment-dependent failures (the #6051 root cause)
    assert any(k in lower for k in ("missing binar", "command not found", "uninstalled", "fresh-install")), (
        f"{label}: must call out environment/setup failures as not-skill-worthy"
    )
    # Negative-framing avoidance
    assert any(k in lower for k in ("negative claim", "do not work", "is broken")), (
        f"{label}: must call out negative-claim phrasings as the failure mode"
    )
    # Positive reframing — "capture the fix, not the failure"
    assert "capture the fix" in lower or "capture the fix " in lower, (
        f"{label}: must redirect tool-failure capture toward the fix, not the constraint"
    )
    # One-off task narratives (#12812 family)
    assert "one-off" in lower, (
        f"{label}: must call out one-off task narratives as not-skill-worthy"
    )


def _assert_unresolved_failure_guidance(prompt: str, label: str) -> None:
    """Unresolved task attempts must not become persistent skill guidance."""
    lower = prompt.lower()
    assert "unresolved failures" in lower, f"{label}: must identify unresolved failures"
    assert "working method" in lower, f"{label}: must require a working method"
    assert "told the user to check manually" in lower, (
        f"{label}: must recognize an explicitly unresolved session"
    )
    assert "never the dead ends" in lower, f"{label}: must exclude failed attempts"
    assert "independently confident" in lower, (
        f"{label}: must limit exceptions to verified alternatives"
    )


def test_skill_review_prompt_rejects_unresolved_failures():
    _assert_unresolved_failure_guidance(AIAgent._SKILL_REVIEW_PROMPT, "_SKILL_REVIEW_PROMPT")


def test_combined_review_prompt_rejects_unresolved_failures():
    _assert_unresolved_failure_guidance(AIAgent._COMBINED_REVIEW_PROMPT, "_COMBINED_REVIEW_PROMPT")


def _assert_read_before_write_guidance(prompt: str, label: str) -> None:
    """Both review prompts must teach the enforced read-before-write handshake.

    The skill_manage guard refuses a patch unless the exact target was loaded
    via skill_view during the review. Without prompt guidance the model walks
    into the refusal and burns iterations retrying (#62397).
    """
    lower = prompt.lower()
    assert "read-before-write" in lower, f"{label}: must name the read-before-write rule"
    assert "skill_view(name)" in prompt, (
        f"{label}: must give the exact SKILL.md pre-read call"
    )
    assert "file_path=..." in prompt, (
        f"{label}: must give the support-file pre-read form"
    )
    assert "existing skill" in lower, f"{label}: patches must target existing skills"
    # Transcript quotes must not be treated as satisfying the guard.
    assert "does not count" in lower or "does NOT count" in prompt or "not satisfy" in lower, (
        f"{label}: must say transcript-quoted content doesn't satisfy the guard"
    )
    # Bounded recovery: one view + one retry, never a loop.
    assert "do not loop" in lower, (
        f"{label}: must bound refusal recovery to a single retry"
    )


def test_skill_review_prompt_teaches_read_before_write():
    _assert_read_before_write_guidance(AIAgent._SKILL_REVIEW_PROMPT, "_SKILL_REVIEW_PROMPT")


def test_combined_review_prompt_teaches_read_before_write():
    _assert_read_before_write_guidance(AIAgent._COMBINED_REVIEW_PROMPT, "_COMBINED_REVIEW_PROMPT")






# ---------------------------------------------------------------------------
# _MEMORY_REVIEW_PROMPT — unchanged, still memory-focused
# ---------------------------------------------------------------------------
