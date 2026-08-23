"""Canonical contract for Mission Control live resources.

The browser route registry describes navigation.  This module describes the
state behind those routes: authority, profile scope, stable entity identity,
safe persistence, refresh strategy, and DOM reconciliation mode.  It is the
single backend authority used by the event fabric and, in later phases, the
persistent read model and bootstrap APIs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ProfileScope = Literal["profile", "global"]
Persistence = Literal["entities", "snapshot", "metadata", "memory-only"]
Coverage = Literal["native", "polled", "derived"]
DomMode = Literal["keyed", "append", "summary", "retained-iframe", "on-demand"]
Operation = Literal[
    "upsert", "delete", "replace-summary", "invalidate", "resync-required"
]


@dataclass(frozen=True)
class ResourceSpec:
    key: str
    profile_scope: ProfileScope
    authority: str
    entity_key: str | None
    projector: str
    persistence: Persistence
    freshness_ttl: int
    coverage: Coverage
    invalidations: tuple[str, ...]
    dom_mode: DomMode


def _resource(
    key: str,
    *,
    authority: str,
    entity_key: str | None,
    projector: str,
    persistence: Persistence = "entities",
    ttl: int = 15,
    coverage: Coverage = "polled",
    scope: ProfileScope = "profile",
    invalidations: tuple[str, ...] = (),
    dom: DomMode = "keyed",
) -> ResourceSpec:
    return ResourceSpec(
        key,
        scope,
        authority,
        entity_key,
        projector,
        persistence,
        ttl,
        coverage,
        invalidations,
        dom,
    )


# Persisted projectors are deliberately resource-specific.  In particular,
# memory/file contents, chat messages, raw settings, secrets and attachment
# paths never enter the read model.
RESOURCE_SPECS: dict[str, ResourceSpec] = {
    "overview.summary": _resource(
        "overview.summary",
        authority="derived",
        entity_key=None,
        projector="overview_summary",
        persistence="snapshot",
        ttl=10,
        coverage="derived",
        dom="summary",
    ),
    "source.health": _resource(
        "source.health",
        authority="mission-control",
        entity_key="source_id",
        projector="source_health",
        persistence="entities",
        ttl=15,
        coverage="native",
        scope="global",
    ),
    "sessions": _resource(
        "sessions",
        authority="dashboard",
        entity_key="id",
        projector="session_metadata",
        ttl=30,
    ),
    "sessions.running": _resource(
        "sessions.running",
        authority="gateway",
        entity_key="session_id",
        projector="running_session_metadata",
        ttl=5,
        dom="summary",
    ),
    "fleet.topology": _resource(
        "fleet.topology",
        authority="derived",
        entity_key="id",
        projector="fleet_node",
        persistence="snapshot",
        ttl=5,
        coverage="derived",
    ),
    "kanban.boards": _resource(
        "kanban.boards",
        authority="local-data-backend",
        entity_key="id",
        projector="kanban_board_metadata",
        ttl=30,
    ),
    "kanban.tasks": _resource(
        "kanban.tasks",
        authority="local-data-backend",
        entity_key="id",
        projector="kanban_task",
        ttl=20,
        invalidations=("kanban.runs",),
    ),
    "kanban.runs": _resource(
        "kanban.runs",
        authority="local-data-backend",
        entity_key="id",
        projector="kanban_run_metadata",
        persistence="metadata",
        ttl=20,
    ),
    "permits": _resource(
        "permits",
        authority="local-data-backend",
        entity_key="permit_id",
        projector="permit",
        ttl=45,
    ),
    "issues": _resource(
        "issues",
        authority="local-data-backend",
        entity_key="id",
        projector="issue",
        ttl=45,
    ),
    "cron.jobs": _resource(
        "cron.jobs",
        authority="dashboard",
        entity_key="id",
        projector="cron_job",
        ttl=90,
    ),
    "activity.events": _resource(
        "activity.events",
        authority="event-fabric",
        entity_key="event_id",
        projector="activity_event",
        persistence="metadata",
        ttl=0,
        coverage="native",
        dom="append",
    ),
    "alerts": _resource(
        "alerts",
        authority="alert-engine",
        entity_key="id",
        projector="alert",
        ttl=5,
        coverage="derived",
    ),
    "analytics.usage": _resource(
        "analytics.usage",
        authority="dashboard",
        entity_key=None,
        projector="usage_summary",
        persistence="snapshot",
        ttl=120,
        dom="summary",
    ),
    "rooms.binding": _resource(
        "rooms.binding",
        authority="local-data-backend",
        entity_key="slot",
        projector="room_binding_metadata",
        ttl=10,
    ),
    "rooms.sessions": _resource(
        "rooms.sessions",
        authority="local-data-backend",
        entity_key="session_id",
        projector="room_session_metadata",
        ttl=10,
    ),
    "action.audit": _resource(
        "action.audit",
        authority="control-store",
        entity_key="id",
        projector="audit_metadata",
        persistence="metadata",
        ttl=5,
        coverage="native",
        scope="global",
        dom="append",
    ),
    "catalog.skills": _resource(
        "catalog.skills",
        authority="dashboard",
        entity_key="name",
        projector="skill_metadata",
        ttl=60,
    ),
    "memory.inventory": _resource(
        "memory.inventory",
        authority="local-data-backend",
        entity_key="file_key",
        projector="memory_file_metadata",
        persistence="metadata",
        ttl=30,
    ),
    "memory.detail": _resource(
        "memory.detail",
        authority="local-data-backend",
        entity_key="file_key",
        projector="none",
        persistence="memory-only",
        ttl=0,
        dom="on-demand",
    ),
    "catalog.profiles": _resource(
        "catalog.profiles",
        authority="dashboard",
        entity_key="name",
        projector="profile_metadata",
        ttl=30,
    ),
    "catalog.models": _resource(
        "catalog.models",
        authority="dashboard",
        entity_key="id",
        projector="model_metadata",
        ttl=30,
    ),
    "catalog.tools": _resource(
        "catalog.tools",
        authority="dashboard",
        entity_key="name",
        projector="tool_metadata",
        ttl=30,
    ),
    "catalog.mcp": _resource(
        "catalog.mcp",
        authority="dashboard",
        entity_key="name",
        projector="mcp_metadata",
        ttl=30,
    ),
    "catalog.plugins": _resource(
        "catalog.plugins",
        authority="dashboard",
        entity_key="name",
        projector="plugin_metadata",
        ttl=30,
    ),
    "repositories": _resource(
        "repositories",
        authority="repository-worker",
        entity_key="name",
        projector="repository_metadata",
        ttl=30,
        coverage="native",
    ),
    "config.webhooks": _resource(
        "config.webhooks",
        authority="dashboard",
        entity_key="name",
        projector="webhook_metadata",
        ttl=30,
    ),
    "config.channels": _resource(
        "config.channels",
        authority="dashboard",
        entity_key="id",
        projector="channel_metadata",
        ttl=30,
    ),
    "artifacts.metadata": _resource(
        "artifacts.metadata",
        authority="dashboard",
        entity_key="id",
        projector="artifact_metadata",
        persistence="metadata",
        ttl=15,
    ),
    "files.metadata": _resource(
        "files.metadata",
        authority="dashboard",
        entity_key="path",
        projector="file_metadata",
        persistence="metadata",
        ttl=15,
    ),
    "system-manager.inventory": _resource(
        "system-manager.inventory",
        authority="system-manager",
        entity_key="entity_key",
        projector="system_inventory_metadata",
        ttl=10,
        coverage="native",
    ),
    "logs.tail": _resource(
        "logs.tail",
        authority="dashboard",
        entity_key="cursor",
        projector="log_metadata",
        persistence="memory-only",
        ttl=2,
        dom="append",
    ),
    "command.status": _resource(
        "command.status",
        authority="dashboard",
        entity_key="id",
        projector="command_metadata",
        persistence="metadata",
        ttl=5,
    ),
    "system.settings": _resource(
        "system.settings",
        authority="dashboard",
        entity_key="section",
        projector="settings_safe_metadata",
        persistence="memory-only",
        ttl=30,
        dom="on-demand",
    ),
    "iframe.health": _resource(
        "iframe.health",
        authority="mission-control",
        entity_key="service",
        projector="iframe_health",
        persistence="entities",
        ttl=15,
        scope="global",
        dom="retained-iframe",
    ),
}


ROUTE_RESOURCES: dict[str, tuple[str, ...]] = {
    "overview": (
        "overview.summary",
        "source.health",
        "sessions.running",
        "kanban.tasks",
        "permits",
        "issues",
        "alerts",
        "analytics.usage",
        "activity.events",
    ),
    "chat": (
        "sessions",
        "sessions.running",
        "catalog.profiles",
        "catalog.models",
        "catalog.tools",
        "permits",
    ),
    "sessions": ("sessions", "sessions.running"),
    "fleet": (
        "fleet.topology",
        "sessions",
        "sessions.running",
        "kanban.tasks",
        "source.health",
    ),
    "kanban": ("kanban.boards", "kanban.tasks", "kanban.runs"),
    "cron": ("cron.jobs",),
    "activity": ("activity.events",),
    "alerts": ("alerts", "source.health"),
    "analytics": ("analytics.usage",),
    "issues": ("issues",),
    "permits": ("permits",),
    "room-binding": ("rooms.binding", "rooms.sessions"),
    "threads": ("rooms.sessions", "sessions"),
    "action-audit": ("action.audit",),
    "skills": ("catalog.skills",),
    "memory": ("memory.inventory", "memory.detail"),
    "profiles": ("catalog.profiles",),
    "models": ("catalog.models",),
    "tools": ("catalog.tools",),
    "mcp": ("catalog.mcp",),
    "plugins": ("catalog.plugins",),
    "repositories": ("repositories",),
    "webhooks": ("config.webhooks",),
    "channels": ("config.channels",),
    "artifacts": ("artifacts.metadata",),
    "files": ("files.metadata",),
    "system-manager": ("system-manager.inventory",),
    "logs": ("logs.tail",),
    "command-center": ("command.status",),
    "settings": ("system.settings",),
    "llama-proxy": ("iframe.health",),
    "9router": ("iframe.health",),
}


@dataclass(frozen=True)
class EventMapping:
    resource_key: str
    operation: Operation


EVENT_RESOURCES: dict[str, EventMapping] = {
    "task.changed": EventMapping("kanban.tasks", "upsert"),
    "run.changed": EventMapping("kanban.runs", "upsert"),
    "session.changed": EventMapping("sessions", "upsert"),
    "session.running": EventMapping("sessions.running", "replace-summary"),
    "permit.changed": EventMapping("permits", "upsert"),
    "issue.changed": EventMapping("issues", "upsert"),
    "cron.changed": EventMapping("cron.jobs", "upsert"),
    "log.appended": EventMapping("logs.tail", "upsert"),
    "alert.changed": EventMapping("alerts", "upsert"),
    "source.health": EventMapping("source.health", "upsert"),
    "cache.invalidated": EventMapping("source.health", "invalidate"),
    "repository.changed": EventMapping("repositories", "invalidate"),
    "system-manager.changed": EventMapping("system-manager.inventory", "invalidate"),
    "audit.changed": EventMapping("action.audit", "upsert"),
    "plugins.changed": EventMapping("catalog.plugins", "invalidate"),
    "profiles.changed": EventMapping("catalog.profiles", "invalidate"),
    "models.changed": EventMapping("catalog.models", "invalidate"),
    "skills.changed": EventMapping("catalog.skills", "invalidate"),
    "mcp.changed": EventMapping("catalog.mcp", "invalidate"),
    "toolsets.changed": EventMapping("catalog.tools", "invalidate"),
    "webhooks.changed": EventMapping("config.webhooks", "invalidate"),
    "channels.changed": EventMapping("config.channels", "invalidate"),
    "memory.changed": EventMapping("memory.inventory", "invalidate"),
    "files.changed": EventMapping("files.metadata", "invalidate"),
    "artifacts.changed": EventMapping("artifacts.metadata", "invalidate"),
    "rooms.changed": EventMapping("rooms.binding", "invalidate"),
    "room-sessions.changed": EventMapping("rooms.sessions", "invalidate"),
    "command.changed": EventMapping("command.status", "invalidate"),
    "iframe.changed": EventMapping("iframe.health", "invalidate"),
    "logs.changed": EventMapping("logs.tail", "invalidate"),
    "settings.changed": EventMapping("system.settings", "invalidate"),
    "gateway.changed": EventMapping("source.health", "invalidate"),
}


def canonical_event(event_type: str, entity_id: str) -> EventMapping:
    """Return the resource operation for every bus business event.

    List-level legacy events carry no entity id and therefore request a
    bounded resource resync instead of pretending a partial payload is a full
    entity.  Unknown event names fail closed so a new producer cannot silently
    bypass the live-state contract.
    """
    try:
        mapping = EVENT_RESOURCES[event_type]
    except KeyError:
        raise ValueError(
            f"event type is outside live resource contract: {event_type}"
        ) from None
    if not entity_id and mapping.operation == "upsert":
        return EventMapping(mapping.resource_key, "invalidate")
    return mapping


def validate_resource_contract() -> None:
    if len(ROUTE_RESOURCES) != 32:
        raise ValueError(f"expected 32 routes, found {len(ROUTE_RESOURCES)}")
    unknown = {
        key
        for resources in ROUTE_RESOURCES.values()
        for key in resources
        if key not in RESOURCE_SPECS
    }
    if unknown:
        raise ValueError(f"routes reference unknown resources: {sorted(unknown)}")
    for key, spec in RESOURCE_SPECS.items():
        if spec.persistence == "entities" and not spec.entity_key:
            raise ValueError(f"entity resource lacks stable key: {key}")
        if spec.persistence != "memory-only" and spec.projector == "none":
            raise ValueError(f"persisted resource lacks sanitizer: {key}")
    event_unknown = {
        mapping.resource_key
        for mapping in EVENT_RESOURCES.values()
        if mapping.resource_key not in RESOURCE_SPECS
    }
    if event_unknown:
        raise ValueError(f"events reference unknown resources: {sorted(event_unknown)}")


validate_resource_contract()
