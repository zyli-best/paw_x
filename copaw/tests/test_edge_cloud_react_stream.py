"""Decision ReAct events use the existing frontend protocol, on wire only."""

import asyncio
import json
from dataclasses import asdict

import pytest
from agentscope.message import TextBlock, ThinkingBlock, ToolResultState
from agentscope.model import ChatResponse
from agentscope.tool import FunctionTool, ToolChunk

from test_edge_cloud import Model, call, clean_capture, config, context, prepare, route, text
from qwenpaw.runtime.edge_cloud.client_stream import ClientDecisionStream
from qwenpaw.runtime.edge_cloud.function_call import consume_decision_call
from qwenpaw.runtime.envelope import Envelope


@pytest.mark.parametrize("stage", ["router", "verifier"])
@pytest.mark.parametrize("state", [ToolResultState.SUCCESS, ToolResultState.ERROR])
async def test_react_text_tools_and_results_are_live_and_keep_distinct_call_ids(tmp_path, stage, state):
    model_gate, tool_gate = asyncio.Event(), asyncio.Event()
    thinking_visible, tool_visible = asyncio.Event(), asyncio.Event()
    agent = "RouterAgent" if stage == "router" else "VerifyAgent"
    submit = "submit_route_decision" if stage == "router" else "submit_verification"
    decision = route() if stage == "router" else {"passed": True}
    original = call("Skill", {"skill": "report"})
    snapshot = asdict(original)
    requests, inputs = [], []

    async def Skill(skill: str):
        inputs.append(skill)
        await tool_gate.wait()
        return ToolChunk(content=[TextBlock(text="actual read result")], state=state)

    async def model(messages, **kwargs):
        requests.append(messages)
        if len(requests) == 2:
            return call(submit, decision)  # Reuses the provider's call-1 ID.

        async def chunks():
            yield ChatResponse(content=[
                ThinkingBlock(thinking="think A"), TextBlock(text="plan A"),
            ], is_last=False)
            await model_gate.wait()
            yield ChatResponse(content=[
                ThinkingBlock(thinking=" + B"), TextBlock(text=" + B"),
            ], is_last=False)
            yield ChatResponse(content=[
                ThinkingBlock(thinking="think A + B"), TextBlock(text="plan A + B"),
                *original.content,
            ], is_last=True)

        return chunks()

    stream = ClientDecisionStream(agent, "cloud-model")
    envelope = Envelope()
    ctx = context(tmp_path)

    async def decide(factory):
        return await consume_decision_call(
            model, [], stage=stage, state=ctx.agent.state,
            read_tools={"Skill": FunctionTool(Skill)},
            response_observer_factory=factory, tool_observer=stream.tool_observer,
        )

    events = []

    async def collect():
        events.append(envelope.tag_client_event(stream.start()).model_copy(deep=True))
        with envelope.client_execution_label(agent, stream.header):
            async for event in stream.run(decide):
                events.append(envelope.tag_client_event(event).model_copy(deep=True))
                if event.object == "message" and event.type == "reasoning":
                    thinking_visible.set()
                if event.object == "message" and event.type == "plugin_call_output":
                    tool_visible.set()
        events.append(envelope.tag_client_event(stream.complete("已完成。")))
        events.extend([event async for event in envelope.finalize()])

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(thinking_visible.wait(), 5)
        assert not inputs and len(requests) == 1 and not task.done()
        model_gate.set()
        await asyncio.wait_for(tool_visible.wait(), 5)
        assert inputs == ["report"] and len(requests) == 1 and not task.done()
        tool_gate.set()
        await asyncio.wait_for(task, 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    output = events[-1].output
    calls = [m.content[0].data for m in output if m.type == "plugin_call"]
    results = [m.content[0].data for m in output if m.type == "plugin_call_output"]
    assert [c["name"] for c in calls] == ["Skill", submit]
    assert len({c["call_id"] for c in calls}) == 2
    assert [c["call_id"] for c in calls] == [r["call_id"] for r in results]
    assert json.loads(calls[0]["arguments"]) == {"skill": "report"}
    assert json.loads(calls[1]["arguments"]) == decision
    assert results[0]["output"] == "actual read result" and results[0]["state"] == state
    assert results[1]["state"] == "success"
    assert all(m.status == "completed" for m in output)
    assert all(m.name == ("assistant" if m.type == "reasoning" else agent)
               for m in output)
    assert [m.content[0].text for m in output if m.type == "reasoning"] == [
        "think A + B",
    ]
    answers = [m.content[0].text for m in output if m.type == "message"]
    assert answers == [
        f"【{agent} | cloud-model】",
        f"【{agent} | cloud-model】\n\nplan A + B",
        f"【{agent} | cloud-model】\n\n已完成。",
    ]
    assert output[0].id != output[-1].id
    assert output[0].content[0].text == stream.header.rstrip()
    assert output[-2].type == "plugin_call_output"
    assert output[-1].content[0].text.endswith("已完成。")
    assert stream.result == decision and len(requests) == 2
    assert asdict(original) == snapshot
    assert envelope.response.output == [] and envelope.collect_partial_blocks() == []
    assert ctx.agent.state.context == []


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("has_text", [False, True])
async def test_router_labels_start_answer_and_conclusion_in_order(
    monkeypatch, tmp_path, enabled, streaming, has_text,
):
    decision = route("complex_or_negative", reason="internal routing reason")
    response = ChatResponse(content=[
        ThinkingBlock(thinking="original router thinking"),
        *([TextBlock(text="original router answer")] if has_text else []),
        *call("submit_route_decision", decision).content,
    ], is_last=True)
    original = asdict(response)

    class RouterModel(Model):
        async def __call__(self, messages, **kwargs):
            result = await super().__call__(messages, **kwargs)
            if not streaming or messages[0].name != "RouterAgent":
                return result

            async def chunks():
                yield ChatResponse(content=[
                    ThinkingBlock(thinking="original router thinking"),
                ], is_last=False)
                if has_text:
                    yield ChatResponse(content=[TextBlock(text="original ")], is_last=False)
                    yield ChatResponse(content=[TextBlock(text="router answer")], is_last=False)
                yield result

            return chunks()

    cloud = RouterModel("actual-cloud-model", [response, text("cloud answer")])
    ctx = context(tmp_path, cloud=cloud)
    orchestrator = prepare(
        monkeypatch, ctx, None, [], settings=config(trace_stream_enabled=enabled),
    )
    del orchestrator._decision
    envelope = Envelope()
    events = [e.model_copy(deep=True) async for e in orchestrator.run(ctx, envelope)]
    events.extend([e.model_copy(deep=True) async for e in envelope.finalize()])
    output = events[-1].output
    router_messages = [m for m in output if m.type == "message" and m.name == "RouterAgent"]
    assert len(router_messages) == (2 + int(has_text)) * int(enabled)
    if enabled:
        assert router_messages[0].content[0].text == "【RouterAgent | actual-cloud-model】"
        assert output[0].id == router_messages[0].id
        conclusion = router_messages[-1]
        assert "已选择 CloudAgent" in conclusion.content[0].text
        assert "internal routing reason" in conclusion.content[0].text
        assert conclusion.id != router_messages[0].id
        result_index = next(i for i,m in enumerate(output)
                            if m.type == "plugin_call_output" and m.name == "RouterAgent")
        cloud_index = next(i for i,m in enumerate(output) if m.name == "CloudAgent")
        assert result_index < output.index(conclusion) < cloud_index
    answers = [m for m in router_messages if m.content[0].text.endswith("original router answer")]
    assert len(answers) == int(enabled and has_text)
    if answers:
        answer = answers[0]
        expected = "【RouterAgent | actual-cloud-model】\n\noriginal router answer"
        assert answer.content[0].text == expected
        snapshots = [e for e in events if e.object == "message" and e.id == answer.id]
        assert all(e.content[0].text.count("【RouterAgent | actual-cloud-model】") == 1
                   for e in snapshots)
        assert snapshots[-1].content[0].text == expected
        if streaming:
            assembled = snapshots[0].content[0].text + "".join(
                e.text for e in events if e.object == "content" and e.msg_id == answer.id
            )
            assert assembled == expected
    thinking = [m for m in output if m.type == "reasoning"]
    assert len(thinking) == int(enabled)
    if thinking:
        assert thinking[0].name == "assistant"
        assert thinking[0].content[0].text == "original router thinking"
        assert output[1].id == thinking[0].id
    calls = [m for m in output if m.type == "plugin_call" and m.name == "RouterAgent"]
    assert len(calls) == int(enabled)
    if calls:
        assert json.loads(calls[0].content[0].data["arguments"]) == decision
    internal = json.dumps(envelope.response.model_dump(mode="json"), ensure_ascii=False)
    saved = json.dumps(ctx.agent.state_dict(), ensure_ascii=False)
    uploaded = json.dumps(ctx.extras["edge_cloud_upload"], ensure_ascii=False)
    assert all(value not in internal + saved + uploaded for value in (
        "【RouterAgent", "original router answer", "original router thinking",
    ))
    assert envelope.collect_partial_blocks() == [("text", "cloud answer")]
    assert len(cloud.requests) == 2 and orchestrator._trace.final_agent == "cloud"
    assert asdict(response) == original


async def test_react_payloads_are_redacted_without_changing_tool_inputs_or_decision(tmp_path):
    secret = "PRIVATE_TOOL_VALUE"
    original = call("Skill", {"skill": secret})
    decision = route(reason=secret)
    seen = []

    async def Skill(skill: str):
        seen.append(skill)
        return ToolChunk(content=[TextBlock(text=json.dumps({
            "api_key": "unpatterned-key", "value": secret, "phone": "13800138000",
        }))])

    responses = [original, call("submit_route_decision", decision)]

    async def model(*args, **kwargs):
        return responses.pop(0)

    stream = ClientDecisionStream("RouterAgent", "cloud", sensitive_words={
        "patterns": [{"type": "custom", "keyword": secret}],
    })

    async def decide(factory):
        return await consume_decision_call(
            model, [], stage="router", state=context(tmp_path).agent.state,
            read_tools={"Skill": FunctionTool(Skill)},
            response_observer_factory=factory, tool_observer=stream.tool_observer,
        )

    events = [e async for e in stream.run(decide)]
    wire = json.dumps([e.model_dump(mode="json") for e in events])
    assert all(value not in wire for value in (secret, "unpatterned-key", "13800138000"))
    assert seen == [secret] and stream.result == decision
    assert json.loads(original.content[0].input) == {"skill": secret}


@pytest.mark.parametrize("failure", ["call", "result"])
async def test_tool_display_failure_does_not_change_decision_or_execute_twice(tmp_path, failure):
    responses = [call("Skill", {"skill": "report"}), call("submit_route_decision", route())]
    reads = []

    async def Skill(skill: str):
        reads.append(skill)
        return ToolChunk(content=[TextBlock(text="evidence")])

    async def model(*args, **kwargs):
        return responses.pop(0)

    async def observe(kind, block):
        if kind == failure:
            raise ValueError("display failed")

    result = await consume_decision_call(
        model, [], stage="router", state=context(tmp_path).agent.state,
        read_tools={"Skill": FunctionTool(Skill)}, tool_observer=observe,
    )
    assert result == route() and reads == ["report"] and not responses


async def test_failed_decision_validation_closes_submission_card_and_preserves_error():
    stream = ClientDecisionStream("VerifyAgent", "cloud")

    async def model(*args, **kwargs):
        return call("submit_verification", {"passed": True})

    def validate(_):
        raise ValueError("missing evidence")

    async def decide(factory):
        return await consume_decision_call(
            model, [], stage="verifier", validate_decision=validate,
            response_observer_factory=factory, tool_observer=stream.tool_observer,
        )

    events = []
    with pytest.raises(ValueError, match="missing evidence"):
        async for event in stream.run(decide):
            events.append(event)
    result = [e for e in events if e.object == "message" and e.type == "plugin_call_output"][-1]
    assert result.status == "completed"
    assert result.content[0].data["state"] == "error"
    assert result.content[0].data["output"] == "missing evidence"


async def test_failed_result_display_ends_pending_card_without_changing_decision(monkeypatch):
    stream = ClientDecisionStream("RouterAgent", "cloud")
    serialize = stream._safe_payload

    def fail_result(value):
        if isinstance(value, str):
            raise ValueError("display unavailable")
        return serialize(value)

    monkeypatch.setattr(stream, "_safe_payload", fail_result)

    async def model(*args, **kwargs):
        return call("submit_route_decision", route())

    async def decide(factory):
        return await consume_decision_call(
            model, [], stage="router", response_observer_factory=factory,
            tool_observer=stream.tool_observer,
        )

    events = [e async for e in stream.run(decide)]
    result = [e for e in events if e.object == "message" and e.type == "plugin_call_output"][-1]
    assert result.status == "failed" and result.content[0].data["state"] == "error"
    assert stream.result == route()


async def test_nonstreaming_openai_response_displays_text_and_reasoning():
    stream = ClientDecisionStream("RouterAgent", "cloud")

    async def model(*args, **kwargs):
        return {"choices": [{"message": {
            "content": "route explanation", "reasoning_content": "model reasoning",
            "tool_calls": [{"id": "call-1", "function": {
                "name": "submit_route_decision", "arguments": json.dumps(route()),
            }}],
        }}]}

    async def decide(factory):
        return await consume_decision_call(
            model, [], stage="router", response_observer_factory=factory,
            tool_observer=stream.tool_observer,
        )

    events = [e async for e in stream.run(decide)]
    assert stream.result == route()
    assert any(e.type == "reasoning" and e.content[0].text.endswith("model reasoning")
               for e in events if e.object == "message")
    assert any(e.type == "message" and e.content[0].text.endswith("route explanation")
               for e in events if e.object == "message")
    assert len([e for e in events if e.object == "message" and e.type == "plugin_call"]) == 1


async def test_cancellation_closes_live_tool_card_and_cancels_reader(tmp_path):
    entered, closed = asyncio.Event(), asyncio.Event()
    stream = ClientDecisionStream("RouterAgent", "cloud")

    async def Skill(skill: str):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def model(*args, **kwargs):
        return call("Skill", {"skill": "report"})

    async def decide(factory):
        return await consume_decision_call(
            model, [], stage="router", state=context(tmp_path).agent.state,
            read_tools={"Skill": FunctionTool(Skill)},
            response_observer_factory=factory, tool_observer=stream.tool_observer,
        )

    events = []

    async def collect():
        async for event in stream.run(decide):
            events.append(event)

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert closed.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    result = [e for e in events if e.object == "message" and e.type == "plugin_call_output"][-1]
    assert result.status == "cancelled" and result.content[0].data["state"] == "interrupted"


async def test_closing_backpressured_stream_closes_provider_generator():
    buffered, closed = asyncio.Event(), asyncio.Event()
    stream = ClientDecisionStream("RouterAgent", "cloud")

    async def model(*args, **kwargs):
        async def chunks():
            try:
                for index in range(200):
                    if index == 64:
                        buffered.set()
                    yield ChatResponse(content=[ThinkingBlock(thinking="x")], is_last=False)
            finally:
                closed.set()
        return chunks()

    async def decide(factory):
        return await consume_decision_call(
            model, [], stage="router", response_observer_factory=factory,
        )

    events = stream.run(decide)
    try:
        await anext(events)  # Start the producer and receive its first thinking output.
        await asyncio.wait_for(buffered.wait(), 5)
    finally:
        await asyncio.wait_for(events.aclose(), 5)
    assert closed.is_set()
