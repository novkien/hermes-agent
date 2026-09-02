"""Trusted per-turn Agent-to-Agent routing context."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional


_ROOT_TASK_ID: ContextVar[Optional[str]] = ContextVar(
    "hermes_a2a_root_task_id", default=None
)


def get_a2a_root_task_id() -> Optional[str]:
    """Return the trusted A2A root task for the current turn, if any."""

    return _ROOT_TASK_ID.get()


@contextmanager
def bind_a2a_root_task_id(task_id: Optional[str]) -> Iterator[None]:
    """Bind a host-derived A2A root task for one agent turn."""

    token = _ROOT_TASK_ID.set(str(task_id or "").strip() or None)
    try:
        yield
    finally:
        _ROOT_TASK_ID.reset(token)


__all__ = ["bind_a2a_root_task_id", "get_a2a_root_task_id"]
