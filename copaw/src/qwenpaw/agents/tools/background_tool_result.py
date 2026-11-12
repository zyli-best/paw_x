# -*- coding: utf-8 -*-
"""Check / pick up a background (offloaded) tool call result."""

from __future__ import annotations

from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...runtime.tool_registry import tool_descriptor


def _text_response(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])


@tool_descriptor(
    async_execution=True,
    description=(
        "Check a background (offloaded) tool call from the current session. "
        "Returns the live status while it is still running; if it has "
        "finished and the result is still waiting for automatic delivery, "
        "returns the result and removes it from the auto-delivery queue "
        "(the result will not be delivered twice)."
    ),
)
async def get_background_tool_result(tool_call_id: str) -> ToolResponse:
    """Check or pick up the result of a background (offloaded) tool call.

    Background tool calls are produced when a tool call exceeds its
    timeout and is moved to the background runner. Their results are
    delivered automatically (injected as a system-notification plus a
    tool_result block) at the start of a later turn. Calling this tool
    picks the result up early.

    Three outcomes:
    - still running: returns the live status; keep working on other
      things instead of calling this tool repeatedly.
    - finished and still queued: returns the full result and removes it
      from the auto-delivery queue (it will NOT be delivered again).
    - not found: the result was already delivered/picked up, or the id
      never belonged to a background call in this session.

    Whichever path (automatic delivery or this query) consumes the
    result first wins; the other is dropped automatically.

    Args:
        tool_call_id: the tool_call_id shown in the offload notification.
    """
    call_id = str(tool_call_id or "").strip()
    if not call_id:
        return _text_response("Error: tool_call_id is required.")

    # session_id：镜像 get_historical_tool_result 的双通道取法
    try:
        from ...app.agent_context import get_current_session_id
    except ImportError:
        get_current_session_id = lambda: None
    from ...config.context import (
        get_current_session_id as get_config_session_id,
    )

    session_id = get_current_session_id() or get_config_session_id() or ""

    from ...tool_calls import get_default_coordinator

    mgr = get_default_coordinator()
    if mgr is None:
        return _text_response(
            "Error: tool coordinator is unavailable; cannot check "
            "background tool calls.",
        )

    print(
        f"[bg-flow] QUERY get_background_tool_result "
        f"tool_call_id={call_id} session={session_id or '<empty>'}",
        flush=True,
    )

    # 1) 仍在运行
    entry = mgr.get(call_id)
    if entry is not None:
        status = getattr(entry.status, "value", str(entry.status))
        print(
            f"[bg-flow] QUERY -> RUNNING tool={entry.ctx.tool_name} "
            f"status={status}",
            flush=True,
        )
        return _text_response(
            f"Background tool call `{entry.ctx.tool_name}` "
            f"(tool_call_id={call_id}) is still running "
            f"(status={status}). Keep working on other things; the "
            "result will be delivered automatically when it finishes. "
            "Avoid calling this tool repeatedly.",
        )

    # 2) 已完成且仍待投递：取件（先到先得）
    hint = (
        await mgr.consume_pending_hint(session_id, call_id)
        if session_id
        else None
    )
    if hint is not None:
        print(
            "[bg-flow] QUERY -> HIT result returned to caller; "
            "auto-delivery skipped",
            flush=True,
        )
        from ...tool_calls._hint import hint_tool_result_block

        block = hint_tool_result_block(hint)
        content: list[Any] = [
            TextBlock(
                type="text",
                text=(
                    f"Result of background tool call "
                    f"(tool_call_id={call_id}) -- picked up via "
                    "query, so it will not be delivered again "
                    "automatically.\n"
                ),
            ),
        ]
        for item in (getattr(block, "output", None) or []):
            content.append(
                item if not isinstance(item, str)
                else TextBlock(type="text", text=item),
            )
        return ToolResponse(content=content)

    # 3) 未找到
    print(
        f"[bg-flow] QUERY -> MISS tool_call_id={call_id} "
        "not running and not queued",
        flush=True,
    )
    return _text_response(
        f"No pending background result found for "
        f"tool_call_id={call_id!r}. It may have already been delivered "
        "(check the latest system-notification in the context), already "
        "been picked up, or never been a background call in this "
        "session. If it was delivered earlier and later compacted away, "
        "use get_historical_tool_result instead.",
    )
