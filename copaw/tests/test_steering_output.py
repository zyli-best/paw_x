"""Output attribution follows actual inference inputs, never receipt timing."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from agentscope.message import ThinkingBlock, TextBlock, ToolCallBlock
from agentscope.model import ChatResponse
from agentscope.tool import FunctionTool, ToolChunk

from qwenpaw.runtime.output_attribution import steer_message_id
from qwenpaw.runtime.session_views import restore_session_view
from test_edge_cloud import Model, call, clean_capture, config, route, text
from test_edge_cloud_steering import ObservedModel, run, setup


def runtime_setup(monkeypatch, tmp_path, cloud, device, *, collaboration, **kwargs):
    result = setup(monkeypatch, tmp_path, cloud, device, **kwargs)
    if not collaboration:
        monkeypatch.setattr(
            "qwenpaw.runtime.edge_cloud.config.load_edge_cloud_config",
            lambda: config(enabled=False),
        )
    return result


def completed(events):
    return [e for e in events if e.object == "message" and e.status == "completed"]


def contents(message):
    return "".join(getattr(c, "text", "") for c in message.content)


def native_with_text(messages, value):
    return next(
        m
        for m in messages
        if any(value in c.get("text", "") for c in m.get("content", []))
    )


@pytest.mark.parametrize("collaboration", [False, True])
@pytest.mark.parametrize("recording", [False, True])
async def test_two_guides_label_only_new_output_and_survive_exports(
    monkeypatch, tmp_path, collaboration, recording
):
    async def during_old_inference(index, _):
        if index == 1:
            await tracker.steer("chat", "client-run", "a", "first correction")
            await tracker.steer("chat", "client-run", "b", "second correction")

    after = ChatResponse(
        content=[
            ThinkingBlock(thinking="new reasoning"),
            TextBlock(text="after guides"),
        ],
        is_last=True,
    )
    if collaboration:
        old = call("submit_route_decision", route())
        old.content.insert(0, TextBlock(text="old route"))
        responses = [old]
    else:
        responses = [text("old answer"), after]
    cloud = ObservedModel("cloud", responses, during_old_inference)
    device = Model("device", [after])
    result = runtime_setup(
        monkeypatch,
        tmp_path,
        cloud,
        device,
        collaboration=collaboration,
        recording=recording,
        tracing=True,
    )
    ctx, _, tracker, session, uploads = result
    events = await run(result)
    messages = completed(events)
    old = next(
        m for m in messages if "old route" in contents(m) or "old answer" in contents(m)
    )
    new = next(m for m in messages if "after guides" in contents(m))
    assert old.id != new.id and steer_message_id(old) is None
    assert steer_message_id(new) == "b"
    assert (
        steer_message_id(next(m for m in messages if "new reasoning" in contents(m)))
        == "b"
    )
    assert not any(steer_message_id(e) == "a" for e in messages)
    # Both inputs still reach the model; the single output marker is not a
    # replacement or last-write-wins change to the existing injection logic.
    actual = device.requests[0][0] if collaboration else cloud.requests[-1][0]
    assert [
        m.metadata["message_id"]
        for m in actual
        if (m.metadata or {}).get("qwenpaw_message_kind")
        in {
            "user_steering",
            "edge_cloud_steering",
        }
    ] == ["a", "b"]
    assert len(cloud.requests) == (1 if collaboration else 2)
    final = next(e for e in reversed(events) if e.object == "response")
    assert steer_message_id(next(m for m in final.output if m.id == new.id)) == "b"
    restored = restore_session_view(SimpleNamespace(extras={}), deepcopy(session.saved))
    assert (
        native_with_text(restored["state"]["context"], "after guides")["metadata"][
            "steer_message_id"
        ]
        == "b"
    )
    assert (
        native_with_text(session.saved["state"]["context"], "after guides")["metadata"][
            "steer_message_id"
        ]
        == "b"
    )
    assert len(uploads) == 1
    assert (
        native_with_text(uploads[0]["delta"], "after guides")["metadata"][
            "steer_message_id"
        ]
        == "b"
    )
    from agentscope.state import AgentState
    from qwenpaw.app.chats import utils
    from qwenpaw.observability.session_delta_redactor import (
        redact_session_delta_payload,
    )

    monkeypatch.setattr(
        utils, "load_config", lambda: SimpleNamespace(user_timezone="UTC")
    )
    history = utils.agentscope_msg_to_message(
        AgentState.model_validate(session.saved["state"]).context,
    )
    replay = next(m for m in history if "after guides" in contents(m))
    assert replay.metadata["metadata"]["steer_message_id"] == "b"
    outgoing = redact_session_delta_payload(
        uploads[0],
        sensitive_words={"patterns": [{"type": "custom", "keyword": "after guides"}]},
    )
    assert any(
        m.get("metadata", {}).get("steer_message_id") == "b" for m in outgoing["delta"]
    )
    assert not any(
        "after guides" in c.get("text", "")
        for m in outgoing["delta"]
        for c in m.get("content", [])
    )
    assert not hasattr(ctx.agent, "_output_response_id")
    if collaboration:
        headers = [m for m in messages if "开始执行任务" in contents(m)]
        assert headers and all(steer_message_id(m) is None for m in headers)


@pytest.mark.parametrize("collaboration", [False, True])
async def test_streaming_reply_and_tools_keep_their_original_snapshot(
    monkeypatch, tmp_path, collaboration
):
    async def read_file(file_path: str):
        # A guide received during the call must not relabel its result.
        await tracker.steer("chat", "client-run", "c", "third correction")
        return ToolChunk(content=[TextBlock(text="tool result")])

    class StreamingModel(Model):
        async def __call__(self, messages, **kwargs):
            response = await super().__call__(messages, **kwargs)
            if len(self.requests) != 1:
                return response

            async def stream():
                yield ChatResponse(content=[TextBlock(text="before B")], is_last=False)
                yield ChatResponse(
                    content=[
                        ToolCallBlock(
                            id="read",
                            name="read_file",
                            input='{"file_path":"test.txt"}',
                        )
                    ],
                    is_last=False,
                )
                yield response

            return stream()

    tool_reply = ChatResponse(
        content=[
            TextBlock(text="before B"),
            ToolCallBlock(
                id="read", name="read_file", input='{"file_path":"test.txt"}'
            ),
        ],
        is_last=True,
    )
    execution = StreamingModel("execution", [tool_reply, text("after C")])
    cloud = (
        Model(
            "cloud",
            [
                call("submit_route_decision", route("device_tools")),
                call(
                    "submit_verification",
                    {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
                ),
            ],
        )
        if collaboration
        else execution
    )
    result = runtime_setup(
        monkeypatch,
        tmp_path,
        cloud,
        execution,
        collaboration=collaboration,
        tools=[FunctionTool(read_file)],
        tracing=True,
        recording=True,
    )
    _, _, tracker, _, uploads = result
    queued_a = queued_b = False

    async def on_event(event):
        nonlocal queued_a, queued_b
        if event.object == "response" and not queued_a:
            queued_a = True
            await tracker.steer("chat", "client-run", "a", "first correction")
        if (
            event.object == "content"
            and "before B" in getattr(event, "text", "")
            and not queued_b
        ):
            queued_b = True
            await tracker.steer("chat", "client-run", "b", "second correction")

    events = await run(result, on_event=on_event)
    messages = completed(events)
    assert queued_a and queued_b
    if not collaboration:
        partial = next(e for e in events if e.object == "message" and e.status == "cancelled" and "before B" in contents(e))
        assert steer_message_id(partial) == "a"
        assert partial.metadata["steer_preempted"] is True
        assert not any(m.type == "plugin_call_output" for m in messages)
        assert steer_message_id(next(m for m in messages if "after C" in contents(m))) == "b"
        assert len(execution.requests) == 2
        assert not any((m.metadata or {}).get("message_id") == "c" for r in execution.requests for m in r[0])
        assert any(m.get("metadata", {}).get("steer_preempted") for m in uploads[0]["delta"])
        return
    assert (
        steer_message_id(next(m for m in messages if "before B" in contents(m))) == "a"
    )
    for m in messages:
        if m.type in {"plugin_call", "plugin_call_output"} and any(
            getattr(c, "data", {}).get("name") == "read_file" for c in m.content
        ):
            assert steer_message_id(m) == "a"
    assert (
        steer_message_id(next(m for m in messages if "after C" in contents(m))) == "c"
    )
    assert len(execution.requests) == 2
    for m in uploads[0]["delta"]:
        if any(c.get("name") == "read_file" for c in m.get("content", [])):
            assert m["metadata"]["steer_message_id"] == "a"


@pytest.mark.parametrize("passed", [False, True])
async def test_late_verifier_guide_does_not_relabel_verdict_or_force_retry(
    monkeypatch, tmp_path, passed
):
    async def during_verdict(index, _):
        if index == 2:
            await tracker.steer("chat", "client-run", "late", "new requirement")

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
            text("cloud correction"),
        ],
        during_verdict,
    )
    result = setup(
        monkeypatch, tmp_path, cloud, Model("device", [text("candidate")]), tracing=True
    )
    ctx, _, tracker, _, _ = result
    events = await run(result)
    messages = completed(events)
    verdict = [m for m in messages if m.name == "VerifyAgent"]
    assert verdict and all(steer_message_id(m) is None for m in verdict)
    assert len(cloud.requests) == (2 if passed else 3)
    assert ctx.extras["edge_cloud_steering"].records["late"]["status"] == (
        "not_applied" if passed else "injected"
    )
    tagged = [m for m in messages if steer_message_id(m)]
    if passed:
        assert not tagged
    else:
        assert (
            steer_message_id(
                next(m for m in tagged if "cloud correction" in contents(m))
            )
            == "late"
        )


async def test_router_natural_read_loop_updates_only_the_next_call(
    monkeypatch, tmp_path
):
    async def list_skills():
        await tracker.steer("chat", "client-run", "route-guide", "use corrected task")
        return ToolChunk(content=[TextBlock(text="no skills")])

    first = call("list_skills", {}, "read-skills")
    first.content.insert(0, TextBlock(text="route before guide"))
    second = call("submit_route_decision", route(), "route-submit")
    second.content.insert(0, TextBlock(text="route after guide"))
    cloud = Model("cloud", [first, second])
    result = setup(
        monkeypatch,
        tmp_path,
        cloud,
        Model("device", [text("done")]),
        tools=[FunctionTool(list_skills)],
        tracing=True,
        recording=True,
    )
    _, _, tracker, _, uploads = result
    messages = completed(await run(result))
    assert (
        steer_message_id(
            next(m for m in messages if "route before guide" in contents(m))
        )
        is None
    )
    assert (
        steer_message_id(
            next(m for m in messages if "route after guide" in contents(m))
        )
        == "route-guide"
    )
    summaries = [m for m in messages if m.name == "RouterAgent" and m.type == "message"]
    assert steer_message_id(summaries[0]) is None  # stage start
    assert steer_message_id(summaries[-1]) == "route-guide"  # decision result
    assert len(cloud.requests) == 2
    assert any(
        m.get("metadata", {}).get("steer_message_id") == "route-guide"
        for m in uploads[0]["delta"]
    )


@pytest.mark.parametrize("collaboration", [False, True])
async def test_stop_saves_partial_reply_with_frozen_attribution(
    monkeypatch, tmp_path, collaboration
):
    class InterruptedModel(Model):
        async def __call__(self, messages, **kwargs):
            await super().__call__(messages, **kwargs)

            async def stream():
                yield ChatResponse(
                    content=[TextBlock(text="partial guided reply")], is_last=False
                )
                await asyncio.Event().wait()

            return stream()

    execution = InterruptedModel("execution")
    cloud = (
        Model("cloud", [call("submit_route_decision", route())])
        if collaboration
        else execution
    )
    result = runtime_setup(
        monkeypatch,
        tmp_path,
        cloud,
        execution,
        collaboration=collaboration,
        recording=True,
        tracing=True,
    )
    _, _, tracker, session, uploads = result
    queued = False

    async def on_event(event):
        nonlocal queued
        if event.object == "response" and not queued:
            queued = True
            await tracker.steer("chat", "client-run", "applied", "first correction")
        if event.object == "content" and "partial guided reply" in getattr(
            event, "text", ""
        ):
            await tracker.steer("chat", "client-run", "pending", "later correction")
            await tracker.request_stop("chat")

    events = await run(result, allow_error=True, on_event=on_event)
    assert not any(steer_message_id(m) == "pending" for m in events)
    restored = restore_session_view(SimpleNamespace(extras={}), deepcopy(session.saved))
    assert (
        native_with_text(restored["state"]["context"], "partial guided reply")[
            "metadata"
        ]["steer_message_id"]
        == "applied"
    )
    assert (
        native_with_text(uploads[0]["delta"], "partial guided reply")["metadata"][
            "steer_message_id"
        ]
        == "applied"
    )


async def test_previous_run_guidance_does_not_tag_new_run(monkeypatch, tmp_path):
    from test_request_budget import message

    result = runtime_setup(
        monkeypatch,
        tmp_path,
        Model("cloud", [text("new task reply")]),
        Model("device"),
        collaboration=False,
    )
    ctx, _, _, _, _ = result
    previous = message("previous correction")
    previous.metadata = {
        "qwenpaw_message_kind": "user_steering",
        "response_id": "previous-response",
        "run_id": "previous-run",
        "message_id": "old-guide",
        "sequence": 99,
    }
    ctx.agent.state.context = [previous, message("previous reply", "assistant")]
    events = await run(result)
    assert not any(steer_message_id(m) for m in completed(events))
