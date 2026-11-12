"""Bound local model responses while preserving existing cloud handoff evidence."""

import asyncio
from collections.abc import AsyncIterable

import pytest
from agentscope.message import Base64Source, DataBlock, TextBlock, ThinkingBlock, ToolCallBlock
from agentscope.model import ChatResponse
from agentscope.tool import FunctionTool, ToolChunk, Toolkit

from test_edge_cloud import (
    Model, call, clean_capture, config, consume, context, existing_skill, prepare, route, text,
)
from qwenpaw.runtime.edge_cloud import execution
from qwenpaw.runtime.edge_cloud.execution import ExecutionJournal, ExecutionModel, LocalExecutionHandoff


@pytest.fixture
def short_timeouts(monkeypatch):
    monkeypatch.setattr(execution, "_FIRST_OUTPUT_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(execution, "_IDLE_OUTPUT_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(execution, "_ROUND_TIMEOUT_SECONDS", {"device": 0.4, "edge": 0.6})


class StreamModel(Model):
    def __init__(self, factory):
        super().__init__("local")
        self.factory = factory

    async def __call__(self, messages, tools=None, **kwargs):
        self.requests.append((messages, tools))
        return self.factory()


async def collect(model):
    response = await model([], tools=[])
    if isinstance(response, AsyncIterable):
        return [chunk async for chunk in response]
    return [response]


def test_confirmed_model_timeout_defaults():
    assert execution._FIRST_OUTPUT_TIMEOUT_SECONDS == 15
    assert execution._IDLE_OUTPUT_TIMEOUT_SECONDS == 20
    assert execution._ROUND_TIMEOUT_SECONDS == {"device": 6 * 60, "edge": 10 * 60}


@pytest.mark.parametrize("waiting_in", ["request", "stream"])
async def test_first_output_timeout_cancels_and_closes_model(short_timeouts, waiting_in, caplog):
    closed = asyncio.Event()

    async def stream():
        try:
            await asyncio.Event().wait()
            yield text("unreachable")
        finally:
            closed.set()

    class WaitingModel(Model):
        async def __call__(self, *args, **kwargs):
            if waiting_in == "stream":
                return stream()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

    journal = ExecutionJournal()
    model = ExecutionModel(WaitingModel("local"), journal)
    with pytest.raises(LocalExecutionHandoff, match="^execution_error$"):
        await asyncio.wait_for(collect(model), 2)
    assert closed.is_set()
    assert not journal.records and not journal.operations
    assert "role=device phase=first_output" in caplog.text


@pytest.mark.parametrize("phase", ["first_output", "idle_output"])
async def test_empty_chunks_do_not_reset_output_deadlines(short_timeouts, phase, caplog):
    closed = asyncio.Event()
    emitted = []

    async def stream():
        try:
            if phase == "idle_output":
                yield ChatResponse(content=[TextBlock(text="started")], is_last=False)
            while True:
                await asyncio.sleep(0.005)
                emitted.append(True)
                yield ChatResponse(content=[], is_last=False, metadata={"keepalive": True})
        finally:
            closed.set()

    model = ExecutionModel(StreamModel(stream), ExecutionJournal())
    with pytest.raises(LocalExecutionHandoff, match="^execution_error$"):
        await asyncio.wait_for(collect(model), 2)
    assert emitted and closed.is_set()
    assert f"phase={phase}" in caplog.text


async def test_first_output_budget_includes_time_opening_stream(short_timeouts, monkeypatch):
    monkeypatch.setattr(execution, "_FIRST_OUTPUT_TIMEOUT_SECONDS", 0.12)
    closed = asyncio.Event()

    class DelayedModel(Model):
        async def __call__(self, *args, **kwargs):
            await asyncio.sleep(0.08)

            async def stream():
                try:
                    await asyncio.sleep(0.08)
                    yield text("too late")
                finally:
                    closed.set()

            return stream()

    model = ExecutionModel(DelayedModel("local"), ExecutionJournal())
    with pytest.raises(LocalExecutionHandoff, match="^execution_error$"):
        await asyncio.wait_for(collect(model), 2)
    assert closed.is_set()


@pytest.mark.parametrize("tier", ["device", "edge"])
async def test_continuous_output_cannot_extend_round_limit(short_timeouts, monkeypatch, tier, caplog):
    monkeypatch.setattr(execution, "_FIRST_OUTPUT_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(execution, "_IDLE_OUTPUT_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(execution, "_ROUND_TIMEOUT_SECONDS", {"device": 0.08, "edge": 0.12})
    emitted, closed = [], asyncio.Event()

    async def stream():
        try:
            while True:
                await asyncio.sleep(0.005)
                emitted.append(True)
                yield ChatResponse(content=[TextBlock(text="more")], is_last=False)
        finally:
            closed.set()

    model = ExecutionModel(StreamModel(stream), ExecutionJournal(), execution_role=tier)
    with pytest.raises(LocalExecutionHandoff, match="^execution_error$"):
        await asyncio.wait_for(collect(model), 2)
    assert len(emitted) > 1 and closed.is_set()
    assert f"role={tier} phase=round" in caplog.text


@pytest.mark.parametrize("block", [
    TextBlock(text="text"),
    ThinkingBlock(thinking="thinking"),
    ToolCallBlock(id="call", name="read_file", input="{}"),
    DataBlock(source=Base64Source(type="base64", media_type="image/png", data="YQ==")),
])
async def test_content_output_refreshes_idle_deadline(short_timeouts, monkeypatch, block):
    monkeypatch.setattr(execution, "_FIRST_OUTPUT_TIMEOUT_SECONDS", 0.06)
    monkeypatch.setattr(execution, "_IDLE_OUTPUT_TIMEOUT_SECONDS", 0.06)

    async def stream():
        for _ in range(4):
            yield ChatResponse(content=[block], is_last=False)
            await asyncio.sleep(0.025)
        yield text("complete")

    model = ExecutionModel(StreamModel(stream), ExecutionJournal())
    responses = await collect(model)
    assert len(responses) == 5 and responses[-1].is_last


@pytest.mark.parametrize("waiting_in", ["request", "stream"])
async def test_provider_cannot_hide_deadline_by_swallowing_cancellation(short_timeouts, waiting_in):
    async def swallow_cancel():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return text("interrupted")

    class SwallowingModel(Model):
        async def __call__(self, *args, **kwargs):
            if waiting_in == "request":
                return await swallow_cancel()

            async def stream():
                yield await swallow_cancel()

            return stream()

    model = ExecutionModel(SwallowingModel("local"), ExecutionJournal())
    with pytest.raises(LocalExecutionHandoff, match="^execution_error$"):
        await asyncio.wait_for(collect(model), 2)


@pytest.mark.parametrize("tier", ["device", "edge"])
async def test_timeout_preserves_existing_handoff_after_successful_write(
    short_timeouts, monkeypatch, tmp_path, tier,
):
    skill = existing_skill(tmp_path, "report") if tier == "edge" else None

    async def run(timed_out):
        closed, writes = asyncio.Event(), []
        target = tmp_path / "result.txt"

        async def write_file(file_path: str, content: str):
            """Write the requested file."""
            target.write_text(content, encoding="utf-8")
            writes.append(content)
            return ToolChunk(content=[TextBlock(text="written")])

        class LowerModel(Model):
            async def __call__(self, *args, **kwargs):
                if self.responses:
                    return await super().__call__(*args, **kwargs)
                if not timed_out:
                    raise LocalExecutionHandoff("execution_error")

                async def stream():
                    try:
                        await asyncio.Event().wait()
                        yield text("unreachable")
                    finally:
                        closed.set()

                return stream()

        class CloudModel(Model):
            async def __call__(self, *args, **kwargs):
                if timed_out:
                    assert closed.is_set(), "Local request must close before cloud takeover"
                return await super().__call__(*args, **kwargs)

        cloud = CloudModel("cloud", [text("complete")])
        ctx = context(tmp_path, [FunctionTool(write_file)], cloud)
        steps = []
        if tier == "edge":
            ctx.agent.toolkit = Toolkit(
                tools=[FunctionTool(write_file)], skills_or_loaders=[str(skill)],
            )
            steps.append(call("Skill", {"skill": "report"}, "skill"))
        steps.append(call("write_file", {"file_path": str(target), "content": "draft"}))
        lower = LowerModel("local", steps)
        orchestrator = prepare(
            monkeypatch, ctx, lower,
            [route("edge_skill" if tier == "edge" else "device_tools",
                   skill="report" if tier == "edge" else "")],
        )
        await asyncio.wait_for(consume(orchestrator, ctx), 3)
        assert writes == ["draft"] and target.read_text(encoding="utf-8") == "draft"
        assert orchestrator._trace.fallback_reason == "execution_error"
        assert orchestrator._trace.final_agent == "cloud"
        assert orchestrator._decision.await_count == 1
        assert ctx.agent.model is cloud and ctx.agent._system_prompt == "DEFAULT SYSTEM"
        handoff = cloud.requests[0][0][-1]
        content = handoff.get_text_content().replace(orchestrator._trace.flow_id, "FLOW")
        assert "write_file" in content and "written" in content and "call-1" in content
        assert "first_output" not in content and "Local model timeout" not in content
        return content, handoff.metadata

    assert await run(True) == await run(False)


@pytest.mark.parametrize("tier", ["device", "edge"])
async def test_long_tool_execution_does_not_use_model_deadline(short_timeouts, monkeypatch, tmp_path, tier):
    monkeypatch.setattr(execution, "_FIRST_OUTPUT_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(execution, "_ROUND_TIMEOUT_SECONDS", {"device": 0.12, "edge": 0.12})

    async def read_file(file_path: str):
        """Read a file after slow tool work."""
        await asyncio.sleep(0.16)
        return ToolChunk(content=[TextBlock(text="evidence")])

    ctx = context(tmp_path, [FunctionTool(read_file)])
    steps = []
    if tier == "edge":
        skill = existing_skill(tmp_path, "report")
        ctx.agent.toolkit = Toolkit(
            tools=[FunctionTool(read_file)], skills_or_loaders=[str(skill)],
        )
        steps.append(call("Skill", {"skill": "report"}, "skill"))
    steps.extend([call("read_file", {"file_path": "a.txt"}), text("done")])
    orchestrator = prepare(
        monkeypatch, ctx, Model("local", steps),
        [route("edge_skill" if tier == "edge" else "device_tools",
               skill="report" if tier == "edge" else ""),
         {"protocol_version": "edge_cloud_verifier.v1", "passed": True}],
    )
    await asyncio.wait_for(consume(orchestrator, ctx), 3)
    assert not orchestrator._trace.fallback and orchestrator._trace.final_agent == tier
    assert not ctx.agent.model.requests


@pytest.mark.parametrize("tier", ["device", "edge"])
async def test_user_cancel_during_stream_never_starts_cloud(short_timeouts, monkeypatch, tmp_path, tier):
    waiting, closed = asyncio.Event(), asyncio.Event()

    async def stream():
        try:
            yield ChatResponse(content=[TextBlock(text="started")], is_last=False)
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    ctx = context(tmp_path)
    if tier == "edge":
        skill = existing_skill(tmp_path, "report")
        ctx.agent.toolkit = Toolkit(skills_or_loaders=[str(skill)])
    orchestrator = prepare(
        monkeypatch, ctx, StreamModel(stream),
        [route("edge_skill" if tier == "edge" else "device_text",
               skill="report" if tier == "edge" else "")],
    )
    task = asyncio.create_task(consume(orchestrator, ctx))
    await asyncio.wait_for(waiting.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert not ctx.agent.model.requests
    assert orchestrator._trace.final_agent == ""
