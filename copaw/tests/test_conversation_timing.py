"""Latency logs distinguish optional review from the original model stream."""

import asyncio
import logging
from dataclasses import replace

import pytest
from agentscope.event import ModelCallStartEvent, TextBlockDeltaEvent
from agentscope.message import Msg, TextBlock
from agentscope.model import ChatResponse

from test_failure_loop import make_agent
from test_model_loop import ReviewModel, record, review_agent


@pytest.fixture(autouse=True)
def enable_timing(monkeypatch):
    monkeypatch.setenv("QWENPAW_REACT_TIMING_ENABLED", "true")


def messages(caplog):
    return [item.getMessage() for item in caplog.records]


def main_agent(monkeypatch):
    agent = make_agent(monkeypatch, None)
    agent.model = ReviewModel()
    agent.model_config.max_retries = 0
    agent.model_config.fallback_model = None
    agent.state.reply_id = "timing-task"
    agent.state.context = [Msg(name="user", role="user", content=[
        TextBlock(text="Private task content should not appear in timing logs"),
    ])]
    return agent


async def test_inactive_gate_logs_skip_and_main_reply_is_unchanged(monkeypatch, caplog):
    agent = main_agent(monkeypatch)
    with caplog.at_level(logging.INFO):
        events = [event async for event in agent._reasoning()]
    logs = "\n".join(messages(caplog))
    assert "reason=not_active" in logs
    assert "MainModel timing stage=request_start" in logs
    assert "MainModel timing stage=first_output" in logs
    assert "status=completed" in logs
    assert "Private task content" not in logs
    assert len(agent.model.requests) == 1
    assert any(isinstance(event, TextBlockDeltaEvent) and event.delta == "done" for event in events)


async def test_no_tools_and_interval_skip_do_not_call_review_model(review_agent, caplog):
    agent = review_agent
    agent._failure_loop_gate.config = replace(agent._failure_loop_gate.config, check_interval=5)
    with caplog.at_level(logging.INFO):
        await agent._inject_failure_recovery()
        record(agent)
        await agent._inject_failure_recovery()
    logs = "\n".join(messages(caplog))
    assert "reason=no_tool_results" in logs
    assert "reason=interval_not_reached new_rounds=1 interval=5" in logs
    assert "stage=review_request_start" not in logs
    assert not agent.model.requests


async def test_review_timing_finishes_before_main_request(review_agent, caplog):
    agent = review_agent
    record(agent)
    with caplog.at_level(logging.INFO):
        events = [event async for event in agent._reasoning()]
    logs = "\n".join(messages(caplog))
    assert logs.index("stage=review_request_start") < logs.index("stage=review_attempt_end")
    assert logs.index("stage=review_attempt_end") < logs.index("stage=check_end")
    assert logs.index("stage=check_end") < logs.index("MainModel timing stage=request_start")
    assert "phase=model_response status=completed elapsed_ms=" in logs
    assert [request["review"] for request in agent.model.requests] == [True, False]
    assert any(isinstance(event, TextBlockDeltaEvent) and event.delta == "done" for event in events)


async def test_review_failure_is_timed_without_preventing_main_reply(review_agent, caplog):
    agent = review_agent
    agent.model_config.max_retries = 0
    agent.model_config.fallback_model = None
    record(agent)
    agent.model.review_response = RuntimeError("review unavailable")
    with caplog.at_level(logging.INFO):
        events = [event async for event in agent._reasoning()]
    logs = "\n".join(messages(caplog))
    assert "phase=model_response status=error elapsed_ms=" in logs
    assert "MainModel timing stage=round_end" in logs
    assert any(isinstance(event, TextBlockDeltaEvent) and event.delta == "done" for event in events)


@pytest.mark.parametrize("error", [RuntimeError("model unavailable"), asyncio.CancelledError()])
async def test_main_request_error_or_cancel_propagates_with_timing(monkeypatch, caplog, error):
    class FailedModel(ReviewModel):
        async def __call__(self, *args, **kwargs):
            raise error

    agent = main_agent(monkeypatch)
    agent.model = FailedModel()
    with caplog.at_level(logging.INFO), pytest.raises(type(error)):
        _ = [event async for event in agent._reasoning()]
    logs = "\n".join(messages(caplog))
    assert f"stage=request_error session=default reply=timing-task iteration=0 error_type={type(error).__name__}" in logs
    assert f"status={type(error).__name__} first_output_ms=none" in logs
    assert "stage=first_output" not in logs


async def test_stream_open_and_empty_chunk_are_not_first_output(monkeypatch, caplog):
    async def chunks():
        yield ChatResponse(content=[], is_last=False)
        yield ChatResponse(content=[TextBlock(text="done")], is_last=False)
        yield ChatResponse(content=[TextBlock(text="done")], is_last=True)

    agent = main_agent(monkeypatch)
    agent.model.responses = [chunks()]
    with caplog.at_level(logging.INFO):
        stream = agent._reasoning_impl()
        first = await anext(stream)
        assert isinstance(first, ModelCallStartEvent)
        assert "stage=first_output" not in "\n".join(messages(caplog))
        remaining = [event async for event in stream]
    logs = "\n".join(messages(caplog))
    assert "response_type=async_generator" in logs
    assert logs.index("stage=request_return") < logs.index("stage=first_output")
    assert logs.count("stage=first_output") == 1
    assert "event_type=TextBlockStartEvent" in logs
    assert any(isinstance(event, TextBlockDeltaEvent) and event.delta == "done" for event in remaining)


def test_gate_configuration_logs_why_it_is_inactive(monkeypatch, caplog, tmp_path):
    from qwenpaw.loop.gates import failure_loop

    monkeypatch.setenv("QWENPAW_FAILURE_LOOP_ENABLED", "1")
    monkeypatch.setattr(failure_loop.sys, "platform", "win32")
    with caplog.at_level(logging.INFO):
        gate = failure_loop.build_failure_loop_gate(workspace_dir=tmp_path, request_context={})
    assert gate is None
    assert "enabled=False reason=disabled_by_config" in "\n".join(messages(caplog))
    assert not (tmp_path / "failure_loop").exists()


async def test_progress_logs_prepared_and_included_without_tool_contents(monkeypatch, caplog):
    from qwenpaw.loop.progress_config import ProgressReminderConfig
    from qwenpaw.loop.progress_reminder import ProgressReminderController
    from test_progress_reminder import Clock, record

    clock = Clock()
    controller = ProgressReminderController(ProgressReminderConfig(enabled=True), clock=clock)
    controller.begin_segment("default", "timing-task")
    record(controller, reply_id="timing-task", text="PRIVATE TOOL CONTENT")
    agent = make_agent(monkeypatch, None, progress=controller)
    agent.model = ReviewModel()
    agent.state.reply_id = "timing-task"
    agent.state.context = [Msg(name="user", role="user", content=[TextBlock(text="task")])]
    clock.now = 600
    with caplog.at_level(logging.INFO):
        _ = [event async for event in agent._reasoning()]
    logs = "\n".join(messages(caplog))
    assert "outcome=prepared" in logs
    assert "outcome=included_in_request" in logs
    assert "hint_tokens=None" not in next(line for line in messages(caplog) if "outcome=included_in_request" in line)
    assert "PRIVATE TOOL CONTENT" not in logs
    assert len(agent.model.requests) == 1
