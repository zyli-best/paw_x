# -*- coding: utf-8 -*-
"""Cloud decisions with bounded, read-only Skill inspection.

Both stages use ``tool_choice=auto``: thinking-mode providers (DeepSeek V4,
Qwen3 and compatible endpoints) reject ``required`` and named-function
choices. A model that answers in text instead of submitting falls through
the existing error path and is handed to CloudAgent.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import AsyncIterable
from typing import Any

from agentscope.message import Msg, ToolCallBlock, ToolResultBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionEngine
from agentscope.tool import ToolChoice, ToolChunk, ToolResponse

from .schemas import RouterDecision, VerifierDecision
from .roles import AGENT_NAMES

logger = logging.getLogger(__name__)

_DECISIONS = {
    "router": ("submit_route_decision", RouterDecision),
    "verifier": ("submit_verification", VerifierDecision),
}

_ALLOWED_READ_TOOLS = {
    "router": frozenset({"Skill", "list_skills", "get_skill_count", "recall_history"}),
    "verifier": frozenset({"Skill", "recall_history"}),
}

_READ_BUDGETS = {"router": 12, "verifier": 2}


def _function_schema(value: Any) -> Any:
    """Use enum as shell.py does; preserve existing required/default fields."""
    if isinstance(value, list):
        return [_function_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _function_schema(item) for key, item in value.items()}
    if "const" in result:
        result["enum"] = [result.pop("const")]
    return result


def decision_call_kwargs(stage: str, read_tools=()) -> dict[str, Any]:
    name, schema = _DECISIONS[stage]
    parameters = _function_schema(schema.model_json_schema())
    return {
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "Submit the decision only; do not execute any action.",
                    "parameters": parameters,
                },
            }
        ]
        + [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in read_tools
        ],
        "tool_choice": ToolChoice(mode="auto"),
    }


async def _consume_model_response_object(
    model: Any, messages: list, *, response_observer_factory=None, **kwargs: Any
) -> Any:
    """Await a model call and keep the completed streamed chunk, if any."""
    observe = None
    if response_observer_factory is not None:
        try:
            observe = response_observer_factory()
        except Exception:
            logger.debug("Client decision observer unavailable")

    async def notify(value=None, *, complete=False):
        nonlocal observe
        if observe is not None:
            try:
                if complete:
                    finish = getattr(observe, "complete", None)
                    if finish is not None:
                        await finish()
                else:
                    await observe(value)
            except Exception:
                # Display failures must not become router/verifier failures.
                # CancelledError deliberately propagates to the original path.
                observe = None
                logger.debug("Client decision observer disabled")

    response = await model(messages, **kwargs)
    if not isinstance(response, AsyncIterable):
        if observe is not None:
            await notify(response)
            await notify(complete=True)
        return response
    completed = None
    last = None
    try:
        async for chunk in response:
            last = chunk
            if _safe_get(chunk, "is_last") is True:
                completed = chunk
            if observe is not None:
                await notify(chunk)
    finally:
        # Cancellation can arrive while the display queue is full and the
        # provider generator is suspended at yield, rather than inside an await.
        close = getattr(response, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:
                logger.debug("Could not close decision model stream", exc_info=True)
    await notify(complete=True)
    return completed or last


def _safe_get(obj: Any, name: str) -> Any:
    """Read a field from dict-like or object-like values."""
    if isinstance(obj, dict):
        return obj.get(name)
    try:
        return getattr(obj, name, None)
    except (AttributeError, KeyError, TypeError):
        return None


def _candidate_tool_call_blocks(value: Any) -> list[Any]:
    """Collect possible tool-call blocks from a response-like object."""
    candidates: list[Any] = []
    if value is None:
        return candidates
    if isinstance(value, list):
        candidates.extend(value)
        return candidates
    if isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, list):
            candidates.extend(content)
        tool_calls = value.get("tool_calls")
        if isinstance(tool_calls, list):
            candidates.extend(tool_calls)
        message = value.get("message")
        candidates.extend(_candidate_tool_call_blocks(message))
        choices = value.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                candidates.extend(_candidate_tool_call_blocks(choice))
        return candidates

    content = _safe_get(value, "content")
    if isinstance(content, list):
        candidates.extend(content)
    tool_calls = _safe_get(value, "tool_calls")
    if isinstance(tool_calls, list):
        candidates.extend(tool_calls)
    message = _safe_get(value, "message")
    candidates.extend(_candidate_tool_call_blocks(message))
    return candidates


def _normalize_tool_call(item: Any) -> tuple[str | None, Any]:
    """Return (name, arguments) from common provider tool-call shapes."""
    function = _safe_get(item, "function")
    if function is not None:
        name = _safe_get(function, "name")
        arguments = _safe_get(function, "arguments")
        return name, arguments

    name = _safe_get(item, "name")
    arguments = _safe_get(item, "arguments")
    if arguments is None:
        arguments = _safe_get(item, "input")
    if arguments is None:
        arguments = _safe_get(item, "args")
    return name, arguments


async def consume_decision_call(
    model: Any,
    messages: list,
    *,
    stage: str,
    read_tools: dict | None = None,
    state: Any = None,
    prepare_messages=None,
    response_observer_factory=None,
    tool_observer=None,
    prepare_request=None,
    on_request_complete=None,
    validate_decision=None,
    output_response_id=None,
) -> dict[str, Any]:
    from ..output_attribution import OutputFrame, capture_model_input, output_scope

    frame = OutputFrame(output_response_id)

    async def notify_tool(kind, block):
        nonlocal tool_observer
        if tool_observer is not None:
            try:
                with output_scope(frame):
                    await tool_observer(kind, block)
            except Exception:
                tool_observer = None
                logger.debug("Client decision tool observer disabled")

    read_tools = read_tools or {}
    allowed = _ALLOWED_READ_TOOLS.get(stage, frozenset())
    if set(read_tools) - allowed:
        raise ValueError(
            f"{stage} read tools are restricted to {sorted(allowed)}"
        )
    call_kwargs = decision_call_kwargs(stage, read_tools.values())
    messages = list(messages)
    name, _ = _DECISIONS[stage]
    # Bound inspection work within one decision; exhaustion uses the existing
    # error/cloud fallback policy rather than starting business work.
    remaining_reads = _READ_BUDGETS.get(stage, 0)
    remaining_history_reads = 32
    while True:
        if prepare_messages is not None:
            messages = await prepare_messages(messages)
        if prepare_request is not None:
            messages = await prepare_request(messages, call_kwargs["tools"])
        frame = OutputFrame(output_response_id)
        with output_scope(frame):
            capture_model_input(messages)
            response = await _consume_model_response_object(
                model,
                list(messages),
                response_observer_factory=response_observer_factory,
                **call_kwargs,
            )
        if "interrupted" in str(_safe_get(response, "finished_reason")).lower():
            raise RuntimeError(
                f"Edge-cloud {stage} function-call response was interrupted"
            )
        if on_request_complete is not None:
            on_request_complete(messages)
        calls = []
        for item in _candidate_tool_call_blocks(response):
            called_name, arguments = _normalize_tool_call(item)
            if called_name is None:
                continue
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Decision tools require object arguments")
            calls.append((item, called_name, arguments))
        if len(calls) == 1 and calls[0][1] == name:
            block = {
                "id": _safe_get(calls[0][0], "id") or "decision-submit",
                "name": name, "input": calls[0][2],
            }
            await notify_tool("call", block)
            try:
                if validate_decision is not None:
                    validate_decision(calls[0][2])
            except Exception as exc:
                await notify_tool("result", {
                    **block, "output": str(exc), "state": ToolResultState.ERROR,
                })
                raise
            await notify_tool("result", {
                **block, "output": "决策已提交。", "state": ToolResultState.SUCCESS,
            })
            return calls[0][2]
        if not calls or any(called not in read_tools for _, called, _ in calls):
            raise ValueError(f"Expected Skill inspection or a single {name} call")
        history_calls = (
            sum(called == "recall_history" for _, called, _ in calls)
            if stage == "verifier" else 0
        )
        inspection_calls = len(calls) - history_calls
        if inspection_calls > remaining_reads:
            raise ValueError("Router Skill inspection budget exhausted")
        if history_calls > remaining_history_reads:
            raise ValueError("Verifier history recall budget exhausted")
        remaining_reads -= inspection_calls
        remaining_history_reads -= history_calls
        blocks = [
            ToolCallBlock(
                id=_safe_get(item, "id") or f"route-read-{remaining_reads}-{i}",
                name=called,
                input=json.dumps(arguments),
            )
            for i, (item, called, arguments) in enumerate(calls)
        ]
        messages.append(Msg(name=AGENT_NAMES[stage], role="assistant", content=blocks))
        results = []
        for block, (_, called, arguments) in zip(blocks, calls):
            await notify_tool("call", block)
            tool = read_tools[called]
            try:
                permission = await PermissionEngine(
                    state.permission_context
                ).check_permission(tool, arguments)
                if permission.behavior != PermissionBehavior.ALLOW:
                    raise PermissionError(permission.message)
                inputs = dict(arguments)
                if tool.is_state_injected:
                    inputs["_agent_state"] = state
                result = tool(**inputs)
                if inspect.isawaitable(result):
                    result = await result
                if isinstance(result, AsyncIterable):
                    collected = ToolResponse()
                    async for chunk in result:
                        collected.append_chunk(chunk)
                    result = collected
                elif isinstance(result, ToolChunk):
                    result = ToolResponse().append_chunk(result)
                results.append(
                    ToolResultBlock(
                        id=block.id,
                        name=called,
                        output=result.content,
                        state=result.state,
                    )
                )
            except Exception as exc:
                results.append(
                    ToolResultBlock(
                        id=block.id,
                        name=called,
                        output=str(exc),
                        state=ToolResultState.ERROR,
                    )
                )
            await notify_tool("result", results[-1])
        messages.append(Msg(name=f"{stage}_tools", role="assistant", content=results))
