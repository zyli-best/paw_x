"""Plugin SSE delivery through real model/tool execution and request endings."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatResponse
from agentscope.tool import FunctionTool, ToolChunk
from fastapi import FastAPI

from qwenpaw.app.channels.console.channel import ConsoleChannel
from qwenpaw.app.task_tracker import TaskTracker
from qwenpaw.plugins import (
    CUSTOM_EVENT_API_VERSION, PluginApi, emit_custom_event, emit_custom_event_sync,
)
from qwenpaw.runtime.executor import AgentExecutor

from test_edge_cloud import Model, call, config, context, route, text
from test_edge_cloud_session_process import runtime_for
from test_request_budget import CountingModel, qwen_agent


async def drain(tracker, queue, key="chat"):
    return [item async for item in tracker.stream_from_queue(queue, key)]


def wire_stream(runtime, request):
    request.user_id = "user"
    request.session_id = "session"
    request.channel = "console"
    channel = ConsoleChannel(process=runtime.run, enabled=True, bot_prefix="")

    async def stream(_):
        async for item in channel.stream_one(request):
            yield item

    return stream


def payloads(events):
    return [json.loads(item.removeprefix("data: ")) for item in events]


@pytest.mark.parametrize("location", ["middleware", "tool", "thread_tool"])
async def test_custom_event_arrives_while_actual_model_or_tool_is_waiting(
    monkeypatch, tmp_path, location,
):
    entered, release, received = asyncio.Event(), asyncio.Event(), asyncio.Event()
    api = PluginApi("kb-plugin", {})

    async def retrieve():
        if location == "thread_tool":
            await asyncio.to_thread(
                emit_custom_event_sync, type="platform_kb_called", data="知识库1,知识库2",
            )
        else:
            await api.emit_custom_event(type="platform_kb_called", data="知识库1,知识库2")
        entered.set()
        await release.wait()

    class RetrievalMiddleware(MiddlewareBase):
        async def on_model_call(self, agent, input_kwargs, next_handler):
            await retrieve()
            return await next_handler(**input_kwargs)

    async def search():
        await retrieve()
        return ToolChunk(content=[TextBlock(text="retrieval result")])

    model = CountingModel(context=100000, output=100)
    middleware = location == "middleware"
    if not middleware:
        model.responses = [
            ChatResponse(content=[ToolCallBlock(id="search", name="search", input="{}")], is_last=True),
            ChatResponse(content=[TextBlock(text="answer")], is_last=True),
        ]
    ctx = context(tmp_path)
    ctx.agent = qwen_agent(
        monkeypatch, model, None, enabled=False,
        middlewares=[RetrievalMiddleware()] if middleware else [],
        tools=[] if middleware else [FunctionTool(search)],
    )
    runtime, session, _ = runtime_for(monkeypatch, ctx, config(enabled=False))
    tracker, events = TaskTracker(), []
    queue, _ = await tracker.attach_or_start("chat", {}, wire_stream(runtime, ctx.request))

    async def collect():
        async for raw in tracker.stream_from_queue(queue, "chat"):
            events.append(json.loads(raw.removeprefix("data: ")))
            if events[-1].get("object") == "custom_event":
                received.set()

    consumer = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(received.wait(), 5)
        assert not release.is_set()
        event = next(item for item in events if item.get("object") == "custom_event")
        assert event["data"] == "知识库1,知识库2"
        assert event["response_id"] == events[0]["id"]
        assert event["session_id"] == ctx.session_id
        reconnect = await tracker.attach("chat", response_id=event["response_id"])
        assert reconnect is not None
        release.set()
        replay = await asyncio.wait_for(drain(tracker, reconnect), 5)
        await asyncio.wait_for(consumer, 5)
        assert payloads(replay) == events
        assert [item["sequence_number"] for item in events] == list(range(1, len(events) + 1))
        assert len([item for item in events if item.get("object") == "custom_event"]) == 1
        assert "platform_kb_called" not in json.dumps(events[-1]["output"])
        assert "platform_kb_called" not in json.dumps(session.saved)
        assert "知识库1" not in json.dumps(model.requests, default=str, ensure_ascii=False)
    finally:
        release.set()
        await tracker.request_stop("chat")
        await asyncio.wait_for(consumer, 5)


@pytest.mark.parametrize("ending", ["complete", "fail", "cancel"])
async def test_accepted_events_precede_terminal_and_replay_after_disconnect(
    monkeypatch, tmp_path, ending,
):
    accepted, release = asyncio.Event(), asyncio.Event()
    ctx = context(tmp_path)
    ctx.request.run_id = "client-run"
    runtime, _, _ = runtime_for(monkeypatch, ctx, config(enabled=False))
    monkeypatch.setattr(runtime, "_try_save_on_cancel", AsyncMock())

    async def execute(self, msgs):
        await emit_custom_event(type="platform_kb_called", data="first")
        await emit_custom_event(type="platform_kb_called", data="second")
        accepted.set()
        await release.wait()
        if ending == "fail":
            raise ValueError("execution failed")
        if False:
            yield

    monkeypatch.setattr(AgentExecutor, "run", execute)
    tracker = TaskTracker()
    queue, _ = await tracker.attach_or_start(
        "chat", {"meta": {"run_id": "client-run"}}, wire_stream(runtime, ctx.request),
    )
    await asyncio.wait_for(accepted.wait(), 5)
    # Detach before finishing, including the race where accepted events have
    # not yet been consumed by the request task.
    await tracker.detach_subscriber("chat", queue)
    if ending == "cancel":
        assert await tracker.request_stop("chat")
    else:
        release.set()
    assert await tracker.wait_all_done(5)
    assert await tracker.get_status("chat") == "idle"
    replay = await tracker.attach("chat", run_id="client-run")
    assert replay is not None
    raw = await asyncio.wait_for(drain(tracker, replay), 5)
    events = payloads(raw)
    custom = [item for item in events if item.get("object") == "custom_event"]
    assert [item["data"] for item in custom] == ["first", "second"]
    terminal = next(item for item in events if item.get("object") == "response"
                    and item["status"] in {"completed", "failed", "cancelled"})
    assert all(item["sequence_number"] < terminal["sequence_number"] for item in custom)
    replay_again = await tracker.attach("chat", response_id=custom[0]["response_id"])
    assert await drain(tracker, replay_again) == raw
    assert await tracker.attach("chat", response_id="another-request") is None


async def test_context_isolation_validation_and_closed_request(monkeypatch, tmp_path):
    assert CUSTOM_EVENT_API_VERSION == 1
    with pytest.raises(RuntimeError, match="active Paw request"):
        await emit_custom_event(type="platform_kb_called", data="outside")

    late_release = asyncio.Event()
    background = []

    async def late():
        await late_release.wait()
        with pytest.raises(RuntimeError, match="no longer accepts"):
            await emit_custom_event(type="platform_kb_called", data="late")

    async def execute(self, msgs):
        identity = self._envelope.response.session_id
        with pytest.raises(ValueError):
            await emit_custom_event(type="", data="bad")
        with pytest.raises(TypeError):
            await emit_custom_event(type="invalid", data=object())
        data = {"ids": [identity]}
        await emit_custom_event(type="example", data=data)
        data["ids"].append("mutated")
        background.append(asyncio.create_task(late()))
        await asyncio.sleep(0)
        if False:
            yield

    monkeypatch.setattr(AgentExecutor, "run", execute)
    runtimes = []
    for identity in ("first", "second"):
        ctx = context(tmp_path / identity)
        ctx.session_id = identity
        runtime, _, _ = runtime_for(monkeypatch, ctx, config(enabled=False))
        runtimes.append((runtime, ctx.request))

    async def collect(runtime, request):
        return [item async for item in runtime.run(request)]

    streams = await asyncio.gather(*(collect(*pair) for pair in runtimes))
    for identity, events in zip(("first", "second"), streams):
        custom = [item for item in events if item.object == "custom_event"]
        assert len(custom) == 1 and custom[0].data == {"ids": [identity]}
        assert custom[0].response_id == events[0].id
        assert events[0].status == "created", "Published mutable envelopes must be snapshots"
    late_release.set()
    await asyncio.wait_for(asyncio.gather(*background), 5)
    with pytest.raises(RuntimeError, match="active Paw request"):
        emit_custom_event_sync(type="example", data=None)


async def test_replay_retention_selects_execution_and_expires(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("qwenpaw.app.task_tracker.time", SimpleNamespace(monotonic=lambda: now[0]))
    tracker = TaskTracker(replay_ttl_seconds=300, max_completed_runs=2)

    async def stream(identity):
        yield f'data: {{"object":"response","id":"{identity}"}}\n\n'

    for identity in ("one", "two", "three"):
        queue, _ = await tracker.attach_or_start("chat", identity, stream)
        await drain(tracker, queue)
    assert await tracker.attach("chat", response_id="one") is None
    assert await tracker.attach("chat", response_id="two") is not None
    replay = await tracker.attach("chat")
    assert payloads(await drain(tracker, replay))[0]["id"] == "three"
    now[0] = 301
    assert await tracker.attach("chat") is None


async def test_closing_the_stream_stops_event_producer_and_resets_context(monkeypatch, tmp_path):
    stopped = asyncio.Event()
    ctx = context(tmp_path)
    runtime, _, _ = runtime_for(monkeypatch, ctx, config(enabled=False))

    async def execute(self, msgs):
        try:
            await emit_custom_event(type="example", data="accepted")
            await asyncio.Event().wait()
        finally:
            stopped.set()
        if False:
            yield

    monkeypatch.setattr(AgentExecutor, "run", execute)
    stream = runtime.run(ctx.request)
    async for event in stream:
        if event.object == "custom_event":
            break
    await stream.aclose()
    assert stopped.is_set()
    with pytest.raises(RuntimeError, match="active Paw request"):
        await emit_custom_event(type="example", data="after close")


async def test_replay_identity_selects_old_turn_while_next_turn_is_active():
    tracker = TaskTracker()
    started, release = asyncio.Event(), asyncio.Event()

    async def stream(identity):
        yield f'data: {{"object":"response","id":"{identity}"}}\n\n'
        if identity == "new":
            started.set()
            await release.wait()

    original, _ = await tracker.attach_or_start("chat", "old", stream)
    old = await drain(tracker, original)
    active, _ = await tracker.attach_or_start("chat", "new", stream)
    try:
        await asyncio.wait_for(started.wait(), 5)
        replay = await tracker.attach("chat", response_id="old")
        assert await drain(tracker, replay) == old
        assert await tracker.get_status("chat") == "running"
    finally:
        release.set()
        await drain(tracker, active)


async def test_collaborative_tool_event_is_live_and_not_saved_as_model_input(monkeypatch, tmp_path):
    entered, release, received = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def read_file():
        await emit_custom_event(type="platform_kb_called", data="private-kb-ids")
        entered.set()
        await release.wait()
        return ToolChunk(content=[TextBlock(text="found")])

    cloud = Model("cloud", [
        call("submit_route_decision", route("device_tools", required_tools=["read_file"])),
        call("submit_verification", {"protocol_version": "edge_cloud_verifier.v1", "passed": True}),
    ])
    lower = Model("device", [call("read_file", {}), text("answer")])
    ctx = context(tmp_path, [FunctionTool(read_file)], cloud)
    runtime, session, uploads = runtime_for(
        monkeypatch, ctx, config(session_process_enabled=True, device_max_tool_rounds=3),
    )
    monkeypatch.setattr(
        "qwenpaw.agents.model_factory.create_openai_compatible_model_and_formatter",
        lambda **_: (lower, None),
    )
    events = []

    async def collect():
        async for event in runtime.run(ctx.request):
            events.append(event)
            if event.object == "custom_event":
                received.set()

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(received.wait(), 5)
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, 10)
        assert [event.sequence_number for event in events] == list(range(1, len(events) + 1))
        assert "private-kb-ids" not in json.dumps(session.saved)
        assert "private-kb-ids" not in json.dumps(uploads)
        assert "private-kb-ids" not in json.dumps(cloud.requests + lower.requests, default=str)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_reconnect_http_preserves_retained_wire_and_identity(monkeypatch):
    from qwenpaw.app.routers import console

    tracker = TaskTracker()

    async def stream(_):
        yield 'data: {"object":"response","id":"response-one","sequence_number":1}\n\n'
        yield 'data: {"object":"custom_event","type":"platform_kb_called","data":"kb","response_id":"response-one","sequence_number":2}\n\n'

    queue, _ = await tracker.attach_or_start("chat", {"run_id": "run-one"}, stream)
    original = await drain(tracker, queue)
    channel = SimpleNamespace(resolve_session_id=lambda **_: "session")
    workspace = SimpleNamespace(
        task_tracker=tracker,
        channel_manager=SimpleNamespace(get_channel=AsyncMock(return_value=channel)),
        chat_manager=SimpleNamespace(get_or_create_chat=AsyncMock(
            return_value=SimpleNamespace(id="chat", name="existing"),
        )),
    )
    monkeypatch.setattr(console, "get_agent_for_request", AsyncMock(return_value=workspace))
    app = FastAPI()
    app.include_router(console.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"session_id": "session", "reconnect": True, "input": [],
                "response_id": "response-one", "run_id": "run-one"}
        response = await client.post("/api/console/chat", json=body)
        assert response.status_code == 200
        assert response.text == "".join(original)
        missing = await client.post("/api/console/chat", json={**body, "response_id": "wrong"})
        assert missing.status_code == 404
