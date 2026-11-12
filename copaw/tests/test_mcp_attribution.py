"""MCP attribution is additive and must not change tool execution or stops."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.agent import Agent, ReActConfig
from agentscope.event import (
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultState
from agentscope.model import ChatResponse
from agentscope.permission import PermissionMode
from agentscope.tool import FunctionTool, ToolChunk, Toolkit

from qwenpaw.drivers.adapters.agentscope_tool import DriverCapabilityTool
from qwenpaw.drivers.capabilities import (
    CapabilityExposure,
    DriverCapability,
    DriverInvocationResult,
    format_capability_id,
)
from qwenpaw.drivers.handlers.mcp import _mcp_tool_to_capability
from qwenpaw.runtime.envelope import Envelope
from qwenpaw.runtime.executor import AgentExecutor
from qwenpaw.schemas import DataContent, FunctionCall, FunctionCallOutput


IDENTITY = {
    "mcp_client_key": "b4628b55-4d58-4519-8308-3538bfce80ce",
    "mcp_tool_name": "查询__项目",
}


def tool(key=IDENTITY["mcp_client_key"], raw_name=IDENTITY["mcp_tool_name"],
         exposed="certification__query", protocol="mcp", invoker=None):
    return DriverCapabilityTool(
        DriverCapability(
            capability_id=format_capability_id(protocol, key, "tool", "invoke", raw_name),
            driver_name=key, protocol=protocol, kind="tool", action="invoke",
            name=raw_name, input_schema={"type": "object", "properties": {}},
            exposure=CapabilityExposure(as_tool=True, tool_name=exposed),
        ),
        invoker or AsyncMock(return_value=DriverInvocationResult(ok=True, value="ok")),
    )


def event(cls, call_id="call-1", name="certification__query", **kwargs):
    if cls in (ToolCallStartEvent, ToolResultStartEvent):
        kwargs["tool_call_name"] = name
    return cls(reply_id="reply-1", tool_call_id=call_id, **kwargs)


def executor(tools):
    agent = SimpleNamespace(
        toolkit=Toolkit(tools=tools),
        state=SimpleNamespace(tool_context=SimpleNamespace(activated_groups=[])),
    )
    return AgentExecutor(agent, Envelope())


async def translate(envelope, value, identity=None):
    return [item.model_copy(deep=True) async for item in envelope.translate_event(
        value, tool_identity=identity,
    )]


def payloads(events):
    return [value.data for value in events if isinstance(value, DataContent)]


def response_payloads(envelope):
    return [content.data for message in envelope.response.output
            for content in message.content if isinstance(content, DataContent)]


@pytest.mark.parametrize("model,field", [(FunctionCall, "arguments"),
                                         (FunctionCallOutput, "output")])
def test_optional_fields_preserve_existing_wire_shape(model, field):
    original = {"call_id": "one", "name": "ordinary", field: ""}
    assert model(**original).model_dump() == original
    assert json.loads(model(**original).model_dump_json()) == original
    enriched = model(**original, **IDENTITY)
    assert enriched.model_dump() == {**original, **IDENTITY}
    assert set(IDENTITY) <= set(model.model_fields)


@pytest.mark.parametrize("key", [IDENTITY["mcp_client_key"], "手动__key"])
@pytest.mark.parametrize("raw_name", ["queryProject", "查询__项目", "a__b__c"])
def test_capability_identity_preserves_configuration_and_original_name(key, raw_name):
    raw = SimpleNamespace(name=raw_name, description="", inputSchema={})
    capability = _mcp_tool_to_capability(key, raw, display_name="认证管理系统")
    instance = DriverCapabilityTool(capability, AsyncMock())
    before = (capability.capability_id, instance.name, instance.input_schema.copy())
    identity = executor([instance])._mcp_tool_identity(event(ToolCallStartEvent,
                                                           name=instance.name))
    assert identity == {"mcp_client_key": key, "mcp_tool_name": raw_name}
    assert before == (capability.capability_id, instance.name, instance.input_schema)
    assert instance.is_mcp is False  # Preserve SDK permission behavior.
    assert instance.mcp_name is None


@pytest.mark.parametrize("ending", ["success", "error", "interrupted"])
async def test_first_frame_results_and_final_response_keep_identity(ending):
    envelope = Envelope()
    first = await translate(envelope, event(ToolCallStartEvent), IDENTITY)
    assert payloads(first)[0] == {
        "call_id": "call-1", "name": "certification__query", "arguments": "",
        **IDENTITY,
    }
    values = [
        event(ToolCallDeltaEvent, delta='{"customer":"test"}'),
        event(ToolCallEndEvent),
        event(ToolResultStartEvent),
        event(ToolResultTextDeltaEvent, delta="result"),
        event(ToolResultDataDeltaEvent, block_id="image", media_type="image/png",
              url="https://example.invalid/image.png"),
        event(ToolResultEndEvent, state=ToolResultState(ending)),
    ]
    events = first[:]
    for value in values:
        events.extend(await translate(envelope, value))
    for data in payloads(events) + response_payloads(envelope):
        assert all(data[key] == value for key, value in IDENTITY.items())
    assert payloads(events)[-1]["state"] == ending
    assert [item async for item in envelope.finalize()]
    assert len(envelope.response.output) == 2


async def test_late_partial_metadata_fills_but_never_overwrites_identity():
    envelope = Envelope()
    await translate(envelope, event(ToolCallStartEvent))
    await translate(envelope, event(ToolCallEndEvent))
    await translate(envelope, event(ToolResultStartEvent,
        metadata={"mcp_client_key": IDENTITY["mcp_client_key"]}))
    await translate(envelope, event(ToolResultTextDeltaEvent, delta="ok",
        metadata={"mcp_client_key": None, "mcp_tool_name": IDENTITY["mcp_tool_name"]}))
    await translate(envelope, event(ToolResultEndEvent, state=ToolResultState.SUCCESS,
        metadata={"mcp_client_key": "another-server", "mcp_tool_name": "wrong"}))
    assert len(response_payloads(envelope)) == 2
    for data in response_payloads(envelope):
        assert all(data[key] == value for key, value in IDENTITY.items())


@pytest.mark.parametrize("phase", ["arguments", "result_start", "partial_result", "done"])
@pytest.mark.parametrize("ending", ["cancel", "error", "timeout"])
async def test_termination_retains_only_existing_mcp_messages_without_new_events(phase, ending):
    envelope = Envelope()
    await translate(envelope, event(ToolCallStartEvent), IDENTITY)
    await translate(envelope, event(ToolCallDeltaEvent, delta='{"partial":'))
    if phase != "arguments":
        await translate(envelope, event(ToolCallEndEvent))
        await translate(envelope, event(ToolResultStartEvent))
    if phase in {"partial_result", "done"}:
        await translate(envelope, event(ToolResultTextDeltaEvent, delta="partial output"))
    if phase == "done":
        await translate(envelope, event(ToolResultEndEvent, state=ToolResultState.SUCCESS))
    original_states = {key: value.get("output_text_acc")
                       for key, value in envelope._tool_calls.items()}
    finalizer = (envelope.cancel_envelope() if ending == "cancel"
                 else envelope.error_envelope(ending))
    emitted = [value async for value in finalizer]
    assert [value.object for value in emitted] == ["response"]
    assert envelope.response.status == ("completed" if ending == "cancel" else "failed")
    assert len(envelope.response.output) == (1 if phase == "arguments" else 2)
    for data in response_payloads(envelope):
        assert all(data[key] == value for key, value in IDENTITY.items())
    assert original_states == {key: value.get("output_text_acc")
                               for key, value in envelope._tool_calls.items()}
    assert [value async for value in envelope.finalize()] == []


@pytest.mark.parametrize("name", ["Skill", "read_file", "mcp__misleading__name"])
async def test_external_and_builtin_tools_without_identity_keep_legacy_behavior(name):
    envelope = Envelope()
    binding = executor([SimpleNamespace(name=name)])
    start = event(ToolCallStartEvent, name=name)
    assert binding._mcp_tool_identity(start) is None
    events = await translate(envelope, start)
    assert payloads(events) == [{"call_id": "call-1", "name": name, "arguments": ""}]
    assert [value async for value in envelope.cancel_envelope()]
    assert not envelope.response.output


def test_binding_uses_active_group_last_winner_without_external_io():
    first, second = tool(key="first"), tool(key="second")
    binding = executor([first, second])
    resolver = AsyncMock(side_effect=AssertionError("No tool discovery is allowed"))
    binding._agent.toolkit._get_available_tools = resolver
    assert binding._mcp_tool_identity(event(ToolCallStartEvent))["mcp_client_key"] == "second"
    binding._agent.toolkit.tool_groups.append(SimpleNamespace(
        name="inactive", tools=[tool(key="inactive")], mcps=[],
    ))
    assert binding._mcp_tool_identity(event(ToolCallStartEvent))["mcp_client_key"] == "second"
    binding._agent.state.tool_context.activated_groups = ["inactive"]
    assert binding._mcp_tool_identity(event(ToolCallStartEvent))["mcp_client_key"] == "inactive"
    resolver.assert_not_called()


def test_dynamic_client_does_not_guess_attribution_or_query_client():
    binding = executor([tool()])
    client = SimpleNamespace(list_tools=AsyncMock())
    binding._agent.toolkit.tool_groups[0].mcps.append(client)
    assert binding._mcp_tool_identity(event(ToolCallStartEvent)) is None
    client.list_tools.assert_not_called()


def test_third_party_broken_metadata_is_nonfatal(caplog):
    class External:
        name = "certification__query"

        @property
        def mcp_client_key(self):
            raise AttributeError("third-party metadata unavailable")

    assert executor([External()])._mcp_tool_identity(event(ToolCallStartEvent)) is None


async def test_same_call_id_reused_by_ordinary_tool_does_not_inherit_mcp_identity():
    envelope = Envelope()
    await translate(envelope, event(ToolCallStartEvent), IDENTITY)
    await translate(envelope, event(ToolCallEndEvent))
    events = await translate(envelope, event(ToolCallStartEvent, name="Skill"))
    assert not set(IDENTITY) & payloads(events)[0].keys()


async def test_non_mcp_capability_execution_is_unchanged():
    invoker = AsyncMock(return_value=DriverInvocationResult(ok=True, value="ok"))
    instance = tool(protocol="other", invoker=invoker)
    assert instance.mcp_client_key is None and instance.mcp_tool_name is None
    result = await instance()
    assert result.state == ToolResultState.SUCCESS
    assert invoker.call_args.args[0].capability_id == instance._capability.capability_id


class Model:
    model = "mcp-attribution-test"
    context_size = 32000
    parameters = SimpleNamespace(max_tokens=1000)

    def __init__(self, calls):
        self.responses = [
            ChatResponse(content=calls, is_last=True),
            ChatResponse(content=[TextBlock(text="done")], is_last=True),
        ]

    async def count_tokens(self, *args, **kwargs):
        return 10

    async def __call__(self, messages, **kwargs):
        return self.responses.pop(0)


def running_executor(tools, calls):
    agent = Agent(
        name="mcp-test", model=Model(calls), system_prompt="test",
        toolkit=Toolkit(tools=tools), react_config=ReActConfig(max_iters=3),
    )
    agent.state.permission_context.mode = PermissionMode.BYPASS
    agent.react_config.interruption_raise_cancelled_error = True
    return AgentExecutor(agent, Envelope())


async def run(binding, events):
    inputs = [Msg(name="user", role="user", content=[TextBlock(text="test")])]
    async for value in binding.run(inputs):
        events.append(value.model_copy(deep=True))


async def test_real_sdk_twenty_calls_keep_correct_owner_and_invocations():
    invoker = AsyncMock(return_value=DriverInvocationResult(ok=True, value="ok"))
    tools = [tool(key="server-one", exposed="first__query", invoker=invoker),
             tool(key="server-two", exposed="second__query", invoker=invoker)]
    calls = [ToolCallBlock(id=f"call-{index}", name=tools[index % 2].name, input="{}")
             for index in range(20)]
    binding = running_executor(tools, calls)
    events = []
    await run(binding, events)
    final = [value async for value in binding._envelope.finalize()]
    assert invoker.await_count == 20
    assert len(response_payloads(binding._envelope)) == 40
    for data in payloads(events) + response_payloads(binding._envelope):
        index = int(data["call_id"].split("-")[-1])
        assert data["mcp_client_key"] == tools[index % 2].mcp_client_key
        assert data["mcp_tool_name"] == IDENTITY["mcp_tool_name"]
    wire = json.loads(final[-1].model_dump_json())
    assert len({content["data"]["call_id"] for message in wire["output"]
                for content in message["content"] if content["type"] == "data"}) == 20


@pytest.mark.parametrize("failure", [RuntimeError("failed"), TimeoutError("timed out")])
async def test_real_sdk_failure_keeps_error_state_and_identity(failure):
    invoker = AsyncMock(side_effect=failure)
    instance = tool(invoker=invoker)
    binding = running_executor([instance], [
        ToolCallBlock(id="call-1", name=instance.name, input="{}"),
    ])
    events = []
    await run(binding, events)
    results = [data for data in payloads(events) if "state" in data]
    assert results[-1]["state"] == "error"
    assert all(results[-1][key] == value for key, value in IDENTITY.items())
    invoker.assert_awaited_once()


async def test_real_sdk_cancel_reaches_running_tool_and_retains_attribution():
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def invoke(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    instance = tool(invoker=invoke)
    binding = running_executor([instance], [
        ToolCallBlock(id="call-1", name=instance.name, input="{}"),
    ])
    events = []
    task = asyncio.create_task(run(binding, events))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(stopped.wait(), timeout=3)
        final = [value async for value in binding._envelope.cancel_envelope()]
        assert final[-1].object == "response"
        assert all(data["mcp_client_key"] == IDENTITY["mcp_client_key"]
                   for data in response_payloads(binding._envelope))
        assert payloads(events)[0]["mcp_tool_name"] == IDENTITY["mcp_tool_name"]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_real_sdk_external_tool_without_fields_still_executes():
    calls = []

    async def external():
        calls.append("executed")
        return ToolChunk(content=[TextBlock(text="external result")])

    instance = FunctionTool(external)
    instance.name = "mcp__external__query"
    binding = running_executor([instance], [
        ToolCallBlock(id="call-1", name=instance.name, input="{}"),
    ])
    events = []
    await run(binding, events)
    assert calls == ["executed"]
    assert payloads(events)
    assert all(not set(IDENTITY) & data.keys() for data in payloads(events))


def test_edge_execution_wrapper_preserves_identity_without_modification():
    from qwenpaw.runtime.edge_cloud.execution import ExecutionJournal

    instance = tool()
    wrapped = ExecutionJournal().wrap(instance, lower=True)
    assert executor([wrapped])._mcp_tool_identity(event(ToolCallStartEvent)) == IDENTITY
