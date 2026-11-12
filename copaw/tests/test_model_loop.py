"""Isolated model review, real Scroll compression, and ReAct ordering."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.event import (
    ConfirmResult,
    ExternalExecutionResultEvent,
    ToolResultEndEvent,
    UserConfirmResultEvent,
)
from agentscope.message import (
    Msg,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
)
from agentscope.model import ChatResponse
from agentscope.tool import FunctionTool, ToolChunk

from qwenpaw.agents.context.failure_middleware import is_failure_hint
from qwenpaw.loop.failure_config import FailureLoopConfig
from qwenpaw.loop.gates.failure_loop import FailureLoopGate
from qwenpaw.loop.model_loop import ADOPTION_NOTICE, REVIEW_PROMPT

from test_failure_loop import make_agent, request_with_hint


class ReviewModel:
    def __init__(self, context=16000, output=8000):
        self.model = "same-as-react"
        self.context_size = context
        self.parameters = SimpleNamespace(max_tokens=output, temperature=0.37)
        self.requests = []
        self.responses = []
        self.review_response = {"has_issue": False, "findings": []}
        self.review_callback = None
        self.stream_review = False

    async def count_tokens(self, messages, tools):
        size = len(json.dumps(tools).encode()) if tools else 0
        for msg in messages:
            for block in msg.content:
                if isinstance(block, TextBlock):
                    size += len(block.text.encode())
                elif isinstance(block, ToolCallBlock):
                    size += len(block.input.encode())
        return (size + 3) // 4

    async def __call__(self, messages, tools=None, tool_choice=None, **kwargs):
        review = messages[0].get_text_content() == REVIEW_PROMPT
        self.requests.append(
            {
                "review": review,
                "messages": [msg.model_copy(deep=True) for msg in messages],
                "tools": tools,
                "tool_choice": tool_choice,
            }
        )
        if review:
            if self.review_callback:
                value = await self.review_callback()
            else:
                value = self.review_response
            if isinstance(value, BaseException):
                raise value
            text = value if isinstance(value, str) else json.dumps(value)
            if self.stream_review:

                async def chunks():
                    for part in (text[:10], text):
                        yield ChatResponse(
                            content=[TextBlock(type="text", text=part)],
                            is_last=True,
                        )

                return chunks()
            return ChatResponse(
                content=[TextBlock(type="text", text=text)], is_last=True
            )
        if self.responses:
            return self.responses.pop(0)
        return ChatResponse(
            content=[TextBlock(type="text", text="done")], is_last=True
        )


def finding(kind="repeat", evidence=None):
    return {
        "has_issue": True,
        "findings": [
            {
                "kind": kind,
                "evidence": evidence or [1, 2],
                "reason": "The same target and unchanged results were inspected twice.",
                "guidance": "Check whether new evidence is needed before another attempt.",
            }
        ],
    }


@pytest.fixture
def review_agent(monkeypatch, tmp_path):
    gate = FailureLoopGate(
        FailureLoopConfig(enabled=True, check_interval=1),
        history_path=tmp_path / "review.db",
    )
    agent = make_agent(monkeypatch, gate)
    agent.model = ReviewModel()
    agent.state.reply_id = "task"
    agent.state.context = [
        Msg(
            name="user",
            role="user",
            content=[
                TextBlock(type="text", text="CURRENT TASK"),
            ],
        )
    ]
    gate.begin_turn("task", "CURRENT TASK")
    yield agent
    gate.close()


def record(
    agent,
    call_id="one",
    *,
    text="unchanged result",
    args=None,
    name="read_file",
    state="success",
    iteration=0,
):
    gate = agent._failure_loop_gate
    reply_id = agent.state.reply_id
    gate.remember_call(
        reply_id,
        call_id,
        name,
        json.dumps(args or {"path": "a.txt"}),
        iteration=iteration,
    )
    gate.append_text(reply_id, call_id, text)
    gate.complete_call(reply_id, call_id, state)


def reviews(agent):
    return [item for item in agent.model.requests if item["review"]]


def test_factory_has_durable_default(monkeypatch, tmp_path):
    from qwenpaw.loop.gates import failure_loop

    monkeypatch.setattr(failure_loop.sys, "platform", "win32")
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path)
    gate = failure_loop.build_failure_loop_gate(
        workspace_dir=None,
        request_context={},
        config=FailureLoopConfig(enabled=True),
    )
    assert gate is not None
    assert gate.config.check_interval == 5
    assert gate.history_path == tmp_path / "failure_loop" / "history.db"
    gate.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [1, 3, 5])
@pytest.mark.parametrize("tools_per_round", [1, 6])
async def test_sdk_review_interval_counts_tool_rounds_and_keeps_all_evidence(
    monkeypatch, tmp_path, interval, tools_per_round
):
    async def inspect_file(path: str) -> ToolChunk:
        """Read the requested target."""
        return ToolChunk(
            content=[TextBlock(type="text", text="result for " + path)],
            state="success",
            is_last=True,
        )

    gate = FailureLoopGate(
        FailureLoopConfig(enabled=True, check_interval=interval),
        history_path=tmp_path / "interval.db",
    )
    agent = make_agent(monkeypatch, gate, [FunctionTool(inspect_file)])
    agent.model = ReviewModel(context=200000, output=8000)
    agent.model.responses = [
        ChatResponse(
            content=[
                ToolCallBlock(
                    id=f"call-{iteration}-{index}",
                    name="inspect_file",
                    input=json.dumps({"path": f"target-{iteration}-{index}"}),
                )
                for index in range(tools_per_round)
            ],
            is_last=True,
        )
        for iteration in range(10)
    ]
    try:
        _ = [
            event
            async for event in agent._reply(
                inputs=Msg(
                    name="user",
                    role="user",
                    content=[TextBlock(type="text", text="inspect targets")],
                )
            )
        ]
        expected_order = []
        for round_number in range(1, 11):
            expected_order.append(False)
            if round_number % interval == 0:
                expected_order.append(True)
        expected_order.append(False)
        assert [
            request["review"] for request in agent.model.requests
        ] == expected_order
        assert [
            json.loads(request["messages"][-1].get_text_content())[
                "completed_records"
            ]
            for request in reviews(agent)
        ] == [
            number * tools_per_round
            for number in range(interval, 11, interval)
        ]
        for request in reviews(agent):
            assert "result for target-0-0" in str(request["messages"])
    finally:
        gate.close()


@pytest.mark.asyncio
async def test_review_interval_survives_resume_before_and_after_a_check(
    review_agent, monkeypatch
):
    agent = review_agent
    config = replace(agent._failure_loop_gate.config, check_interval=5)
    agent._failure_loop_gate.config = config
    path = agent._failure_loop_gate.history_path
    for index in range(3):
        record(agent, str(index), iteration=index)
        await agent._inject_failure_recovery()
    # Text-only reasoning and a repeated event do not create tool rounds.
    agent.state.cur_iter += 100
    agent._failure_loop_gate.complete_call("task", "2", "success")
    await agent._inject_failure_recovery()
    assert reviews(agent) == []
    agent._failure_loop_gate.close()
    for start, stop in ((3, 5), (5, 10)):
        gate = FailureLoopGate(config, history_path=path)
        agent = make_agent(monkeypatch, gate)
        agent.model = ReviewModel()
        agent.state.reply_id = "task"
        gate.begin_turn("task")
        try:
            for index in range(start, stop):
                record(agent, str(index), iteration=index)
                await agent._inject_failure_recovery()
                assert len(reviews(agent)) == (1 if index == stop - 1 else 0)
            assert gate._state().inspector.count == stop
        finally:
            gate.close()


@pytest.mark.asyncio
async def test_failed_review_waits_another_configured_interval(review_agent):
    agent = review_agent
    gate = agent._failure_loop_gate
    gate.config = replace(gate.config, check_interval=5)
    agent.model_config.max_retries = 0
    agent.model_config.fallback_model = None
    agent.model.review_response = RuntimeError("review unavailable")
    for index in range(10):
        record(agent, str(index), iteration=index)
        await agent._inject_failure_recovery()
        assert len(reviews(agent)) == (index + 1) // 5
    assert await request_with_hint(agent) == agent.state.context


@pytest.mark.asyncio
async def test_review_gets_only_current_tool_evidence_and_same_model_config(
    review_agent,
):
    agent = review_agent
    agent.state.context.insert(
        0,
        Msg(
            name="assistant",
            role="assistant",
            content=[
                TextBlock(
                    type="text",
                    text="OTHER TASK SECRET AND REACT INSTRUCTIONS",
                ),
            ],
        ),
    )
    parameters = agent.model.parameters
    await agent._inject_failure_recovery()
    assert not reviews(agent)  # no completed tools, no review request
    record(agent, "one", text="FIRST RESULT", args={"path": "confirmed.txt"})
    record(agent, "two", text="SECOND RESULT", args={"path": "confirmed.txt"})
    await agent._inject_failure_recovery()
    requests = reviews(agent)
    assert len(requests) == 1
    request = requests[0]
    text = str(request["messages"])
    assert "FIRST RESULT" in text and "SECOND RESULT" in text
    assert "confirmed.txt" in text and "CURRENT TASK" in text
    assert "OTHER TASK SECRET" not in text
    assert request["tools"] == [] and request["tool_choice"].mode == "none"
    assert request["messages"][0].get_text_content() == REVIEW_PROMPT
    assert agent.model.parameters is parameters
    assert parameters.max_tokens == 8000 and parameters.temperature == 0.37
    assert await request_with_hint(agent) == agent.state.context
    await agent._inject_failure_recovery()
    assert len(reviews(agent)) == 1  # same evidence is not checked again


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["repeat", "similar", "cycle"])
async def test_model_findings_produce_optional_transient_guidance(
    review_agent, kind
):
    agent = review_agent
    record(agent, "one")
    record(agent, "two", name="glob_search")
    agent.model.review_response = finding(kind)
    agent.model.stream_review = True
    await agent._inject_failure_recovery()
    messages = await request_with_hint(agent)
    hints = [msg for msg in messages if is_failure_hint(msg)]
    assert len(hints) == 1
    assert ADOPTION_NOTICE in hints[0].get_text_content()
    assert kind in hints[0].get_text_content()
    assert await agent.model.count_tokens(hints, []) <= 512
    assert not any(is_failure_hint(msg) for msg in agent.state.context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "review_error",
    [None, RuntimeError("review API failed"), TimeoutError("review timeout")],
    ids=["guidance", "api-error", "timeout"],
)
async def test_actual_sdk_waits_for_review_before_next_inference(
    monkeypatch, tmp_path, review_error
):
    async def inspect_file(path: str) -> ToolChunk:
        """Read a confirmed file."""
        return ToolChunk(
            content=[TextBlock(type="text", text="same contents")],
            state="success",
            is_last=True,
        )

    gate = FailureLoopGate(
        FailureLoopConfig(enabled=True, check_interval=1),
        history_path=tmp_path / "order.db",
    )
    agent = make_agent(monkeypatch, gate, [FunctionTool(inspect_file)])
    model = ReviewModel()
    agent.model = model
    agent.model_config.max_retries = 0
    agent.model_config.fallback_model = None
    model.responses = [
        ChatResponse(
            content=[
                ToolCallBlock(
                    id=call_id,
                    name="inspect_file",
                    input='{"path":"confirmed.txt"}',
                )
            ],
            is_last=True,
        )
        for call_id in ("one", "two")
    ]

    async def callback():
        if review_error is not None:
            return review_error
        return (
            finding()
            if gate._state().inspector.count == 2
            else {"has_issue": False}
        )

    model.review_callback = callback
    try:
        _ = [
            event
            async for event in agent._reply(
                inputs=Msg(
                    name="user",
                    role="user",
                    content=[TextBlock(type="text", text="inspect")],
                )
            )
        ]
        assert [req["review"] for req in model.requests] == [
            False,
            True,
            False,
            True,
            False,
        ]
        last = model.requests[-1]["messages"]
        assert any(is_failure_hint(msg) for msg in last) is (
            review_error is None
        )
        assert not any(is_failure_hint(msg) for msg in agent.state.context)
    finally:
        gate.close()


@pytest.mark.asyncio
async def test_pending_tool_delays_review(review_agent):
    agent = review_agent
    record(agent, "one")
    record(agent, "two")
    gate = agent._failure_loop_gate
    gate.remember_call("task", "pending", "read_file", "{}")
    await agent._inject_failure_recovery()
    assert reviews(agent) == []
    gate.append_text("task", "pending", "new result")
    gate.complete_call("task", "pending", "success")
    await agent._inject_failure_recovery()
    assert len(reviews(agent)) == 1
    assert gate._state().inspector.count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["result", "pending", "reply", "session", "iteration"]
)
async def test_prepared_guidance_is_rechecked_before_delivery(
    review_agent, monkeypatch, change
):
    agent = review_agent
    record(agent, "one")
    record(agent, "two")
    agent.model.review_response = finding()
    await agent._inject_failure_recovery()
    assert any(is_failure_hint(msg) for msg in await request_with_hint(agent))
    if change == "result":
        record(agent, "corrected", text="progress")
    elif change == "pending":
        agent._failure_loop_gate.remember_call(
            "task", "pending", "read_file", "{}"
        )
    elif change == "reply":
        agent.state.reply_id = "next-task"
    elif change == "session":
        monkeypatch.setattr(
            "qwenpaw.loop.gates.loop_gate._session_id", lambda: "other"
        )
    else:
        agent.state.cur_iter += 1
    assert await request_with_hint(agent) == agent.state.context


@pytest.mark.asyncio
async def test_main_retry_reuses_guidance_and_next_round_does_not_replay(
    review_agent, monkeypatch
):
    agent = review_agent
    record(agent, "one")
    record(agent, "two")
    agent.model.review_response = finding()
    await agent._inject_failure_recovery()
    calls = []
    original_call = ReviewModel.__call__

    async def retry_once(model, messages, **kwargs):
        calls.append([msg for msg in messages if is_failure_hint(msg)])
        if len(calls) == 1:
            raise RuntimeError("temporary main request failure")
        return await original_call(model, messages, **kwargs)

    monkeypatch.setattr(ReviewModel, "__call__", retry_once)
    agent.model_config.max_retries = 1
    await agent._call_model(messages=list(agent.state.context), tools=[])
    assert len(calls) == 2 and calls[0][0] is calls[1][0]
    assert len(reviews(agent)) == 1
    agent.state.cur_iter += 1
    await agent._inject_failure_recovery()
    assert len(reviews(agent)) == 1
    await agent._call_model(messages=list(agent.state.context), tools=[])
    assert calls[-1] == []


@pytest.mark.asyncio
async def test_guidance_budget_keeps_whole_entries_and_adoption_notice(
    review_agent,
):
    agent = review_agent
    record(agent, "one")
    record(agent, "two")
    result = finding()
    result["findings"] = result["findings"] * 3
    agent.model.review_response = result
    await agent._inject_failure_recovery()
    hints = [
        msg for msg in await request_with_hint(agent) if is_failure_hint(msg)
    ]
    assert len(hints) == 1
    assert ADOPTION_NOTICE in hints[0].get_text_content()
    assert await agent.model.count_tokens(hints, []) <= 512
    gate = agent._failure_loop_gate
    gate.config = replace(gate.config, max_hint_tokens=1)
    assert await request_with_hint(agent) == agent.state.context


@pytest.mark.asyncio
async def test_hint_counting_failure_skips_advice_only(
    review_agent, monkeypatch
):
    agent = review_agent
    record(agent, "one")
    record(agent, "two")
    agent.model.review_response = finding()
    await agent._inject_failure_recovery()
    monkeypatch.setattr(
        agent.model,
        "count_tokens",
        AsyncMock(side_effect=ValueError("count failed")),
    )
    assert await request_with_hint(agent) == agent.state.context


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["ratio", "budget"])
async def test_isolated_scroll_uses_both_react_triggers(review_agent, trigger):
    agent = review_agent
    original_context = list(agent.state.context)
    for index in range(12):
        record(agent, str(index), text="result" * 600, iteration=index)
    inspector = agent._failure_loop_gate._state().inspector
    before = await agent.model.count_tokens(
        **(await inspector.view._prepare_model_input())
    )
    agent.model.context_size = int(
        before / (0.85 if trigger == "ratio" else 0.6)
    )
    agent.model.parameters.max_tokens = int(
        agent.model.context_size * (0.05 if trigger == "ratio" else 0.5)
    )
    await agent._inject_failure_recovery()
    assert len(reviews(agent)) == 1
    request = reviews(agent)[0]
    after = await agent.model.count_tokens(request["messages"], [])
    assert after < before
    assert (
        after + agent.model.parameters.max_tokens <= agent.model.context_size
    )
    assert inspector.scroll.last_compress["evicted"] > 0
    assert agent.state.context == original_context
    with sqlite3.connect(inspector.history.path) as reader:
        count = reader.execute(
            "SELECT COUNT(*) FROM conversation_history WHERE session_id=? AND kind='model_turn'",
            (inspector.scope,),
        ).fetchone()[0]
    assert count == 12


@pytest.mark.asyncio
async def test_single_large_result_is_archived_before_shortening(review_agent):
    agent = review_agent
    huge = "HEAD" + "x" * 200000 + "TAIL"
    record(agent, text=huge, args={"path": "p" * 50000})
    await agent._inject_failure_recovery()
    assert len(reviews(agent)) == 1
    request = reviews(agent)[0]
    assert await agent.model.count_tokens(request["messages"], []) <= 8000
    assert "archived" in str(request["messages"])
    inspector = agent._failure_loop_gate._state().inspector
    with sqlite3.connect(inspector.history.path) as reader:
        raw = reader.execute(
            "SELECT content FROM conversation_history WHERE session_id=? AND kind='model_turn'",
            (inspector.scope,),
        ).fetchone()[0]
    assert huge in raw and "p" * 50000 in raw


@pytest.mark.asyncio
async def test_restarted_agent_restores_only_the_same_task(
    monkeypatch, tmp_path
):
    path = tmp_path / "shared.db"
    for session, reply, marker in (
        ("s", "task", "OWN"),
        ("s", "other", "OTHER TASK"),
        ("other", "task", "OTHER SESSION"),
    ):
        inspector_gate = FailureLoopGate(
            FailureLoopConfig(enabled=True, check_interval=1),
            history_path=path,
        )
        monkeypatch.setattr(inspector_gate, "_session_id", lambda: session)
        inspector_gate.begin_turn(reply)
        inspector_gate.remember_call(reply, marker, "read_file", "{}")
        inspector_gate.append_text(reply, marker, marker)
        inspector_gate.complete_call(reply, marker, "success")
        inspector_gate.close()
    gate = FailureLoopGate(
        FailureLoopConfig(enabled=True, check_interval=1),
        history_path=path,
    )
    monkeypatch.setattr(gate, "_session_id", lambda: "s")
    gate.begin_turn("task")
    agent = make_agent(monkeypatch, gate)
    agent.state.reply_id = "task"
    agent.model = ReviewModel()
    try:
        record(agent, "new", text="NEW OWN")
        await agent._inject_failure_recovery()
        text = str(reviews(agent)[0]["messages"])
        assert "OWN" in text and "NEW OWN" in text
        assert "OTHER TASK" not in text and "OTHER SESSION" not in text
        assert gate._state().inspector.count == 2
    finally:
        gate.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["denied", "external"])
async def test_fresh_sdk_resume_reviews_saved_records_and_actual_result(
    review_agent, monkeypatch, kind
):
    previous = review_agent
    record(previous, "previous", text="previous completed result")
    path = previous._failure_loop_gate.history_path
    previous._failure_loop_gate.close()
    gate = FailureLoopGate(
        FailureLoopConfig(enabled=True, check_interval=1), history_path=path
    )
    agent = make_agent(monkeypatch, gate)
    agent.model = ReviewModel()
    agent.model.review_response = finding()
    agent.state.reply_id = "task"
    call = ToolCallBlock(
        id="resumed",
        name="read_file",
        input='{"path":"confirmed.txt"}',
        state=(
            ToolCallState.ASKING
            if kind == "denied"
            else ToolCallState.SUBMITTED
        ),
    )
    agent.state.context = [
        Msg(
            id="task",
            name="test",
            role="assistant",
            content=[
                ToolResultBlock(
                    id="old",
                    name="read_file",
                    output="DO NOT REPLAY OLD MAIN HISTORY",
                    state="error",
                ),
                call,
            ],
        )
    ]
    if kind == "denied":
        event = UserConfirmResultEvent(
            reply_id="task",
            confirm_results=[ConfirmResult(confirmed=False, tool_call=call)],
        )
    else:
        event = ExternalExecutionResultEvent(
            reply_id="task",
            execution_results=[
                ToolResultBlock(
                    id="resumed",
                    name="read_file",
                    output="actual external error result",
                    state="error",
                )
            ],
        )
    try:
        events = [result async for result in agent._reply(inputs=event)]
        assert (
            sum(isinstance(result, ToolResultEndEvent) for result in events)
            == 1
        )
        assert gate._state().inspector.count == 2
        requests = reviews(agent)
        assert len(requests) == 1
        text = str(requests[0]["messages"])
        assert "previous completed result" in text and "resumed" in text
        assert "DO NOT REPLAY OLD MAIN HISTORY" not in text
        assert [request["review"] for request in agent.model.requests] == [
            True,
            False,
        ]
        assert any(
            is_failure_hint(msg)
            for msg in agent.model.requests[-1]["messages"]
        )
    finally:
        gate.close()


@pytest.mark.asyncio
async def test_real_validation_failure_is_reviewed_without_local_classification(
    review_agent, monkeypatch
):
    executed = []

    async def inspect_file(path: str) -> ToolChunk:
        """Read a required path."""
        executed.append(path)
        return ToolChunk(content=[], is_last=True)

    gate = review_agent._failure_loop_gate
    agent = make_agent(monkeypatch, gate, [FunctionTool(inspect_file)])
    agent.model = ReviewModel()
    agent.model.responses = [
        ChatResponse(
            content=[
                ToolCallBlock(id="invalid", name="inspect_file", input="{}")
            ],
            is_last=True,
        )
    ]
    _ = [
        event
        async for event in agent._reply(
            inputs=Msg(
                name="user",
                role="user",
                content=[TextBlock(type="text", text="inspect")],
            )
        )
    ]
    assert executed == []
    assert [request["review"] for request in agent.model.requests] == [
        False,
        True,
        False,
    ]
    result = gate._state().inspector._latest[1]
    assert result["arguments"] == {} and result["state"] == "error"
    assert "path" in result["text"]


@pytest.mark.asyncio
async def test_stale_review_is_discarded_when_observations_change(
    review_agent,
):
    agent = review_agent
    record(agent, "one")
    record(agent, "two")

    async def callback():
        record(agent, "new-progress", text="changed result")
        return finding()

    agent.model.review_callback = callback
    await agent._inject_failure_recovery()
    assert await request_with_hint(agent) == agent.state.context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    ["not JSON", finding(evidence=[1, 999]), finding(evidence=[1, 1])],
    ids=["invalid-json", "invented-evidence", "one-record"],
)
async def test_invalid_or_unfounded_findings_are_not_injected(
    review_agent, response
):
    agent = review_agent
    record(agent, "one")
    record(agent, "two")
    agent.model.review_response = response
    await agent._inject_failure_recovery()
    assert await request_with_hint(agent) == agent.state.context


@pytest.mark.asyncio
async def test_same_retry_and_fallback_settings_are_used(review_agent):
    agent = review_agent
    record(agent, "one")
    record(agent, "two")
    agent.model.review_response = RuntimeError("temporary review failure")
    fallback = ReviewModel(context=10000, output=6000)
    fallback.review_response = finding("cycle")
    agent.model_config.max_retries = 1
    agent.model_config.fallback_model = fallback
    await agent._inject_failure_recovery()
    assert len(reviews(agent)) == 2
    assert len(fallback.requests) == 1
    assert "Possible cycle" in str(await request_with_hint(agent))
    assert agent.model.parameters.max_tokens == 8000
    assert fallback.parameters.max_tokens == 6000


@pytest.mark.asyncio
async def test_cancellation_propagates_instead_of_sending_late_guidance(
    review_agent,
):
    agent = review_agent
    record(agent)
    agent.model.review_response = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await agent._inject_failure_recovery()


@pytest.mark.asyncio
async def test_disabled_compression_never_sends_oversized_review(review_agent):
    agent = review_agent
    agent._agent_config.running = SimpleNamespace(
        light_context_config=SimpleNamespace(
            context_compact_config=SimpleNamespace(enabled=False),
        )
    )
    record(agent, text="x" * 200000)
    await agent._inject_failure_recovery()
    assert reviews(agent) == []
    inspector = agent._failure_loop_gate._state().inspector
    assert inspector.scroll.last_compress == {
        "evicted": 0,
        "compacted": 0,
        "folded": 0,
    }
    assert await request_with_hint(agent) == agent.state.context


@pytest.mark.asyncio
async def test_failed_archive_write_does_not_drop_data_or_send_overflow(
    review_agent,
    monkeypatch,
):
    agent = review_agent
    record(agent, text="x" * 200000)
    inspector = agent._failure_loop_gate._state().inspector
    monkeypatch.setattr(
        inspector.scroll, "_persist_guarded", lambda view: False
    )
    await agent._inject_failure_recovery()
    assert reviews(agent) == []
    assert "x" * 200000 in inspector.view.state.context[-1].get_text_content()


@pytest.mark.asyncio
async def test_all_completed_tools_are_reviewed(
    review_agent,
):
    agent = review_agent
    gate = agent._failure_loop_gate
    for index in range(5):
        record(agent, str(index), name="glob_search", text="No matching files")
    await agent._inject_failure_recovery()
    assert gate._state().inspector.count == 5
    assert not gate._state().suspended
    assert len(reviews(agent)) == 1


@pytest.mark.asyncio
async def test_media_payload_is_archived_but_not_mistaken_for_visual_evidence(
    review_agent,
):
    agent = review_agent
    gate = agent._failure_loop_gate
    gate.remember_call("task", "image", "desktop_screenshot", "{}")
    gate.note_data(
        "task",
        "image",
        {
            "media_type": "image/png",
            "data": "SECRET_BINARY_PAYLOAD",
            "url": None,
        },
    )
    gate.complete_call("task", "image", "success")
    await agent._inject_failure_recovery()
    text = str(reviews(agent)[0]["messages"])
    assert "SECRET_BINARY_PAYLOAD" not in text
    assert "image/png" in text and "non_text_content_not_analyzed" in text
    inspector = gate._state().inspector
    with sqlite3.connect(inspector.history.path) as reader:
        raw = reader.execute(
            "SELECT content FROM conversation_history WHERE session_id=? AND kind='model_turn'",
            (inspector.scope,),
        ).fetchone()[0]
    assert "SECRET_BINARY_PAYLOAD" in raw


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["reply", "session"])
async def test_review_arriving_after_task_switch_is_discarded(
    review_agent, monkeypatch, boundary
):
    agent = review_agent
    record(agent, "one")
    record(agent, "two")

    async def callback():
        if boundary == "reply":
            agent.state.reply_id = "new-task"
            agent._failure_loop_gate.begin_turn("new-task")
        else:
            monkeypatch.setattr(
                agent._failure_loop_gate,
                "_session_id",
                lambda: "other-session",
            )
        return finding()

    agent.model.review_callback = callback
    await agent._inject_failure_recovery()
    assert await request_with_hint(agent) == agent.state.context


@pytest.mark.asyncio
async def test_parallel_tools_finish_before_one_review(monkeypatch, tmp_path):
    entered = set()
    barrier = asyncio.Event()

    async def inspect_target(target: str) -> ToolChunk:
        """Inspect an independent target."""
        entered.add(target)
        if len(entered) == 2:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), timeout=3)
        return ToolChunk(
            content=[TextBlock(type="text", text=target)],
            state="success",
            is_last=True,
        )

    gate = FailureLoopGate(
        FailureLoopConfig(enabled=True, check_interval=1),
        history_path=tmp_path / "parallel.db",
    )
    agent = make_agent(monkeypatch, gate, [FunctionTool(inspect_target)])
    agent.model = ReviewModel()
    agent.model.responses = [
        ChatResponse(
            content=[
                ToolCallBlock(
                    id=target,
                    name="inspect_target",
                    input=json.dumps({"target": target}),
                )
                for target in ("a", "b")
            ],
            is_last=True,
        )
    ]
    try:
        _ = [
            event
            async for event in agent._reply(
                inputs=Msg(
                    name="user",
                    role="user",
                    content=[TextBlock(type="text", text="inspect both")],
                )
            )
        ]
        assert entered == {"a", "b"}
        assert [req["review"] for req in agent.model.requests] == [
            False,
            True,
            False,
        ]
        assert gate._state().inspector.count == 2
        assert '"iteration": 0' in str(reviews(agent)[0]["messages"])
    finally:
        gate.close()
