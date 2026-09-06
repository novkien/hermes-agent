"""Optional plugin-owned lifetime for a delegated oneshot invocation.

Core owns agent construction, stdout and cleanup. The selected plugin owns its
native task storage, waiting and authenticated continuation. Ordinary oneshots
remain single-turn. A configured lifetime must never silently fail open.
"""
from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def oneshot_run(agent, config):
    section = config.get("oneshot", {})
    if not isinstance(section, dict):
        raise ValueError("oneshot must be a mapping")
    owner = section.get("lifecycle")
    if owner is None:
        yield agent.run_conversation
        return
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("oneshot.lifecycle must name one installed plugin")

    from hermes_cli.plugins import invoke_hook

    # Only construct a handle in the hook. Waiting runs outside invoke_hook so
    # callback timeout/failure isolation cannot turn a failed mission into a
    # successful ordinary oneshot or abandon a still-running callback thread.
    candidates = invoke_hook("on_oneshot_start", agent=agent, config=config, owner=owner)
    matching = [item for item in candidates if isinstance(item, dict) and item.get("owner") == owner]
    if len(matching) != 1 or not callable(matching[0].get("run")) or not callable(matching[0].get("close")):
        raise RuntimeError(f"Required oneshot lifecycle unavailable or ambiguous: {owner}")
    handle = matching[0]
    try:
        yield handle["run"]
    finally:
        # Includes cancellation, exceptions, budget exhaustion and normal return.
        # The owner must settle its tasks before core closes the agent/database.
        handle["close"]()
