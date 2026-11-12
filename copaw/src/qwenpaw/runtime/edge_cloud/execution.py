"""Request-scoped lower-tier execution limits, without replacing session state."""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
from collections.abc import AsyncIterable
from contextlib import aclosing, nullcontext
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import FunctionTool, ToolBase, ToolChunk, ToolResponse, ToolChoice

from .safety import FileOperationJournal, WRITE_TOOLS
from .function_call import _candidate_tool_call_blocks, _normalize_tool_call, _safe_get

logger = logging.getLogger(__name__)

_FIRST_OUTPUT_TIMEOUT_SECONDS = 15.0
_IDLE_OUTPUT_TIMEOUT_SECONDS = 20.0
_ROUND_TIMEOUT_SECONDS = {"device": 360.0, "edge": 600.0}


class LocalExecutionHandoff(BaseException):
    """Control signal which the SDK must not turn into another tool error."""


class _ModelResponseDeadline:
    """Bound one model request, including opening and consuming its stream."""

    def __init__(self, role: str):
        self.role = role
        self._loop = asyncio.get_running_loop()
        self._started = self._loop.time()
        self._round_deadline = self._started + _ROUND_TIMEOUT_SECONDS[role]
        self._last_output_at = None

    def observe(self, chunk):
        # Usage-only/empty chunks must not keep an otherwise stalled model alive.
        for block in _safe_get(chunk, "content") or []:
            if any(_safe_get(block, field) for field in ("text", "thinking")):
                self._last_output_at = self._loop.time()
                return
            name, arguments = _normalize_tool_call(block)
            source = _safe_get(block, "source")
            if name or arguments or any(
                _safe_get(source, field) for field in ("data", "url")
            ):
                self._last_output_at = self._loop.time()
                return

    async def wait(self, awaitable):
        if self._last_output_at is None:
            deadline = self._started + _FIRST_OUTPUT_TIMEOUT_SECONDS
            phase = "first_output"
        else:
            deadline = self._last_output_at + _IDLE_OUTPUT_TIMEOUT_SECONDS
            phase = "idle_output"
        if self._round_deadline <= deadline:
            deadline, phase = self._round_deadline, "round"
        timeout = asyncio.timeout_at(deadline)
        try:
            async with timeout:
                result = await awaitable
        except TimeoutError:
            if not timeout.expired():
                raise
            self._expire(phase)
        # Some providers swallow cancellation and return an interrupted chunk.
        if timeout.expired() or self._loop.time() >= deadline:
            self._expire(phase)
        return result

    def _expire(self, phase):
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        logger.warning(
            "Local model timeout: role=%s phase=%s elapsed_seconds=%.3f",
            self.role, phase, self._loop.time() - self._started,
        )
        # Reuse the existing handoff reason and payload without adding metadata.
        raise LocalExecutionHandoff("execution_error") from None


class ExecutionJournal(FileOperationJournal):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict[str, Any]] = []
        self.last_failed_tool = ""
        self.failure_streak = 0
        self.stop_reason = ""
        self.edge_mode = False
        self.required_skill = ""
        self.skill_loaded = False
        self.tool_rounds = 0
        self.history = None
        self._lock = asyncio.Lock()

    def check(self) -> None:
        if self.stop_reason:
            raise LocalExecutionHandoff(self.stop_reason)

    def wrap(self, tool: Any, *, lower: bool) -> ToolBase:
        return _ExecutionTool(tool, self, lower=lower)

    def complete(self, record: dict, chunk: Any) -> None:
        state = getattr(chunk, "state", ToolResultState.ERROR)
        state = getattr(state, "value", state)
        previous_state = record.get("status")
        record["status"] = state
        record["result"] = chunk.model_dump(mode="json")
        # A terminal error can be delivered before the consumer closes the
        # stream. Refresh its evidence on close without counting it twice.
        if state == previous_state:
            return
        if state == "error":
            self.failure_streak = (
                self.failure_streak + 1
                if self.last_failed_tool == record["tool"]
                else 1
            )
            self.last_failed_tool = record["tool"]
            if self.failure_streak == 3:
                self.stop_reason = "SAME_TOOL_3_FAILURES"
        elif state == "success":
            self.last_failed_tool = ""
            self.failure_streak = 0
            if record["tool"] == "Skill" and self.edge_mode:
                self.required_skill = record.get("arguments", {}).get("skill", "")
                self.skill_loaded = True
        elif state in {"interrupted", "running"}:
            self.stop_reason = "UNKNOWN_TOOL_OUTCOME"


class ExecutionModel:
    """Limit lower-tier model input and stop reasoning after a forced handoff."""

    def __init__(
        self,
        model: Any,
        journal: ExecutionJournal,
        *,
        task_view=None,
        cloud_model=None,
        tool_round_limit=None,
        execution_role="device",
    ):
        self._inner = model
        self.journal = journal
        self.task_view = task_view
        self.cloud_model = cloud_model
        self.tool_round_limit = tool_round_limit
        self.execution_role = execution_role

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def count_tokens(self, messages, tools=None, **kwargs):
        if self.task_view is not None:
            messages = self.task_view.project(messages)
        return await self._inner.count_tokens(messages=messages, tools=tools, **kwargs)

    async def generate_structured_output(self, *args, **kwargs):
        self.journal.check()
        # Native compression may summarize canonical conversation history.
        # Keep it on the original cloud model; never disclose it to lower tiers.
        model = self.cloud_model if self.cloud_model is not None else self._inner
        return await model.generate_structured_output(*args, **kwargs)

    async def __call__(self, messages, *args, **kwargs):
        self.journal.check()
        if self.task_view is not None:
            # ReAct always supplies tools (including []). Scroll's optional
            # headline calls omit them and operate on original cloud history.
            if (
                kwargs.get("tools") is None
                and not args
                and self.cloud_model is not None
            ):
                return await self.cloud_model(messages, **kwargs)
            messages = self.task_view.project(messages)
            policy = getattr(self.task_view.agent, "_context_input_policy", None)
            if policy is not None:
                policy.validate_guidance(messages)
        from ..output_attribution import capture_model_input

        capture_model_input(messages)
        if (
            self.tool_round_limit is not None
            and self.journal.tool_rounds >= self.tool_round_limit
        ):
            kwargs["tool_choice"] = ToolChoice(mode="none")
        deadline = _ModelResponseDeadline(self.execution_role)
        response = await deadline.wait(self._inner(messages, *args, **kwargs))
        counted = False

        def guard(chunk):
            nonlocal counted
            if counted or not any(
                _normalize_tool_call(item)[0]
                for item in _candidate_tool_call_blocks(chunk)
            ):
                return
            if (
                self.tool_round_limit is not None
                and self.journal.tool_rounds >= self.tool_round_limit
            ):
                self.journal.stop_reason = "DEVICE_TOOL_ROUND_LIMIT"
                self.journal.check()
            self.journal.tool_rounds += 1
            counted = True

        if isinstance(response, AsyncIterable):

            async def stream():
                iterator = response.__aiter__()
                try:
                    while True:
                        try:
                            chunk = await deadline.wait(anext(iterator))
                        except StopAsyncIteration:
                            return
                        deadline.observe(chunk)
                        guard(chunk)
                        yield chunk
                finally:
                    close = getattr(iterator, "aclose", None)
                    if close is not None:
                        try:
                            await close()
                        except Exception:
                            logger.warning("Could not close local model stream", exc_info=True)

            return stream()
        guard(response)
        return response


class TaskInputView:
    """Project model input, leaving canonical state and Scroll hooks intact."""

    def __init__(self, agent, task_message):
        self.agent, self.task_message = agent, task_message

    def project(self, messages):
        # SDK assistant messages for this reply use reply_id, including tool
        # calls/results. Old history, summaries, Scroll indices and middleware
        # memory additions are never lower-tier input, even after compression.
        policy = getattr(self.agent, "_context_input_policy", None)
        guidance_ids = {m.id for m in policy.injected} if policy is not None else set()
        current = [
            message
            for message in messages
            if message.id in guidance_ids or (
                message.role == "assistant" and (
                    message.id == self.agent.state.reply_id
                    or (message.metadata or {}).get("context_reply_id") == self.agent.state.reply_id
                )
            )
        ]
        system = [messages[0]] if messages and messages[0].role == "system" else []
        return [*system, self.task_message, *current]


class _ExecutionTool(ToolBase):
    def __init__(self, inner: Any, journal: ExecutionJournal, *, lower: bool):
        super().__init__()
        self.inner, self.journal, self.lower = inner, journal, lower
        # Only this built-in's DENIED branches are known to run before any
        # mutation. A same-named custom/MCP tool is not evidence of no effect.
        from ...agents.tools.file_io import write_file

        self._pre_execution_denial = (
            isinstance(inner, FunctionTool)
            and getattr(inner, "_func", None) is write_file
        )
        for name in (
            "name",
            "description",
            "input_schema",
            "is_read_only",
            "is_external_tool",
            "is_state_injected",
            "is_mcp",
            "mcp_name",
            "is_concurrency_safe",
        ):
            setattr(self, name, getattr(inner, name, getattr(ToolBase, name, None)))
        if lower:
            self.is_concurrency_safe = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def check_permissions(self, *args, **kwargs):
        if self.lower:
            self.journal.check()
        return await self.inner.check_permissions(*args, **kwargs)

    async def __call__(self, **kwargs):
        async def invoke():
            if self.lower:
                self.journal.check()
                if (
                    self.journal.edge_mode
                    and self.name
                    not in {
                        "Skill",
                        "list_skills",
                        "get_skill_count",
                        "reset_tools",
                        "ResetTools",
                    }
                    and not self.journal.skill_loaded
                ):
                    self.journal.stop_reason = "EDGE_SKILL_NOT_LOADED"
                    self.journal.check()
                if (
                    self.journal.edge_mode
                    and self.name == "Skill"
                    and self.journal.required_skill
                    and kwargs.get("skill") != self.journal.required_skill
                ):
                    self.journal.stop_reason = (
                        "MULTIPLE_SKILLS_REQUIRED"
                        if self.journal.skill_loaded
                        else "EDGE_SKILL_MISMATCH"
                    )
                    self.journal.check()
            operation = None
            previous_outcome_unknown = False
            if self.name in WRITE_TOOLS:
                effective = {
                    **{
                        key: value["default"]
                        for key, value in self.input_schema.get(
                            "properties", {}
                        ).items()
                        if "default" in value
                    },
                    **kwargs,
                }
                key = self.journal.key(self.name, effective)
                previous = self.journal.operations.get(key)
                if previous is not None and not self.lower:
                    yield ToolChunk(
                        state=ToolResultState.ERROR,
                        content=[
                            TextBlock(
                                text=(
                                    f"This file operation was already attempted ({previous['status']}). "
                                    "Inspect current state and complete only remaining work; do not replay blindly."
                                )
                            )
                        ],
                    )
                    return
                previous_outcome_unknown = (
                    previous is not None
                    and previous["status"] not in {"succeeded", "not_executed"}
                )
                operation = {"tool": self.name, "status": "unknown"}
                if previous_outcome_unknown:
                    operation["previous_outcome_unknown"] = True
                self.journal.operations[key] = operation
            self.journal.calls += 1
            record = {
                "call_seq": self.journal.calls,
                "tool": self.name,
                "arguments": copy.deepcopy(
                    {k: v for k, v in kwargs.items() if k != "_agent_state"}
                ),
                "status": "unknown",
                "result": None,
            }
            if self.lower:
                self.journal.records.append(record)
            last = None
            response = ToolResponse()
            finished = False
            received_chunks = 0

            def capture_text():
                history = self.journal.history
                return history.capture_tool_text(record) if self.lower and history is not None else nullcontext()

            def complete():
                if self.lower:
                    self.journal.complete(record, response)
                if operation is not None:
                    status = "unknown"
                    if response.state == ToolResultState.SUCCESS:
                        status = "succeeded"
                    elif (
                        response.state == ToolResultState.DENIED
                        and self._pre_execution_denial
                        and received_chunks == 1
                    ):
                        status = "not_executed"
                    record["file_operation_status"] = status
                    # A successful retry does not prove that an earlier
                    # interrupted/partially failed write left no extra effects.
                    operation["status"] = (
                        "unknown" if previous_outcome_unknown else status
                    )

            def append(chunk):
                nonlocal received_chunks
                received_chunks += 1
                response.append_chunk(chunk)
                # ToolCoordinator stops consuming immediately on these states.
                # Their outcome must be recorded before control leaves yield.
                if chunk.state in {
                    ToolResultState.ERROR, ToolResultState.INTERRUPTED,
                    ToolResultState.DENIED,
                }:
                    complete()

            try:
                with capture_text():
                    result = self.inner(**kwargs)
                    if inspect.isawaitable(result):
                        result = await result
                if hasattr(result, "__aiter__"):
                    iterator = result.__aiter__()
                    while True:
                        try:
                            with capture_text():
                                chunk = await anext(iterator)
                        except StopAsyncIteration:
                            break
                        last = chunk
                        append(chunk)
                        yield chunk
                elif isinstance(result, ToolChunk):
                    last = result
                    append(result)
                    finished = True
                    complete()
                    yield result
                elif isinstance(result, ToolResponse):
                    last = ToolChunk(
                        state=result.state,
                        content=result.content,
                        metadata=result.metadata,
                    )
                    append(last)
                    finished = True
                    complete()
                    yield last
                else:
                    iterator = iter(result)
                    while True:
                        try:
                            with capture_text():
                                chunk = next(iterator)
                        except StopIteration:
                            break
                        last = chunk
                        append(chunk)
                        yield chunk
                finished = True
            except Exception as exc:
                last = ToolChunk(
                    state=ToolResultState.ERROR,
                    content=[TextBlock(text=str(exc))],
                )
                append(last)
                finished = True
                yield last
            finally:
                if last is not None and (
                    finished or response.state in {
                        ToolResultState.ERROR, ToolResultState.INTERRUPTED,
                        ToolResultState.DENIED,
                    }
                ):
                    # Preserve the aggregate result, including chunks consumed
                    # after an error, without repeating failure accounting.
                    complete()
                elif self.lower:
                    self.journal.stop_reason = "UNKNOWN_TOOL_OUTCOME"

        async def stream():
            async with aclosing(invoke()) as chunks:
                while True:
                    # Keep execution and accounting serialized. On terminal
                    # failure, release before yield: the coordinator may never
                    # resume this stream, and queued calls still need the lock.
                    async with self.journal._lock:
                        async for chunk in chunks:
                            if chunk.state in {
                                ToolResultState.ERROR, ToolResultState.INTERRUPTED,
                                ToolResultState.DENIED,
                            }:
                                break
                            yield chunk
                        else:
                            return
                    yield chunk

        return stream()
