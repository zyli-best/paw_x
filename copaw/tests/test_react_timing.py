"""Timing must preserve streaming, tool concurrency, and error behaviour."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from agentscope.message import Msg, TextBlock, ThinkingBlock, ToolCallBlock
from agentscope.model import ChatResponse
from agentscope.tool import FunctionTool, ToolChunk

from qwenpaw.agents import react_timing
from test_failure_loop import entry_functions, make_agent
from test_model_loop import ReviewModel


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(react_timing, "perf_counter", clock)
    return clock


def timing_logs(caplog):
    return [
        item.getMessage() for item in caplog.records
        if "MainModel timing " in item.getMessage()
        or "ReAct timing " in item.getMessage()
    ]


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False), ("1", True), ("0", False)])
def test_entry_switch_and_cli_forwarding(monkeypatch, value, expected):
    import os

    monkeypatch.setattr(os, "environ", {react_timing.TIMING_ENV: value})
    entry = entry_functions()
    assert entry["parse_entry_cli_args"](["--reload"]) == ["--reload"]
    assert react_timing.timing_enabled() is expected
    entry["parse_entry_cli_args"](["--react-timing-enabled", str(not expected).lower()])
    assert react_timing.timing_enabled() is not expected
    entry["parse_entry_cli_args"](["--qwenpaw-react-timing-enabled", value])
    assert react_timing.timing_enabled() is expected


def event(kind, identity="block"):
    return SimpleNamespace(type=kind, block_id=identity, tool_call_id=identity)


def test_reasoning_wait_thinking_and_overlapping_tool_arguments(clock, caplog):
    with caplog.at_level(logging.INFO):
        timer = react_timing.ReasoningTiming(("session", "reply", 0))
        clock.now = 2
        timer.observe(event("THINKING_BLOCK_START"))
        clock.now = 5
        timer.observe(event("THINKING_BLOCK_END"))
        timer.observe(event("TEXT_BLOCK_START"))
        clock.now = 7
        timer.observe(event("TEXT_BLOCK_END"))
        timer.observe(event("TOOL_CALL_START", "a"))
        clock.now = 8
        timer.observe(event("TOOL_CALL_START", "b"))
        clock.now = 9
        timer.observe(event("TOOL_CALL_END", "a"))
        clock.now = 10
        timer.observe(event("TOOL_CALL_END", "b"))
        timer.finish("completed")
    logs = timing_logs(caplog)
    summary = next(line for line in logs if "stage=round_end" in line)
    assert "first_output_ms=2000.0 elapsed_ms=10000.0" in summary
    assert "thinking_stream_ms=3000.0 thinking_blocks=1" in summary
    assert "text_stream_ms=2000.0 tool_args_stream_ms=3000.0" in summary
    assert sum("stage=thinking_end" in line for line in logs) == 1


@pytest.mark.parametrize("thinking", [False, True])
def test_hidden_thinking_is_unavailable_and_open_span_closes_on_cancel(clock, caplog, thinking):
    with caplog.at_level(logging.INFO):
        timer = react_timing.ReasoningTiming(("session", "reply", 0))
        if thinking:
            clock.now = 1
            timer.observe(event("THINKING_BLOCK_START"))
        clock.now = 3
        timer.finish("CancelledError")
    logs = "\n".join(timing_logs(caplog))
    assert "status=CancelledError" in logs
    assert f"thinking_stream_ms={'2000.0' if thinking else 'unavailable'}" in logs


async def test_real_thinking_stream_has_separate_wait_and_output_times(monkeypatch, clock, caplog):
    monkeypatch.setenv(react_timing.TIMING_ENV, "true")

    async def chunks():
        clock.now = 2
        yield ChatResponse(content=[ThinkingBlock(thinking="PRIVATE THOUGHT")], is_last=False)
        clock.now = 5
        yield ChatResponse(content=[TextBlock(text="done")], is_last=False)
        clock.now = 7
        yield ChatResponse(content=[
            ThinkingBlock(thinking="PRIVATE THOUGHT"), TextBlock(text="done"),
        ], is_last=True)

    agent = make_agent(monkeypatch, None)
    agent.model = ReviewModel()
    agent.model.responses = [chunks()]
    with caplog.at_level(logging.INFO):
        events = [evt async for evt in agent._reasoning_impl()]
    logs = "\n".join(timing_logs(caplog))
    assert "first_output_ms=2000.0 elapsed_ms=7000.0" in logs
    assert "thinking_stream_ms=3000.0 thinking_blocks=1 text_stream_ms=2000.0" in logs
    assert "PRIVATE" not in logs
    assert any(getattr(evt, "delta", None) == "done" for evt in events)


async def run_tool_round(monkeypatch, clock, *, parallel, enabled):
    monkeypatch.setenv(react_timing.TIMING_ENV, str(enabled).lower())
    count = 0
    both_started = asyncio.Event()

    async def tool_result():
        nonlocal count
        if parallel:
            count += 1
            if count == 2:
                both_started.set()
            await both_started.wait()
            clock.now = 2
        else:
            clock.now += 1
        return ToolChunk(content=[TextBlock(text="PRIVATE TOOL RESULT")])

    async def write():
        """Write a result without touching the filesystem."""
        return await tool_result()

    async def read():
        """Read a result without touching the filesystem."""
        return await tool_result()

    agent = make_agent(monkeypatch, None, tools=[
        FunctionTool(write, is_concurrency_safe=parallel),
        FunctionTool(read, is_concurrency_safe=parallel),
    ])
    agent.model = ReviewModel()
    agent.model.responses = [
        ChatResponse(content=[
            ToolCallBlock(id="write-call", name="write", input="{}"),
            ToolCallBlock(id="read-call", name="read", input="{}"),
        ], is_last=True),
        ChatResponse(content=[TextBlock(text="done")], is_last=True),
    ]
    events = [event async for event in agent.reply_stream(inputs=Msg(
        name="user", role="user", content=[TextBlock(text="PRIVATE TASK")],
    ))]
    return agent, events


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
async def test_real_react_round_times_tools_and_act_without_changing_execution(
    monkeypatch, clock, caplog, parallel, enabled,
):
    with caplog.at_level(logging.INFO):
        agent, events = await run_tool_round(
            monkeypatch, clock, parallel=parallel, enabled=enabled,
        )
    assert len(agent.model.requests) == 2
    assert any(getattr(evt, "delta", None) == "done" for evt in events)
    logs = timing_logs(caplog)
    if not enabled:
        assert not logs
        return
    assert "PRIVATE" not in "\n".join(logs)
    ends = [line for line in logs if "stage=tool_end" in line]
    assert len(ends) == 2
    for name in ("write", "read"):
        line = next(line for line in ends if f"tool={name} " in line)
        assert "status=success" in line
        assert f"elapsed_ms={2000.0 if parallel else 1000.0:.1f}" in line
    act = [line for line in logs if "stage=act_end" in line]
    assert len(act) == 1
    assert "status=completed tool_count=2 finished_calls=2 elapsed_ms=2000.0" in act[0]


async def test_tool_error_is_reported_once_and_still_reaches_model(monkeypatch, caplog):
    monkeypatch.setenv(react_timing.TIMING_ENV, "true")

    async def write():
        """Fail a write operation."""
        raise RuntimeError("PRIVATE FAILURE")

    agent = make_agent(monkeypatch, None, tools=[FunctionTool(write)])
    agent.model = ReviewModel()
    agent.model.responses = [
        ChatResponse(content=[ToolCallBlock(id="write-call", name="write", input="{}")], is_last=True),
        ChatResponse(content=[TextBlock(text="done")], is_last=True),
    ]
    with caplog.at_level(logging.INFO):
        _ = [evt async for evt in agent.reply_stream(inputs=Msg(
            name="user", role="user", content=[TextBlock(text="task")],
        ))]
    logs = timing_logs(caplog)
    ends = [line for line in logs if "stage=tool_end" in line]
    assert len(ends) == 1 and "status=error" in ends[0]
    assert "status=error" in next(line for line in logs if "stage=act_end" in line)
    assert "PRIVATE" not in "\n".join(logs)
    assert len(agent.model.requests) == 2


async def test_cancelled_tool_still_logs_its_duration(monkeypatch, caplog):
    monkeypatch.setenv(react_timing.TIMING_ENV, "true")
    started = asyncio.Event()

    async def write():
        """Wait until the test cancels this call."""
        started.set()
        await asyncio.Event().wait()

    agent = make_agent(monkeypatch, None, tools=[FunctionTool(write)])

    async def consume():
        return [item async for item in agent._acting_impl(
            ToolCallBlock(id="cancel-call", name="write", input="{}"),
        )]

    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    ends = [line for line in timing_logs(caplog) if "stage=tool_end" in line]
    assert len(ends) == 1
    assert "elapsed_ms=" in ends[0]
    assert "status=interrupted" in ends[0] or "status=cancelled" in ends[0]


async def test_one_act_spans_multiple_execution_batches(monkeypatch, clock, caplog):
    monkeypatch.setenv(react_timing.TIMING_ENV, "true")

    async def read():
        """Return a read result."""
        clock.now += 1
        return ToolChunk(content=[TextBlock(text="read")])

    async def write():
        """Return a write result."""
        clock.now += 1
        return ToolChunk(content=[TextBlock(text="written")])

    agent = make_agent(monkeypatch, None, tools=[
        FunctionTool(read, is_concurrency_safe=True),
        FunctionTool(write, is_concurrency_safe=False),
    ])
    agent.model = ReviewModel()
    agent.model.responses = [
        ChatResponse(content=[
            ToolCallBlock(id=str(i), name=name, input="{}")
            for i, name in enumerate(("read", "write", "read"))
        ], is_last=True),
        ChatResponse(content=[TextBlock(text="done")], is_last=True),
    ]
    with caplog.at_level(logging.INFO):
        _ = [evt async for evt in agent.reply_stream(inputs=Msg(
            name="user", role="user", content=[TextBlock(text="task")],
        ))]
    ends = [line for line in timing_logs(caplog) if "stage=act_end" in line]
    assert len(ends) == 1
    assert "tool_count=3 finished_calls=3 elapsed_ms=3000.0" in ends[0]


async def test_background_tool_logs_actual_end_after_act_returns(monkeypatch, clock, caplog):
    from qwenpaw.tool_calls import ToolCoordinator, ToolCoordinatorMiddleware

    monkeypatch.setenv(react_timing.TIMING_ENV, "true")
    release = asyncio.Event()
    coordinator = ToolCoordinator(default_timeout_secs=0.01)

    async def write():
        """Finish only when the test releases the background call."""
        await release.wait()
        return ToolChunk(content=[TextBlock(text="written")])

    agent = make_agent(monkeypatch, None, tools=[FunctionTool(write)])
    agent._acting_middlewares = [ToolCoordinatorMiddleware(coordinator)]
    agent.model = ReviewModel()
    agent.model.responses = [
        ChatResponse(content=[ToolCallBlock(id="background", name="write", input="{}")], is_last=True),
        ChatResponse(content=[TextBlock(text="done")], is_last=True),
    ]
    try:
        with caplog.at_level(logging.INFO):
            _ = [evt async for evt in agent.reply_stream(inputs=Msg(
                name="user", role="user", content=[TextBlock(text="task")],
            ))]
            logs = timing_logs(caplog)
            assert any("stage=act_end" in line and "status=offloaded" in line for line in logs)
            assert not any("stage=tool_end" in line for line in logs)
            entry = coordinator.get("background")
            clock.now = 3
            release.set()
            await entry.background_task
    finally:
        release.set()
        await coordinator.shutdown()
    ends = [line for line in timing_logs(caplog) if "stage=tool_end" in line]
    assert len(ends) == 1
    assert "tool=write" in ends[0] and "elapsed_ms=3000.0" in ends[0]
