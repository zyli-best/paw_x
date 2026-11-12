"""Explicit request-local hint types, shared by delivery and budget handling."""

from __future__ import annotations

from typing import Any

from ...config.context import get_current_session_id
from ...constant import (
    FAILURE_RECOVERY_MESSAGE_TAG,
    PROGRESS_REMINDER_MESSAGE_TAG,
    QWENPAW_MESSAGE_TAG_KEY,
)

# Lower priority is discarded first. Never identify a hint from its text.
_HINTS = {
    PROGRESS_REMINDER_MESSAGE_TAG: ("progress_reminder", "progress_reminder"),
    FAILURE_RECOVERY_MESSAGE_TAG: ("failure_loop", "failure_recovery"),
}
HINT_OMISSION_ORDER = tuple(_HINTS)


def hint_type(msg: Any, agent: Any = None) -> str | None:
    metadata = getattr(msg, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    tag = metadata.get(QWENPAW_MESSAGE_TAG_KEY)
    if not isinstance(tag, str) or tag not in _HINTS:
        return None
    source, name = _HINTS[tag]
    if (
        metadata.get("source"),
        getattr(msg, "name", None),
        getattr(msg, "role", None),
    ) != (source, name, "user"):
        return None
    if agent is not None:
        if metadata.get("reply_id") != getattr(agent.state, "reply_id", None):
            return None
        if "iteration" in metadata and metadata["iteration"] != getattr(
            agent.state, "cur_iter", None
        ):
            return None
        if "session_id" in metadata and metadata["session_id"] != (
            get_current_session_id() or "default"
        ):
            return None
    return tag


def is_optional_hint(msg: Any, agent: Any = None) -> bool:
    return hint_type(msg, agent) is not None


def is_progress_hint(msg: Any) -> bool:
    return hint_type(msg) == PROGRESS_REMINDER_MESSAGE_TAG
