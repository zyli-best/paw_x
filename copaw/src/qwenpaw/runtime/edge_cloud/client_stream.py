"""Client-only process output using the existing message/content protocol."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, is_dataclass
from typing import Any
from uuid import uuid4

from ...schemas import DataContent, Message, MessageType, Role, RunStatus, TextContent
from ...observability.session_delta_redactor import redact_sensitive_value
from ..output_attribution import current_frame, tag_output
from .function_call import _safe_get

logger = logging.getLogger(__name__)


def client_agent_header(agent: str, model: str, *, sensitive_words=None) -> str:
    """Sanitize only the added label, never the model or tool output."""
    try:
        text, _ = redact_sensitive_value(
            f"【{agent} | {model or 'unknown'}】\n\n",
            sensitive_words=sensitive_words, use_defaults=True,
        )
        return text
    except Exception:
        logger.debug("Client agent label redaction unavailable")
        return f"【{agent}】\n\n"


def client_execution_start(agent: str, header: str) -> Message:
    """Identify a stage before tools, even when the model produces no text.

    The supplied header is already redacted. This independent display message
    must be emitted outside the execution label scope to avoid a second prefix.
    """
    return Message(
        id="msg_" + uuid4().hex,
        object="message",
        name=agent,
        type=MessageType.MESSAGE,
        role=Role.ASSISTANT,
        status=RunStatus.Completed,
        content=[TextContent(
            text=header + "开始执行任务，后续工具调用和模型输出由此 Agent 完成。",
            delta=False,
            index=0,
            status=RunStatus.Completed,
        )],
    )


def _response_blocks(response: Any) -> list[Any]:
    """Read AgentScope responses and non-streaming OpenAI-shaped responses."""
    blocks = []
    content = _safe_get(response, "content")
    if isinstance(content, str):
        blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        blocks.extend(content)
    reasoning = _safe_get(response, "reasoning_content")
    if isinstance(reasoning, str):
        blocks.append({"type": "thinking", "thinking": reasoning})
    blocks.extend(_safe_get(response, "tool_calls") or [])
    message = _safe_get(response, "message")
    if message is not None:
        blocks.extend(_response_blocks(message))
    for choice in _safe_get(response, "choices") or []:
        blocks.extend(_response_blocks(choice))
    return blocks


class _DecisionResponseObserver:
    """AgentScope chunks are deltas; its last chunk is a full snapshot."""

    def __init__(self, stream):
        self.stream = stream
        self.frame = current_frame()
        self.raw: dict[str, str] = {}
        self.messages: dict[str, Message] = {}

    async def __call__(self, response):
        blocks = _response_blocks(response)
        for kind, field in ((MessageType.REASONING, "thinking"), (MessageType.MESSAGE, "text")):
            text = "".join(
                value for block in blocks
                if isinstance(value := _safe_get(block, field), str)
            )
            if not text:
                continue
            if _safe_get(response, "is_last") is False:
                self.raw[kind] = self.raw.get(kind, "") + text
            else:
                self.raw[kind] = text
            # Decision output follows execution output: preserve model content
            # and let Envelope label only the client-visible answer copy.
            safe = self.raw[kind]
            message = self.messages.get(kind)
            if message is None:
                message = Message(
                    id="msg_" + uuid4().hex, object="message",
                    name="assistant",
                    type=kind, role=Role.ASSISTANT, status=RunStatus.InProgress,
                    content=[TextContent(
                        text=safe, delta=True, index=0, status=RunStatus.InProgress,
                    )],
                )
                self.messages[kind] = message
                tag_output(message, self.frame.steer_message_id if self.frame else None)
                await self.stream._emit(message)
            elif safe != message.content[0].text:
                previous = message.content[0].text
                message.content[0].text = safe
                if safe.startswith(previous):
                    await self.stream._queue.put(TextContent(
                        text=safe[len(previous):], msg_id=message.id,
                        delta=True, index=0, status=RunStatus.InProgress,
                    ))
                else:
                    await self.stream._emit(message)

    async def complete(self):
        for message in self.messages.values():
            message.status = RunStatus.Completed
            message.content[0].status = RunStatus.Completed
            message.content[0].delta = False
            await self.stream._emit(message)


class ClientDecisionStream:
    """Keep display buffers separate from business output and session state.

    Only the decision call runs in a child task, inheriting the current model
    capture context. Its result and exceptions still reach the original
    router/verifier validation and fallback paths.
    """

    def __init__(self, agent: str, model: str, *, sensitive_words=None):
        self.agent = agent
        self._sensitive_words = sensitive_words
        self.header = client_agent_header(
            agent, model, sensitive_words=sensitive_words,
        )
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=64)
        self._active_messages: dict[str, Message] = {}
        self._tool_calls: dict[str, Message] = {}
        self.result: dict[str, Any] | None = None
        self._decision_frame = None

    async def _emit(self, message):
        if message.status == RunStatus.InProgress:
            self._active_messages[message.id] = message
        else:
            self._active_messages.pop(message.id, None)
        # Producers may update the buffer before the consumer drains its queue.
        await self._queue.put(message.model_copy(deep=True))

    def _safe_payload(self, value):
        """Redact a wire-only copy, including structured secret fields."""
        def serialize(item):
            if hasattr(item, "model_dump"):
                return item.model_dump(mode="json")
            return asdict(item) if is_dataclass(item) else str(item)

        try:
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            else:
                value = json.loads(json.dumps(value, default=serialize))
            value, _ = redact_sensitive_value(
                value, sensitive_words=self._sensitive_words, use_defaults=True,
            )
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        except Exception:
            return "过程内容暂不可用。"

    async def tool_observer(self, kind, block):
        block_id, name = _safe_get(block, "id"), _safe_get(block, "name")
        if kind == "call":
            frame = current_frame()
            identity = frame.steer_message_id if frame else None
            call_id = "decision_" + uuid4().hex
            data = {
                "call_id": call_id, "name": name,
                "arguments": self._safe_payload(_safe_get(block, "input")),
            }
            message = Message(
                id="msg_" + uuid4().hex, object="message", name=self.agent,
                type=MessageType.PLUGIN_CALL, role=Role.ASSISTANT,
                status=RunStatus.Completed,
                content=[DataContent(data=data, index=0, status=RunStatus.Completed)],
            )
            await self._emit(tag_output(message, identity))
            output = Message(
                id="msg_" + uuid4().hex, object="message", name=self.agent,
                type=MessageType.PLUGIN_CALL_OUTPUT, role=Role.TOOL,
                status=RunStatus.InProgress,
                content=[DataContent(data={
                    "call_id": call_id, "name": name, "output": "",
                }, index=0, status=RunStatus.InProgress)],
            )
            self._tool_calls[block_id] = output
            tag_output(output, identity)
            await self._emit(output)
        elif kind == "result" and block_id in self._tool_calls:
            output = self._tool_calls[block_id]
            value = _safe_get(block, "output")
            if isinstance(value, list) and all(
                isinstance(_safe_get(item, "text"), str) for item in value
            ):
                value = "".join(_safe_get(item, "text") for item in value)
            state = _safe_get(block, "state")
            output.content[0].data.update(
                output=self._safe_payload(value), state=getattr(state, "value", state),
            )
            output.status = RunStatus.Completed
            output.content[0].status = RunStatus.Completed
            await self._emit(output)
            self._tool_calls.pop(block_id, None)

    def _safe_text(self, text: str) -> str:
        try:
            redacted, _ = redact_sensitive_value(
                text,
                sensitive_words=self._sensitive_words,
                use_defaults=True,
            )
            return redacted
        except Exception:
            # Never fall back to sending unredacted display text.
            logger.debug("Client decision text redaction unavailable")
            return "过程说明暂不可用，任务继续按原流程执行。"

    def start(self) -> Message:
        """Identify the stage before its first thinking or tool event."""
        return self._message(self.header.rstrip())

    def complete(self, summary: str) -> Message:
        # A fresh ID places the conclusion after the ReAct events. Replacing
        # the start message would move the conclusion ahead of those events.
        return tag_output(
            self._message(self.header + self._safe_text(summary)),
            self._decision_frame.steer_message_id if self._decision_frame else None,
        )

    def _message(self, text: str) -> Message:
        return Message(
            id="msg_" + uuid4().hex,
            object="message",
            name=self.agent,
            type=MessageType.MESSAGE,
            role=Role.ASSISTANT,
            status=RunStatus.Completed,
            content=[
                TextContent(
                    text=text,
                    delta=False,
                    index=0,
                    status=RunStatus.Completed,
                )
            ],
        )

    def response_observer(self):
        self._decision_frame = current_frame()
        return _DecisionResponseObserver(self)

    def _close_active(self, status, state, output="调用已中断。"):
        for message in self._active_messages.values():
            copied = message.model_copy(deep=True)
            copied.status = status
            for content in copied.content:
                content.status = status
                if isinstance(content, DataContent):
                    content.data.update(output=output, state=state)
            yield copied
        self._active_messages.clear()
        self._tool_calls.clear()

    async def run(self, decision):
        task = asyncio.create_task(decision(self.response_observer))

        def wake_consumer(_task):
            # A full queue already wakes the consumer. Its loop checks task
            # completion again after draining, so no reserved sentinel slot is
            # needed and cancellation cannot deadlock on a full queue.
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

        task.add_done_callback(wake_consumer)
        try:
            while not task.done() or not self._queue.empty():
                event = await self._queue.get()
                if event is not None:
                    yield event
            self.result = await task
            # An observer may have failed after opening a card. End that
            # display without changing the completed business decision.
            for message in self._close_active(
                RunStatus.Failed, "error", "过程展示未完成。",
            ):
                yield message
        except asyncio.CancelledError:
            for message in self._close_active(RunStatus.Cancelled, "interrupted"):
                yield message
            raise
        except Exception:
            for message in self._close_active(RunStatus.Failed, "error"):
                yield message
            raise
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
