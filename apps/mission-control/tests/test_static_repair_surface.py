#!/usr/bin/env python3
"""Static acceptance checks for the owner-reported dashboard failure surfaces."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "frontend" / "dist"


def text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def require(relative: str, *needles: str) -> None:
    content = text(relative)
    for needle in needles:
        assert needle in content, f"{relative}: missing required contract marker {needle!r}"


def forbid(relative: str, *needles: str) -> None:
    content = text(relative)
    for needle in needles:
        assert needle not in content, f"{relative}: known broken pattern still present {needle!r}"


def main() -> None:
    require("frontend/dist/app.js", "buildHash(route.path, params)", "summarizeSourceHealth(capabilitiesEnvelope?.data)")
    require("frontend/dist/profile.js", "delete route.params[PROFILE_KEY]", "setProfileInLocation")
    require("frontend/dist/tabs/chat.js", "export function createChat", "export function createChatModal")
    # The chat tab is now an orchestrator over tabs/chat/* and pure/chat-*.
    # These markers pin the split itself: the turn reducer must stay pure (no
    # DOM), and the tab must keep going through it rather than poking the
    # transcript from inside the SSE handler again.
    require("frontend/dist/pure/chat-turn.js", "export function reduceTurn", "export function createTurn")
    forbid("frontend/dist/pure/chat-turn.js", "document.", "window.")
    require("frontend/dist/tabs/chat/composer.js", "reduceTurn(turn, event)", "controller.abort()")
    require("frontend/dist/tabs/chat/turn-view.js", "export function createTurnView")
    # Live-verified (browser, 2026-08-12): app.js's navigate() re-mounts and
    # re-activates the CACHED tab instance for the current route on every call
    # (tabInstances keys by route, not by params). openThread used to call
    # onNavigate('chat', {s: sessionId}) to sync the URL after every selection
    # — including the one navigate()'s own re-activation triggers — so opening
    # a session looped forever: activate() -> load() -> openThread() ->
    # onNavigate() -> navigate() -> activate() -> ... 512+ requests observed
    # before this landed. The fix writes the URL directly with
    # window.history.replaceState, bypassing app.js's router entirely for this
    # in-place sync.
    require("frontend/dist/tabs/chat.js", "window.history.replaceState(")
    forbid("frontend/dist/tabs/chat.js", "onNavigate('chat', { s: sessionId }")
    # The render-smoke DOM shim is what would have caught this in CI: it needs
    # `history` on its `window` shim for the deep-link activation path to run
    # at all instead of throwing before reaching the loop.
    require("tools/render_smoke.mjs", "history: { replaceState:")
    # Overview used to index the kanban envelope as if it were an array. The
    # repair is that it shapes through `taskRows`; the Stage 6 rebuild moved
    # that call into the `loadEnvelope` pick rather than removing it, so the
    # marker follows the call, not the old local variable name.
    require("frontend/dist/tabs/overview.js", "pick: taskRows", "import { createStatRow }")
    # Same repair, same rule: pin the CALL, not the local variable that happens
    # to receive it. The Stage 7 filter work renamed it to `loaded` so the
    # filtered subset could keep the name `rows`.
    require("frontend/dist/tabs/kanban.js", "taskRows(tasks.data)")
    require("frontend/dist/tabs/sessions.js", "sessionRows(sessions.data)", "offset")
    require("frontend/dist/tabs/alerts.js", "alertRows(alerts.data)")
    require("frontend/dist/tabs/skills.js", "export function renderSkills", "Array.isArray(raw)")
    require("frontend/dist/tabs/plugins.js", "export function renderPlugins", "Array.isArray(raw)")
    require("frontend/dist/tabs/logs.js", "export function createLogs", "logLines(envelope.data)")
    require("frontend/dist/tabs/command-center.js", "export function createCommandCenter", "/api/upstream/api/ops/doctor")
    require("frontend/dist/tabs/llama-proxy.js", "export function createLlamaProxy", "LLaMA_PROXY_URL")
    require("frontend/dist/pure/data-shape.js", "export function listFrom", "export function summarizeSourceHealth")
    # Context-window gauge. The panel must stay INSIDE .chat-composer and above
    # .chat-composer-box: that placement is what makes it expand upward into
    # the transcript instead of overlaying it or pushing the input off-screen.
    require(
        "frontend/dist/tabs/chat/context-panel.js",
        "chat-context-panel",
        "/context`",
    )
    require(
        "frontend/dist/tabs/chat/composer.js",
        "if (contextPanel) box.append(contextPanel.node);",
    )
    require("frontend/dist/pure/context-window.js", "export function normalizeContextWindow")
    require("frontend/dist/styles.css", ".chat-context-panel", "--context-reserve")

    forbid("frontend/dist/tabs/overview.js", "(runningTasks.data || []).slice")
    forbid("frontend/dist/app.js", "capabilities.sources")
    forbid("frontend/dist/pure/hash-router.js", "query.set('profile'")

    expected = [
        "api.js",
        "app.js",
        "profile.js",
        "pure/data-shape.js",
        "pure/hash-router.js",
        "pure/context-window.js",
        "tabs/overview.js",
        "tabs/chat.js",
        "tabs/chat/composer.js",
        "tabs/chat/transcript.js",
        "tabs/chat/turn-view.js",
        "tabs/chat/sider.js",
        "tabs/chat/context-panel.js",
        "tabs/chat/palette.js",
        "tabs/chat/run-trace.js",
        "tabs/chat/attachments.js",
        "tabs/chat/permits.js",
        "pure/chat-turn.js",
        "pure/chat-session.js",
        "pure/chat-model.js",
        "tabs/sessions.js",
        "tabs/kanban.js",
        "tabs/alerts.js",
        "tabs/skills.js",
        "tabs/plugins.js",
        "tabs/logs.js",
        "tabs/command-center.js",
        "tabs/llama-proxy.js",
    ]
    for relative in expected:
        assert (DIST / relative).is_file(), f"missing deployable asset: {relative}"

    print("STATIC_REPAIR_SURFACE_TESTS=PASS")


if __name__ == "__main__":
    main()
