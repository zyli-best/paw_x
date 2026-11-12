"""Optional persisted transcript views, restored before agent construction.

A view replaces a contiguous context segment only in the saved snapshot. Its
first message keeps the original segment in existing Msg.metadata, so loading
restores the exact model context without teaching models about display records.
Ordinary sessions pass through unchanged. No provider or collaboration imports
belong in this module.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import logging
from typing import Callable
from uuid import uuid4

_METADATA_KEY = "qwenpaw_session_context_view"
_CONTEXT_KEY = "session_context_views"
logger = logging.getLogger(__name__)


@dataclass
class ContextView:
    canonical: list[dict]
    display: list[dict]
    after_id: str = ""


def make_context_view(
    canonical: list[dict],
    display: list[dict],
    *,
    view_id: str,
    after_id: str,
) -> ContextView:
    """Keep recovery data once per group, inside the existing metadata field."""
    display = deepcopy(display)
    for index, message in enumerate(display):
        marker = {"version": 1, "id": view_id}
        if index == 0:
            marker["canonical"] = deepcopy(canonical)
        message.setdefault("metadata", {})[_METADATA_KEY] = marker
    return ContextView(deepcopy(canonical), display, after_id)


class SessionContextViews:
    """Request-local saved views; active AgentState always remains canonical."""

    def __init__(self) -> None:
        self.history: list[ContextView] = []
        self.current: Callable[[list[dict]], ContextView | None] | None = None
        self.view_id = uuid4().hex

    def project(self, context: list[dict]) -> list[dict]:
        views = list(self.history)
        if self.current is not None:
            current = self.current(context)
            if current is not None:
                views.append(current)
        output, cursor = [], 0
        for view in views:
            width = len(view.canonical)
            start = None
            for index in range(cursor, len(context) - width + 1):
                if width:
                    matches = context[index : index + width] == view.canonical
                else:
                    # Interrupted routing may have produced only display
                    # events, before any native context was appended.
                    matches = (
                        context[index - 1].get("id", "") == view.after_id
                        if index
                        else not view.after_id
                    )
                if matches:
                    start = index
                    break
            if start is None:
                # Scroll may have removed a whole old turn. Never resurrect
                # it or replace a partially surviving native tool chain.
                continue
            output.extend(context[cursor:start])
            output.extend(deepcopy(view.display))
            cursor = start + width
        output.extend(context[cursor:])
        return output


def context_views(ctx) -> SessionContextViews:
    if getattr(ctx, "extras", None) is None:
        ctx.extras = {}
    views = ctx.extras.get(_CONTEXT_KEY)
    if views is None:
        views = ctx.extras[_CONTEXT_KEY] = SessionContextViews()
    return views


def restore_session_view(ctx, snapshot: dict) -> dict:
    """Decode only explicitly marked snapshots, independent of current flags.

    Older recordings remain available on the next save even when creating new
    recordings is disabled. Unmarked sessions retain the original object.
    """
    state = snapshot.get("state")
    context = state.get("context") if isinstance(state, dict) else None
    if not isinstance(context, list):
        return snapshot
    output, history, index = [], [], 0
    while index < len(context):
        message = context[index]
        marker = (message.get("metadata") or {}).get(_METADATA_KEY)
        if not (
            isinstance(marker, dict)
            and marker.get("version") == 1
            and isinstance(marker.get("canonical"), list)
            and marker.get("id")
        ):
            output.append(message)
            index += 1
            continue
        end = index + 1
        while end < len(context):
            following = (context[end].get("metadata") or {}).get(_METADATA_KEY)
            if not isinstance(following, dict) or (
                following.get("id") != marker["id"] or following.get("version") != 1
            ):
                break
            end += 1
        canonical = deepcopy(marker["canonical"])
        history.append(
            ContextView(
                canonical,
                deepcopy(context[index:end]),
                output[-1].get("id", "") if output else "",
            )
        )
        output.extend(canonical)
        index = end
    if not history:
        return snapshot
    if ctx is not None:
        context_views(ctx).history = history
    return {**snapshot, "state": {**state, "context": output}}


def canonical_session_state(snapshot: dict) -> dict:
    """Read native context for command, memory and usage consumers."""
    return restore_session_view(None, snapshot)


def display_message_metadata(metadata: dict) -> dict:
    """Keep recovery-only context out of public history response metadata."""
    if _METADATA_KEY not in metadata:
        return metadata
    return {key: value for key, value in metadata.items() if key != _METADATA_KEY}


def snapshot_session_view(ctx, snapshot: dict) -> dict:
    """Project a save-only copy; leave uploads and the running agent intact."""
    views = (getattr(ctx, "extras", None) or {}).get(_CONTEXT_KEY)
    steering = (getattr(ctx, "extras", None) or {}).get("normal_steering")
    if views is None and steering is None:
        return snapshot
    state = snapshot.get("state")
    context = state.get("context") if isinstance(state, dict) else None
    if not isinstance(context, list):
        return snapshot
    try:
        if views is not None:
            context = views.project(context)
        if steering is not None:
            from .normal_transcript import project_interruptions

            context = project_interruptions(steering, context)
        return {**snapshot, "state": {**state, "context": context}}
    except Exception:
        logger.warning(
            "Session transcript projection failed; saving native state", exc_info=True
        )
        return snapshot
