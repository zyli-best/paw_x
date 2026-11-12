"""Keep GPT Chat routing local to non-thinking, non-local shell reviews."""

import copy
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import openai
import pytest
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.model import OpenAIChatModel, OpenAIResponseModel
from agentscope.model._model_response import ChatResponse

from qwenpaw.agents import model_factory
from qwenpaw.agents.tools import shell
from qwenpaw.providers import ProviderManager
from qwenpaw.providers.openai_provider import OpenAIProvider
from qwenpaw.providers.openai_response_provider import OpenAIResponseProvider
from qwenpaw.providers.retry_chat_model import RetryChatModel
from qwenpaw.token_usage import TokenRecordingModelWrapper


ENV = "QWENPAW_SHELL_GUARD_DISABLE_THINKING"
DECISION = {
    "verdict": "allow_shell", "category": "external_process", "confidence": 0.99,
    "reason": "Mock decision", "recommendation": None,
}


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for name in (
        ENV, shell._SHELL_GUARD_THINKING_PARAM_ENV, shell._SHELL_GUARD_TOKEN_PARAM_ENV,
        shell._WINDOWS_DIRECT_SHELL_LLM_GUARD_MAX_TOKENS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ProviderManager, "_instance", None)


@pytest.fixture
def factory(monkeypatch):
    def build(name="gpt-5.6-sol", *, provider_class=OpenAIProvider,
              provider_id="fw-agent-project", is_local=False, generation=None,
              base_url="https://gateway.invalid/v1"):
        provider = provider_class(
            id=provider_id, name="Test", is_custom=True, is_local=is_local,
            base_url=base_url, api_key="test-secret-key",
            custom_headers={"X-Project": "test-project"},
            generate_kwargs=generation or {},
            extra_models=[{"id": name, "name": name, "max_tokens": 4096,
                           "max_input_length": 1050000}],
        )
        raw = provider.get_chat_model_instance(name)
        raw.max_retries = 0  # Same as model_factory._wrap_model_and_formatter.
        raw.client_kwargs["timeout"] = 12.5
        usage = TokenRecordingModelWrapper(provider_id, raw)
        model = RetryChatModel(usage)
        model._qwenpaw_request_budget = object()
        create = Mock(return_value=(model, raw.formatter))
        monkeypatch.setattr(model_factory, "create_model_and_formatter", create)
        monkeypatch.setattr(shell, "_current_agent_id_for_shell_guard", lambda: "agent-test")
        monkeypatch.setattr(ProviderManager, "_instance", SimpleNamespace(
            get_provider=lambda identifier: provider if identifier == provider_id else None,
        ))
        return SimpleNamespace(
            provider=provider, raw=raw, usage=usage, model=model, create=create,
        )
    return build


class Captured(Exception):
    """Stop at the SDK boundary without sending a request."""


@pytest.fixture
def sdk(monkeypatch):
    chat = AsyncMock(side_effect=Captured)
    responses = AsyncMock(side_effect=Captured)
    make_client = Mock(return_value=SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=chat)),
        responses=SimpleNamespace(create=responses),
    ))
    monkeypatch.setattr(openai, "AsyncClient", make_client)
    return SimpleNamespace(chat=chat, responses=responses, factory=make_client)


@pytest.mark.parametrize("name", ["gpt-5.6-sol", "gpt-5.6-luna"])
@pytest.mark.parametrize("provider_class", [OpenAIProvider, OpenAIResponseProvider])
@pytest.mark.parametrize("use_tools", [True, False])
async def test_shell_chat_and_main_responses_are_isolated(
    monkeypatch, caplog, factory, sdk, name, provider_class, use_tools,
):
    monkeypatch.setenv(ENV, "1")
    original = factory(name, provider_class=provider_class, generation={
        "reasoning_effort": "high", "extra_body": {"custom_option": True},
    })
    main = original.provider.get_chat_model_instance(name)
    provider_before = copy.deepcopy(original.provider.model_dump())
    main_before = copy.deepcopy(main._extra_generate_kwargs)
    old_parameters = original.raw.parameters.model_dump()
    budget = original.model._qwenpaw_request_budget
    caplog.set_level(logging.INFO, logger=shell.__name__)

    model = shell._create_shell_guard_model(trace_id="trace-test")
    chat = shell._shell_guard_model_chain(model)[-1]
    assert model is original.model and model._inner is original.usage
    assert model._qwenpaw_request_budget is budget
    assert isinstance(chat, OpenAIChatModel) and not isinstance(chat, OpenAIResponseModel)
    assert isinstance(chat.formatter, OpenAIChatFormatter)
    assert chat.credential is original.raw.credential
    assert chat.client_kwargs == original.raw.client_kwargs
    assert chat.context_size == original.raw.context_size == 1050000
    assert chat.max_retries == original.raw.max_retries == 0
    assert chat.formatter.max_bytes == original.raw.formatter.max_bytes
    assert model.parameters is original.usage.parameters is chat.parameters
    original.create.assert_called_once_with(agent_id="agent-test")

    kwargs = shell._build_shell_guard_request_kwargs(model, use_tools=use_tools)
    messages = [Msg(name="user", role="user", content=[TextBlock(text="Review")])]
    with pytest.raises(Captured):
        await chat._call_api(name, messages, **kwargs)
    sdk.chat.assert_awaited_once()
    sdk.responses.assert_not_awaited()
    wire = sdk.chat.call_args.kwargs
    assert wire["reasoning_effort"] == "none"
    assert wire["max_completion_tokens"] == 4096
    assert wire["stream"] is True and "messages" in wire
    assert not {"input", "reasoning", "max_output_tokens", "max_tokens"}.intersection(wire)
    assert wire["extra_body"] == {"custom_option": True}
    assert bool(wire.get("tools")) is use_tools
    if use_tools:
        assert wire["tools"][0]["function"]["name"] == shell._SHELL_GUARD_DECISION_TOOL_NAME

    with pytest.raises(Captured):
        await main._call_api(name, messages)
    sdk.responses.assert_awaited_once()
    assert sdk.responses.call_args.kwargs["reasoning"] == {"effort": "high"}
    assert original.provider.model_dump() == provider_before
    assert main._extra_generate_kwargs == main_before
    assert original.raw.parameters.model_dump() == old_parameters
    assert "stage=route" in caplog.text and "trace_id=trace-test" in caplog.text
    assert "api=chat_completions changed=True reason=gpt_thinking_disabled" in caplog.text
    assert "test-secret-key" not in caplog.text


@pytest.mark.parametrize("name,provider_class,provider_id,is_local", [
    ("deepseek-v4-pro", OpenAIProvider, "project", False),
    ("qwen3.8-max-0902", OpenAIProvider, "project", False),
    ("gpt-oss-120b", OpenAIProvider, "project", False),
    ("deepseek-v4-pro", OpenAIResponseProvider, "project", False),
    ("qwen3.8-max-0902", OpenAIResponseProvider, "project", False),
    ("gpt-5.6-sol", OpenAIResponseProvider, "fw-agent-local", False),
    ("gpt-5.6-sol", OpenAIResponseProvider, "custom-local", True),
    ("gpt-5.6-sol", OpenAIProvider, "fw-agent-local", False),
    ("gpt-6.1-sol", OpenAIResponseProvider, "project", False),
])
def test_other_models_local_and_unknown_gpt_keep_their_instances(
    monkeypatch, factory, name, provider_class, provider_id, is_local,
):
    monkeypatch.setenv(ENV, "1")
    original = factory(name, provider_class=provider_class,
                       provider_id=provider_id, is_local=is_local)
    before = original.provider.model_dump()
    model = shell._create_shell_guard_model()
    assert model is original.model and original.usage._model is original.raw
    assert model.parameters is original.raw.parameters
    assert original.provider.model_dump() == before


@pytest.mark.parametrize("generation,changed", [
    ({"reasoning_effort": "high"}, False),
    ({}, False),
    ({"disable_thinking": True}, True),
    ({"extra_body": {"disable_thinking": True}}, True),
    ({"disable_thinking": False, "extra_body": {"disable_thinking": True}}, False),
])
def test_guard_disable_precedence_is_preserved(monkeypatch, factory, generation, changed):
    monkeypatch.setenv(ENV, "0")
    original = factory(generation=generation)
    model = shell._create_shell_guard_model()
    assert (original.usage._model is not original.raw) is changed
    assert model is original.model


@pytest.mark.parametrize("variable,value", [
    (shell._SHELL_GUARD_TOKEN_PARAM_ENV, "max_output_tokens"),
    (shell._SHELL_GUARD_THINKING_PARAM_ENV, "reasoning"),
])
def test_explicit_responses_overrides_keep_existing_protocol(monkeypatch, factory, variable, value):
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setenv(variable, value)
    original = factory()
    assert shell._create_shell_guard_model() is original.model
    assert original.usage._model is original.raw


def test_loopback_is_not_used_to_classify_a_provider_as_local(monkeypatch, factory):
    monkeypatch.setenv(ENV, "1")
    original = factory(base_url="http://127.0.0.1:8000/v1")
    shell._create_shell_guard_model()
    assert isinstance(original.usage._model, OpenAIChatModel)
    assert str(original.usage._model.credential.base_url) == str(original.raw.credential.base_url)


def test_unknown_provider_is_not_guessed(monkeypatch, factory):
    monkeypatch.setenv(ENV, "1")
    original = factory()
    monkeypatch.setattr(ProviderManager, "_instance", None)
    assert shell._create_shell_guard_model() is original.model
    assert original.usage._model is original.raw


@pytest.mark.parametrize("use_tools", [True, False])
async def test_both_shell_entrypoints_select_chat(monkeypatch, factory, tmp_path, use_tools):
    monkeypatch.setenv(ENV, "1")
    factory()
    content = [ToolCallBlock(
        id="call_guard", name=shell._SHELL_GUARD_DECISION_TOOL_NAME,
        input=json.dumps(DECISION),
    )] if use_tools else [TextBlock(text=json.dumps(DECISION))]
    consume = AsyncMock(return_value=ChatResponse(content=content, is_last=True))
    monkeypatch.setattr(shell, "_guard_call_with_param_fallback", consume)
    call = shell._call_windows_direct_shell_llm_guard_with_tool_call if use_tools else shell._call_windows_direct_shell_llm_guard_with_json
    result = await call(cmd="example --version", cwd=tmp_path,
                        shell_executable=None, timeout=20, deadline=100,
                        trace_id="entry-test")
    model, _messages, kwargs = consume.call_args.args
    assert isinstance(shell._shell_guard_model_chain(model)[-1], OpenAIChatModel)
    assert kwargs["reasoning_effort"] == "none"
    assert bool(kwargs.get("tools")) is use_tools
    assert result.verdict == "allow_shell"


async def test_real_sdk_chat_stream_decision(monkeypatch, factory):
    monkeypatch.setenv(ENV, "1")
    factory()
    model = shell._create_shell_guard_model()
    raw = shell._shell_guard_model_chain(model)[-1]
    wire = []
    arguments = json.dumps(DECISION)

    def respond(request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["X-Project"] == "test-project"
        wire.append(json.loads(request.content))
        base = {"id": "chatcmpl_test", "object": "chat.completion.chunk",
                "created": 1, "model": "gpt-5.6-sol", "usage": None}
        deltas = [
            {"tool_calls": [{"index": 0, "id": "call_guard", "type": "function",
                             "function": {"name": shell._SHELL_GUARD_DECISION_TOOL_NAME,
                                          "arguments": arguments[:30]}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": arguments[30:]}}]},
            {},
        ]
        events = [{**base, "choices": [{"index": 0, "delta": delta,
                   "finish_reason": "tool_calls" if index == 2 else None}]}
                  for index, delta in enumerate(deltas)]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=body + "data: [DONE]\n\n")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    raw.client_kwargs["http_client"] = http_client
    real_factory = openai.AsyncClient

    def make_client(**kwargs):
        sdk_client = real_factory(**kwargs)
        sdk_client._platform = "Windows"  # Avoid background OS probing in mock-only tests.
        return sdk_client

    monkeypatch.setattr(openai, "AsyncClient", make_client)
    try:
        response = await shell._consume_model_response_object(raw, [
            Msg(name="user", role="user", content=[TextBlock(text="Review")]),
        ], **shell._build_shell_guard_request_kwargs(model, use_tools=True))
    finally:
        await http_client.aclose()
    assert len(wire) == 1 and wire[0]["stream"] is True
    assert wire[0]["reasoning_effort"] == "none"
    assert wire[0]["max_completion_tokens"] == 4096
    assert response.content[0].id == "call_guard"
    assert shell._extract_shell_guard_tool_call_arguments(response) == DECISION
