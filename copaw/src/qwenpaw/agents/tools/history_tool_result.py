# -*- coding: utf-8 -*-
"""Read one historical tool result from the current session."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...runtime.tool_registry import tool_descriptor
from ...constant import WORKING_DIR

_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|]')


def _text_response(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])


def _sanitize_filename(name: str) -> str:
    return _UNSAFE_FILENAME_RE.sub("--", name)


def _session_save_path(
    *,
    workspace: Path,
    session_id: str,
    user_id: str,
    channel: str,
) -> Path:
    safe_sid = _sanitize_filename(session_id)
    safe_uid = _sanitize_filename(user_id) if user_id else ""
    filename = f"{safe_uid}_{safe_sid}.json" if safe_uid else f"{safe_sid}.json"
    if channel:
        return workspace / "sessions" / _sanitize_filename(channel) / filename
    return workspace / "sessions" / filename


def _stringify_output(value: Any) -> str:
    """Convert a tool_result output payload to compact readable text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                if item.get("type") == "text" and "text" in item:
                    parts.append(str(item["text"]))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def _truncate(text: str, max_chars: int) -> str:
    limit = max(1000, min(int(max_chars or 12000), 60000))
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n\n[truncated: returned first {limit} characters only]"
    )


def _iter_session_messages(state: dict[str, Any]):
    """Yield message dictionaries from old and v2 session layouts."""
    agent_state = state.get("agent", {})
    candidates = [
        agent_state.get("state", {}).get("context", []),
        agent_state.get("memory", {}).get("content", []),
    ]

    for content in candidates:
        if not isinstance(content, list):
            continue
        for item in content:
            msg_dict = item
            if isinstance(item, (list, tuple)) and item:
                msg_dict = item[0]
            if isinstance(msg_dict, dict):
                yield msg_dict


@tool_descriptor(
    async_execution=True,
    description="Return one historical tool result from the current session.",
)
async def get_historical_tool_result(  # pylint: disable=R0911,R0912
    tool_call_id: str,
    max_chars: int = 12000,
) -> ToolResponse:
    """Return one compacted historical tool result from the current session (not for background/offloaded tool calls).

    Retrieve the exact output of a single **past** tool call that has been
    compacted out of the live context.  When the session context is
    summarised, earlier tool calls are collapsed into a
    ``<historical-tool-calls>`` list that keeps only their ``tool_call_id``;
    this tool reads that one call's full result back from the current
    session's persisted history file, and returns only the requested result
    (never the whole history).

    Use it only for a ``tool_call_id`` listed in ``<historical-tool-calls>``
    — an earlier turn in the SAME session whose output is no longer shown
    inline.  Do NOT use it for:
    - a tool call made in the current task/turn — its inputs and outputs
      are already visible in the live context, so this tool adds nothing
      there;
    - a background (offloaded) tool call — while it runs, or after it
      finishes but before its result is delivered, use
      ``get_background_tool_result`` or wait for the automatic delivery;
      once delivered and later compacted away, this tool is the correct
      way to read it back.

    It is bound to the current ``session_id`` and cannot read other
    sessions' history.

    The stored output is rendered as readable text (text blocks joined;
    dict/list payloads serialised to JSON) and truncated to ``max_chars``.
    If the id is empty, the session history file is missing or unreadable,
    or no matching ``tool_result`` exists, an error message is returned
    instead of raising.

    Args:
        tool_call_id: The id shown in ``<historical-tool-calls>`` — a past
            call from the current session, not one just made in the current
            task (current-task calls are already visible inline) and not a
            background (offloaded) call (use ``get_background_tool_result``
            for those).
        max_chars: Maximum characters to return.  Defaults to 12000 and is
            clamped to the range 1000–60000.
    """
    call_id = str(tool_call_id or "").strip()
    if not call_id:
        return _text_response("Error: tool_call_id is required.")

    try:
        from ...app.agent_context import (
            get_current_channel,
            get_current_session_id,
            get_current_user_id,
        )
    except ImportError:
        get_current_channel = lambda: None
        get_current_session_id = lambda: None
        get_current_user_id = lambda: None

    from ...config.context import (
        get_current_session_id as get_config_session_id,
        get_current_workspace_dir,
    )

    session_id = get_current_session_id() or get_config_session_id() or ""
    user_id = get_current_user_id() or ""
    channel = get_current_channel() or ""
    if not session_id:
        return _text_response(
            "Error: current session_id is unavailable; cannot read history.",
        )

    # Runtime guard: steer background (offloaded) calls to the correct
    # channels instead of reporting a misleading "no history" error.
    from ...tool_calls import get_default_coordinator

    mgr = get_default_coordinator()
    if mgr is not None:
        entry = mgr.get(call_id)
        if entry is not None:
            status = getattr(entry.status, "value", str(entry.status))
            print(
                f"[bg-flow] HIST-GUARD running call "
                f"tool_call_id={call_id} tool={entry.ctx.tool_name} "
                f"status={status}",
                flush=True,
            )
            return _text_response(
                f"Background tool call `{entry.ctx.tool_name}` "
                f"(tool_call_id={call_id}) is still running "
                f"(status={status}); it has no historical record yet. "
                "Its result will be added to your context automatically "
                "when it finishes, or you can pick it up early with "
                "get_background_tool_result(tool_call_id=...). This tool "
                "only serves compacted historical results.",
            )
        if call_id in await mgr.pending_hint_tool_call_ids(session_id):
            print(
                f"[bg-flow] HIST-GUARD queued call "
                f"tool_call_id={call_id} -> auto-delivery pending",
                flush=True,
            )
            return _text_response(
                f"Background tool call (tool_call_id={call_id}) has "
                "completed; its result is queued for automatic delivery "
                "and will appear in your context shortly. You can also "
                "pick it up now with "
                "get_background_tool_result(tool_call_id=...). This tool "
                "only serves compacted historical results.",
            )

    workspace = Path(get_current_workspace_dir() or WORKING_DIR)
    session_path = _session_save_path(
        workspace=workspace,
        session_id=session_id,
        user_id=user_id,
        channel=channel,
    )
    if not session_path.exists():
        return _text_response(
            "Error: session history file does not exist for current session.",
        )

    try:
        state = json.loads(session_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _text_response(f"Error: session history JSON is invalid: {exc}")
    except OSError as exc:
        return _text_response(f"Error: failed to read session history: {exc}")

    for msg_dict in _iter_session_messages(state):
        blocks = msg_dict.get("content", [])
        if isinstance(blocks, dict):
            blocks = [blocks]
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and str(block.get("id") or "") == call_id
            ):
                output = _stringify_output(block.get("output", ""))
                return _text_response(_truncate(output, max_chars))

    return _text_response(
        f"Error: no historical tool_result found for "
        f"tool_call_id={call_id!r}. If this id belongs to a background "
        "(offloaded) call from the current turn, wait for automatic "
        "delivery or use get_background_tool_result instead -- active "
        "calls are not written to the session history file.",
    )
