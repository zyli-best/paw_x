"""Check single-provider GPT routing and reasoning at the SDK boundary."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.agent import ContextConfig
from agentscope.formatter import OpenAIResponseFormatter
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import DashScopeChatModel, OpenAIChatModel, OpenAIResponseModel

from qwenpaw.loop.failure_config import FailureLoopConfig
from qwenpaw.loop.gates.failure_loop import FailureLoopGate
from qwenpaw.providers.openai_provider import OpenAIProvider
from qwenpaw.providers.openai_response_provider import OpenAIResponseProvider


GATEWAY = "https://gateway.example/api/dev/model/v2/project"
TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file",
        "parameters": {"type": "object", "properties": {}},
    },
}


def provider(*, generation=None, model_generation=None, model="gpt-5.6-sol"):
    return OpenAIProvider(
        id="fw-agent-project", name="Project models", is_custom=True,
        api_key="test-key", base_url=GATEWAY,
        custom_headers={"X-Project": "test"},
        generate_kwargs=generation or {},
        extra_models=[{
            "id": model, "name": model, "max_tokens": 4096,
            "max_input_length": 1050000,
            "generate_kwargs": model_generation or {},
        }],
    )


@pytest.fixture
def sdk(monkeypatch):
    import openai

    response = SimpleNamespace(
        id="resp_test", usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"has_issue":false,"findings":[]}', tool_calls=None,
        ))],
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(
            type="output_text", text='{"has_issue":false,"findings":[]}',
        )])],
    )
    responses = AsyncMock(return_value=response)
    chat = AsyncMock(return_value=response)
    responses.chat_create = chat
    client = SimpleNamespace(
        responses=SimpleNamespace(create=responses),
        chat=SimpleNamespace(completions=SimpleNamespace(create=chat)),
    )
    factory = lambda **kwargs: client
    monkeypatch.setattr(openai, "AsyncClient", factory)
    return responses


@pytest.mark.parametrize("name", ["gpt-5.6-sol", "gpt-5.6-luna"])
def test_custom_gpt_routes_without_configuration_fields_or_mutation(name):
    config = provider(model=name, model_generation={"reasoning_effort": "high"})
    before = copy.deepcopy(config.model_dump())
    model = config.get_chat_model_instance(name)
    assert isinstance(model, OpenAIResponseModel)
    assert isinstance(model.formatter, OpenAIResponseFormatter)
    assert model.credential.id == "qwenpaw-fw-agent-project"
    assert str(model.credential.base_url).rstrip("/") == GATEWAY
    assert model.client_kwargs["default_headers"] == {"X-Project": "test"}
    assert model.context_size == 1050000
    assert model.parameters.max_tokens == 4096
    assert model._extra_generate_kwargs == {"reasoning": {"effort": "high"}}
    assert config.model_dump() == before


@pytest.mark.parametrize("name,expected", [
    ("qwen3.8-max-0902", DashScopeChatModel),
    ("deepseek-v4-pro", OpenAIChatModel),
    ("wan2.7-t2v", OpenAIChatModel),
    ("gpt-oss-120b", OpenAIChatModel),
    ("gpt-6.1-sol", OpenAIChatModel),
    ("private-alias", OpenAIChatModel),
])
def test_other_model_routes_are_unchanged(name, expected):
    config = provider(model=name)
    if name == "private-alias":
        config.extra_models[0].name = "gpt-5.6-sol"
    assert isinstance(config.get_chat_model_instance(name), expected)


@pytest.mark.parametrize("generation,model_generation,call,expected", [
    ({}, {}, {}, None),
    ({}, {"reasoning_effort": None}, {}, None),
    ({}, {"reasoning_effort": "high"}, {}, {"effort": "high"}),
    ({"reasoning": {"effort": "high", "summary": "auto"}},
     {"reasoning_effort": "low"}, {}, {"effort": "low", "summary": "auto"}),
    ({"reasoning_effort": "high"}, {},
     {"reasoning": {"effort": "none"}}, {"effort": "none"}),
    ({"reasoning": {"effort": "high"}}, {},
     {"reasoning_effort": "low"}, {"effort": "low"}),
    ({}, {"extra_body": {"reasoning_effort": "high", "keep": True}},
     {}, {"effort": "high"}),
])
async def test_reasoning_aliases_defaults_and_override_priority(
    sdk, generation, model_generation, call, expected,
):
    config = provider(generation=generation, model_generation=model_generation)
    before = copy.deepcopy(config.model_dump())
    model = config.get_chat_model_instance("gpt-5.6-sol")
    model.stream = False
    await model([Msg(name="user", role="user", content=[TextBlock(text="Read a file")])],
                tools=[TOOL], **call)
    wire = sdk.call_args.kwargs
    assert wire.get("reasoning") == expected
    if expected is None:
        assert "reasoning" not in wire
    assert "reasoning_effort" not in wire
    assert "reasoning_effort" not in wire.get("extra_body", {})
    assert wire["max_output_tokens"] == 4096 and "max_tokens" not in wire
    assert wire["tools"][0]["name"] == "read_file"
    assert "function" not in wire["tools"][0]
    assert "input" in wire and "messages" not in wire
    assert config.model_dump() == before


def test_conflicting_aliases_are_rejected_without_changing_config():
    config = provider(model_generation={
        "reasoning_effort": "high", "reasoning": {"effort": "low"},
    })
    before = copy.deepcopy(config.model_dump())
    with pytest.raises(ValueError, match="Conflicting reasoning"):
        config.get_chat_model_instance("gpt-5.6-sol")
    assert config.model_dump() == before


async def test_responses_formatter_keeps_tool_result_call_id(sdk):
    model = provider().get_chat_model_instance("gpt-5.6-luna")
    model.stream = False
    await model([
        Msg(name="assistant", role="assistant", content=[
            ToolCallBlock(id="call_file", name="read_file", input="{}"),
        ]),
        Msg(name="tool", role="assistant", content=[
            ToolResultBlock(id="call_file", name="read_file", output="contents"),
        ]),
    ], tools=[TOOL])
    items = sdk.call_args.kwargs["input"]
    assert [item["type"] for item in items] == [
        "function_call", "function_call_output",
    ]
    assert all(item["call_id"] == "call_file" for item in items)


@pytest.mark.parametrize("name,generation,expected", [
    ("gpt-5.6-sol", {"reasoning_effort": "high"}, {"effort": "high"}),
    ("gpt-5.6-sol", {"reasoning": {"effort": "high"}}, {"effort": "high"}),
    ("gpt-5.6-luna", {}, None),
    ("qwen3.8-max-0902", {"thinking_enable": True, "thinking_budget": 2048}, None),
    ("deepseek-v4-pro", {"extra_body": {"thinking": {"type": "enabled"}}}, None),
])
async def test_failure_loop_gate_keeps_main_model_thinking(
    sdk, monkeypatch, tmp_path, name, generation, expected,
):
    config = provider(model=name, model_generation=generation)
    before = copy.deepcopy(config.model_dump())
    model = config.get_chat_model_instance(name)
    model.stream = False
    monkeypatch.setattr(model, "count_tokens", AsyncMock(return_value=50))
    host = SimpleNamespace(
        model=model, context_config=ContextConfig(),
        model_config=SimpleNamespace(fallback_model=None, max_retries=0),
        _agent_config=SimpleNamespace(running=SimpleNamespace(
            light_context_config=SimpleNamespace(
                context_compact_config=SimpleNamespace(enabled=False),
            ),
        )),
    )
    gate = FailureLoopGate(
        FailureLoopConfig(enabled=True, check_interval=1),
        history_path=tmp_path / "review.db",
    )
    try:
        gate.begin_turn("task", "Review completed file reads")
        gate.remember_call("task", "call_file", "read_file", "{}", iteration=0)
        gate.append_text("task", "call_file", "actual file contents")
        gate.complete_call("task", "call_file", "success")
        assert await gate._state().inspector.inspect(host, check_interval=1) == ()
        create = sdk if isinstance(model, OpenAIResponseModel) else sdk.chat_create
        create.assert_awaited_once()
        wire = create.call_args.kwargs
        assert wire.get("reasoning") == expected
        assert not wire.get("tools") and wire["tool_choice"] == "none"
        assert "actual file contents" in str(wire.get("input", wire.get("messages")))
        if name.startswith("qwen"):
            assert wire["extra_body"]["enable_thinking"] is True
            assert wire["extra_body"]["thinking_budget"] == 2048
        elif name.startswith("deepseek"):
            assert wire["extra_body"]["thinking"] == {"type": "enabled"}
        assert config.model_dump() == before
    finally:
        gate.close()


@pytest.mark.parametrize("provider_class", [OpenAIProvider, OpenAIResponseProvider])
async def test_connection_check_uses_responses(monkeypatch, provider_class):
    import qwenpaw.providers.openai_provider as module

    async def chunks():
        yield SimpleNamespace(type="response.created")

    create = AsyncMock(return_value=chunks())
    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setattr(module, "AsyncOpenAI", lambda **kwargs: client)
    config = provider_class.model_validate(provider().model_dump())
    assert await config.check_model_connection("gpt-5.6-sol", timeout=9) == (True, "")
    assert create.call_args.kwargs["model"] == "gpt-5.6-sol"
    assert create.call_args.kwargs["timeout"] == 9
