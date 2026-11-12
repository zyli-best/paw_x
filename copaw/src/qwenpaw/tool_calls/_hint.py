# -*- coding: utf-8 -*-
"""Construct hint messages for completed background tool calls."""

from __future__ import annotations

from typing import Any


def make_offload_hint_msg(entry: Any) -> Any:
    """Construct a hint Msg for a completed offloaded tool call.

    The hint contains a system-notification TextBlock and a
    ToolResultBlock with the finalized response content.
    """
    from agentscope.message import Msg, TextBlock, ToolResultBlock

    end = entry.end_state or "unknown"
    notification = TextBlock(
        type="text",
        text=(
            "<system-notification>\n"
            f"Background tool call `{entry.ctx.tool_name}` "
            f"completed with state={end}. "
            f"tool_call_id={entry.ctx.tool_call_id}. "
            "This is not an agent task_id and cannot be used with "
            "check_agent_task, nor retrieved with "
            "get_historical_tool_result. The full result follows in "
            "the next tool_result block.\n"
            "</system-notification>"
        ),
    )
    tool_result = ToolResultBlock(
        type="tool_result",
        id=entry.ctx.tool_call_id,
        name=entry.ctx.tool_name,
        output=list(entry.final_response.content or []),
        state=entry.final_response.state,
    )
    # AgentScope 2.0 validates that system messages contain only text
    # blocks.  This hint must carry a ToolResultBlock so provider formatters
    # keep it on the tool-result path; use assistant role intentionally.
    return Msg(
        name="system",
        role="assistant",
        content=[notification, tool_result],
        metadata=dict(entry.ctx.extra.get("steer_origin") or {}),
    )


def hint_tool_result_block(hint: Any) -> Any | None:
    """Return the first tool_result block of *hint*, if any."""
    for block in getattr(hint, "content", None) or []:
        if getattr(block, "type", None) == "tool_result":
            return block
    return None


def extract_hint_tool_call_id(hint: Any) -> str | None:
    """Return the tool_call_id carried by *hint*, if any."""
    block = hint_tool_result_block(hint)
    call_id = str(getattr(block, "id", "") or "") if block else ""
    return call_id or None
