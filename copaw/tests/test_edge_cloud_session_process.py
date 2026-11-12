"""Opt-in transcript snapshots must round-trip to the original model context."""

import asyncio
import ast
import argparse
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import (
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agentscope.model import ChatResponse
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, ToolChunk, Toolkit

from test_edge_cloud import (
    Model,
    call,
    clean_capture,
    config,
    context,
    existing_skill,
    route,
    text,
)
from qwenpaw.hooks.session.session_hook import SessionLoadHook, SessionSaveHook
from qwenpaw.runtime.builder import AgentBuilder
from qwenpaw.runtime.edge_cloud.config import load_edge_cloud_config
from qwenpaw.runtime.edge_cloud.session_process import SessionProcessRecorder
from qwenpaw.runtime.hooks import HookResult
from qwenpaw.runtime.phases import Phase
from qwenpaw.runtime.runtime import Runtime
from qwenpaw.runtime.session_views import (
    canonical_session_state,
    restore_session_view,
    snapshot_session_view,
)
from qwenpaw.schemas import AudioContent, DataContent, FileContent, Message, TextContent


class MemorySession:
    def __init__(self, state=None):
        self.saved = deepcopy(state)

    async def load_session_state(self, *, agent, **kwargs):
        if self.saved is not None:
            agent.load_state_dict(deepcopy(self.saved))

    async def save_session_state(self, *, agent, **kwargs):
        self.saved = deepcopy(agent.state_dict())


def runtime_for(monkeypatch, ctx, settings, *, session=None):
    session = session or MemorySession()
    uploads = []
    monkeypatch.setattr(
        "qwenpaw.observability.session_delta_langfuse.schedule_session_delta_upload",
        uploads.append,
    )
    monkeypatch.setattr(
        "qwenpaw.observability.session_delta_langfuse.is_session_delta_langfuse_enabled",
        lambda: True,
    )

    async def hooks(phase, ctx):
        if phase == Phase.PRE_AGENT_BUILD:
            return await SessionLoadHook().run(ctx)
        if phase == Phase.POST_RESPONSE:
            return await SessionSaveHook().run(ctx)
        return HookResult()

    ctx.context_injections = []
    ctx.agent.close = AsyncMock()
    ctx.workspace = SimpleNamespace(
        session=session,
        plugins=SimpleNamespace(
            hook_registry=SimpleNamespace(run=hooks),
            slash_command_registry=SimpleNamespace(
                dispatch=AsyncMock(return_value=None)
            ),
            active_mode_names=lambda _: set(),
        ),
    )

    async def build(self, ctx):
        if getattr(ctx, "session_state", None):
            ctx.agent.state = AgentState.model_validate(ctx.session_state["state"])
        return ctx.agent

    runtime = Runtime(workspace=ctx.workspace, app_services=None)
    monkeypatch.setattr(runtime, "_normalize", lambda request: request)
    monkeypatch.setattr(runtime, "_build_context", lambda _: ctx)
    monkeypatch.setattr(AgentBuilder, "build", build)
    monkeypatch.setattr(AgentBuilder, "_build_request_context", lambda _: {})
    monkeypatch.setattr(
        "qwenpaw.runtime.edge_cloud.config.load_edge_cloud_config",
        lambda: settings,
    )
    return runtime, session, uploads


def wire_message(identity, kind="message", content=None, **kwargs):
    return Message(
        id=identity,
        object="message",
        type=kind,
        role="assistant",
        content=content or [],
        **kwargs,
    )


@pytest.mark.parametrize("collaboration", [False, True])
@pytest.mark.parametrize("recording", [False, True])
@pytest.mark.parametrize("tracing", [False, True])
async def test_runtime_requires_collaboration_and_recording_and_keeps_uploads_native(
    monkeypatch,
    tmp_path,
    collaboration,
    recording,
    tracing,
):
    cloud = Model(
        "cloud", [call("submit_route_decision", route()), text("ordinary answer")]
    )
    if not collaboration:
        cloud.responses = [text("ordinary answer")]
    lower = Model("lower", [text("device answer")])
    ctx = context(tmp_path, cloud=cloud)
    settings = config(
        enabled=collaboration,
        session_process_enabled=recording,
        trace_stream_enabled=tracing,
    )
    runtime, session, uploads = runtime_for(monkeypatch, ctx, settings)
    monkeypatch.setattr(
        "qwenpaw.agents.model_factory.create_openai_compatible_model_and_formatter",
        lambda **_: (lower, None),
    )
    events = [event.model_copy(deep=True) async for event in runtime.run(ctx.request)]
    canonical = ctx.agent.state_dict()
    assert AgentState.model_validate(session.saved["state"])
    assert session.saved["scroll"] == canonical["scroll"]
    restored = restore_session_view(SimpleNamespace(extras={}), session.saved)
    assert restored == canonical
    if collaboration and recording:
        assert session.saved != canonical
        saved_text = json.dumps(session.saved["state"]["context"], ensure_ascii=False)
        assert ("【RouterAgent | cloud】" in saved_text) is tracing
        assert ("【DeviceAgent | lower】" in saved_text) is tracing
        # All visible text messages are present exactly once in content.
        stored_ids = [msg["id"] for msg in session.saved["state"]["context"]]
        final_output = events[-1].output
        for message in final_output:
            if message.content:
                assert stored_ids.count(message.id) == 1
    else:
        assert session.saved == canonical
        assert "session_context_views" not in ctx.extras
    assert "qwenpaw_session_context_view" not in json.dumps(uploads, default=str)
    assert ctx.agent.model is cloud


async def test_edge_full_process_restores_identical_router_history_on_next_turn(
    monkeypatch, tmp_path
):
    async def read_file(file_path: str):
        return ToolChunk(content=[TextBlock(text="real tool result")])

    decision = route(
        "edge_skill", skill="weather-query", required_tools=["Skill", "read_file"]
    )
    cloud = Model(
        "cloud",
        [
            call("Skill", {"skill": "weather-query"}, "router-skill"),
            ChatResponse(
                content=[
                    ThinkingBlock(thinking="router thinking"),
                    *call("submit_route_decision", decision).content,
                ],
                is_last=True,
            ),
            call("Skill", {"skill": "weather-query"}, "verify-skill"),
            ChatResponse(
                content=[
                    ThinkingBlock(thinking="verifier thinking"),
                    *call(
                        "submit_verification",
                        {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
                    ).content,
                ],
                is_last=True,
            ),
        ],
    )
    lower = Model(
        "edge",
        [
            call("Skill", {"skill": "weather-query"}, "skill-id"),
            call("read_file", {"file_path": "forecast.txt"}, "file-id"),
            text("edge answer </think>"),
        ],
    )
    ctx = context(tmp_path, cloud=cloud)
    directory = existing_skill(
        tmp_path / "skills", "weather-query", "Read the requested forecast file."
    )
    ctx.agent.toolkit = Toolkit(
        tools=[FunctionTool(read_file)], skills_or_loaders=[str(directory)]
    )
    runtime, session, uploads = runtime_for(
        monkeypatch,
        ctx,
        config(session_process_enabled=True, trace_stream_enabled=True),
    )
    monkeypatch.setattr(
        "qwenpaw.agents.model_factory.create_openai_compatible_model_and_formatter",
        lambda **_: (lower, None),
    )
    events = [event async for event in runtime.run(ctx.request)]
    canonical = ctx.agent.state_dict()
    persisted = AgentState.model_validate(session.saved["state"])
    content = [block for message in persisted.context for block in message.content]
    assert {block.type for block in content} >= {
        "text",
        "thinking",
        "tool_call",
        "tool_result",
    }
    text_body = "\n".join(block.text for block in content if block.type == "text")
    assert all(
        f"【{agent} |" in text_body
        for agent in ("RouterAgent", "EdgeAgent", "VerifyAgent")
    )
    assert text_body.count("edge answer </think>") == 1
    assert [b.thinking for b in content if b.type == "thinking"] == [
        "router thinking",
        "verifier thinking",
    ]
    for call_block in (block for block in content if block.type == "tool_call"):
        assert (
            sum(
                block.id == call_block.id
                for block in content
                if block.type == "tool_result"
            )
            == 1
        )
    assert all(
        message.name == ctx.agent.name
        for message in persisted.context
        if message.role == "assistant"
    )
    assert restore_session_view(SimpleNamespace(extras={}), session.saved) == canonical

    # Reload through the real session hook. Both the next router and ordinary
    # model path see the exact original native context, with no display extras.
    next_ctx = context(tmp_path, cloud=Model("cloud"))
    next_ctx.workspace = ctx.workspace
    await SessionLoadHook().run(next_ctx)
    assert next_ctx.session_state == canonical
    from qwenpaw.runtime.edge_cloud.orchestrator import EdgeCloudOrchestrator

    restored = AgentState.model_validate(next_ctx.session_state["state"])
    assert EdgeCloudOrchestrator._context_text(
        restored.context
    ) == EdgeCloudOrchestrator._context_text(ctx.agent.state.context)
    assert snapshot_session_view(next_ctx, canonical) == session.saved
    from qwenpaw.app.chats.utils import agentscope_msg_to_message

    replay = agentscope_msg_to_message(persisted.context)
    assert sum(m.type == "plugin_call" for m in replay) == sum(
        b.type == "tool_call" for b in content
    )
    assert "qwenpaw_session_context_view" not in json.dumps(
        [m.model_dump(mode="json") for m in replay]
    )
    assert uploads and events[-1].status == "completed"

    # Turn off new process storage and perform a real second collaboration.
    previous_display = deepcopy(session.saved["state"]["context"])
    next_cloud = Model("next-cloud", [call("submit_route_decision", route())])
    next_ctx = context(tmp_path, cloud=next_cloud)
    next_lower = Model("next-device", [text("second answer")])
    next_runtime, _, _ = runtime_for(
        monkeypatch,
        next_ctx,
        config(session_process_enabled=False, trace_stream_enabled=True),
        session=session,
    )
    monkeypatch.setattr(
        "qwenpaw.agents.model_factory.create_openai_compatible_model_and_formatter",
        lambda **_: (next_lower, None),
    )
    _ = [event async for event in next_runtime.run(next_ctx.request)]
    router_input = json.loads(next_cloud.requests[0][0][-1].get_text_content())
    assert json.loads(router_input["history"]) == canonical["state"]["context"]
    assert (
        session.saved["state"]["context"][: len(previous_display)] == previous_display
    )
    assert (
        restore_session_view(SimpleNamespace(extras={}), session.saved)
        == next_ctx.agent.state_dict()
    )
    assert "【DeviceAgent | next-device】" not in json.dumps(
        session.saved, ensure_ascii=False
    )


def test_deltas_replacements_partial_tools_and_attachments_keep_native_types(tmp_path):
    ctx = context(tmp_path)
    recorder = SessionProcessRecorder(ctx)
    recorder.record(wire_message("text"))
    recorder.record(
        TextContent(
            msg_id="text", index=0, delta=True, text="【EdgeAgent | edge】\n\nA"
        )
    )
    recorder.record(TextContent(msg_id="text", index=0, delta=True, text="B </think>"))
    recorder.record(
        TextContent(
            msg_id="text",
            index=0,
            delta=False,
            text="【EdgeAgent | edge】\n\nAB </think>",
        )
    )
    recorder.record(
        wire_message("thinking", "reasoning", [TextContent(text="actual thinking")])
    )
    recorder.record(
        wire_message(
            "call",
            "plugin_call",
            [
                DataContent(
                    data={
                        "call_id": "partial",
                        "name": "read_file",
                        "arguments": "{",
                    }
                )
            ],
        )
    )
    recorder.record(
        wire_message(
            "result",
            "plugin_call_output",
            [
                DataContent(
                    data={
                        "call_id": "partial",
                        "name": "read_file",
                        "output": "partial output",
                        "state": "interrupted",
                    }
                )
            ],
            status="cancelled",
        )
    )
    recorder.record(
        wire_message(
            "file",
            content=[
                FileContent(
                    file_url=(tmp_path / "report.txt").as_uri(), filename="report.txt"
                )
            ],
        )
    )
    recorder.record(
        wire_message("audio", content=[AudioContent(data="YWJj", format="wav")])
    )
    canonical = ctx.agent.state_dict()
    saved = snapshot_session_view(ctx, canonical)
    assert canonical == ctx.agent.state_dict()
    assert restore_session_view(SimpleNamespace(extras={}), saved) == canonical
    messages = AgentState.model_validate(saved["state"]).context
    blocks = [block for message in messages for block in message.content]
    assert sum(getattr(block, "text", "").count("AB </think>") for block in blocks) == 1
    assert (
        next(block for block in blocks if block.type == "thinking").thinking
        == "actual thinking"
    )
    assert next(block for block in blocks if block.type == "tool_call").input == "{"
    assert (
        next(block for block in blocks if block.type == "tool_result").state
        == "interrupted"
    )
    assert next(block for block in blocks if block.type == "data").name == "report.txt"
    audio = next(
        block
        for block in blocks
        if block.type == "data" and block.source.type == "base64"
    )
    assert audio.source.data == "YWJj" and audio.source.media_type == "audio/wav"


async def test_cancel_during_router_records_emitted_partial_without_polluting_agent(
    monkeypatch, tmp_path
):
    waiting = asyncio.Event()

    class WaitingModel(Model):
        async def __call__(self, messages, **kwargs):
            async def chunks():
                yield ChatResponse(
                    content=[ThinkingBlock(thinking="partial router thinking")],
                    is_last=False,
                )
                await waiting.wait()

            return chunks()

    ctx = context(tmp_path, cloud=WaitingModel("cloud"))
    runtime, session, _ = runtime_for(
        monkeypatch,
        ctx,
        config(session_process_enabled=True, trace_stream_enabled=True),
    )
    visible = asyncio.Event()

    async def collect():
        async for event in runtime.run(ctx.request):
            if event.object == "message" and event.type == "reasoning":
                visible.set()

    task = asyncio.create_task(collect())
    await asyncio.wait_for(visible.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.saved is not None
    saved = AgentState.model_validate(session.saved["state"])
    assert any(
        block.type == "thinking" and block.thinking == "partial router thinking"
        for message in saved.context
        for block in message.content
    )
    assert (
        restore_session_view(SimpleNamespace(extras={}), session.saved)
        == ctx.agent.state_dict()
    )


def test_old_recordings_survive_disabling_new_recording_and_scroll_does_not_resurrect(
    tmp_path,
):
    ctx = context(tmp_path)
    recorder = SessionProcessRecorder(ctx)
    ctx.agent.state.context.extend(
        [
            *ctx.input_msgs,
            Msg(
                name=ctx.agent.name,
                role="assistant",
                content=[TextBlock(text="native answer")],
            ),
        ]
    )
    recorder.record(
        wire_message(
            "answer", content=[TextContent(text="【EdgeAgent | edge】\n\nanswer")]
        )
    )
    canonical = ctx.agent.state_dict()
    first_saved = snapshot_session_view(ctx, canonical)
    next_ctx = SimpleNamespace(extras={})
    native = restore_session_view(next_ctx, first_saved)
    ordinary = Msg(
        name="user", role="user", content=[TextBlock(text="next ordinary request")]
    ).model_dump(mode="json")
    native["state"]["context"].append(ordinary)
    next_saved = snapshot_session_view(next_ctx, native)
    assert next_saved["state"]["context"][:-1] == first_saved["state"]["context"]
    assert next_saved["state"]["context"][-1] == ordinary
    scrolled = deepcopy(native)
    scrolled["state"]["context"] = [ordinary]
    assert snapshot_session_view(next_ctx, scrolled) == scrolled


def test_main_configuration_is_opt_in_and_preserves_env(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "main.py"
    nodes = [
        node
        for node in ast.parse(source.read_text(encoding="utf-8-sig")).body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"parse_entry_cli_args", "apply_devco_bundle_env"}
    ]
    entry = {
        "argparse": argparse,
        "os": os,
        "sys": sys,
        "_INIT_ONLY_ENV": "TEST_INIT_ONLY",
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), entry)
    monkeypatch.setattr(os, "environ", {})
    assert not load_edge_cloud_config().session_process_enabled
    # Honor explicit startup configuration independently of the user's
    # current main.py default (which is not part of the upload change).
    os.environ["QWENPAW_EDGE_CLOUD_SESSION_PROCESS_ENABLED"] = "0"
    entry["parse_entry_cli_args"]([])
    assert not load_edge_cloud_config().session_process_enabled
    os.environ["QWENPAW_EDGE_CLOUD_SESSION_PROCESS_ENABLED"] = "1"
    entry["parse_entry_cli_args"]([])
    assert load_edge_cloud_config().session_process_enabled
    assert entry["parse_entry_cli_args"](
        ["--edge-cloud-session-process-enabled", "0", "--reload"]
    ) == ["--reload"]
    assert not load_edge_cloud_config().session_process_enabled


@pytest.mark.parametrize(
    "excluded", ["not-ready", "other-agent", "cron", "goal", "mission", "coding"]
)
async def test_storage_flag_never_changes_existing_request_exclusions(
    monkeypatch, tmp_path, excluded
):
    ctx = context(tmp_path, cloud=Model("cloud", [text("ordinary answer")]))
    settings = config(session_process_enabled=True, trace_stream_enabled=True)
    if excluded == "not-ready":
        settings = config(session_process_enabled=True, edge_model="", device_model="")
    elif excluded == "other-agent":
        ctx.agent_id = "other"
    elif excluded == "cron":
        ctx.request.session_source = "cron"
    elif excluded in {"goal", "mission"}:
        ctx.input_msgs[-1].content = [TextBlock(text=f"/{excluded} ordinary task")]
    else:
        ctx.request.request_context = {"acp_coding_project": {"path": str(tmp_path)}}
    runtime, session, _ = runtime_for(monkeypatch, ctx, settings)
    if excluded == "coding":
        ctx.workspace.plugins.active_mode_names = lambda _: {"coding"}
    for recorder in (
        "session_process.SessionProcessRecorder", "langfuse_delta.LangfuseDeltaRecorder",
    ):
        monkeypatch.setattr(
            f"qwenpaw.runtime.edge_cloud.{recorder}.__init__",
            lambda *_: pytest.fail("An excluded request must not start a recorder"),
        )
    _ = [event async for event in runtime.run(ctx.request)]
    assert session.saved == ctx.agent.state_dict()


@pytest.mark.parametrize("release", [False, True])
async def test_recording_obeys_real_final_file_display_gate(tmp_path, release):
    from agentscope.event import EventType
    from qwenpaw.runtime.edge_cloud.final_files_display import VerifiedFinalFilesDisplay
    from qwenpaw.runtime.envelope import Envelope
    from qwenpaw.app.chats.utils import agentscope_msg_to_message

    ctx = context(tmp_path)
    recorder = SessionProcessRecorder(ctx)
    ctx.agent.state.context.extend(
        [
            *ctx.input_msgs,
            Msg(
                name=ctx.agent.name,
                role="assistant",
                content=[
                    ToolCallBlock(
                        id="hidden", name="send_final_files_to_user", input="{}"
                    ),
                    ToolResultBlock(
                        id="hidden",
                        name="send_final_files_to_user",
                        output="UNVERIFIED_PRIVATE_CANDIDATE",
                        state="success",
                    ),
                ],
            ),
        ]
    )
    recorder.record(
        wire_message("visible", content=[TextContent(text="visible answer")])
    )
    envelope = Envelope()
    gate = VerifiedFinalFilesDisplay(envelope, "send_final_files_to_user")
    envelope._client_tool_display = gate
    for kind, extra in [
        (EventType.TOOL_CALL_START, {}),
        (EventType.TOOL_CALL_DELTA, {"delta": "{}"}),
        (EventType.TOOL_CALL_END, {}),
        (EventType.TOOL_RESULT_START, {}),
        (EventType.TOOL_RESULT_TEXT_DELTA, {"delta": "UNVERIFIED_PRIVATE_CANDIDATE"}),
        (EventType.TOOL_RESULT_END, {"state": "success"}),
    ]:
        event = SimpleNamespace(
            type=kind.value,
            tool_call_id="hidden",
            tool_call_name="send_final_files_to_user",
            **extra,
        )
        async for frame in envelope.translate_event(event):
            recorder.record(frame)
    async for frame in envelope.heartbeat():
        recorder.record(frame)
    if release:
        for frame in gate.release():
            recorder.record(frame)
    else:
        gate.discard()
    saved = snapshot_session_view(ctx, ctx.agent.state_dict())
    native = AgentState.model_validate(saved["state"])
    display = agentscope_msg_to_message(native.context)
    public = json.dumps([message.model_dump(mode="json") for message in display])
    assert ("UNVERIFIED_PRIVATE_CANDIDATE" in public) is release
    assert ("send_final_files_to_user" in public) is release
    assert canonical_session_state(saved) == ctx.agent.state_dict()


async def test_commands_usage_and_memory_read_original_context(monkeypatch, tmp_path):
    from qwenpaw.runtime.builtin_commands import _load_agent_state, _save_agent_state
    from qwenpaw.agents.memory.proactive.proactive_utils import _process_session_memory
    from qwenpaw.token_usage import turn_usage

    ctx = context(tmp_path)
    recorder = SessionProcessRecorder(ctx)
    ctx.agent.state.context.extend(
        [
            *ctx.input_msgs,
            Msg(
                name=ctx.agent.name,
                role="assistant",
                content=[TextBlock(text="native final answer")],
            ),
        ]
    )
    recorder.record(
        wire_message(
            "answer",
            content=[
                TextContent(text="【VerifyAgent | cloud】\n\nverification conclusion")
            ],
        )
    )
    canonical = ctx.agent.state_dict()
    session = MemorySession(snapshot_session_view(ctx, canonical))
    next_ctx = context(tmp_path)
    next_ctx.workspace = SimpleNamespace(session=session)
    native, payload = await _load_agent_state(next_ctx)
    assert native.context == ctx.agent.state.context
    await _save_agent_state(next_ctx, native, scroll_block=payload["scroll"])
    assert canonical_session_state(session.saved) == canonical
    assert "【VerifyAgent | cloud】" in json.dumps(session.saved, ensure_ascii=False)
    snapshot = {"agent": deepcopy(session.saved)}
    session.get_session_state_dict = AsyncMock(return_value=snapshot)
    memory = await _process_session_memory("s", "u", next_ctx.workspace)
    assert any(
        item["message"].get_text_content() == "native final answer" for item in memory
    )
    assert "verification conclusion" not in str(memory)
    estimator = AsyncMock(return_value={"tokens": 1})
    monkeypatch.setattr(turn_usage, "snapshot_context_usage_for_state", estimator)
    monkeypatch.setattr(
        turn_usage, "reconcile_turn_with_context", lambda turn, context: turn
    )
    session.update_session_state = AsyncMock()
    await turn_usage.finalize_console_turn_usage(
        session=session,
        session_id="s",
        user_id="u",
        channel="console",
        agent_id="default",
    )
    assert estimator.await_args.args[0].context == native.context
    # Clearing/replacing a session must not restore old display-only groups.
    await _save_agent_state(next_ctx, AgentState(), preserve_context_views=False)
    assert session.saved["state"]["context"] == []
