"""Room-binding reader: whitelist-extracts room keys from config.yaml.

Returns platforms.telegram.extra.room_chat_id, .room_slots, and a whitelisted
projection of .group_topics[].topics[] for the group matching room_chat_id.
No secret/API key from config.yaml is ever read into the response.
"""

from __future__ import annotations

from typing import Any

import yaml

from .config import ROOM_BINDING_KEYS, ROOM_SLOT_KEYS, ROOM_TOPIC_KEYS


def _read_group_topics(
    extra: dict[str, Any], room_chat_id: Any
) -> list[dict[str, Any]]:
    """Whitelist-extract topics for the group_topics entry matching room_chat_id.

    Only ROOM_TOPIC_KEYS fields are copied per topic; unmatched groups (any
    chat_id other than the room chat) are ignored entirely.
    """
    groups = extra.get("group_topics")
    if not isinstance(groups, list):
        return []

    topics: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        if room_chat_id is not None and str(group.get("chat_id")) != str(room_chat_id):
            continue
        raw_topics = group.get("topics")
        if not isinstance(raw_topics, list):
            continue
        for item in raw_topics:
            if not isinstance(item, dict):
                continue
            topic: dict[str, Any] = {}
            for key in ROOM_TOPIC_KEYS:
                if key in item:
                    topic[key] = item[key]
            if topic:
                topics.append(topic)
    return topics


def read_room_binding(config_path: str) -> dict[str, Any] | None:
    """Return the room binding payload, or None when config is degraded.

    A degraded config (missing file, YAML parse error, missing telegram.extra
    section) yields None so the endpoint can report the source as degraded
    instead of leaking any partial configuration.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        if not isinstance(cfg, dict):
            return None
        # Room binding lives under the top-level telegram section
        # (platforms.telegram mirrors the gateway section and has no extra).
        telegram = cfg.get("telegram", {})
        if not isinstance(telegram, dict):
            return None
        extra = telegram.get("extra", {})
        if not isinstance(extra, dict):
            return None
    except (OSError, yaml.YAMLError):
        return None

    out: dict[str, Any] = {}
    room_chat_id = extra.get("room_chat_id")
    if room_chat_id is not None:
        out["room_chat_id"] = room_chat_id

    slots_raw = extra.get("room_slots")
    if isinstance(slots_raw, list):
        slots = []
        for item in slots_raw:
            if not isinstance(item, dict):
                continue
            slot: dict[str, Any] = {}
            for key in ROOM_SLOT_KEYS:
                if key in item:
                    slot[key] = item[key]
            if slot:
                slots.append(slot)
        out["room_slots"] = slots

    if not out:
        return None

    out["topics"] = _read_group_topics(extra, room_chat_id)

    return out
