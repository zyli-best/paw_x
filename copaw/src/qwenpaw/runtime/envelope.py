# -*- coding: utf-8 -*-
"""SSE envelope state machine.

Translates agentscope ``EventType`` events into the frontend's
streaming envelope protocol.  Tracks per-request state (text blocks,
reasoning blocks, tool calls) and emits the correct event sequence
that ``Builder.tsx`` expects.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import aclosing, contextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict

from .message_convert import _media_type_to_block_type
from .output_attribution import OutputFrame, steer_message_id, tag_output

logger = logging.getLogger(__name__)


def _gen_msg_id() -> str:
    return "msg_" + uuid.uuid4().hex


class Envelope:
    """SSE envelope generation + state machine.

    One instance per ``Runtime.run()`` invocation.  Methods are async
    generators that yield schema objects (``AgentResponse``, ``Message``,
    ``TextContent``, ``DataContent``) identical to what the legacy
    ``stream_query`` produced.
    """

    def __init__(self, session_id: str = "", *, defer_sequence: bool = False) -> None:
        from ..schemas import (
            AgentResponse,
            Message,
            MessageType,
            Role,
            RunStatus,
        )

        self._response = AgentResponse(output=[], status=RunStatus.Created)
        self._response.object = "response"
        self._response.id = "response_" + uuid.uuid4().hex
        self._response.created_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        )
        self._response.session_id = session_id

        self._message_id = _gen_msg_id()
        self._completed_message = Message(
            id=self._message_id,
            type=MessageType.MESSAGE,
            role=Role.ASSISTANT,
            content=[],
            status=RunStatus.InProgress,
        )
        self._completed_message.name = "assistant"
        self._completed_message.object = "message"
        self._message_started = False

        self._text_blocks: Dict[str, Dict[str, Any]] = {}
        self._reasoning_blocks: Dict[str, Dict[str, Any]] = {}
        self._tool_calls: Dict[str, Dict[str, Any]] = {}
        self._data_blocks: Dict[str, Dict[str, Any]] = {}

        self._seq_counter = 0
        self._defer_sequence = defer_sequence

        self._error_text: str | None = None
        self._error_code: str = "error"
        self._finalized = False

        # Opt-in display state only; never read by cancel-save or session hooks.
        self._client_messages: Dict[str, Any] | None = None
        self._client_message_order: Dict[str, int] = {}
        self._client_execution_label: tuple[str, str] | None = None
        self._client_execution_messages: Dict[str, tuple[str, str]] = {}
        self._client_first_text_index: Dict[str, int | None] = {}
        self._client_text_started: set[str] = set()
        self._client_tool_display = None

    # ------------------------------------------------------------------
    # Sequence number helper
    # ------------------------------------------------------------------

    def next_sequence_number(self) -> int:
        self._seq_counter += 1
        return self._seq_counter

    def _tag_seq(self, obj: Any) -> Any:
        if not self._defer_sequence:
            obj.sequence_number = self.next_sequence_number()
        if self._client_tool_display is not None and getattr(obj, "object", None) == "response":
            obj = self._client_tool_display.filter_response(obj)
        if self._client_messages is not None:
            if getattr(obj, "object", None) == "message":
                self._client_message_order.setdefault(
                    obj.id, len(self._client_message_order),
                )
                if (
                    self._client_execution_label is not None
                    and obj.type == "message"
                    and obj.role == "assistant"
                ):
                    self._client_execution_messages.setdefault(
                        obj.id, self._client_execution_label,
                    )
                return self._client_execution_message(obj)
            elif getattr(obj, "object", None) == "response":
                # The SDK orders a completed response by output, appending
                # missing streamed messages at the end. Include display blocks
                # in a wire-only copy so the initial chronology survives.
                output = {message.id: message for message in obj.output}
                output.update(self._client_messages)
                ordered = sorted(
                    output.values(),
                    key=lambda message: self._client_message_order.get(
                        message.id, len(self._client_message_order),
                    ),
                )
                ordered = [self._client_execution_message(message) for message in ordered]
                return obj.model_copy(update={"output": ordered}, deep=True)
            elif getattr(obj, "object", None) == "content":
                label = self._client_execution_messages.get(obj.msg_id)
                if label is not None and obj.type == "text":
                    first_index = self._client_first_text_index.setdefault(obj.msg_id, obj.index)
                    if obj.index == first_index:
                        started = obj.msg_id in self._client_text_started
                        self._client_text_started.add(obj.msg_id)
                        if not obj.delta or not started:
                            return obj.model_copy(update={"text": label[1] + obj.text})
        return obj

    def _client_execution_message(self, message: Any) -> Any:
        label = self._client_execution_messages.get(message.id)
        if label is None:
            return message
        copied = message.model_copy(update={"name": label[0]}, deep=True)
        for content in copied.content:
            if content.type == "text":
                content.text = label[1] + content.text
                self._client_first_text_index.setdefault(message.id, content.index)
                self._client_text_started.add(message.id)
                break
        return copied

    @contextmanager
    def client_execution_label(self, agent: str, header: str):
        """Label answer text only; keep thinking and tool payloads unchanged."""
        self.enable_client_message_order()
        previous = self._client_execution_label
        self._client_execution_label = (agent, header)
        try:
            yield
        finally:
            self._client_execution_label = previous

    def enable_client_message_order(self) -> None:
        if self._client_messages is None:
            self._client_messages = {}

    def tag_client_event(self, event: Any) -> Any:
        """Track a display-only message outside the business response."""
        self.enable_client_message_order()
        tagged = self._tag_seq(event)
        if getattr(event, "object", None) == "message":
            self._client_messages[event.id] = event.model_copy(deep=True)
        elif getattr(event, "object", None) == "content":
            message = self._client_messages.get(getattr(event, "msg_id", ""))
            if message is not None and message.content:
                content = message.content[0]
                if getattr(event, "delta", False):
                    content.text += event.text
                else:
                    content.text = event.text
        return tagged

    # ------------------------------------------------------------------
    # Response lifecycle
    # ------------------------------------------------------------------

    async def emit_response_created(self) -> AsyncGenerator[Any, None]:
        from ..schemas import RunStatus

        self._response.status = RunStatus.Created
        yield self._tag_seq(self._response)
        self._response.status = RunStatus.InProgress
        yield self._tag_seq(self._response)

    # ------------------------------------------------------------------
    # Text message finalize helper
    # ------------------------------------------------------------------

    def _should_finalize_text_message(self) -> bool:
        return (
            self._message_started and len(self._completed_message.content) > 0
        )

    async def _finalize_text_message(self) -> AsyncGenerator[Any, None]:
        """Finalize the current text message before a tool call starts."""
        from ..schemas import RunStatus

        self._completed_message.status = RunStatus.Completed
        self._response.output.append(self._completed_message)
        yield self._tag_seq(self._completed_message)

        self._message_id = _gen_msg_id()
        from ..schemas import Message, MessageType, Role

        self._completed_message = Message(
            id=self._message_id,
            type=MessageType.MESSAGE,
            role=Role.ASSISTANT,
            content=[],
            status=RunStatus.InProgress,
        )
        self._completed_message.name = "assistant"
        self._completed_message.object = "message"
        self._message_started = False
        self._text_blocks = {}

    # ------------------------------------------------------------------
    # Event translation
    # ------------------------------------------------------------------

    @staticmethod
    def _remember_mcp_identity(
        state: Dict[str, Any], event: Any,
        tool_identity: dict[str, str] | None = None,
    ) -> None:
        metadata = getattr(event, "metadata", None)
        incoming = dict(metadata) if isinstance(metadata, dict) else {}
        incoming.update(tool_identity or {})
        identity = state.setdefault("mcp_identity", {})
        fields = ("mcp_client_key", "mcp_tool_name")
        # A later external frame cannot replace an already-bound identity.
        if any(
            key in identity and isinstance(incoming.get(key), str)
            and incoming[key] and identity[key] != incoming[key]
            for key in fields
        ):
            return
        for key in fields:
            value = incoming.get(key)
            if isinstance(value, str) and value:
                identity.setdefault(key, value)
        # Late attribution also belongs in messages already accumulated for
        # response.output, without re-emitting or changing their lifecycle.
        if identity:
            for key in ("message", "output_message"):
                message = state.get(key)
                for content in getattr(message, "content", ()) or ():
                    data = getattr(content, "data", None)
                    if isinstance(data, dict):
                        data.update(identity)

    async def translate_event(
        self, event: Any, *, tool_identity: dict[str, str] | None = None,
    ) -> AsyncGenerator[Any, None]:
        events = self._translate_event(event, tool_identity=tool_identity)
        if self._client_tool_display is not None:
            events = self._client_tool_display.translate_event(event, events)
        async with aclosing(events):
            async for output in events:
                yield output

    # pylint: disable=too-many-return-statements
    # pylint: disable=too-many-branches,too-many-statements
    async def _translate_event(  # noqa: C901, PLR0912, PLR0915
        self,
        event: Any,
        *,
        tool_identity: dict[str, str] | None = None,
    ) -> AsyncGenerator[Any, None]:
        """Translate one agentscope ``EventType`` event
        into 0..N envelope objects.
        """
        from agentscope.event import EventType
        from ..schemas import (
            ContentType,
            DataContent,
            FunctionCall,
            FunctionCallOutput,
            ImageContent,
            AudioContent,
            VideoContent,
            Message,
            MessageType,
            Role,
            RunStatus,
            TextContent,
        )

        evt_type = getattr(event, "type", None)
        if hasattr(evt_type, "value"):
            evt_type = evt_type.value

        identity = steer_message_id(event)
        # A receipt cannot relabel an in-flight reply. Only a new model block
        # with a different captured input starts a new assistant message.
        if evt_type in {
            EventType.TEXT_BLOCK_START.value,
            EventType.TEXT_BLOCK_DELTA.value,
            EventType.THINKING_BLOCK_START.value,
            EventType.THINKING_BLOCK_DELTA.value,
            EventType.DATA_BLOCK_START.value,
        }:
            if (
                self._message_started
                and steer_message_id(self._completed_message) != identity
            ):
                async for obj in self._finalize_text_message():
                    yield obj
            if not self._message_started and evt_type in {
                EventType.TEXT_BLOCK_START.value,
                EventType.TEXT_BLOCK_DELTA.value,
                EventType.DATA_BLOCK_START.value,
            }:
                tag_output(self._completed_message, identity)

        call_id = getattr(event, "tool_call_id", None)
        if (
            evt_type != EventType.TOOL_CALL_START.value
            and call_id in self._tool_calls
        ):
            self._remember_mcp_identity(
                self._tool_calls[call_id], event, tool_identity,
            )

        # === TEXT BLOCK ===
        if evt_type == EventType.TEXT_BLOCK_START.value:
            if not self._message_started:
                yield self._tag_seq(self._completed_message)
                self._message_started = True
            block_id = event.block_id
            index = len(self._text_blocks)
            self._text_blocks[block_id] = {"index": index, "text": ""}

        elif evt_type == EventType.TEXT_BLOCK_DELTA.value:
            if not self._message_started:
                yield self._tag_seq(self._completed_message)
                self._message_started = True
            block_id = event.block_id
            delta = event.delta or ""
            state = self._text_blocks.setdefault(
                block_id,
                {"index": len(self._text_blocks), "text": ""},
            )
            state["text"] += delta
            chunk = TextContent(
                type=ContentType.TEXT,
                text=delta,
                delta=True,
                index=state["index"],
            )
            chunk.msg_id = self._message_id
            yield self._tag_seq(chunk)

        elif evt_type == EventType.TEXT_BLOCK_END.value:
            block_id = event.block_id
            state = self._text_blocks.get(block_id)
            if state is None:
                return
            final_chunk = TextContent(
                type=ContentType.TEXT,
                text=state["text"],
                delta=False,
                index=state["index"],
            )
            final_chunk.msg_id = self._message_id
            yield self._tag_seq(final_chunk)
            self._completed_message.content.append(
                TextContent(
                    type=ContentType.TEXT,
                    text=state["text"],
                    delta=False,
                    index=state["index"],
                ),
            )

        # === THINKING BLOCK ===
        elif evt_type == EventType.THINKING_BLOCK_START.value:
            block_id = event.block_id
            r_msg_id = _gen_msg_id()
            r_envelope = Message(
                id=r_msg_id,
                type=MessageType.REASONING,
                role=Role.ASSISTANT,
                content=[],
                status=RunStatus.InProgress,
            )
            r_envelope.name = "assistant"
            r_envelope.object = "message"
            tag_output(r_envelope, identity)
            self._reasoning_blocks[block_id] = {
                "msg_id": r_msg_id,
                "envelope": r_envelope,
                "text": "",
            }
            yield self._tag_seq(r_envelope)

        elif evt_type == EventType.THINKING_BLOCK_DELTA.value:
            block_id = event.block_id
            delta = getattr(event, "delta", "") or ""
            state = self._reasoning_blocks.get(block_id)
            if state is None:
                r_msg_id = _gen_msg_id()
                r_envelope = Message(
                    id=r_msg_id,
                    type=MessageType.REASONING,
                    role=Role.ASSISTANT,
                    content=[],
                    status=RunStatus.InProgress,
                )
                r_envelope.name = "assistant"
                r_envelope.object = "message"
                tag_output(r_envelope, identity)
                state = {
                    "msg_id": r_msg_id,
                    "envelope": r_envelope,
                    "text": "",
                }
                self._reasoning_blocks[block_id] = state
                yield self._tag_seq(r_envelope)
            state["text"] += delta
            r_chunk = TextContent(
                type=ContentType.TEXT,
                text=delta,
                delta=True,
                index=0,
            )
            r_chunk.msg_id = state["msg_id"]
            yield self._tag_seq(r_chunk)

        elif evt_type == EventType.THINKING_BLOCK_END.value:
            block_id = event.block_id
            state = self._reasoning_blocks.get(block_id)
            if state is None:
                return
            r_final = TextContent(
                type=ContentType.TEXT,
                text=state["text"],
                delta=False,
                index=0,
            )
            r_final.msg_id = state["msg_id"]
            yield self._tag_seq(r_final)
            state["envelope"].content.append(
                TextContent(
                    type=ContentType.TEXT,
                    text=state["text"],
                    delta=False,
                    index=0,
                ),
            )
            state["envelope"].status = RunStatus.Completed
            self._response.output.append(state["envelope"])
            yield self._tag_seq(state["envelope"])

        # === TOOL CALL ===
        elif evt_type == EventType.TOOL_CALL_START.value:
            # P0-5: finalize current text message if needed
            if self._should_finalize_text_message():
                async for obj in self._finalize_text_message():
                    yield obj

            call_id = event.tool_call_id
            msg_id = _gen_msg_id()
            plugin_call_message = Message(
                id=msg_id,
                type=MessageType.PLUGIN_CALL,
                role=Role.ASSISTANT,
                content=[],
                status=RunStatus.InProgress,
            )
            plugin_call_message.name = "assistant"
            plugin_call_message.object = "message"
            tag_output(plugin_call_message, identity)

            state = {
                "name": event.tool_call_name,
                "args_json_acc": "",
                "message": plugin_call_message,
                "output_text_acc": "",
                "output_data_blocks": {},
                "steer_message_id": identity,
            }
            self._remember_mcp_identity(state, event, tool_identity)
            self._tool_calls[call_id] = state

            stub_content = DataContent(
                type=ContentType.DATA,
                data=FunctionCall(
                    call_id=call_id,
                    name=event.tool_call_name,
                    arguments="",
                    **state["mcp_identity"],
                ).model_dump(),
                delta=False,
                index=0,
            )
            stub_content.msg_id = msg_id

            yield self._tag_seq(plugin_call_message.in_progress())
            yield self._tag_seq(stub_content.in_progress())

        elif evt_type == EventType.TOOL_CALL_DELTA.value:
            call_id = event.tool_call_id
            state = self._tool_calls.get(call_id)
            if state is None:
                return
            state["args_json_acc"] += event.delta or ""

            delta_content = DataContent(
                type=ContentType.DATA,
                data=FunctionCall(
                    call_id=call_id,
                    name=state["name"],
                    arguments=state["args_json_acc"],
                    **state.get("mcp_identity", {}),
                ).model_dump(),
                delta=False,
                index=0,
            )
            delta_content.msg_id = state["message"].id
            yield self._tag_seq(delta_content.in_progress())

        elif evt_type == EventType.TOOL_CALL_END.value:
            call_id = event.tool_call_id
            state = self._tool_calls.get(call_id)
            if state is None:
                return

            final_content = DataContent(
                type=ContentType.DATA,
                data=FunctionCall(
                    call_id=call_id,
                    name=state["name"],
                    arguments=state["args_json_acc"],
                    **state.get("mcp_identity", {}),
                ).model_dump(),
                delta=False,
            )
            state["message"].add_content(new_content=final_content)
            yield self._tag_seq(final_content.completed())
            self._response.output.append(state["message"])
            yield self._tag_seq(state["message"].completed())

        # === TOOL RESULT ===
        elif evt_type == EventType.TOOL_RESULT_START.value:
            call_id = event.tool_call_id
            state = self._tool_calls.get(call_id)
            if state is None:
                state = {
                    "name": event.tool_call_name,
                    "args_json_acc": "",
                    "output_text_acc": "",
                    "output_data_blocks": {},
                }
                self._tool_calls[call_id] = state
                self._remember_mcp_identity(state, event, tool_identity)

            out_msg_id = _gen_msg_id()
            out_message = Message(
                id=out_msg_id,
                type=MessageType.PLUGIN_CALL_OUTPUT,
                role=Role.TOOL,
                content=[],
                status=RunStatus.InProgress,
            )
            out_message.name = "assistant"
            out_message.object = "message"
            tag_output(out_message, state.get("steer_message_id"))

            stub_content = DataContent(
                type=ContentType.DATA,
                data=FunctionCallOutput(
                    call_id=call_id,
                    name=state["name"],
                    output="",
                    **state.get("mcp_identity", {}),
                ).model_dump(),
                delta=False,
                index=0,
            )
            stub_content.msg_id = out_msg_id

            state["output_message"] = out_message
            state["output_text_acc"] = ""
            state["output_data_blocks"] = {}

            yield self._tag_seq(out_message.in_progress())
            yield self._tag_seq(stub_content.in_progress())

        elif evt_type == EventType.TOOL_RESULT_TEXT_DELTA.value:
            call_id = event.tool_call_id
            state = self._tool_calls.get(call_id)
            if state is None:
                return
            state["output_text_acc"] += event.delta or ""

            delta_content = self._build_tool_result_content(
                call_id,
                state,
                ContentType,
                FunctionCallOutput,
            )
            delta_content.msg_id = state["output_message"].id
            yield self._tag_seq(delta_content.in_progress())

        elif evt_type == EventType.TOOL_RESULT_DATA_DELTA.value:
            call_id = event.tool_call_id
            state = self._tool_calls.get(call_id)
            if state is None:
                return

            block_id = event.block_id
            media_type = getattr(event, "media_type", None)
            block_type = _media_type_to_block_type(media_type)

            blocks_dict: dict = state["output_data_blocks"]
            url = getattr(event, "url", None)
            b64 = getattr(event, "data", None)

            if block_id in blocks_dict:
                existing = blocks_dict[block_id]
                if b64:
                    existing["source"]["data"] = (
                        existing["source"].get("data", "") + b64
                    )
            else:
                source: dict[str, Any] = {}
                if url:
                    source = {
                        "type": "url",
                        "url": url,
                        "media_type": media_type or "",
                    }
                elif b64:
                    source = {
                        "type": "base64",
                        "data": b64,
                        "media_type": media_type or "",
                    }
                blocks_dict[block_id] = {
                    "type": block_type,
                    "source": source,
                }

            delta_content = self._build_tool_result_content(
                call_id,
                state,
                ContentType,
                FunctionCallOutput,
            )
            delta_content.msg_id = state["output_message"].id
            yield self._tag_seq(delta_content.in_progress())

        elif evt_type == EventType.TOOL_RESULT_END.value:
            call_id = event.tool_call_id
            state = self._tool_calls.get(call_id)
            if state is None:
                return

            tool_state = getattr(event, "state", None)
            if hasattr(tool_state, "value"):
                tool_state = tool_state.value

            final_content = self._build_tool_result_content(
                call_id,
                state,
                ContentType,
                FunctionCallOutput,
                tool_state=tool_state,
            )

            out_message = state.get("output_message")
            if out_message is None:
                out_message = Message(
                    id=_gen_msg_id(),
                    type=MessageType.PLUGIN_CALL_OUTPUT,
                    role=Role.TOOL,
                    content=[],
                    status=RunStatus.InProgress,
                )
                out_message.name = "assistant"
                out_message.object = "message"
                tag_output(out_message, state.get("steer_message_id"))

            out_message.add_content(new_content=final_content)
            yield self._tag_seq(final_content.completed())
            self._response.output.append(out_message)
            yield self._tag_seq(out_message.completed())

        # === DATA BLOCK ===
        elif evt_type == EventType.DATA_BLOCK_START.value:
            block_id = event.block_id
            media_type = getattr(event, "media_type", "")

            if not self._message_started:
                yield self._tag_seq(self._completed_message)
                self._message_started = True

            self._data_blocks[block_id] = {
                "media_type": media_type,
                "data_acc": "",
            }

        elif evt_type == EventType.DATA_BLOCK_DELTA.value:
            block_id = event.block_id
            state = self._data_blocks.get(block_id)
            if state is None:
                return
            state["data_acc"] += event.data or ""
            # No intermediate yield: partial base64 cannot be decoded or
            # rendered by the frontend.  Content is emitted once on
            # DATA_BLOCK_END with the complete payload.

        elif evt_type == EventType.DATA_BLOCK_END.value:
            block_id = event.block_id
            state = self._data_blocks.get(block_id)
            if state is None:
                return

            media_type = state["media_type"]
            b64_data = state["data_acc"]
            major = (media_type.split("/", 1)[0]) if media_type else ""
            index = len(self._completed_message.content)

            if major == "audio":
                fmt = media_type.split("/", 1)[1] if "/" in media_type else ""
                content_block = AudioContent(
                    type=ContentType.AUDIO,
                    data=b64_data,
                    format=fmt,
                    delta=False,
                    index=index,
                )
            elif major == "video":
                video_uri = f"data:{media_type};base64,{b64_data}"
                content_block = VideoContent(
                    type=ContentType.VIDEO,
                    video_url=video_uri,
                    delta=False,
                    index=index,
                )
            else:
                data_uri = f"data:{media_type};base64,{b64_data}"
                content_block = ImageContent(
                    type=ContentType.IMAGE,
                    image_url=data_uri,
                    delta=False,
                    index=index,
                )

            content_block.msg_id = self._message_id
            self._completed_message.content.append(content_block)
            yield self._tag_seq(content_block)

        # === MODEL_CALL_END ===
        elif evt_type == EventType.MODEL_CALL_END.value:
            input_tokens = getattr(event, "input_tokens", 0)
            output_tokens = getattr(event, "output_tokens", 0)
            usage = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
            self._response.usage = usage
            if self._message_started:
                self._completed_message.usage = usage

        # === EXCEED_MAX_ITERS ===
        elif evt_type == EventType.EXCEED_MAX_ITERS.value:
            agent_name = getattr(event, "name", "agent")
            err_msg_id = _gen_msg_id()
            err_message = Message(
                id=err_msg_id,
                type=MessageType.MESSAGE,
                role=Role.ASSISTANT,
                content=[],
                status=RunStatus.InProgress,
            )
            err_message.name = "assistant"
            err_message.object = "message"
            yield self._tag_seq(err_message)

            err_text = TextContent(
                type=ContentType.TEXT,
                text=(
                    f"[Warning] Agent '{agent_name}' has reached "
                    f"the maximum number of iterations."
                ),
                delta=False,
                index=0,
            )
            err_text.msg_id = err_msg_id
            yield self._tag_seq(err_text)

            err_message.content.append(err_text)
            err_message.status = RunStatus.Completed
            self._response.output.append(err_message)
            yield self._tag_seq(err_message)

        # === HINT_BLOCK (P2-2: warn and drop) ===
        # ``EventType.HINT_BLOCK`` does not exist in every agentscope version;
        # guard the lookup so an unmatched event falls through to a no-op
        # instead of raising AttributeError and killing the whole turn.
        elif (_hb := getattr(EventType, "HINT_BLOCK", None)) is not None and (
            evt_type == _hb.value
        ):
            source = getattr(event, "source", None) or ""
            logger.warning(
                "HintBlockEvent received but not rendered: "
                "block_id=%s source=%s",
                getattr(event, "block_id", "?"),
                source,
            )

    # ------------------------------------------------------------------
    # Tool result content builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_tool_result_content(
        call_id: str,
        state: Dict[str, Any],
        ContentType: Any,
        FunctionCallOutput: Any,
        tool_state: Any = None,
    ) -> Any:
        from ..schemas import DataContent

        blocks_dict: dict = state.get("output_data_blocks", {})
        text_acc: str = state.get("output_text_acc", "")

        if blocks_dict:
            output_blocks: list[dict[str, Any]] = list(
                blocks_dict.values(),
            )
            if text_acc:
                output_blocks.append({"type": "text", "text": text_acc})
            tool_output: Any = json.dumps(
                output_blocks,
                ensure_ascii=False,
            )
        else:
            tool_output = text_acc

        data_dict = FunctionCallOutput(
            call_id=call_id,
            name=state["name"],
            output=tool_output,
            **state.get("mcp_identity", {}),
        ).model_dump()
        if tool_state is not None:
            data_dict["state"] = tool_state

        return DataContent(
            type=ContentType.DATA,
            data=data_dict,
            delta=False,
            index=0,
        )

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def heartbeat(self) -> AsyncGenerator[Any, None]:
        yield self._tag_seq(self._response)

    # ------------------------------------------------------------------
    # Command short-circuit
    # ------------------------------------------------------------------

    async def append_final_files(
        self, files: list[dict], *, error: str = "",
    ) -> AsyncGenerator[Any, None]:
        """Append released attachments using the existing message protocol."""
        from ..schemas import (
            AudioContent, FileContent, ImageContent, TextContent, VideoContent,
        )
        if self._finalized:
            return
        blocks = []
        if error:
            blocks.append(TextContent(text=error))
        for item in files:
            # The existing console replaceMediaURL adds its API base and auth;
            # other channels resolve file:// through their native file sender.
            from pathlib import Path

            url = Path(item["snapshot"]).as_uri()
            major = item["media_type"].split("/", 1)[0]
            if major == "image":
                block = ImageContent(image_url=url)
            elif major == "audio":
                block = AudioContent(data=url, format=item["media_type"].split("/", 1)[-1])
            elif major == "video":
                block = VideoContent(video_url=url)
            else:
                block = FileContent(
                    filename=item["name"], file_name=item["name"], file_url=url,
                )
            blocks.append(block)
        for block in blocks:
            block.msg_id = self._message_id
            block.index = len(self._completed_message.content)
            block.delta = False
            self._completed_message.content.append(block)
            block.completed()
        if blocks:
            self._message_started = True
            # The existing client merges non-delta content into its last block.
            # Publish the whole message so every attachment remains distinct.
            yield self._tag_seq(self._completed_message.model_copy(deep=True))

    async def from_msg(self, cmd_msg: Any) -> AsyncGenerator[Any, None]:
        """Translate a completed ``Msg`` from a slash
        command into a full envelope sequence.
        """
        from ..schemas import ContentType, RunStatus, TextContent

        cmd_text = cmd_msg.get_text_content() or ""

        if not self._message_started:
            yield self._tag_seq(self._completed_message)
            self._message_started = True

        tc = TextContent(
            type=ContentType.TEXT,
            text=cmd_text,
            delta=False,
            index=0,
        )
        tc.msg_id = self._message_id
        yield self._tag_seq(tc)

        self._completed_message.content.append(tc)
        self._completed_message.status = RunStatus.Completed
        self._completed_message.metadata = (
            getattr(cmd_msg, "metadata", None) or {}
        )
        self._response.output.append(self._completed_message)
        yield self._tag_seq(self._completed_message)

        self._response.status = RunStatus.Completed
        self._response.completed_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        )
        yield self._tag_seq(self._response)
        self._finalized = True

    # ------------------------------------------------------------------
    # Error / Cancel
    # ------------------------------------------------------------------

    async def error_envelope(
        self,
        error_text: str,
        error_code: str = "error",
    ) -> AsyncGenerator[Any, None]:
        self._error_text = error_text
        self._error_code = error_code
        async for obj in self._finalize_response():
            yield obj

    async def cancel_envelope(self) -> AsyncGenerator[Any, None]:
        async for obj in self._finalize_response():
            yield obj

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------

    def _retain_partial_mcp_messages(self) -> None:
        """Retain emitted MCP data in the final response, without stop events."""
        from ..schemas import (
            ContentType, DataContent, FunctionCall, FunctionCallOutput,
        )

        present = {message.id for message in self._response.output}
        for call_id, state in self._tool_calls.items():
            identity = state.get("mcp_identity")
            if not identity:
                continue
            for key in ("message", "output_message"):
                message = state.get(key)
                if message is None or message.id in present:
                    continue
                snapshot = message.model_copy(deep=True)
                if not snapshot.content:
                    if key == "message":
                        content = DataContent(data=FunctionCall(
                            call_id=call_id, name=state["name"],
                            arguments=state["args_json_acc"], **identity,
                        ).model_dump())
                    else:
                        content = self._build_tool_result_content(
                            call_id, state, ContentType, FunctionCallOutput,
                        )
                    snapshot.content.append(content)
                self._response.output.append(snapshot)
                present.add(snapshot.id)

    async def finalize(self) -> AsyncGenerator[Any, None]:
        if self._finalized:
            return
        async for obj in self._finalize_response():
            yield obj

    async def _finalize_response(self) -> AsyncGenerator[Any, None]:
        from ..schemas import ContentType, RunStatus, TextContent

        if self._finalized:
            return

        if self._message_started:
            # Back-fill any partially accumulated text blocks that were
            # not finalized (TEXT_BLOCK_END never fired, e.g. on cancel).
            if not self._completed_message.content:
                for state in self._text_blocks.values():
                    text = state.get("text", "")
                    if text:
                        self._completed_message.content.append(
                            TextContent(
                                type=ContentType.TEXT,
                                text=text,
                                delta=False,
                                index=state.get("index", 0),
                            ),
                        )

            if self._completed_message.content:
                self._completed_message.status = RunStatus.Completed
                self._response.output.append(self._completed_message)
                yield self._tag_seq(self._completed_message)

        self._retain_partial_mcp_messages()

        if self._error_text:
            self._response.status = RunStatus.Failed
            self._response.error = {
                "code": self._error_code,
                "message": self._error_text,
            }
        else:
            self._response.status = RunStatus.Completed
        self._response.completed_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        )
        yield self._tag_seq(self._response)
        self._finalized = True

    # ------------------------------------------------------------------
    # Partial content extraction (used by cancel-save)
    # ------------------------------------------------------------------

    def collect_partial_blocks(self) -> list[tuple[str, str]]:
        """Return ``(block_type, content)`` tuples for streaming content
        accumulated in this envelope that has **not** been finalized.

        * ``("thinking", text)`` — from reasoning blocks whose envelope
          status is still ``InProgress`` (i.e. the interrupted iteration).
        * ``("text", text)`` — from text blocks (reset on every tool-call
          start, so they belong to the current iteration only).

        This is a public-API entry point so that callers (e.g.
        ``Runtime._try_save_on_cancel``) do not need to reach into
        private state.
        """
        from ..schemas import RunStatus

        result: list[tuple[str, str]] = []

        for state in self._reasoning_blocks.values():
            env = state.get("envelope")
            if env is not None and env.status == RunStatus.Completed:
                continue
            text = state.get("text", "")
            if text:
                result.append(("thinking", text))

        for state in self._text_blocks.values():
            text = state.get("text", "")
            if text:
                result.append(("text", text))

        return result

    def partial_output_frame(self) -> OutputFrame:
        """Use the interrupted output's attribution for native cancel-save."""
        from ..schemas import RunStatus

        for state in reversed(list(self._reasoning_blocks.values())):
            message = state["envelope"]
            if message.status == RunStatus.InProgress:
                return OutputFrame(self._response.id, steer_message_id(message))
        return OutputFrame(
            self._response.id, steer_message_id(self._completed_message),
        )

    def collect_tool_output(self) -> dict[str, str]:
        """Return ``{call_id: accumulated_output}`` for tool calls that
        received partial results before the stream was interrupted.

        Only includes entries where ``output_text_acc`` is non-empty.
        """
        result: dict[str, str] = {}
        for call_id, state in self._tool_calls.items():
            output = state.get("output_text_acc", "")
            if output:
                result[call_id] = output
        return result

    @property
    def response(self) -> Any:
        return self._response

    @property
    def agent_ref(self) -> Any:
        """Access point for the agent reference used in session save."""
        return None
    async def begin_sampling(self):
        """Ordinary steering only: delimit the display of one model attempt."""
        if self._message_started:
            async for obj in self._finalize_text_message():
                yield obj
        self._sampling_output_ids = {m.id for m in self._response.output}
        self._sampling_reasoning_ids = set(self._reasoning_blocks)
        self._sampling_call_ids = set(self._tool_calls)

    async def interrupt_sampling(self):
        """Close unfinished display cards without completing the outer reply."""
        from ..schemas import (
            ContentType, DataContent, FunctionCall, Message, MessageType,
            Role, RunStatus, TextContent,
        )

        baseline = getattr(self, "_sampling_output_ids", set())
        messages = {m.id: m for m in self._response.output if m.id not in baseline}
        for key, state in self._reasoning_blocks.items():
            if key in getattr(self, "_sampling_reasoning_ids", set()):
                continue
            message = state["envelope"]
            if message.status == RunStatus.InProgress:
                message.content = [TextContent(text=state["text"], index=0)]
                message.status = RunStatus.Cancelled
            messages[message.id] = message
        if self._message_started:
            message = self._completed_message
            by_index = {c.index: c for c in message.content}
            for state in self._text_blocks.values():
                by_index[state["index"]] = TextContent(
                    text=state["text"], index=state["index"],
                )
            message.content = [by_index[i] for i in sorted(by_index)]
            message.status = RunStatus.Cancelled
            messages[message.id] = message
        for key, state in self._tool_calls.items():
            if key in getattr(self, "_sampling_call_ids", set()):
                continue
            message = state["message"]
            if not message.content:
                message.content = [DataContent(type=ContentType.DATA, data=FunctionCall(
                    call_id=key, name=state["name"],
                    arguments=state["args_json_acc"], **state.get("mcp_identity", {}),
                ).model_dump())]
            message.status = RunStatus.Cancelled
            messages[message.id] = message
        present = {m.id for m in self._response.output}
        for message in messages.values():
            message.metadata = {**(message.metadata or {}), "steer_preempted": True}
            if message.id not in present:
                self._response.output.append(message)
            yield self._tag_seq(message)
        # Cancel-save must not resurrect these discarded model fragments.
        self._reasoning_blocks = {
            k: v for k, v in self._reasoning_blocks.items()
            if k in getattr(self, "_sampling_reasoning_ids", set())
        }
        self._tool_calls = {
            k: v for k, v in self._tool_calls.items()
            if k in getattr(self, "_sampling_call_ids", set())
        }
        self._text_blocks = {}
        self._data_blocks = {}
        self._message_id = _gen_msg_id()
        self._completed_message = Message(
            id=self._message_id, type=MessageType.MESSAGE, role=Role.ASSISTANT,
            content=[], status=RunStatus.InProgress, name="assistant", object="message",
        )
        self._message_started = False


__all__ = ["Envelope"]
