"""Only lower-tier final-file tool display waits for verification."""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import TextBlock
from agentscope.tool import FunctionTool, ToolChunk, Toolkit

from test_edge_cloud import (
    Model, call, clean_capture, config, context, existing_skill, prepare, route, text,
)
from test_final_files import ctx, file
from qwenpaw.agents.tools.send_final_files import send_final_files_to_user
from qwenpaw.runtime import final_files as delivery
from qwenpaw.runtime.builder import AgentBuilder
from qwenpaw.runtime.edge_cloud.orchestrator import EdgeCloudOrchestrator
from qwenpaw.runtime.envelope import Envelope
from qwenpaw.runtime.final_files.prompts import TOOL_NAME


def tool_contents(events, name):
    return [
        content.data
        for event in events
        for content in (
            [event] if event.object == "content"
            else (getattr(event, "content", None) or [])
        )
        if getattr(content, "type", None) == "data"
        and isinstance(getattr(content, "data", None), dict)
        and content.data.get("name") == name
    ]


async def test_available_but_unused_final_file_tool_keeps_original_display(
    ctx, tmp_path, monkeypatch,
):
    edge_ctx = context(tmp_path, [FunctionTool(send_final_files_to_user)])
    orchestrator = prepare(
        monkeypatch, edge_ctx, Model("device", [text("ordinary answer")]),
        [route("device_tools"), {
            "protocol_version": "edge_cloud_verifier.v1", "passed": True,
        }],
    )
    envelope = Envelope()
    events = [event async for event in orchestrator.run(edge_ctx, envelope)]
    assert envelope._client_tool_display is not None
    assert envelope._client_messages is None
    assert not tool_contents(events, TOOL_NAME)
    assert not delivery.current().submitted


@pytest.mark.parametrize("tier", ["device", "edge"])
@pytest.mark.parametrize("trace_enabled", [False, True])
@pytest.mark.parametrize("outcome", ["pass", "fail", "error", "cancel"])
async def test_final_file_tool_waits_for_verdict_without_changing_execution(
    ctx, tmp_path, monkeypatch, tier, trace_enabled, outcome,
):
    candidate = file(tmp_path, "candidate.txt")
    repaired = file(tmp_path, "repaired.txt")
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def read_file(file_path: str):
        return ToolChunk(content=[TextBlock(text="ordinary read result")])

    tools = [FunctionTool(read_file), FunctionTool(send_final_files_to_user)]
    cloud = Model("cloud", [
        # Reusing the lower-tier call id must not hide a cloud submission.
        call(TOOL_NAME, {"file_paths": [repaired]}, "submit"), text("cloud done"),
    ])
    edge_ctx = context(tmp_path, tools, cloud)
    steps = []
    if tier == "edge":
        directory = existing_skill(tmp_path, "report")
        edge_ctx.agent.toolkit = Toolkit(tools=tools, skills_or_loaders=[str(directory)])
        steps.append(call("Skill", {"skill": "report"}, "skill"))
    steps.extend([
        call("read_file", {"file_path": candidate}, "read"),
        call(TOOL_NAME, {"file_paths": [candidate]}, "submit"),
        text("lower-tier answer remains streamed"),
    ])
    orchestrator = prepare(
        monkeypatch, edge_ctx, Model(tier, steps),
        [
            route("edge_skill" if tier == "edge" else "device_tools",
                  **({"skill": "report"} if tier == "edge" else {})),
            {"protocol_version": "edge_cloud_verifier.v1", "passed": outcome == "pass"},
        ],
        settings=config(device_max_tool_rounds=3, trace_stream_enabled=trace_enabled),
    )
    decide = orchestrator._decision
    captured = {}

    async def pause_verification(ctx, stage, prompt, payload, **kwargs):
        if stage == "verifier":
            captured.update(payload)
            entered.set()
            await proceed.wait()
            if outcome == "error":
                raise ValueError("verification unavailable")
        return await decide(ctx, stage, prompt, payload, **kwargs)

    orchestrator._decision = AsyncMock(side_effect=pause_verification)
    envelope, events = Envelope(), []

    async def collect():
        async for event in orchestrator.run(edge_ctx, envelope):
            events.append(event.model_copy(deep=True))
        async for event in delivery.finish(edge_ctx, envelope):
            events.append(event.model_copy(deep=True))
        async for event in envelope.finalize():
            events.append(event.model_copy(deep=True))

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert delivery.current().submitted and not delivery.current().verified
        assert envelope._client_tool_display is not None
        assert envelope._client_tool_display.pending
        assert envelope._client_tool_display._hidden
        assert not tool_contents(events, TOOL_NAME)
        assert tool_contents(events, "read_file")
        assert "lower-tier answer remains streamed" in json.dumps(
            [event.model_dump(mode="json") for event in events],
        )
        assert captured["final_files"]["submitted"]
        assert any(record["tool"] == TOOL_NAME for record in captured["tool_results"])
        # Internal response and cancellation/session evidence are retained.
        assert tool_contents(envelope.response.output, TOOL_NAME)
        assert "pending_verification" in envelope.collect_tool_output()["submit"]
        heartbeat = [event async for event in envelope.heartbeat()]
        assert not tool_contents(heartbeat[-1].output, TOOL_NAME)
        assert [event async for event in delivery.finish(edge_ctx, envelope)] == []
        before_verdict = len(events)

        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            events.extend([event async for event in envelope.finalize()])
        else:
            proceed.set()
            await asyncio.wait_for(task, 10)

        final = events[-1]
        assert final.object == "response"
        submitted = tool_contents(events[before_verdict:], TOOL_NAME)
        final_tools = tool_contents(final.output, TOOL_NAME)
        files = [
            getattr(content, "filename", "")
            for message in final.output for content in message.content
            if content.type == "file"
        ]
        if outcome == "cancel":
            assert not submitted and not final_tools and not files
            assert not cloud.requests
        else:
            assert submitted and final_tools
            visible = json.dumps(submitted + final_tools)
            expected = "candidate.txt" if outcome == "pass" else "repaired.txt"
            assert files == [expected]
            assert expected in visible
            assert bool(cloud.requests) is (outcome != "pass")
            if outcome != "pass":
                assert "candidate.txt" not in visible
                assert orchestrator._trace.final_agent == "cloud"
                assert orchestrator._trace.fallback_reason == (
                    "VALIDATION_FAIL" if outcome == "fail" else "VALIDATION_UNCERTAIN"
                )
            if trace_enabled:
                verdict_index = max(
                    index for index, event in enumerate(events)
                    if event.object == "message" and event.name == "VerifyAgent"
                )
                assert not tool_contents(events[:verdict_index + 1], TOOL_NAME)
                final_verdict_index = next(
                    index for index, message in enumerate(final.output)
                    if message.name == "VerifyAgent"
                )
                assert not tool_contents(final.output[:final_verdict_index + 1], TOOL_NAME)
        # Exercise the actual frontend merger when console dependencies
        # are installed; the backend coverage above remains standalone.
        root = Path(__file__).resolve().parents[1]
        node = shutil.which("node")
        if (
            tier == "device" and outcome in {"pass", "fail"} and node
            and (root / "console/node_modules/typescript").exists()
        ):
            snapshot = tmp_path / "display-events.json"
            snapshot.write_text(json.dumps({
                "events": [event.model_dump(mode="json") for event in events],
                "before_verdict": before_verdict, "outcome": outcome,
            }, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run([
                node, str(root / "tests/helpers/replay_final_files_display.cjs"),
                str(root), str(snapshot),
            ], capture_output=True, text=True, encoding="utf-8", timeout=30)
            assert result.returncode == 0, result.stdout + result.stderr
        sequences = [event.sequence_number for event in events]
        assert sequences == sorted(set(sequences))
        assert ctx.agent is not edge_ctx.agent
        assert edge_ctx.agent.model is cloud
    finally:
        proceed.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("mode", ["disabled", "direct_cloud"])
@pytest.mark.parametrize("trace_enabled", [False, True])
async def test_other_execution_modes_show_submission_without_verifier(
    ctx, tmp_path, monkeypatch, mode, trace_enabled,
):
    from qwenpaw.runtime.hooks import HookResult
    from qwenpaw.runtime.runtime import Runtime

    path = file(tmp_path, "ordinary.txt")
    waiting, proceed = asyncio.Event(), asyncio.Event()

    class PausingCloud(Model):
        async def __call__(self, *args, **kwargs):
            if self.requests:
                waiting.set()
                await proceed.wait()
            return await super().__call__(*args, **kwargs)

    cloud = PausingCloud("cloud", [
        call(TOOL_NAME, {"file_paths": [path]}), text("done"),
    ])
    request_ctx = context(tmp_path, [FunctionTool(send_final_files_to_user)], cloud)
    request_ctx.context_injections = []
    request_ctx.agent.close = AsyncMock()
    plugins = SimpleNamespace(
        hook_registry=SimpleNamespace(run=AsyncMock(return_value=HookResult())),
        slash_command_registry=SimpleNamespace(dispatch=AsyncMock(return_value=None)),
        active_mode_names=lambda _: set(),
    )
    request_ctx.workspace = SimpleNamespace(plugins=plugins)
    runtime = Runtime(workspace=request_ctx.workspace, app_services=None)
    monkeypatch.setattr(runtime, "_normalize", lambda request: request)
    monkeypatch.setattr(runtime, "_build_context", lambda _: request_ctx)
    monkeypatch.setattr(AgentBuilder, "build", AsyncMock(return_value=request_ctx.agent))
    monkeypatch.setattr(AgentBuilder, "_build_request_context", lambda _: {})
    monkeypatch.setattr(
        "qwenpaw.runtime.edge_cloud.config.load_edge_cloud_config",
        lambda: config(enabled=mode != "disabled", trace_stream_enabled=trace_enabled),
    )
    decision = AsyncMock(return_value=route("complex_or_negative"))
    monkeypatch.setattr(EdgeCloudOrchestrator, "_decision", decision)
    events = []

    async def collect():
        async for event in runtime.run(SimpleNamespace(request_context={})):
            events.append(event.model_copy(deep=True))

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(waiting.wait(), 10)
        assert tool_contents(events, TOOL_NAME)
        assert not request_ctx._envelope._client_tool_display
        assert decision.await_count == int(mode == "direct_cloud")
        proceed.set()
        await asyncio.wait_for(task, 10)
        assert tool_contents(events[-1].output, TOOL_NAME)
    finally:
        proceed.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
