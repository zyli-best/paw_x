"""Exercise provider routing and the SDK requests used by shell review."""

import ast
import copy
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg, TextBlock

from qwenpaw.agents.tools import shell
from qwenpaw.providers import ProviderManager
from qwenpaw.providers.dashscope_provider import DashScopeProvider
from qwenpaw.providers.openai_provider import OpenAIProvider
from qwenpaw.providers.openai_response_provider import OpenAIResponseProvider


ENV = "QWENPAW_SHELL_GUARD_DISABLE_THINKING"
GATEWAY = "https://gateway.example/api/dev/model/v2/project"


@pytest.fixture(autouse=True)
def isolated_guard_settings(monkeypatch):
    for key in (ENV, shell._SHELL_GUARD_THINKING_PARAM_ENV,
                shell._SHELL_GUARD_TOKEN_PARAM_ENV,
                shell._WINDOWS_DIRECT_SHELL_LLM_GUARD_MAX_TOKENS_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(ProviderManager, "_instance", None)


def guard_model(monkeypatch, provider_class, name, *, provider_id="fw-agent-project",
                is_local=False, base_url=GATEWAY, generation=None):
    provider = provider_class(
        id=provider_id, name=provider_id, api_key="test-key",
        base_url=base_url, is_custom=True, is_local=is_local,
        generate_kwargs=generation or {},
    )
    raw = provider.get_chat_model_instance(name)
    wrapper = SimpleNamespace(
        _provider_id=provider_id, _model=raw, parameters=raw.parameters,
    )
    monkeypatch.setattr(ProviderManager, "_instance", SimpleNamespace(
        get_provider=lambda identifier: provider if identifier == provider_id else None,
    ))
    return provider, raw, wrapper


class WireCaptured(Exception):
    """Stop at the SDK boundary; never contact a model endpoint."""


async def sdk_request(monkeypatch, raw, kwargs):
    import openai

    create = AsyncMock(side_effect=WireCaptured)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        responses=SimpleNamespace(create=create),
    )
    monkeypatch.setattr(openai, "AsyncClient", lambda **options: client)
    with pytest.raises(WireCaptured):
        await raw._call_api(
            raw.model, [Msg(name="user", role="user", content=[TextBlock(text="Review command")])],
            **kwargs,
        )
    return create.call_args.kwargs


@pytest.mark.parametrize("use_tools", [True, False], ids=["function-call", "json"])
@pytest.mark.parametrize("provider_class,name,base_url,expected", [
    (OpenAIProvider, "qwen3.8-max-0902", GATEWAY, "enable_thinking"),
    (OpenAIProvider, "qwen3.8-max", GATEWAY, "enable_thinking"),
    (OpenAIProvider, "qwen3.8-flash", GATEWAY, "enable_thinking"),
    (OpenAIProvider, "qwen3.7-max", GATEWAY, "enable_thinking"),
    (OpenAIProvider, "qwen3.7-plus", GATEWAY, "enable_thinking"),
    (OpenAIProvider, "deepseek-v4.1-flash", GATEWAY, "thinking"),
    (OpenAIProvider, "deepseek-v4-pro", GATEWAY, "thinking"),
    (OpenAIProvider, "deepseek-v4-flash-0731", GATEWAY, "thinking"),
    (OpenAIProvider, "gpt-5.6-sol", GATEWAY, "reasoning"),
    (OpenAIProvider, "gpt-5.6-luna", GATEWAY, "reasoning"),
    (OpenAIResponseProvider, "gpt-5.6-sol", GATEWAY, "reasoning"),
    (OpenAIResponseProvider, "deepseek-v4-pro", GATEWAY, "reasoning"),
    (OpenAIResponseProvider, "qwen3.8-max-0902", GATEWAY, "enable_thinking"),
    (DashScopeProvider, "deepseek-v4-pro",
     "https://dashscope.aliyuncs.com/compatible-mode/v1", "enable_thinking"),
])
async def test_online_disable_reaches_sdk_without_inherited_thinking(
    monkeypatch, caplog, provider_class, name, base_url, expected, use_tools,
):
    monkeypatch.setenv(ENV, "1")
    generation = {
        "max_tokens": 4096, "reasoning_effort": "xhigh",
        "extra_body": {
            "thinking": {"type": "enabled"}, "enable_thinking": True,
            "thinking_budget": 2048,
            "chat_template_kwargs": {"enable_thinking": True, "keep": "value"},
        },
    }
    before = copy.deepcopy(generation)
    provider, raw, model = guard_model(
        monkeypatch, provider_class, name, base_url=base_url, generation=generation,
    )
    original_parameters = raw.parameters
    original_values = original_parameters.model_dump()
    with caplog.at_level(logging.INFO):
        kwargs = shell._build_shell_guard_request_kwargs(model, use_tools=use_tools)
    wire = await sdk_request(monkeypatch, raw, kwargs)
    body = wire.get("extra_body", {})
    if expected == "enable_thinking":
        assert body[expected] is False
    elif expected == "thinking":
        assert body[expected] == {"type": "disabled"}
    elif expected == "reasoning":
        assert wire[expected] == {"effort": "none"}
    else:
        assert wire[expected] == "none"
    for key in ("thinking", "enable_thinking", "thinking_budget", "reasoning_effort"):
        if key != expected:
            assert key not in body
            assert key not in wire
    assert body["chat_template_kwargs"] == {"keep": "value"}
    assert bool(wire.get("tools")) is use_tools
    assert provider.generate_kwargs == before
    assert original_parameters.model_dump() == original_values
    assert model.parameters is raw.parameters and raw.parameters is not original_parameters
    assert "thinking_mode=disabled" in caplog.text


@pytest.mark.parametrize("name", [
    "Qwen3.6-35B-A3B", "qwen3.5-122b-a10b-fp8", "gpt-oss-120b",
    "namespace/Qwen3.6-35B-A3B",
])
@pytest.mark.parametrize("use_tools", [True, False])
async def test_protected_local_models_keep_thinking_on_remote_gateway(
    monkeypatch, caplog, name, use_tools,
):
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setenv(shell._SHELL_GUARD_THINKING_PARAM_ENV, "reasoning_effort")
    _, raw, model = guard_model(monkeypatch, OpenAIProvider, name, generation={
        "reasoning_effort": "high", "disable_thinking": True,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": True}},
    })
    with caplog.at_level(logging.INFO):
        kwargs = shell._build_shell_guard_request_kwargs(model, use_tools=use_tools)
    wire = await sdk_request(monkeypatch, raw, kwargs)
    assert wire["reasoning_effort"] == "high"
    assert wire["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
    assert "disable_thinking" not in wire
    assert "thinking_mode=preserved_local" in caplog.text


@pytest.mark.parametrize("provider_id,is_local,base_url", [
    ("fw-agent-local", False, GATEWAY),
    ("custom-local", True, GATEWAY),
    ("custom", True, "http://127.0.0.1:8000/v1"),
])
async def test_local_provider_identity_takes_precedence_over_online_model_name(
    monkeypatch, provider_id, is_local, base_url,
):
    monkeypatch.setenv(ENV, "1")
    _, raw, model = guard_model(
        monkeypatch, OpenAIProvider, "gpt-5.6-sol", provider_id=provider_id,
        is_local=is_local, base_url=base_url,
        generation={"reasoning_effort": "high"},
    )
    wire = await sdk_request(monkeypatch, raw, shell._build_shell_guard_request_kwargs(
        model, use_tools=True,
    ))
    assert wire["reasoning_effort"] == "high"


async def test_online_provider_using_loopback_proxy_still_disables_thinking(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    _, raw, model = guard_model(
        monkeypatch, OpenAIProvider, "gpt-5.6-sol",
        base_url="http://127.0.0.1:8000/v1",
        generation={"reasoning_effort": "high"},
    )
    wire = await sdk_request(monkeypatch, raw, shell._build_shell_guard_request_kwargs(
        model, use_tools=True,
    ))
    assert wire["reasoning"] == {"effort": "none"}


@pytest.mark.parametrize("name", ["gpt-5.6-sol", "gpt-5.6-luna"])
@pytest.mark.parametrize("use_tools", [True, False])
@pytest.mark.parametrize("generation,expected", [
    ({"reasoning_effort": "high"}, {"effort": "high"}),
    ({"reasoning": {"effort": "high"}}, {"effort": "high"}),
    ({}, None),
])
async def test_gpt_shell_thinking_enabled_uses_responses_without_changing_main(
    monkeypatch, name, use_tools, generation, expected,
):
    monkeypatch.setenv(ENV, "0")
    provider, raw, model = guard_model(
        monkeypatch, OpenAIProvider, name, generation=generation,
    )
    main = provider.get_chat_model_instance(name)
    original = copy.deepcopy(main._extra_generate_kwargs)
    wire = await sdk_request(monkeypatch, raw, shell._build_shell_guard_request_kwargs(
        model, use_tools=use_tools,
    ))
    assert "input" in wire and "messages" not in wire
    assert wire.get("reasoning") == expected
    assert "reasoning_effort" not in wire
    assert bool(wire.get("tools")) is use_tools
    assert main._extra_generate_kwargs == original
    assert provider.generate_kwargs == generation


async def test_disabled_switch_preserves_online_configuration(monkeypatch):
    monkeypatch.setenv(ENV, "0")
    _, raw, model = guard_model(monkeypatch, OpenAIProvider, "qwen3.8-max-0902",
                               generation={"thinking_enable": True, "thinking_budget": 2048})
    wire = await sdk_request(monkeypatch, raw, shell._build_shell_guard_request_kwargs(
        model, use_tools=True,
    ))
    assert wire["extra_body"]["enable_thinking"] is True
    assert wire["extra_body"]["thinking_budget"] == 2048


def test_unknown_online_reasoning_protocol_is_not_guessed(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    _, _, model = guard_model(monkeypatch, OpenAIProvider, "gpt-6.1-sol")
    with pytest.raises(ValueError, match="no verified thinking-disable protocol"):
        shell._build_shell_guard_request_kwargs(model, use_tools=True)


def test_entry_switch_preserves_environment_and_consumes_cli_option(monkeypatch):
    import argparse
    import os
    import sys

    source = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in {"parse_entry_cli_args", "apply_devco_bundle_env"}]
    entry = dict(argparse=argparse, os=os, sys=sys, _INIT_ONLY_ENV="TEST_INIT_ONLY")
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), entry)
    monkeypatch.setattr(os, "environ", {})
    parse = entry["parse_entry_cli_args"]
    assert parse(["--port", "8088", "--reload"]) == ["--port", "8088", "--reload"]
    assert os.environ[ENV] == "1"
    os.environ[ENV] = "0"
    assert parse([]) == [] and os.environ[ENV] == "0"
    assert parse(["--shell-guard-disable-thinking", "0", "--reload"]) == ["--reload"]
    assert os.environ[ENV] == "0"
    assert parse(["--qwenpaw-shell-guard-disable-thinking", "1"]) == []
    assert os.environ[ENV] == "1"
