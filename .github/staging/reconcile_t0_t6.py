#!/usr/bin/env python3
"""Reconcile owner source contracts after the ordinary upstream merge."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path


SUBSCRIPTION_FUNCTION = textwrap.dedent(
    """
    def build_nous_subscription_prompt(valid_tool_names: "set[str] | None" = None) -> str:
        'Build a compact Nous subscription capability block for the system prompt.'
        try:
            from hermes_cli.nous_subscription import get_nous_subscription_features
            from tools.tool_backend_helpers import managed_nous_tools_enabled
        except Exception as exc:
            logger.debug("Failed to import Nous subscription helper: %s", exc)
            return ""

        if not managed_nous_tools_enabled():
            return ""

        valid_names = set(valid_tool_names or set())
        relevant_tool_names = {
            "web_search",
            "web_extract",
            "browser_navigate",
            "browser_snapshot",
            "browser_click",
            "browser_type",
            "browser_scroll",
            "browser_console",
            "browser_press",
            "browser_get_images",
            "browser_vision",
            "image_generate",
            "text_to_speech",
            "terminal",
            "process",
            "execute_code",
        }

        if valid_names and not (valid_names & relevant_tool_names):
            return ""

        features = get_nous_subscription_features()

        def _status_line(feature) -> str:
            if feature.managed_by_nous:
                return f"- {feature.label}: active via Nous subscription"
            if feature.active:
                current = feature.current_provider or "configured provider"
                return f"- {feature.label}: currently using {current}"
            if feature.included_by_default and features.nous_auth_present:
                return f"- {feature.label}: included with Nous subscription, not currently selected"
            if feature.key == "modal" and features.nous_auth_present:
                return f"- {feature.label}: optional via Nous subscription"
            return f"- {feature.label}: not currently available"

        lines = [
            "# Nous Subscription",
            "Nous subscription includes managed web tools (Firecrawl), image generation (FAL), OpenAI TTS, OpenAI Whisper STT, and browser automation (Browser Use) by default. Modal execution is optional.",
            "Current capability status:",
        ]
        lines.extend(_status_line(feature) for feature in features.items())
        lines.extend(
            [
                "When a Nous-managed feature is active, do not ask the user for Firecrawl, FAL, OpenAI TTS, OpenAI Whisper, or Browser-Use API keys.",
                "If the user is not subscribed and asks for a capability that Nous subscription would unlock or simplify, suggest Nous subscription as one option alongside direct setup or local alternatives.",
                "Do not mention subscription unless the user asks about it or it directly solves the current missing capability.",
                "Useful commands: hermes setup, hermes setup tools, hermes setup terminal, hermes status.",
            ]
        )
        return "\\n".join(lines)
    """
).lstrip()


def reconcile_prompt_builder() -> str | None:
    path = Path("agent/prompt_builder.py")
    text = path.read_text(encoding="utf-8")
    if "def build_nous_subscription_prompt(" in text:
        return None

    marker = (
        "# =========================================================================\n"
        "# Context files (SOUL.md, AGENTS.md, .cursorrules)\n"
        "# =========================================================================\n"
    )
    if text.count(marker) != 1:
        raise RuntimeError("cannot locate prompt-builder compatibility marker")
    text = text.replace(
        marker,
        SUBSCRIPTION_FUNCTION.rstrip() + "\n\n\n" + marker,
        1,
    )
    path.write_text(text, encoding="utf-8")
    return "agent/prompt_builder.py:build_nous_subscription_prompt"


def reconcile_run_agent() -> str | None:
    path = Path("run_agent.py")
    text = path.read_text(encoding="utf-8")
    export = "    build_nous_subscription_prompt,\n"
    if export in text:
        return None

    marker = (
        "    build_environment_hints,\n"
        "    load_soul_md,\n"
    )
    if text.count(marker) != 1:
        raise RuntimeError("cannot locate run_agent prompt-builder re-export marker")
    text = text.replace(
        marker,
        "    build_environment_hints,\n"
        "    build_nous_subscription_prompt,\n"
        "    load_soul_md,\n",
        1,
    )
    path.write_text(text, encoding="utf-8")
    return "run_agent.py:build_nous_subscription_prompt re-export"


def reconcile_mission_control() -> str:
    path = Path("hermes_cli/mission_control.py")
    text = path.read_text(encoding="utf-8")

    import_anchor = "from __future__ import annotations\n\nimport os\n"
    if "import importlib\n" not in text:
        if text.count(import_anchor) != 1:
            raise RuntimeError("cannot locate Mission Control import anchor")
        text = text.replace(
            import_anchor,
            "from __future__ import annotations\n\nimport importlib\nimport os\n",
            1,
        )

    helper_marker = "\n\n@dataclass(frozen=True)\nclass MissionControlUnitContext:"
    helper = (
        "\n\ndef _gateway_cli():\n"
        '    """Resolve the current gateway module without retaining a stale package binding."""\n'
        '    return importlib.import_module("hermes_cli.gateway")\n'
    )
    if "def _gateway_cli():" not in text:
        if text.count(helper_marker) != 1:
            raise RuntimeError("cannot locate Mission Control helper anchor")
        text = text.replace(helper_marker, helper + helper_marker, 1)

    legacy_import = "    from hermes_cli import gateway as gateway_cli\n"
    count = text.count(legacy_import)
    if count < 8:
        raise RuntimeError(
            f"Mission Control lazy gateway surface is incomplete: {count} imports"
        )
    text = text.replace(legacy_import, "    gateway_cli = _gateway_cli()\n")
    if legacy_import in text:
        raise RuntimeError("stale Mission Control gateway import remains")

    path.write_text(text, encoding="utf-8")
    return "hermes_cli/mission_control.py:dynamic gateway module resolution"


def update_report(reconciliations: list[str]) -> None:
    path = Path("reports/upstream-sync/2026-08-26/t6-merge-validation.json")
    report = json.loads(path.read_text(encoding="utf-8"))
    existing = report.get("owner_source_reconciliations", [])
    if not isinstance(existing, list):
        raise RuntimeError("invalid owner_source_reconciliations report field")
    report["owner_source_reconciliations"] = sorted(
        {str(item) for item in [*existing, *reconciliations]}
    )
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    reconciliations = [
        item
        for item in (
            reconcile_prompt_builder(),
            reconcile_run_agent(),
            reconcile_mission_control(),
        )
        if item
    ]
    update_report(reconciliations)
    print(json.dumps({"owner_source_reconciliations": reconciliations}, indent=2))


if __name__ == "__main__":
    main()
