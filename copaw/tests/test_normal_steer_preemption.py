"""Ordinary steering uses real SDK boundaries and provider wire formatters."""

import asyncio
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest
from agentscope.formatter import OpenAIChatFormatter, OpenAIResponseFormatter
from agentscope.message import (
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agentscope.model import ChatResponse
from agentscope.tool import FunctionTool, ToolResponse
from openai.types.responses import ResponseReasoningItem, ResponseFunctionToolCall

from qwenpaw.agents.model_factory import _create_file_block_support_formatter
from qwenpaw.runtime.normal_preemption import (
    normal_scope,
    SteerPreempted,
    NormalModelCallMiddleware,
)
from qwenpaw.runtime.normal_protocol import REPLAY_KEY, export_view
from qwenpaw.runtime.session_views import restore_session_view
from qwenpaw.runtime.steering import SteeringState
from qwenpaw.tool_calls import ToolCoordinator
from test_edge_cloud import Model, text, clean_capture
from test_edge_cloud_steering import run
from test_steering_output import runtime_setup, contents


def state():
    result = SteeringState("response", "default", "run", "chat")
    result.phase = "running"
    return result


@pytest.mark.parametrize("boundary", ["thinking", "text", "arguments"])
async def test_partial_attempt_is_display_only_then_both_guides_reach_next_request(
    monkeypatch, tmp_path, boundary
):
    closed = asyncio.Event()
    executed = []

    async def dangerous():
        executed.append(True)
        return ToolResponse(content=[TextBlock(text="real result")])

    class Streaming(Model):
        async def __call__(self, messages, **kwargs):
            await super().__call__(messages, **kwargs)
            if len(self.requests) > 1:
                assert closed.is_set(), "old model stream must close before restart"
                return text("corrected answer")

            async def chunks():
                try:
                    yield ChatResponse(
                        content=[ThinkingBlock(thinking="old thought")], is_last=False
                    )
                    if boundary == "text":
                        yield ChatResponse(
                            content=[TextBlock(text="old partial text")], is_last=False
                        )
                    elif boundary == "arguments":
                        yield ChatResponse(
                            content=[
                                ToolCallBlock(
                                    id="old-call", name="dangerous", input='{"x":'
                                )
                            ],
                            is_last=False,
                        )
                    await asyncio.Event().wait()
                finally:
                    closed.set()

            return chunks()

    model = Streaming("qwen")
    result = runtime_setup(
        monkeypatch,
        tmp_path,
        model,
        model,
        collaboration=False,
        recording=True,
        tracing=True,
        tools=[FunctionTool(dangerous)],
    )
    ctx, _, tracker, session, uploads = result
    sent = False

    async def on_event(event):
        nonlocal sent
        if sent or event.object != "content":
            return
        target = (
            (boundary == "thinking" and "old thought" in getattr(event, "text", ""))
            or (boundary == "text" and "old partial text" in getattr(event, "text", ""))
            or (
                boundary == "arguments"
                and getattr(event, "data", {}).get("arguments") == '{"x":'
            )
        )
        if target:
            sent = True
            await tracker.steer("chat", "client-run", "a", "keep original task")
            await tracker.steer("chat", "client-run", "b", "use corrected directory")
            # A retry is a receipt lookup, not a third preemption.
            await tracker.steer("chat", "client-run", "b", "use corrected directory")

    events = await run(result, on_event=on_event)
    assert sent and closed.is_set() and not executed
    assert len(model.requests) == 2
    latest = model.requests[-1][0]
    assert [
        (m.metadata or {}).get("message_id")
        for m in latest
        if (m.metadata or {}).get("qwenpaw_message_kind") == "user_steering"
    ] == ["a", "b"]
    assert "old thought" not in str([m.model_dump() for m in latest])
    assert "old-call" not in str([m.model_dump() for m in latest])
    interrupted = [
        e
        for e in events
        if e.object == "message" and (e.metadata or {}).get("steer_preempted")
    ]
    assert interrupted and any(e.status == "cancelled" for e in interrupted)
    final = [e for e in events if e.object == "response"][-1]
    assert final.status == "completed"
    assert (
        next(m for m in final.output if "corrected answer" in contents(m)).metadata[
            "steer_message_id"
        ]
        == "b"
    )
    saved = session.saved["state"]["context"]
    assert any((m.get("metadata") or {}).get("steer_preempted") for m in saved)
    restored = restore_session_view(SimpleNamespace(extras={}), deepcopy(session.saved))
    assert not any(
        (m.get("metadata") or {}).get("steer_preempted")
        for m in restored["state"]["context"]
    )
    assert any(
        (m.get("metadata") or {}).get("steer_preempted") for m in uploads[0]["delta"]
    )
    assert [r["status"] for r in uploads[0]["model_requests"]] == [
        "steered",
        "completed",
    ]
    assert uploads[0]["model_requests"][-1]["metadata"]["steer_message_id"] == "b"
    assert "qwenpaw_session_context_view" not in str(uploads[0])
    from qwenpaw.observability.session_delta_redactor import (
        redact_session_delta_payload,
    )

    redacted = redact_session_delta_payload(
        uploads[0],
        sensitive_words={"patterns": [{"type": "custom", "keyword": "old thought"}]},
    )
    assert "old thought" not in str(redacted)
    assert ctx.extras["normal_steering"].latest_sequence == 2


async def test_executed_a_keeps_real_result_b_is_skipped_before_dispatch(
    monkeypatch, tmp_path
):
    executed = []

    async def a():
        executed.append("a")
        await tracker.steer("chat", "client-run", "fix", "skip remaining old tools")
        return ToolResponse(content=[TextBlock(text="A actually completed")])

    async def b():
        executed.append("b")
        return ToolResponse(content=[TextBlock(text="B actually completed")])

    old = ChatResponse(
        content=[
            ThinkingBlock(thinking="complete original reasoning"),
            ToolCallBlock(id="a-id", name="a", input="{}"),
            ToolCallBlock(id="b-id", name="b", input="{}"),
        ],
        is_last=True,
    )
    model = Model("deepseek", [old, text("corrected")])
    result = runtime_setup(
        monkeypatch,
        tmp_path,
        model,
        model,
        collaboration=False,
        tools=[FunctionTool(a), FunctionTool(b)],
    )
    _, _, tracker, _, _ = result
    await run(result)
    assert executed == ["a"]
    blocks = [block for m in model.requests[-1][0] for block in m.content]
    results = {block.id: block for block in blocks if block.type == "tool_result"}
    assert "A actually completed" in str(results["a-id"].output)
    assert results["b-id"].state == "denied"
    assert results["b-id"].metadata["steer_superseded"] is True
    assert any(
        block.type == "thinking" and block.thinking == "complete original reasoning"
        for block in blocks
    )


@pytest.mark.parametrize("yield_on_steer", [False, True])
async def test_running_tool_waits_or_yields_without_cancelling_and_keeps_origin(
    yield_on_steer,
):
    controller = state()
    controller.tool_origins["tool"] = {
        "sequence": 0,
        "steer_message_id": None,
        "run_id": "run",
    }
    coordinator = ToolCoordinator(default_timeout_secs=60)
    started, release = asyncio.Event(), asyncio.Event()

    async def handler(**_):
        started.set()
        await release.wait()
        yield ToolResponse(id="tool", content=[TextBlock(text="real result")])

    async def collect():
        return [
            item
            async for item in coordinator.execute(
                ToolCallBlock(id="tool", name="test", input="{}"),
                handler,
                session_id="session",
                agent_id="default",
                root_session_id="session",
                normal_steering=controller,
                yield_on_steer=yield_on_steer,
            )
        ]

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(started.wait(), 2)
        entry = coordinator.get("tool")
        deadline = entry.ctx.deadline
        controller.enqueue("fix", "change requirement")
        if yield_on_steer:
            output = await asyncio.wait_for(asyncio.shield(task), 2)
            assert output[-1].metadata["offloaded"] is True
            assert entry.ctx.deadline == deadline and not entry.ctx.is_cancelled
            assert not entry.background_task.done()
            release.set()
            await asyncio.wait_for(entry.background_task, 2)
            # Wait on the completion notification, not a wall-clock timeout.
            for _ in range(20):
                hints = await coordinator.pop_pending_hints("session")
                if hints:
                    break
                await asyncio.sleep(0)
            assert hints and hints[0].metadata["steer_message_id"] is None
            assert "real result" in str(hints[0].content)
        else:
            await asyncio.sleep(0)
            assert not task.done() and not entry.ctx.is_cancelled
            release.set()
            output = await asyncio.wait_for(task, 2)
            assert "real result" in str(output[-1].content)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("model_name", ["qwen3", "deepseek-reasoner", "gpt-4.1"])
async def test_chat_wire_preserves_each_completed_reasoning_generation(model_name):
    controller = state()
    controller.model_name = model_name
    history = [
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ThinkingBlock(thinking="reason A"),
                ToolCallBlock(id="a", name="a", input="{}"),
                ToolResultBlock(id="a", name="a", output="actual A"),
                ThinkingBlock(thinking="reason B"),
                ToolCallBlock(id="b", name="b", input="{}"),
                ToolResultBlock(id="b", name="b", output="actual B"),
            ],
        ),
        Msg(name="user", role="user", content=[TextBlock(text="correction")]),
    ]
    before = deepcopy(history)
    formatter = _create_file_block_support_formatter(OpenAIChatFormatter)()
    with normal_scope(controller):
        wire = await formatter.format(history)
    assistants = [m for m in wire if m.get("role") == "assistant"]
    assert len(assistants) == 2
    if model_name.startswith("gpt"):
        assert all("reasoning_content" not in m for m in assistants)
    else:
        assert [m["reasoning_content"] for m in assistants] == ["reason A", "reason B"]
    assert [m["tool_call_id"] for m in wire if m.get("role") == "tool"] == ["a", "b"]
    assert history == before


async def test_responses_replays_completed_encrypted_reasoning_and_excludes_it_from_export():
    from qwenpaw.providers.openai_response_provider import (
        OpenAIResponseModelCompat,
        _normalize_response_input,
    )

    raw = ResponseReasoningItem(
        id="rs_done",
        type="reasoning",
        summary=[],
        encrypted_content="opaque-exact-value",
    )
    response = ChatResponse(
        content=[ToolCallBlock(id="call", name="read", input="{}")], is_last=True
    )
    native_call = ResponseFunctionToolCall(
        type="function_call", call_id="call", name="read", arguments="{}"
    )
    OpenAIResponseModelCompat._retain_reasoning(
        response, SimpleNamespace(output=[raw, native_call])
    )
    history = [
        Msg(
            name="assistant",
            role="assistant",
            content=[
                *response.content,
                ToolResultBlock(id="call", name="read", output="real result"),
            ],
        ),
        Msg(name="user", role="user", content=[TextBlock(text="new guide")]),
    ]
    formatter = _create_file_block_support_formatter(OpenAIResponseFormatter)()
    with normal_scope(state()):
        wire = await formatter.format(history)
    wire = _normalize_response_input(wire)
    assert [m["type"] for m in wire] == [
        "reasoning",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert wire[0] == raw.model_dump(mode="json", exclude_none=True)
    exported = export_view(
        {"delta": [m.model_dump() for m in history], "model_requests": wire}
    )
    assert "opaque-exact-value" not in str(exported) and REPLAY_KEY not in str(exported)


async def test_responses_stream_completion_keeps_reasoning_item_order_and_empty_summaries():
    from qwenpaw.providers.openai_response_provider import OpenAIResponseModelCompat

    raw = [
        ResponseReasoningItem(
            id="rs_a", type="reasoning", summary=[], encrypted_content="opaque-a"
        ),
        ResponseFunctionToolCall(
            type="function_call", call_id="a", name="read", arguments="{}"
        ),
        ResponseReasoningItem(
            id="rs_b",
            type="reasoning",
            summary=[{"type": "summary_text", "text": "second reason"}],
            encrypted_content="opaque-b",
        ),
        ResponseFunctionToolCall(
            type="function_call", call_id="b", name="read", arguments="{}"
        ),
    ]
    completed = SimpleNamespace(output=raw, usage=None)
    closed = asyncio.Event()

    async def events():
        try:
            yield SimpleNamespace(type="response.completed", response=completed)
        finally:
            closed.set()

    model = object.__new__(OpenAIResponseModelCompat)
    with normal_scope(state()):
        chunks = [
            chunk
            async for chunk in model._parse_stream_response(datetime.now(), events())
        ]
    assert closed.is_set() and len(chunks) == 1 and chunks[0].is_last
    blocks = chunks[0].content
    assert [b.type for b in blocks] == [
        "thinking",
        "tool_call",
        "thinking",
        "tool_call",
    ]
    assert [b.reasoning_item_id for b in blocks if b.type == "thinking"] == [
        "rs_a",
        "rs_b",
    ]
    history = [
        Msg(
            name="assistant",
            role="assistant",
            content=[
                *blocks,
                ToolResultBlock(id="a", name="read", output="a done"),
                ToolResultBlock(id="b", name="read", output="b done"),
            ],
        )
    ]
    with normal_scope(state()):
        wire = await _create_file_block_support_formatter(
            OpenAIResponseFormatter
        )().format(history)
    assert [m["type"] for m in wire] == [
        "reasoning",
        "function_call",
        "reasoning",
        "function_call",
        "function_call_output",
        "function_call_output",
    ]
    assert (
        wire[0]["encrypted_content"] == "opaque-a"
        and wire[2]["encrypted_content"] == "opaque-b"
    )


async def test_redacted_responses_summary_cannot_reintroduce_original_replay_text():
    from qwenpaw.providers.openai_response_provider import OpenAIResponseModelCompat
    from qwenpaw.observability.session_delta_redactor import redact_model_protocol_value

    raw = ResponseReasoningItem(
        id="rs",
        type="reasoning",
        summary=[{"type": "summary_text", "text": "secret-value"}],
        encrypted_content="opaque",
    )
    chunk = text("done")
    OpenAIResponseModelCompat._retain_reasoning(chunk, SimpleNamespace(output=[raw]))
    original = [Msg(name="assistant", role="assistant", content=chunk.content)]
    redacted, report = redact_model_protocol_value(
        original,
        sensitive_words={"patterns": [{"type": "custom", "keyword": "secret-value"}]},
    )
    assert report.matched
    with (
        normal_scope(state()),
        pytest.raises(ValueError, match="reasoning was modified"),
    ):
        await _create_file_block_support_formatter(OpenAIResponseFormatter)().format(
            redacted
        )


async def test_guide_during_before_hook_prevents_tool_dispatch():
    controller = state()
    controller.tool_origins["tool"] = {"sequence": 0}
    coordinator = ToolCoordinator()
    entered, release = asyncio.Event(), asyncio.Event()
    executed = []

    async def before(params, _):
        entered.set()
        await release.wait()
        return params

    async def handler(**_):
        executed.append(True)
        yield ToolResponse(id="tool", content=[TextBlock(text="should not execute")])

    coordinator.hooks.register("test", before=before)

    async def collect():
        return [
            item
            async for item in coordinator.execute(
                ToolCallBlock(id="tool", name="test", input="{}"),
                handler,
                session_id="session",
                agent_id="default",
                root_session_id="session",
                normal_steering=controller,
            )
        ]

    task = asyncio.create_task(collect())
    await asyncio.wait_for(entered.wait(), 2)
    controller.enqueue("fix", "skip old tool")
    release.set()
    output = await asyncio.wait_for(task, 2)
    assert not executed and output[-1].state == "denied"
    assert output[-1].metadata["steer_superseded"]


async def test_inflight_request_is_cancelled_but_duplicate_receipt_does_not_preempt_again():
    controller = state()
    entered, closed = asyncio.Event(), asyncio.Event()

    async def handler(**_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    middleware = NormalModelCallMiddleware(controller)
    task = asyncio.create_task(
        middleware.on_model_call(
            None,
            {"messages": [], "current_model": SimpleNamespace(model="qwen")},
            handler,
        )
    )
    await entered.wait()
    controller.enqueue("a", "change")
    with pytest.raises(SteerPreempted):
        await task
    assert closed.is_set()
    guide = Msg(
        name="user",
        role="user",
        content=[TextBlock(text="change")],
        metadata={
            "qwenpaw_message_kind": "user_steering",
            "response_id": "response",
            "sequence": 1,
            "message_id": "a",
        },
    )
    controller.enqueue("a", "change")

    async def complete(**_):
        return text("done")

    result = await middleware.on_model_call(None, {"messages": [guide]}, complete)
    assert result.content[0].text == "done"


async def test_background_completion_does_not_repeat_function_call_output():
    history = [
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ThinkingBlock(thinking="start the tool"),
                ToolCallBlock(id="a", name="shell", input="{}"),
                ToolResultBlock(id="a", name="shell", output="running in background"),
            ],
        ),
        Msg(
            name="system",
            role="assistant",
            content=[
                TextBlock(text="background tool completed"),
                ToolResultBlock(id="a", name="shell", output="actual completion"),
            ],
        ),
    ]
    with normal_scope(state()):
        wire = await _create_file_block_support_formatter(OpenAIChatFormatter)().format(
            history
        )
    assert len([m for m in wire if m.get("role") == "tool"]) == 1
    assert all(m.get("reasoning_content") for m in wire if m.get("role") == "assistant")
    assert wire[-1]["role"] == "user" and "actual completion" in str(
        wire[-1]["content"]
    )
