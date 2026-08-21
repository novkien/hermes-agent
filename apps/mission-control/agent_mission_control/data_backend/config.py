"""Source allowlist, settings, and capability metadata.

The four sources below are the ONLY SQLite files this service may open.
Paths are expanded at startup (``~`` is expanded); any other path is rejected.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Multi-board kanban source: every direct child directory of this directory
# that contains a kanban.db is a board. The `task` board remains the default
# for backward compatibility.
DEFAULT_KANBAN_BOARD = "task"

# Worker profile state: every direct child directory of this directory that
# contains a state.db holds one Hermes profile's real session/message history.
# A Kanban worker's conversation lives here, never in the default state.db —
# see the worker-session resolver in queries.py.
STATE_QUERY_BUDGET_MS = 5_000
STATE_SEARCH_BUDGET_MS = 10_000
DEFAULT_QUERY_BUDGET_MS = 5_000

# Rooms: whitelist-extract ONLY these keys from config.yaml.
ROOM_BINDING_KEYS = (
    "room_chat_id",
    "room_slots",
)
ROOM_SLOT_KEYS = (
    "slot",
    "ceo_thread_id",
    "coder_thread_id",
    "research_thread_id",
    "system_thread_id",
)

# Room topics: whitelist-extract ONLY these keys from each
# telegram.extra.group_topics[].topics[] entry. No secret/API key is ever
# read into the response — only routing/labeling metadata.
ROOM_TOPIC_KEYS = (
    "name",
    "thread_id",
    "skills",
    "cross_thread",
    "enabled_skills",
    "enabled_toolsets",
)

# Query parameters that are never accepted (raw SQL / generic access probes).
FORBIDDEN_QUERY_PARAMS = frozenset(
    {"sql", "query", "raw", "table", "columns", "where", "select", "from", "limit_sql"}
)

# Per-source sort allowlists (column names only, never arbitrary expressions).
KANBAN_TASK_SORTS = (
    "created_at",
    "started_at",
    "completed_at",
    "priority",
    "last_heartbeat_at",
)
PERMIT_SORTS = ("created_at", "updated_at", "permit_id", "status")
ISSUE_SORTS = (
    "id",
    "first_seen_at",
    "last_seen_at",
    "created_at",
    "updated_at",
    "occurrence_count",
    "severity",
    "status",
)

# Tasks core columns exposed on the list view (body intentionally excluded).
TASK_CORE_COLUMNS = (
    "id",
    "title",
    "assignee",
    "status",
    "priority",
    "created_by",
    "created_at",
    "started_at",
    "completed_at",
    "workspace_path",
    "current_run_id",
    "session_id",
    "tenant",
    "last_heartbeat_at",
    "last_failure_error",
    "block_kind",
    "goal_mode",
    "max_runtime_seconds",
    "skills",
)

# Messages metadata columns ONLY (content/api_content never selected).
MESSAGE_META_COLUMNS = (
    "id",
    "session_id",
    "role",
    "tool_call_id",
    "tool_name",
    "timestamp",
    "token_count",
    "finish_reason",
)

# llm_provider_requests metadata columns ONLY (payload_json never selected).
LLM_REQUEST_META_COLUMNS = (
    "id",
    "session_id",
    "api_request_id",
    "turn_id",
    "api_call_count",
    "attempt",
    "captured_at",
    "provider",
    "model",
    "api_mode",
    "transport",
    "base_request_id",
)

# Attachment metadata columns (stored_path NEVER returned).
ATTACHMENT_META_COLUMNS = (
    "id",
    "task_id",
    "filename",
    "content_type",
    "size",
    "uploaded_by",
    "created_at",
)

# Run metadata columns (metadata JSON excluded).
RUN_META_COLUMNS = (
    "id",
    "task_id",
    "profile",
    "step_key",
    "status",
    "worker_pid",
    "max_runtime_seconds",
    "last_heartbeat_at",
    "started_at",
    "ended_at",
    "outcome",
    "summary",
    "error",
)

# Comment columns exposed in task detail.
COMMENT_COLUMNS = ("id", "task_id", "author", "body", "created_at")

# Session row columns returned by the timeline (system_prompt excluded).
SESSION_TIMELINE_EXCLUDED = ("system_prompt",)


@dataclass(frozen=True)
class SourceSpec:
    id: str
    path: str
    tables: tuple[str, ...]
    allowed_filters: tuple[str, ...] = ()
    allowed_sorts: tuple[str, ...] = ()
    query_budget_ms: int = DEFAULT_QUERY_BUDGET_MS
    row_counts: bool = True
    note: str = ""


def _resolved(path: Path) -> str:
    return str(path.expanduser().resolve())


def default_sources(hermes_home: Path) -> dict[str, SourceSpec]:
    return {
        "kanban": SourceSpec(
            id="kanban",
            path=_resolved(hermes_home / "kanban" / "boards" / "task" / "kanban.db"),
            tables=(
                "tasks",
                "task_links",
                "task_comments",
                "task_events",
                "task_runs",
                "task_attachments",
                "kanban_notify_subs",
            ),
            allowed_filters=("status", "assignee", "profile"),
            allowed_sorts=KANBAN_TASK_SORTS,
            query_budget_ms=DEFAULT_QUERY_BUDGET_MS,
        ),
        "permits": SourceSpec(
            id="permits",
            path=_resolved(hermes_home / "workspace" / "state" / "permits.db"),
            tables=("permits",),
            allowed_filters=("status", "severity", "approved", "executed"),
            allowed_sorts=PERMIT_SORTS,
            query_budget_ms=DEFAULT_QUERY_BUDGET_MS,
        ),
        "issues": SourceSpec(
            id="issues",
            path=_resolved(
                hermes_home / "workspace" / "state" / "agent-notes" / "issues.db"
            ),
            tables=("issues", "issue_occurrences"),
            allowed_filters=("status", "severity"),
            allowed_sorts=ISSUE_SORTS,
            query_budget_ms=DEFAULT_QUERY_BUDGET_MS,
        ),
        "state": SourceSpec(
            id="state",
            path=_resolved(hermes_home / "state.db"),
            tables=(
                "sessions",
                "messages",
                "session_model_usage",
                "llm_provider_requests",
            ),
            allowed_filters=(),
            allowed_sorts=(),
            query_budget_ms=STATE_QUERY_BUDGET_MS,
            row_counts=False,
            note="state.db exposes only the four typed endpoints; full-table counts are forbidden",
        ),
    }


class Settings:
    """All local backend paths, resolved exactly once during composition."""

    def __init__(
        self,
        hermes_home: str | Path,
        *,
        sources: dict[str, SourceSpec] | None = None,
        kanban_boards_dir: str | Path | None = None,
        default_kanban_board: str = DEFAULT_KANBAN_BOARD,
        state_profiles_dir: str | Path | None = None,
        config_path: str | Path | None = None,
        memory_dir: str | Path | None = None,
        scripts_dir: str | Path | None = None,
        room_bindings_db: str | Path | None = None,
    ) -> None:
        self.hermes_home = Path(hermes_home).expanduser().resolve()
        self.sources = sources if sources is not None else default_sources(self.hermes_home)
        self.kanban_boards_dir = _resolved(
            Path(kanban_boards_dir)
            if kanban_boards_dir is not None
            else self.hermes_home / "kanban" / "boards"
        )
        self.default_kanban_board = default_kanban_board
        self.state_profiles_dir = _resolved(
            Path(state_profiles_dir)
            if state_profiles_dir is not None
            else self.hermes_home / "profiles"
        )
        self.config_path = _resolved(
            Path(config_path) if config_path is not None else self.hermes_home / "config.yaml"
        )
        self.memory_dir = Path(
            memory_dir if memory_dir is not None else self.hermes_home / "memories"
        ).expanduser().resolve()
        self.scripts_dir = Path(
            scripts_dir if scripts_dir is not None else self.hermes_home / "scripts"
        ).expanduser().resolve()
        self.room_bindings_db = Path(
            room_bindings_db
            if room_bindings_db is not None
            else self.hermes_home
            / "workspace"
            / "state"
            / "session-injector"
            / "room_bindings.sqlite3"
        ).expanduser().resolve()

    def resolve_path(self, source_id: str) -> str:
        """Return the exact allowlisted path for a source id."""
        try:
            return self.sources[source_id].path
        except KeyError:
            raise ValueError(f"unknown source: {source_id}")


def build_settings(hermes_home: str | Path | None = None) -> Settings:
    configured_home = hermes_home or os.environ.get("HERMES_HOME") or "~/.hermes"
    return Settings(configured_home)
