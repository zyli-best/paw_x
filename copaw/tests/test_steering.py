"""Ordinary steering: admission, real SDK tool boundaries, Scroll and export."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse
from agentscope.tool import FunctionTool, ToolChunk

from qwenpaw.app.task_tracker import TaskTracker
from qwenpaw.runtime.steering import SteeringError, SteeringState
from qwenpaw.hooks.session.session_hook import (
    _build_session_delta_payload, schedule_steering_upload,
)
from test_request_budget import CountingModel, message, qwen_agent, scroll


def bind(agent, original, identity="response-test"):
    state = SteeringState(identity, "default", "client-run", "chat")
    state.original_ids = {original.id}
    state.phase = "running"
    agent._normal_steering = state
    return state


async def test_http_status_receipts_and_stale_execution(monkeypatch):
    from qwenpaw.app.routers import console

    tracker = TaskTracker()
    ready, done = asyncio.Event(), asyncio.Event()

    async def stream(_):
        state = await tracker.bind_steering("response-one", "default", "client-run")
        state.phase = "running"
        ready.set()
        await done.wait()
        yield "data: {}\n\n"

    workspace = SimpleNamespace(
        agent_id="default", task_tracker=tracker,
        chat_manager=SimpleNamespace(get_chat=AsyncMock(return_value=object())),
    )
    monkeypatch.setattr(console, "get_agent_for_request", AsyncMock(return_value=workspace))
    app = FastAPI()
    app.include_router(console.router, prefix="/api")
    queue, _ = await tracker.attach_or_start("chat", {}, stream)
    await asyncio.wait_for(ready.wait(), 5)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            status = await client.get("/api/console/chat/status", params={"chat_id": "chat"})
            assert status.json() == {
                "agent_id": "default", "chat_id": "chat", "status": "running",
                "exists": True, "run_id": "client-run", "can_steer": True,
            }
            body = {"chat_id": "chat", "run_id": "client-run",
                    "message_id": "a", "text": "change directory"}
            first, duplicate = await asyncio.gather(*[
                client.post("/api/console/chat/steer", json=body) for _ in range(2)
            ])
            assert sorted([first.status_code, duplicate.status_code]) == [200, 202]
            assert first.json() == duplicate.json()
            conflict = await client.post("/api/console/chat/steer", json={**body, "text": "different"})
            assert conflict.status_code == 409
            stale = await client.post("/api/console/chat/steer", json={**body, "run_id": "old"})
            assert stale.status_code == 409
            second = await client.post("/api/console/chat/steer", json={**body, "message_id": "b"})
            assert second.json()["sequence"] == 2
            done.set()
            _ = [item async for item in tracker.stream_from_queue(queue, "chat")]
            idle = await client.get("/api/console/chat/status", params={"chat_id": "chat"})
            assert idle.json()["run_id"] is None
            assert idle.json()["exists"] is False
            assert idle.json()["can_steer"] is False
            assert (await client.post("/api/console/chat/steer", json=body)).status_code == 409
    finally:
        done.set()
        await tracker.wait_all_done(5)


async def test_two_steers_wait_for_tool_and_precede_scroll(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_for_user():
        entered.set()
        await release.wait()
        return ToolChunk(content=[TextBlock(text="tool finished")])

    class SpyScroll:
        def __init__(self):
            self.windows = []
        async def compress(self, agent, config=None):
            self.windows.append(deepcopy(agent.state.context))
        def on_save(self, *args):
            pass

    manager = SpyScroll()
    model = CountingModel(context=100000, output=100)
    model.responses = [
        ChatResponse(content=[ToolCallBlock(id="tool", name="wait_for_user", input="{}")], is_last=True),
        ChatResponse(content=[TextBlock(text="fixed")], is_last=True),
    ]
    agent = qwen_agent(monkeypatch, model, manager, tools=[FunctionTool(wait_for_user)])
    original = message("finish the original task")
    state = bind(agent, original)

    async def consume():
        return [event async for event in agent.reply_stream(inputs=[original])]

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        state.enqueue("a", "use PowerShell")
        state.enqueue("b", "use D drive")
        assert not state.injected
        release.set()
        await asyncio.wait_for(task, 5)
        assert len(state.injected) == 2
        assert [m.get_text_content() for m in manager.windows[-1] if m.id in {s.id for s in state.injected}] == ["use PowerShell", "use D drive"]
        assert all(m.id in {s.id for s in model.requests[-1][0]} for m in state.injected)
        ids = [m.id for m in agent.state.context]
        assert len(ids) == len(set(ids)), "assistant segments must have distinct history ids"
        assert state.enqueue("a", "use PowerShell")[0]["status"] == "injected"
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_scroll_pressure_preserves_original_and_all_steers(monkeypatch, scroll):
    model = CountingModel(context=10000, output=6000)
    agent = qwen_agent(monkeypatch, model, scroll)
    original = message("original task")
    agent.state.context = [message("old"), message("x" * 5500, "assistant"), original]
    state = bind(agent, original)
    state.enqueue("a", "first correction")
    state.enqueue("b", "second correction")
    await agent.compress_context()
    state.assert_preserved(agent.state.context)
    assert scroll.last_compress["evicted"] > 0
    assert [m.get_text_content() for m in state.injected] == ["first correction", "second correction"]
    rows = scroll._history._conn.execute("SELECT content FROM conversation_history").fetchall()
    assert "first correction" in "\n".join(row["content"] or "" for row in rows)
    agent.state.cur_iter += 1
    state.enqueue("c", "third correction in the next iteration")
    await agent.compress_context()
    state.assert_preserved(agent.state.context)
    assert len(state.injected) == 3


async def test_steer_during_final_inference_continues_same_reply(monkeypatch):
    original = message("original")
    model = CountingModel(context=100000, output=100)
    agent = qwen_agent(monkeypatch, model, None, enabled=False)
    state = bind(agent, original)

    class InferenceModel(CountingModel):
        async def __call__(self, messages, **kwargs):
            if not self.requests:
                state.enqueue("late", "also fix the test")
            return await super().__call__(messages, **kwargs)

    agent.model = InferenceModel(context=100000, output=100)
    _ = [event async for event in agent.reply_stream(inputs=[original])]
    assert len(agent.model.requests) == 2
    assert len(state.injected) == 1
    assert state.phase == "closing"


async def test_normal_delta_uses_ids_after_scroll_and_cancel_upload_once(monkeypatch):
    original = message("original")
    agent = SimpleNamespace(state=SimpleNamespace(context=[original], reply_id="reply", cur_iter=0))
    state = bind(agent, original)
    state.enqueue("one", "keep this correction")
    await state.prepare(agent)
    # Loaded offsets can be invalidated by Scroll. The new message must still
    # be exported even when the rebuilt window grows beyond its old length.
    ctx = SimpleNamespace(
        session_id="test", agent_id="default", workspace_dir="", request=SimpleNamespace(user_id="user", channel="console", request_context={}),
        extras={"normal_steering": state, "session_loaded_context_len": 1, "session_loaded_context_ids": {"old"}},
    )
    full = {"state": {"context": [m.model_dump(mode="json") for m in [*agent.state.context, message("done", "assistant")]]}}
    uploads = []
    monkeypatch.setattr("qwenpaw.observability.session_delta_langfuse.schedule_session_delta_upload", uploads.append)
    schedule_steering_upload(ctx, full)
    schedule_steering_upload(ctx, full)
    assert len(uploads) == 1
    assert uploads[0]["delta_start"] == 0
    assert uploads[0]["delta_end"] == len(full["state"]["context"])
    injected = [m for m in uploads[0]["delta"] if m["id"] == state.injected[0].id]
    assert len(injected) == 1 and injected[0]["role"] == "user"
    assert injected[0]["metadata"]["response_id"] == "response-test"


async def test_retry_and_late_arrival_respect_iteration_boundary():
    original = message("original")
    agent = SimpleNamespace(state=SimpleNamespace(context=[original], reply_id="reply", cur_iter=0))
    state = bind(agent, original)
    await state.prepare(agent)
    state.enqueue("a", "one")
    await state.prepare(agent)  # already took this iteration's snapshot
    assert not state.injected
    agent.state.cur_iter += 1
    await state.prepare(agent)
    await state.prepare(agent)
    assert len(state.injected) == 1
    assert state.enqueue("a", "one")[1] is True
    with pytest.raises(SteeringError):
        state.enqueue("a", "changed")


async def test_unbound_runs_do_not_accept_steering():
    tracker = TaskTracker()
    await tracker.register_external_task("chat")
    try:
        status = await tracker.get_chat_runtime("chat", "default")
        assert status["run_id"] is None and not status["can_steer"]
        assert status["exists"] is True
        with pytest.raises(SteeringError, match="does not support"):
            await tracker.steer("chat", "anything", "a", "correction")
    finally:
        await tracker.unregister_external_task("chat")


async def test_close_during_redaction_does_not_commit_or_duplicate():
    original = message("original")
    agent = SimpleNamespace(state=SimpleNamespace(context=[original], reply_id="reply", cur_iter=0))
    state = bind(agent, original)
    state.enqueue("one", "correction")
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare(messages):
        entered.set()
        await release.wait()
        return messages

    state.prepare_inputs = prepare
    task = asyncio.create_task(state.prepare(agent))
    await asyncio.wait_for(entered.wait(), 5)
    state.close()
    release.set()
    await asyncio.wait_for(task, 5)
    receipt, duplicate = state.enqueue("one", "correction")
    assert duplicate and receipt["status"] == "not_applied"
    assert agent.state.context == [original] and not state.injected


async def test_compression_cannot_silently_drop_injected_input():
    original = message("original")
    agent = SimpleNamespace(state=SimpleNamespace(context=[original], reply_id="reply", cur_iter=0))
    state = bind(agent, original)
    state.enqueue("one", "correction")
    await state.prepare(agent)
    with pytest.raises(RuntimeError, match="compression removed"):
        state.assert_preserved([original])
    with pytest.raises(RuntimeError, match="compression removed"):
        state.assert_preserved(state.injected)


async def test_steering_reuses_existing_input_redaction(monkeypatch):
    from qwenpaw.observability.model_input_redaction_guard import TurnRedactionScope

    original = message("original")
    agent = SimpleNamespace(state=SimpleNamespace(context=[original], reply_id="reply", cur_iter=0))
    state = bind(agent, original)
    scope = TurnRedactionScope(agent=agent)
    scope.enabled = True
    scope.config = {"mode": "auto"}

    def redact(messages, **kwargs):
        replacement = messages[0].model_copy(deep=True)
        replacement.content = [TextBlock(text="[redacted]")]
        return [replacement], SimpleNamespace(matched=True)

    monkeypatch.setattr("qwenpaw.observability.model_input_redaction_guard.redact_model_protocol_value", redact)
    state.prepare_inputs = scope.prepare_initial_inputs
    state.enqueue("one", "private text")
    await state.prepare(agent)
    assert state.injected[0].get_text_content() == "[redacted]"
    assert state.injected[0].metadata["message_id"] == "one"


async def test_cancel_persists_session_even_when_upload_scheduling_fails(monkeypatch):
    from qwenpaw.runtime.runtime import Runtime

    original = message("original")
    agent = SimpleNamespace(state=SimpleNamespace(context=[original], reply_id="reply", cur_iter=0))
    state = bind(agent, original)
    state.enqueue("one", "correction")
    await state.prepare(agent)
    agent.state_dict = lambda: {"state": {"context": [m.model_dump(mode="json") for m in agent.state.context]}}
    save = AsyncMock()
    workspace = SimpleNamespace(session=SimpleNamespace(save_session_state=save))
    ctx = SimpleNamespace(agent=agent, workspace=workspace, session_id="test", request=SimpleNamespace(), extras={"normal_steering": state})

    def fail(*_):
        raise RuntimeError("upload unavailable")

    monkeypatch.setattr("qwenpaw.hooks.session.session_hook.schedule_steering_upload", fail)
    await Runtime(workspace=workspace, app_services=None)._try_save_on_cancel(ctx)
    save.assert_awaited_once()


@pytest.mark.parametrize("as_model", [False, True])
@pytest.mark.parametrize("run_id", [None, "5f576e5a-4726-4fbb-a532-5f43af8dd17b"])
def test_console_preserves_optional_client_run_id(as_model, run_id):
    from qwenpaw.app.channels.console.channel import ConsoleChannel
    from qwenpaw.app.routers.console import _extract_session_and_payload
    from qwenpaw.schemas import AgentRequest

    body = {"session_id": "session", "input": [{"role": "user", "content": [{"type": "text", "text": "task"}]}]}
    if run_id is not None:
        body["run_id"] = run_id
    payload = _extract_session_and_payload(AgentRequest(**body) if as_model else body)
    request = ConsoleChannel(process=AsyncMock(), enabled=True, bot_prefix="").build_agent_request_from_native(payload)
    assert getattr(request, "run_id", None) == run_id
    assert ("run_id" in payload["meta"]) is (run_id is not None)


@pytest.mark.parametrize("stop", [False, True])
async def test_tracked_runtime_uploads_injected_input_on_completion_and_stop(monkeypatch, tmp_path, stop):
    from test_edge_cloud import config, context
    from test_edge_cloud_session_process import runtime_for

    entered, release = asyncio.Event(), asyncio.Event()
    inferred, finish = asyncio.Event(), asyncio.Event()

    async def wait_for_user():
        entered.set()
        await release.wait()
        return ToolChunk(content=[TextBlock(text="tool finished")])

    class ControlledModel(CountingModel):
        async def __call__(self, messages, **kwargs):
            if self.requests:
                inferred.set()
                await finish.wait()
            return await super().__call__(messages, **kwargs)

    model = ControlledModel(context=100000, output=100)
    model.responses = [
        ChatResponse(content=[ToolCallBlock(id="tool", name="wait_for_user", input="{}")], is_last=True),
        ChatResponse(content=[TextBlock(text="fixed")], is_last=True),
    ]
    ctx = context(tmp_path)
    ctx.request.run_id = "client-run"
    ctx.agent = qwen_agent(monkeypatch, model, None, enabled=False, tools=[FunctionTool(wait_for_user)])
    runtime, session, uploads = runtime_for(monkeypatch, ctx, config(enabled=False))
    tracker = TaskTracker()
    ctx.workspace.task_tracker = tracker

    async def stream(_):
        async for event in runtime.run(ctx.request):
            yield str(event)

    queue, _ = await tracker.attach_or_start("chat", {}, stream)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        status = await tracker.get_chat_runtime("chat", "default")
        assert status["can_steer"] and status["run_id"] == "client-run"
        await tracker.steer("chat", "client-run", "guide", "use the corrected path")
        release.set()
        await asyncio.wait_for(inferred.wait(), 5)
        if stop:
            await tracker.request_stop("chat")
        else:
            finish.set()
        _ = [item async for item in tracker.stream_from_queue(queue, "chat")]
        assert len(uploads) == 1
        injected = [m for m in uploads[0]["delta"] if (m.get("metadata") or {}).get("message_id") == "guide"]
        assert len(injected) == 1
        assert injected[0]["metadata"]["run_id"] == "client-run"
        assert injected[0]["metadata"]["chat_id"] == "chat"
        assert injected[0]["metadata"]["agent_id"] == "default"
        assert any(m["id"] == injected[0]["id"] for m in session.saved["state"]["context"])
        assert (await tracker.get_chat_runtime("chat", "default"))["exists"] is False
    finally:
        release.set()
        finish.set()
        await tracker.request_stop("chat")
        await tracker.wait_all_done(5)


@pytest.mark.parametrize("enabled,run_id", [(False, None), (True, "client-run")])
async def test_legacy_and_collaborative_fallback_exist_without_steering(monkeypatch, tmp_path, enabled, run_id):
    from test_edge_cloud import config, context
    from test_edge_cloud_session_process import runtime_for

    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingModel(CountingModel):
        async def __call__(self, messages, **kwargs):
            entered.set()
            await release.wait()
            return await super().__call__(messages, **kwargs)

    ctx = context(tmp_path)
    if run_id:
        ctx.request.run_id = run_id
    ctx.agent = qwen_agent(monkeypatch, BlockingModel(context=100000, output=100), None, enabled=False)
    runtime, _, _ = runtime_for(monkeypatch, ctx, config(enabled=enabled))
    tracker = TaskTracker()
    ctx.workspace.task_tracker = tracker
    monkeypatch.setattr("qwenpaw.runtime.edge_cloud.orchestrator.EdgeCloudOrchestrator.should_handle", lambda *_: False)

    async def stream(_):
        async for event in runtime.run(ctx.request):
            yield str(event)

    queue, _ = await tracker.attach_or_start("chat", {}, stream)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        status = await tracker.get_chat_runtime("chat", "default")
        assert status == {"agent_id": "default", "chat_id": "chat", "exists": True,
                          "status": "running", "run_id": None, "can_steer": False}
        assert not hasattr(ctx.agent, "_normal_steering")
        with pytest.raises(SteeringError, match="does not support"):
            await tracker.steer("chat", "client-run", "guide", "correction")
        release.set()
        _ = [item async for item in tracker.stream_from_queue(queue, "chat")]
    finally:
        release.set()
        await tracker.request_stop("chat")
        await tracker.wait_all_done(5)
