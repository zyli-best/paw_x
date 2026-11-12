"""Collaborative guidance uses only natural inference boundaries."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import TextBlock
from agentscope.tool import FunctionTool, ToolChunk

from qwenpaw.app.task_tracker import RunControlError, TaskTracker
from qwenpaw.runtime.edge_cloud.safety import EdgeCloudRedaction
from qwenpaw.runtime.edge_cloud.steering import EdgeCloudSteeringController
from qwenpaw.runtime.envelope import Envelope
from qwenpaw.runtime.session_views import restore_session_view
from test_edge_cloud import Model, call, clean_capture, config, context, route, text
from test_edge_cloud_session_process import runtime_for
from test_request_budget import CountingModel, message, qwen_agent, scroll


class ObservedModel(Model):
    def __init__(self, name, responses, callback=None):
        super().__init__(name, responses)
        self.callback = callback

    async def __call__(self, messages, tools=None, **kwargs):
        if self.callback is not None:
            await self.callback(len(self.requests) + 1, messages)
        return await super().__call__(messages, tools=tools, **kwargs)


def setup(
    monkeypatch,
    tmp_path,
    cloud,
    device,
    *,
    tools=(),
    recording=False,
    tracing=False,
    run_id="client-run",
    request_context=None,
    session=None,
):
    ctx = context(tmp_path)
    ctx.request = SimpleNamespace(
        run_id=run_id,
        user_id="user",
        channel="console",
        request_context=request_context or {},
    )
    ctx.agent = qwen_agent(monkeypatch, cloud, None, enabled=False, tools=tools)
    runtime, session, uploads = runtime_for(
        monkeypatch,
        ctx,
        config(session_process_enabled=recording, trace_stream_enabled=tracing),
        session=session,
    )
    if request_context:
        monkeypatch.setattr(
            "qwenpaw.runtime.builder.AgentBuilder._build_request_context",
            lambda _: request_context,
        )
    monkeypatch.setattr(
        "qwenpaw.agents.model_factory.create_openai_compatible_model_and_formatter",
        lambda **_: (device, None),
    )
    tracker = TaskTracker()
    ctx.workspace.task_tracker = tracker
    return ctx, runtime, tracker, session, uploads


async def run(setup_result, *, allow_error=False, on_event=None):
    ctx, runtime, tracker, _, _ = setup_result
    events = []

    async def stream(_):
        async for event in runtime.run(ctx.request):
            events.append(event.model_copy(deep=True))
            if on_event is not None:
                await on_event(event)
            yield str(event)

    queue, _ = await tracker.attach_or_start("chat", {}, stream)

    async def drain():
        return [event async for event in tracker.stream_from_queue(queue, "chat")]

    try:
        await asyncio.wait_for(drain(), 10)
        if not allow_error:
            assert getattr(ctx, "error", None) is None
        assert not (await tracker.get_chat_runtime("chat", "default"))["exists"]
        return events
    finally:
        await tracker.request_stop("chat")
        await tracker.wait_all_done(5)


def guidance(messages):
    return [
        m
        for m in messages
        if (m.metadata or {}).get("qwenpaw_message_kind") == "edge_cloud_steering"
    ]


@pytest.mark.parametrize("recording", [False, True])
@pytest.mark.parametrize("tracing", [False, True])
@pytest.mark.parametrize("compact", [False, True])
async def test_router_is_not_retried_and_selected_execution_sees_two_guides(
    monkeypatch, tmp_path, recording, tracing, compact
):
    monkeypatch.setattr(
        "qwenpaw.runtime.executor.COMPACT_HISTORICAL_TOOL_CONTEXT", compact
    )

    async def during_router(index, messages):
        assert index == 1
        status = await tracker.get_chat_runtime("chat", "default")
        assert status["can_steer"] and status["run_id"] == "client-run"
        first, duplicate = await asyncio.gather(
            *[tracker.steer("chat", "client-run", "a", "use D drive") for _ in range(2)]
        )
        assert sorted([first[1], duplicate[1]]) == [False, True]
        with pytest.raises(RunControlError):
            await tracker.steer("chat", "client-run", "a", "other text")
        await tracker.steer("chat", "client-run", "b", "check before editing")
        assert not guidance(messages)

    cloud = ObservedModel(
        "cloud", [call("submit_route_decision", route())], during_router
    )
    device = Model("device", [text("fixed")])
    result = setup(
        monkeypatch, tmp_path, cloud, device, recording=recording, tracing=tracing
    )
    ctx, _, tracker, session, uploads = result
    await run(result)
    assert len(cloud.requests) == len(device.requests) == 1
    assert [m.get_text_content() for m in guidance(device.requests[0][0])] == [
        "use D drive",
        "check before editing",
    ]
    assert not hasattr(ctx.agent, "_normal_steering")
    restored = restore_session_view(SimpleNamespace(extras={}), deepcopy(session.saved))
    ids = [m["id"] for m in restored["state"]["context"]]
    assert len(ids) == len(set(ids))
    assert ids.count(ctx.input_msgs[0].id) == 1
    assert len(uploads) == 1
    uploaded = [
        m
        for m in uploads[0]["delta"]
        if (m.get("metadata") or {}).get("qwenpaw_message_kind")
        == "edge_cloud_steering"
    ]
    assert [m["metadata"]["message_id"] for m in uploaded] == ["a", "b"]
    assert all(m["role"] == "user" for m in uploaded)
    assert uploads[0]["edge_cloud"]["flow_id"] == uploaded[0]["metadata"]["flow_id"]
    assert uploads[0]["model_requests"][-1]["steering_sequence"] == 2


@pytest.mark.parametrize("passed", [False, True])
async def test_verifier_output_is_used_without_retry_or_forced_continuation(
    monkeypatch, tmp_path, passed
):
    async def during_verifier(index, messages):
        if index == 2:
            await tracker.steer("chat", "client-run", "late", "use corrected config")
            assert not guidance(messages)

    cloud = ObservedModel(
        "cloud",
        [
            call("submit_route_decision", route("device_tools")),
            call(
                "submit_verification",
                {
                    "protocol_version": "edge_cloud_verifier.v1",
                    "passed": passed,
                    "failure_reason": "" if passed else "fix required",
                },
            ),
            text("cloud fixed"),
        ],
        during_verifier,
    )
    device = Model("device", [text("candidate")])
    result = setup(monkeypatch, tmp_path, cloud, device, recording=True, tracing=True)
    ctx, _, tracker, session, uploads = result
    await run(result)
    controller = ctx.extras["edge_cloud_steering"]
    assert len(device.requests) == 1
    assert len(cloud.requests) == (2 if passed else 3)
    assert controller.records["late"]["status"] == (
        "not_applied" if passed else "injected"
    )
    if not passed:
        assert [m.get_text_content() for m in guidance(cloud.requests[-1][0])] == [
            "use corrected config"
        ]
    uploaded = [
        m
        for m in uploads[0]["delta"]
        if (m.get("metadata") or {}).get("message_id") == "late"
    ]
    assert len(uploaded) == (0 if passed else 1)
    if passed:
        assert "use corrected config" not in json.dumps(session.saved)
    assert (
        uploads[0]["edge_cloud"]["steering"][0]["status"]
        == controller.records["late"]["status"]
    )


async def test_guide_during_last_cloud_inference_is_not_injected(monkeypatch, tmp_path):
    async def late(index, _):
        if index == 2:
            await tracker.steer("chat", "client-run", "late", "do more")

    cloud = ObservedModel(
        "cloud",
        [call("submit_route_decision", route("complex_or_negative")), text("done")],
        late,
    )
    result = setup(monkeypatch, tmp_path, cloud, Model("device"))
    ctx, _, tracker, session, uploads = result
    await run(result)
    assert len(cloud.requests) == 2
    assert not ctx.extras["edge_cloud_steering"].injected
    assert "do more" not in json.dumps(session.saved)
    assert uploads[0]["edge_cloud"]["steering"][0]["status"] == "not_applied"


async def test_existing_router_read_loop_receives_guidance_once(monkeypatch, tmp_path):
    async def get_skill_count():
        return ToolChunk(content=[TextBlock(text="0")])

    async def first(index, _):
        if index == 1:
            await tracker.steer("chat", "client-run", "a", "prefer D drive")

    cloud = ObservedModel(
        "cloud",
        [call("get_skill_count", {}), call("submit_route_decision", route())],
        first,
    )
    device = Model("device", [text("fixed")])
    result = setup(
        monkeypatch, tmp_path, cloud, device, tools=[FunctionTool(get_skill_count)]
    )
    _, _, tracker, _, _ = result
    await run(result)
    assert len(cloud.requests) == 2
    assert (
        len(guidance(cloud.requests[1][0])) == len(guidance(device.requests[0][0])) == 1
    )


@pytest.mark.parametrize("stop", [False, True])
async def test_tool_batch_waits_then_injects_and_saves_on_completion_or_stop(
    monkeypatch, tmp_path, stop
):
    async def read_file(file_path: str):
        await tracker.steer("chat", "client-run", "a", "first correction")
        await tracker.steer("chat", "client-run", "b", "second correction")
        assert not ctx.extras["edge_cloud_steering"].injected
        return ToolChunk(content=[TextBlock(text="tool completed")])

    async def second(index, messages):
        if index == 2:
            assert len(guidance(messages)) == 2
            if stop:
                await tracker.request_stop("chat")
                await asyncio.Event().wait()

    cloud = Model(
        "cloud",
        [
            call("submit_route_decision", route("device_tools", need_tools=True)),
            call(
                "submit_verification",
                {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
            ),
        ],
    )
    device = ObservedModel(
        "device", [call("read_file", {"file_path": "a.txt"}), text("fixed")], second
    )
    result = setup(
        monkeypatch,
        tmp_path,
        cloud,
        device,
        tools=[FunctionTool(read_file)],
        recording=True,
        tracing=True,
    )
    ctx, _, tracker, session, uploads = result
    await run(result, allow_error=stop)
    assert len(ctx.extras["edge_cloud_steering"].injected) == 2
    assert len(uploads) == 1
    assert (
        len(
            [
                m
                for m in uploads[0]["delta"]
                if (m.get("metadata") or {}).get("qwenpaw_message_kind")
                == "edge_cloud_steering"
            ]
        )
        == 2
    )
    restored = restore_session_view(SimpleNamespace(extras={}), deepcopy(session.saved))
    ids = [m["id"] for m in restored["state"]["context"]]
    assert len(ids) == len(set(ids))
    assert all(m.id in ids for m in ctx.extras["edge_cloud_steering"].injected)


async def test_scroll_protects_root_and_all_guidance(monkeypatch, scroll):
    model = CountingModel(context=10000, output=6000)
    agent = qwen_agent(monkeypatch, model, scroll)
    root = message("original task")
    agent.state.context = [message("old"), message("x" * 5500, "assistant"), root]
    ctx = SimpleNamespace(agent=agent, agent_id="default", input_msgs=[root], extras={})
    controller = EdgeCloudSteeringController(ctx, "response", "run", "chat")
    controller.start(EdgeCloudRedaction({}), "flow")
    agent._context_input_policy = controller
    controller.enqueue("a", "first correction")
    controller.enqueue("b", "second correction")
    await agent.compress_context()
    controller.validate(agent.state.context)
    assert scroll.last_compress["evicted"] > 0
    agent.state.cur_iter += 1
    controller.enqueue("c", "third correction")
    await agent.compress_context()
    controller.validate(agent.state.context)
    assert len(controller.injected) == 3


async def test_missing_run_id_keeps_collaboration_unsupported(monkeypatch, tmp_path):
    async def first(index, _):
        if index == 1:
            status = await tracker.get_chat_runtime("chat", "default")
            assert (
                status["exists"]
                and not status["can_steer"]
                and status["run_id"] is None
            )

    cloud = ObservedModel("cloud", [call("submit_route_decision", route())], first)
    result = setup(
        monkeypatch, tmp_path, cloud, Model("device", [text("done")]), run_id=None
    )
    ctx, _, tracker, _, _ = result
    await run(result)
    assert "edge_cloud_steering" not in ctx.extras


@pytest.mark.parametrize(
    "mode,decision,redacted",
    [
        ("off", None, False),
        ("auto", None, True),
        ("confirm", "approved", False),
        ("confirm", "denied", True),
    ],
)
async def test_real_session_redaction_and_upload_copy(
    monkeypatch, tmp_path, mode, decision, redacted
):
    from qwenpaw.app.chats.session import SafeJSONSession
    from qwenpaw.runtime.edge_cloud.safety import _Scope
    from qwenpaw.observability.session_delta_redactor import (
        redact_session_delta_payload,
    )

    private = "private-audit-example"
    rc = {
        "model_input_redaction": {"mode": mode},
        "sensitive_words": {"patterns": [{"type": "custom", "keyword": private}]},
    }
    confirmations = AsyncMock(return_value=decision or "denied")
    if decision is not None:
        monkeypatch.setattr(_Scope, "_decision_for", confirmations)

    async def first(index, _):
        if index == 1:
            await tracker.steer("chat", "client-run", "secret", private)

    cloud = ObservedModel("cloud", [call("submit_route_decision", route())], first)
    store = SafeJSONSession(str(tmp_path / "sessions"))
    result = setup(
        monkeypatch,
        tmp_path,
        cloud,
        Model("device", [text("done")]),
        recording=True,
        request_context=rc,
        session=store,
    )
    ctx, _, tracker, _, uploads = result
    await run(result)
    path = tmp_path / "sessions" / "console" / "user_edge-test.json"
    assert path.is_file()
    assert (private not in path.read_text(encoding="utf-8")) is redacted
    raw = json.loads(path.read_text(encoding="utf-8"))["agent"]
    restored = restore_session_view(SimpleNamespace(extras={}), raw)
    assert any(
        (m.get("metadata") or {}).get("message_id") == "secret"
        for m in restored["state"]["context"]
    )
    outgoing = redact_session_delta_payload(
        uploads[0], sensitive_words=rc["sensitive_words"]
    )
    assert private not in json.dumps(outgoing["delta"])
    assert confirmations.await_count == (1 if decision is not None else 0)


@pytest.mark.parametrize("compact", [False, True])
async def test_real_scroll_and_optional_history_restore_keep_guidance(
    monkeypatch, tmp_path, scroll, compact
):
    from qwenpaw.runtime.edge_cloud.execution import TaskInputView
    from qwenpaw.runtime.executor import AgentExecutor

    cloud = CountingModel(context=10000, output=6000)
    agent = qwen_agent(monkeypatch, cloud, scroll)
    agent.state.context = [
        message("history " + str(i), "assistant" if i % 2 else "user")
        for i in range(20)
    ]
    root = message("original task")
    ctx = SimpleNamespace(agent=agent, agent_id="default", input_msgs=[root], extras={})
    controller = EdgeCloudSteeringController(ctx, "response", "run", "chat")
    controller.start(EdgeCloudRedaction({}), "flow")
    agent._context_input_policy = controller
    controller.enqueue("a", "correction")
    monkeypatch.setattr(
        "qwenpaw.runtime.executor.COMPACT_HISTORICAL_TOOL_CONTEXT", compact
    )
    _ = [
        event
        async for event in AgentExecutor(agent, Envelope(session_id="test")).run([root])
    ]
    controller.validate(agent.state.context)
    assert len(controller.injected) == 1
    projected = TaskInputView(agent, message("execution task")).project(
        [message("system", "system"), *agent.state.context]
    )
    controller.validate_guidance(projected)


async def test_stop_before_first_model_completes_keeps_v2_upload(monkeypatch, tmp_path):
    async def block_first(index, _):
        assert index == 1
        assert ctx.extras["edge_cloud_steering"].injected
        await tracker.request_stop("chat")
        await asyncio.Event().wait()

    cloud = ObservedModel(
        "cloud", [call("submit_route_decision", route())], block_first
    )
    result = setup(monkeypatch, tmp_path, cloud, Model("device"), recording=True)
    ctx, _, tracker, session, uploads = result

    async def before_router(event):
        if event.object == "response":
            state = await tracker.get_chat_runtime("chat", "default")
            if state["can_steer"]:
                await tracker.steer(
                    "chat", "client-run", "early", "keep this correction"
                )

    await run(result, allow_error=True, on_event=before_router)
    assert len(uploads) == 1
    assert uploads[0]["protocol"] == "edge_cloud_trace.v2"
    assert any(
        (m.get("metadata") or {}).get("message_id") == "early"
        for m in uploads[0]["delta"]
    )
    assert "keep this correction" in json.dumps(session.saved)


async def test_http_endpoints_dispatch_to_independent_backend(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from qwenpaw.app.routers import console

    tracker = TaskTracker()
    ready, done = asyncio.Event(), asyncio.Event()
    ctx = SimpleNamespace(agent_id="default", input_msgs=[], extras={})

    async def stream(_):
        controller = await tracker.bind_run_control(
            lambda chat: EdgeCloudSteeringController(ctx, "response", "run", chat)
        )
        controller.start(EdgeCloudRedaction({}), "flow")
        ready.set()
        await done.wait()
        yield "data: {}\n\n"

    workspace = SimpleNamespace(
        agent_id="default",
        task_tracker=tracker,
        chat_manager=SimpleNamespace(get_chat=AsyncMock(return_value=object())),
    )
    monkeypatch.setattr(
        console, "get_agent_for_request", AsyncMock(return_value=workspace)
    )
    app = FastAPI()
    app.include_router(console.router, prefix="/api")
    queue, _ = await tracker.attach_or_start("chat", {}, stream)
    await asyncio.wait_for(ready.wait(), 5)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (
                await client.get("/api/console/chat/status", params={"chat_id": "chat"})
            ).json()["can_steer"]
            body = dict(chat_id="chat", run_id="run", message_id="a", text="correction")
            assert (
                await client.post("/api/console/chat/steer", json=body)
            ).status_code == 202
            assert (
                await client.post("/api/console/chat/steer", json=body)
            ).status_code == 200
            conflict = await client.post(
                "/api/console/chat/steer", json={**body, "text": "changed"}
            )
            assert (
                conflict.status_code == 409
                and conflict.json()["detail"]["code"] == "MESSAGE_ID_CONFLICT"
            )
            assert (
                await client.post(
                    "/api/console/chat/steer", json={**body, "run_id": "old"}
                )
            ).status_code == 409
    finally:
        done.set()
        _ = [event async for event in tracker.stream_from_queue(queue, "chat")]


async def test_later_run_restores_history_without_reuploading_previous_guidance(
    monkeypatch, tmp_path
):
    from qwenpaw.app.chats.session import SafeJSONSession

    store = SafeJSONSession(str(tmp_path / "sessions"))
    traces = []
    for index in range(2):

        async def first(number, _):
            if number == 1:
                await tracker.steer(
                    "chat", f"run-{index}", f"guide-{index}", f"correction-{index}"
                )

        cloud = ObservedModel("cloud", [call("submit_route_decision", route())], first)
        result = setup(
            monkeypatch,
            tmp_path,
            cloud,
            Model("device", [text("done")]),
            recording=True,
            session=store,
            run_id=f"run-{index}",
        )
        ctx, _, tracker, _, uploads = result
        await run(result)
        if index:
            assert "correction-0" in cloud.requests[0][0][1].get_text_content()
        injected = [
            m
            for m in uploads[0]["delta"]
            if (m.get("metadata") or {}).get("qwenpaw_message_kind")
            == "edge_cloud_steering"
        ]
        assert [m["metadata"]["message_id"] for m in injected] == [f"guide-{index}"]
        traces.append(uploads[0]["edge_cloud"]["flow_id"])
    assert traces[0] != traces[1]


async def test_edge_skill_projection_keeps_guidance_and_split_reply(
    monkeypatch, tmp_path
):
    from agentscope.tool import Toolkit
    from test_edge_cloud import existing_skill

    async def during_route(index, _):
        if index == 2:
            await tracker.steer("chat", "client-run", "a", "first edge correction")

    async def during_edge(index, messages):
        if index == 1:
            assert len(guidance(messages)) == 1
            await tracker.steer("chat", "client-run", "b", "second edge correction")

    cloud = ObservedModel(
        "cloud",
        [
            call("Skill", {"skill": "report"}, "inspect"),
            call("submit_route_decision", route("edge_skill", skill="report")),
            call(
                "submit_verification",
                {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
            ),
        ],
        during_route,
    )
    edge = ObservedModel(
        "edge", [call("Skill", {"skill": "report"}, "load"), text("done")], during_edge
    )
    result = setup(monkeypatch, tmp_path, cloud, edge)
    ctx, _, tracker, _, uploads = result
    ctx.agent.toolkit = Toolkit(
        skills_or_loaders=[str(existing_skill(tmp_path, "report"))]
    )
    await run(result)
    assert len(cloud.requests) == 3 and len(edge.requests) == 2
    assert len(guidance(edge.requests[-1][0])) == 2
    assert uploads[0]["edge_cloud"]["final_agent"] == "edge"


async def test_failure_saves_and_uploads_already_injected_guidance(
    monkeypatch, tmp_path
):
    async def fail_after_injection(index, messages):
        if index == 1:
            await tracker.steer("chat", "client-run", "a", "keep on error")
        else:
            assert guidance(messages)
            raise RuntimeError("simulated provider failure")

    cloud = ObservedModel(
        "cloud",
        [call("submit_route_decision", route("complex_or_negative"))],
        fail_after_injection,
    )
    result = setup(monkeypatch, tmp_path, cloud, Model("device"), recording=True)
    ctx, _, tracker, session, uploads = result
    ctx.agent.model_config.max_retries = 0
    await run(result, allow_error=True)
    assert isinstance(ctx.error, RuntimeError)
    assert "keep on error" in json.dumps(session.saved)
    assert len(uploads) == 1
    assert any(
        (m.get("metadata") or {}).get("message_id") == "a" for m in uploads[0]["delta"]
    )


@pytest.mark.parametrize("failure", [False, True])
async def test_close_or_failure_during_redaction_never_commits_input(failure):
    ctx = SimpleNamespace(
        agent_id="default",
        input_msgs=[message("task")],
        agent=SimpleNamespace(state=SimpleNamespace(context=[])),
        extras={},
    )
    controller = EdgeCloudSteeringController(ctx, "response", "run", "chat")
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare(messages):
        entered.set()
        await release.wait()
        if failure:
            raise ValueError("redaction failed")
        return messages

    controller.start(SimpleNamespace(prepare=prepare), "flow")
    controller.enqueue("a", "pending")
    task = asyncio.create_task(controller.prepare_view([]))
    await asyncio.wait_for(entered.wait(), 5)
    if not failure:
        controller.close()
    release.set()
    if failure:
        with pytest.raises(ValueError):
            await task
    else:
        await task
    assert controller.records["a"]["status"] == "not_applied"
    assert ctx.agent.state.context == []
