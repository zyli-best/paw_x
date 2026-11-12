# -*- coding: utf-8 -*-
"""Agent execution driver.

Drives ``agent.reply_stream(inputs=msgs)`` with heartbeat wrapping
and delegates each ``EventType`` event to ``Envelope.translate_event()``.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

from .envelope import Envelope
from .heartbeat import (
    _iter_with_heartbeat,
    _HEARTBEAT_TICK,
    HEARTBEAT_INTERVAL_SECONDS,
)

logger = logging.getLogger(__name__)


import json

from ..constant import (
    COMPACT_HISTORICAL_TOOL_CONTEXT,
    LOOP_CONTINUATION_MESSAGE_TAG,
    QWENPAW_MESSAGE_TAG_KEY,
)

from agentscope.message import Msg, TextBlock

_TASK_KIND_KEY = "qwenpaw_task_kind"
_TASK_ORIGINAL_INPUT_KEY = "qwenpaw_original_input"
_LOOP_RESULT_KEY = "qwenpaw_loop_result"


class AgentExecutor:
    """Execute the agent's reply stream and translate
    events into SSE envelopes.

    One instance per ``Runtime.run()`` invocation.  The executor owns the
    heartbeat wrapper but not the agent itself (that belongs to the
    ``HookContext``).
    """

    def __init__(
        self, agent: Any, envelope: Envelope, *, redaction_scope_factory=None,
    ) -> None:
        self._agent = agent
        self._envelope = envelope
        self._redaction_scope_factory = redaction_scope_factory

    def _mcp_tool_identity(self, event: Any) -> dict[str, str] | None:
        """Read local bindings only; never discover tools or affect execution."""
        if getattr(event, "type", None) not in (
            "TOOL_CALL_START", "TOOL_RESULT_START",
        ):
            return None
        try:
            toolkit = getattr(self._agent, "toolkit", None)
            state = getattr(self._agent, "state", None)
            context = getattr(state, "tool_context", None)
            groups = {
                "basic", *(getattr(context, "activated_groups", None) or []),
            }
            selected = None
            # Match Toolkit's group order and last-wins resolution, without
            # calling its async resolver (which may perform external I/O).
            for group in getattr(toolkit, "tool_groups", ()) or ():
                if getattr(group, "name", None) not in groups:
                    continue
                for tool in getattr(group, "tools", ()) or ():
                    if getattr(tool, "name", None) == event.tool_call_name:
                        selected = tool
                if getattr(group, "mcps", None):
                    # Dynamic clients resolve after local tools and may
                    # override them. Do not guess or query those clients.
                    selected = None
            identity = {}
            for key in ("mcp_client_key", "mcp_tool_name"):
                value = getattr(selected, key, None)
                if isinstance(value, str) and value:
                    identity[key] = value
            capability = getattr(selected, "_capability", None)
            if (
                getattr(capability, "protocol", None) == "mcp"
                and len(identity) != 2
                and event.type == "TOOL_CALL_START"
            ):
                logger.warning(
                    "MCP attribution incomplete: call_id=%s tool=%s",
                    event.tool_call_id, event.tool_call_name,
                )
            return identity or None
        except Exception:
            # Optional third-party metadata must not break or delay a call.
            logger.warning(
                "MCP attribution unavailable: call_id=%s",
                getattr(event, "tool_call_id", ""),
                exc_info=True,
            )
            return None

    @staticmethod
    def _message_has_content_block(msg, block_type: str) -> bool:
        """Return whether *msg* contains a given AgentScope block type."""
        try:
            return bool(msg.get_content_blocks(block_type))
        except Exception:
            pass
        content = getattr(msg, "content", None)
        if isinstance(content, dict):
            content = [content]
        if isinstance(content, list):
            return any(
                self_block_type == block_type
                for self_block_type in (
                    AgentExecutor._block_attr(block, "type")
                    for block in content
                )
                if self_block_type is not None
            )
        return False

    @staticmethod
    def _block_attr(block, key: str, default=None):
        """Read a content block field from dict or object blocks."""
        if isinstance(block, dict):
            return block.get(key, default)
        return getattr(block, key, default)

    @staticmethod
    def _message_content_blocks(msg, block_types: tuple[str, ...]) -> list:
        """Return matching content blocks from AgentScope or raw content."""
        blocks = []
        for block_type in block_types:
            try:
                blocks.extend(msg.get_content_blocks(block_type))
            except Exception:
                pass

        if blocks:
            return blocks

        content = getattr(msg, "content", None)
        if isinstance(content, dict):
            content = [content]
        if isinstance(content, list):
            return [
                block
                for block in content
                if AgentExecutor._block_attr(block, "type") in block_types
            ]
        return []

    @staticmethod
    def _message_text(msg) -> str:
        """Return plain text content best-effort."""
        try:
            return msg.get_text_content() or ""
        except Exception:
            pass
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
            return "\n".join(part for part in parts if part)
        return ""

    @staticmethod
    def _msg_metadata(msg) -> dict:
        metadata = getattr(msg, "metadata", None)
        return metadata if isinstance(metadata, dict) else {}

    @classmethod
    def _is_loop_continuation(cls, msg) -> bool:
        """Return whether a user message is an internal loop continuation."""
        return (
            cls._msg_metadata(msg).get(QWENPAW_MESSAGE_TAG_KEY)
            == LOOP_CONTINUATION_MESSAGE_TAG
        )

    @classmethod
    def _task_kind_for_user_msg(cls, msg) -> str:
        metadata = cls._msg_metadata(msg)
        kind = str(metadata.get(_TASK_KIND_KEY) or "").strip().lower()
        if kind:
            return kind

        text = cls._message_text(msg).lstrip().lower()
        if text.startswith("/goal"):
            return "goal"
        if text.startswith("/mission"):
            return "mission"
        return "normal"

    @classmethod
    def _task_input_text(cls, msg) -> str:
        metadata = cls._msg_metadata(msg)
        original = metadata.get(_TASK_ORIGINAL_INPUT_KEY)
        if isinstance(original, str) and original.strip():
            return original.strip()
        return cls._message_text(msg).strip()

    @staticmethod
    def _clone_msg_without_reasoning(msg):
        """Clone a message for model context without reasoning/thinking data."""
        try:
            msg_dict = msg.to_dict()
        except Exception:
            return msg

        content = msg_dict.get("content")
        if isinstance(content, list):
            clean_content = [
                block
                for block in content
                if isinstance(block, dict)
                and block.get("type") not in {"thinking", "reasoning"}
            ]
            if not clean_content:
                return None
            msg_dict["content"] = clean_content

        msg_dict.pop("reasoning_content", None)
        metadata = msg_dict.get("metadata")
        if isinstance(metadata, dict):
            metadata.pop("reasoning_content", None)
            metadata.pop("reasoning", None)
            metadata.pop("thinking", None)

        try:
            return Msg.from_dict(msg_dict)
        except Exception:
            return msg

    @staticmethod
    def _compact_tool_input_for_context(tool_input) -> str:
        """Serialize a tool input example with a small token footprint."""
        try:
            text = json.dumps(tool_input, ensure_ascii=False)
        except TypeError:
            text = str(tool_input)
        text = " ".join(text.split())
        max_chars = 160
        if len(text) > max_chars:
            return text[: max_chars - 3] + "..."
        return text

    @classmethod
    def _historical_tool_target(cls, name: str, tool_input) -> str:
        """Extract a short reuse target for historical tool calls."""
        if isinstance(tool_input, dict):
            for field in (
                "path",
                "file_path",
                "url",
                "query",
                "pattern",
                "command",
                "cmd",
                "action",
            ):
                value = tool_input.get(field)
                if value:
                    target = str(value)
                    break
            else:
                target = cls._compact_tool_input_for_context(tool_input)
        else:
            target = cls._compact_tool_input_for_context(tool_input)

        target = " ".join(target.split())
        max_chars = 120
        if len(target) > max_chars:
            target = target[: max_chars - 3] + "..."
        return target or name

    @classmethod
    def _tool_input_from_block(cls, block):
        return (
            cls._block_attr(block, "input", None)
            or cls._block_attr(block, "arguments", None)
            or cls._block_attr(block, "args", None)
            or cls._block_attr(block, "raw_input", "")
        )

    @staticmethod
    def _parse_tool_input(tool_input):
        if isinstance(tool_input, str):
            try:
                return json.loads(tool_input)
            except json.JSONDecodeError:
                return tool_input
        return tool_input

    @classmethod
    def _is_goal_completion_tool_call(cls, block) -> bool:
        btype = cls._block_attr(block, "type")
        if btype not in ("tool_use", "tool_call"):
            return False
        if cls._block_attr(block, "name") != "update_goal":
            return False

        parsed_input = cls._parse_tool_input(cls._tool_input_from_block(block))
        if not isinstance(parsed_input, dict):
            return False
        status = str(parsed_input.get("status") or "").strip().lower()
        return status in {"complete", "blocked"}

    @classmethod
    def _visible_text_from_msg(
        cls,
        msg,
        *,
        stop_at_goal_completion: bool = False,
    ) -> tuple[str, bool]:
        """Extract user-visible text from a message, omitting tool internals."""
        content = getattr(msg, "content", None)
        if isinstance(content, dict):
            content = [content]
        if not isinstance(content, list):
            text = cls._message_text(msg).strip()
            return text, False

        parts = []
        goal_completed = False
        for block in content:
            if stop_at_goal_completion and cls._is_goal_completion_tool_call(block):
                goal_completed = True
                break
            btype = cls._block_attr(block, "type")
            if btype == "text":
                text = str(cls._block_attr(block, "text", "") or "").strip()
                if text:
                    parts.append(text)

        return "\n".join(parts).strip(), goal_completed

    @staticmethod
    def _text_msg(name: str, role: str, text: str) -> Msg:
        return Msg(
            name=name,
            role=role,
            content=[TextBlock(type="text", text=text)],
        )

    def _build_historical_tool_calls_summary(self, context: list):
        """Build a compact text summary of historical tool calls."""
        records = []
        seen_keys = set()
        max_records = 20

        for msg in reversed(context):
            tool_calls = self._message_content_blocks(
                msg,
                ("tool_use", "tool_call"),
            )

            for block in reversed(tool_calls):
                name = str(self._block_attr(block, "name") or "unknown")
                call_id = str(self._block_attr(block, "id") or "")
                if not call_id:
                    continue
                tool_input = self._tool_input_from_block(block)
                target = self._historical_tool_target(
                    name,
                    tool_input,
                )
                key = (name, target)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                records.append(
                    {
                        "id": call_id,
                        "tool": name,
                        "target": target,
                    },
                )
                if len(records) >= max_records:
                    break
            if len(records) >= max_records:
                break

        if not records:
            return None

        records.reverse()
        lines = [
            "<historical-tool-calls>",
            (
                "Historical tool outputs were omitted. If an exact historical "
                "tool result is needed, call "
                "get_historical_tool_result(tool_call_id=TOOL_CALL_ID); "
                "avoid repeating listed historical tool calls in normal cases."
            ),
            "id | tool | target",
        ]
        for record in records:
            lines.append(
                f"{record['id']} | {record['tool']} | {record['target']}",
            )
        lines.append("</historical-tool-calls>")

        return self._text_msg("user", "user", "\n".join(lines))

    def _build_compact_task_messages(self, context: list) -> list[Msg]:
        """Build historical user/assistant task pairs without tool payloads."""
        compact: list[Msg] = []
        current: dict[str, Any] | None = None

        def flush_current() -> None:
            nonlocal current
            if current is None:
                return
            user_text = str(current.get("user_text") or "").strip()
            assistant_parts = [
                part.strip()
                for part in current.get("assistant_parts", [])
                if str(part).strip()
            ]
            if user_text:
                compact.append(self._text_msg("user", "user", user_text))
            if assistant_parts:
                compact.append(
                    self._text_msg(
                        str(current.get("assistant_name") or "default"),
                        "assistant",
                        "\n\n".join(assistant_parts),
                    ),
                )
            current = None

        for msg in context:
            role = str(getattr(msg, "role", ""))
            text = self._message_text(msg).strip()
            if text.startswith("<system-hint>"):
                continue

            if role == "system":
                clean_text, _ = self._visible_text_from_msg(msg)
                if clean_text:
                    compact.append(
                        self._text_msg(
                            str(getattr(msg, "name", "system") or "system"),
                            "system",
                            clean_text,
                        ),
                    )
                continue

            if role == "user":
                if self._is_loop_continuation(msg):
                    continue
                flush_current()
                task_text = self._task_input_text(msg)
                if not task_text:
                    continue
                current = {
                    "kind": self._task_kind_for_user_msg(msg),
                    "user_text": task_text,
                    "assistant_parts": [],
                    "assistant_name": "default",
                }
                continue

            if role != "assistant" or current is None:
                continue

            clean_text, goal_completed = self._visible_text_from_msg(
                msg,
                stop_at_goal_completion=True,
            )
            if clean_text:
                current["assistant_parts"].append(clean_text)
                current["assistant_name"] = getattr(msg, "name", None) or "default"

            if goal_completed:
                current["kind"] = "goal"
                flush_current()
                continue

            metadata = self._msg_metadata(msg)
            if metadata.get(_LOOP_RESULT_KEY) == "mission_completed":
                current["kind"] = "mission"
                flush_current()

        flush_current()
        return compact

    def _compact_context_for_model(self) -> tuple[list | None, int]:
        """Replace agent.state.context with a compact model-only view."""
        state = getattr(self._agent, "state", None)
        context = getattr(state, "context", None)
        if not isinstance(context, list):
            return None, []

        original_context = list(context)
        filtered_context = self._build_compact_task_messages(original_context)

        tool_summary = self._build_historical_tool_calls_summary(original_context)
        if tool_summary is not None:
            filtered_context.append(tool_summary)

        context.clear()
        context.extend(filtered_context)
        return original_context, len(filtered_context)

    def _restore_context_after_model_call(
        self,
        original_context: list | None,
        compact_context_len: int,
        compact_ids: set[str] | None = None,
    ) -> None:
        """Restore full history and keep messages generated in this model call."""
        if original_context is None:
            return
        state = getattr(self._agent, "state", None)
        context = getattr(state, "context", None)
        if not isinstance(context, list):
            return

        policy = getattr(self._agent, "_context_input_policy", None)
        restored = (
            policy.restore_context(original_context, context, compact_ids or set())
            if policy is not None else None
        )
        new_messages = context[max(compact_context_len, 0) :]
        context.clear()
        context.extend(restored if restored is not None else [*original_context, *new_messages])

    async def run(
        self,
        msgs: list[Any],
    ) -> AsyncGenerator[Any, None]:
        """Drive ``agent.reply_stream`` and yield SSE envelope objects.

        Wraps the raw event stream with ``_iter_with_heartbeat`` so long
        idle periods (e.g. tool-guard approval waits) emit keep-alive
        envelopes instead of letting the connection drop.
        """
        original_model_context = None
        compact_model_context_len = 0
        compact_model_ids = set()
        restore_model = None
        turn_redaction_scope = None
        previous_response_id = getattr(self._agent, "_output_response_id", None)
        self._agent._output_response_id = self._envelope.response.id
        try:
            if COMPACT_HISTORICAL_TOOL_CONTEXT:
                (
                    original_model_context,
                    compact_model_context_len,
                ) = self._compact_context_for_model()
                compact_model_ids = {msg.id for msg in self._agent.state.context}

            from ..observability.model_input_redaction_guard import (
                TurnRedactionScope,
                maybe_wrap_model_for_redaction_confirm,
            )

            turn_redaction_scope = (
                self._redaction_scope_factory(self._agent)
                if self._redaction_scope_factory is not None
                else TurnRedactionScope(agent=self._agent)
            )
            prepared_msgs = await turn_redaction_scope.prepare_initial_inputs(msgs)
            steering = getattr(self._agent, "_normal_steering", None)
            if steering is not None:
                steering.prepare_inputs = turn_redaction_scope.prepare_initial_inputs
                steering.prepare_model_call = turn_redaction_scope.prepare_model_call
            restore_model = maybe_wrap_model_for_redaction_confirm(
                self._agent,
                turn_scope=turn_redaction_scope,
            )
            agent_iter = self._agent.reply_stream(inputs=prepared_msgs).__aiter__()

            async for event in _iter_with_heartbeat(
                agent_iter,
                HEARTBEAT_INTERVAL_SECONDS,
            ):
                if event is _HEARTBEAT_TICK:
                    async for obj in self._envelope.heartbeat():
                        yield obj
                    continue

                from .final_files import observe_event
                from .normal_preemption import SamplingPreempted

                if isinstance(event, SamplingPreempted):
                    async for obj in self._envelope.interrupt_sampling():
                        if steering is not None:
                            steering.interruptions.append({
                                "anchor_id": event.anchor_id,
                                "message": obj.model_copy(deep=True),
                            })
                        yield obj
                    continue

                event_type = getattr(getattr(event, "type", None), "value", getattr(event, "type", None))
                if steering is not None and event_type == "MODEL_CALL_START":
                    async for obj in self._envelope.begin_sampling():
                        yield obj

                observe_event(event)
                async for obj in self._envelope.translate_event(
                    event, tool_identity=self._mcp_tool_identity(event),
                ):
                    yield obj
        finally:
            if previous_response_id is None:
                del self._agent._output_response_id
            else:
                self._agent._output_response_id = previous_response_id
            steering = getattr(self._agent, "_normal_steering", None)
            if steering is not None:
                steering.prepare_inputs = None
                steering.prepare_model_call = None
            if turn_redaction_scope is not None:
                await turn_redaction_scope.finalize()
            if restore_model is not None:
                restore_model()
            self._restore_context_after_model_call(
                original_model_context,
                compact_model_context_len,
                compact_model_ids,
            )


__all__ = ["AgentExecutor"]
