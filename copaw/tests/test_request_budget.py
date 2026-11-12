"""Configured ReAct budgets and real Scroll/AgentScope compression routing."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agentscope.agent import Agent, ReActConfig
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatResponse
from agentscope.tool import FunctionTool, ToolChunk, Toolkit

from qwenpaw.agents.context.budget_middleware import RequestBudgetMiddleware
from qwenpaw.agents.context.request_budget import (
    RequestBudget,
    RequestSnapshot,
    budget_for_model,
    configured_model_budget,
)
from qwenpaw.agents.context.scroll.history import HistoryStore
from qwenpaw.agents.context.scroll.manager import ScrollContextManager
from qwenpaw.exceptions import (
    ConfigurationException,
    ModelContextLengthExceededException,
)
from qwenpaw.observability.model_input_redaction_guard import (
    ModelInputRedactionGuard,
    TurnRedactionScope,
    maybe_wrap_model_for_redaction_confirm,
)
from qwenpaw.providers.provider import ModelInfo


def message(text, role="user", **kwargs):
    return Msg(
        name=role,
        role=role,
        content=[TextBlock(type="text", text=text)],
        **kwargs,
    )


class CountingModel:
    """Deterministic token counts for control-flow tests, without network IO."""

    def __init__(self, context=1000, output=600):
        self.model = "budget-test"
        self.context_size = context
        self.parameters = SimpleNamespace(max_tokens=output)
        self.requests = []
        self.responses = []

    async def count_tokens(self, messages, tools):
        total = len(json.dumps(tools)) if tools else 0
        for msg in messages:
            for block in msg.content:
                if isinstance(block, TextBlock):
                    total += len(block.text)
                elif isinstance(block, ToolCallBlock):
                    total += len(block.input)
                elif isinstance(block, ToolResultBlock):
                    total += (
                        len(block.output)
                        if isinstance(block.output, str)
                        else sum(
                            len(item.text)
                            for item in block.output
                            if isinstance(item, TextBlock)
                        )
                    )
        return total

    async def __call__(self, messages, tools=None, **kwargs):
        self.requests.append((list(messages), tools))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return ChatResponse(
            content=[TextBlock(type="text", text="done")], is_last=True
        )


def failure_message(text, reply_id="turn"):
    from qwenpaw.constant import (
        FAILURE_RECOVERY_MESSAGE_TAG,
        QWENPAW_MESSAGE_TAG_KEY,
    )

    return Msg(
        name="failure_recovery",
        role="user",
        content=[
            TextBlock(type="text", text=text),
        ],
        metadata={
            QWENPAW_MESSAGE_TAG_KEY: FAILURE_RECOVERY_MESSAGE_TAG,
            "source": "failure_loop",
            "reply_id": reply_id,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("base_fits", [True, False])
async def test_optional_failure_hint_cannot_block_fitting_request(
    enabled, base_fits
):
    model = CountingModel()
    current = message("x" * (350 if base_fits else 450))
    hint = failure_message("h" * 100)
    agent = SimpleNamespace(
        state=SimpleNamespace(
            context=[current],
            reply_id="turn",
            cur_iter=0,
        )
    )
    manager = SimpleNamespace(compress_to_budget=AsyncMock())
    middleware = RequestBudgetMiddleware(manager, compression_enabled=enabled)
    handler = AsyncMock()
    kwargs = dict(current_model=model, messages=[current, hint], tools=[])
    if base_fits:
        await middleware.on_model_call(agent, kwargs, handler)
        assert handler.await_args.kwargs["messages"] == [current]
    else:
        with pytest.raises(ModelContextLengthExceededException):
            await middleware.on_model_call(agent, kwargs, handler)
        handler.assert_not_awaited()
    assert manager.compress_to_budget.await_count == int(enabled)
    assert agent.state.context == [current]
    assert model.parameters.max_tokens == 600


@pytest.mark.asyncio
async def test_other_gate_reminder_is_not_optional_failure_hint():
    current = message("x" * 350)
    other_gate = message("DoomLoop " * 20)
    agent = SimpleNamespace(
        state=SimpleNamespace(
            context=[current],
            reply_id="turn",
            cur_iter=0,
        )
    )
    middleware = RequestBudgetMiddleware(
        SimpleNamespace(
            compress_to_budget=AsyncMock(),
        )
    )
    handler = AsyncMock()
    with pytest.raises(ModelContextLengthExceededException):
        await middleware.on_model_call(
            agent,
            dict(
                current_model=CountingModel(),
                messages=[current, other_gate],
                tools=[],
            ),
            handler,
        )
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_interleaved_plugin_input_can_omit_only_optional_hint():
    first, last = message("x" * 100), message("y" * 100)
    plugin = message("plugin content " * 5)
    hint = failure_message("h" * 200)
    agent = SimpleNamespace(
        state=SimpleNamespace(
            context=[first, last],
            reply_id="turn",
            cur_iter=0,
        )
    )
    manager = SimpleNamespace(compress_to_budget=AsyncMock())
    middleware = RequestBudgetMiddleware(manager)
    handler = AsyncMock()
    await middleware.on_model_call(
        agent,
        dict(
            current_model=CountingModel(),
            messages=[first, plugin, last, hint],
            tools=[],
        ),
        handler,
    )
    assert handler.await_args.kwargs["messages"] == [first, plugin, last]
    manager.compress_to_budget.assert_not_awaited()
    assert agent.state.context == [first, last]


@pytest.mark.parametrize(
    "inputs,expected", [(65536, False), (65537, True), (1000, False)]
)
def test_screenshot_limits_and_equality(inputs, expected):
    budget = RequestBudget(131072, 65536)
    assert budget.input_limit == 65536
    assert budget.exceeded(inputs) is expected


@pytest.mark.parametrize("invalid", [0, -1, True, "100"])
def test_budget_requires_positive_integer_limits(invalid):
    with pytest.raises(ValueError):
        RequestBudget(invalid, 100)


def test_factory_captures_user_configuration_including_catalog_default_value():
    info = ModelInfo(
        id="test", name="test", max_input_length=131072, max_tokens=65536
    )
    raw_model = CountingModel(context=1000000, output=65536)
    provider = SimpleNamespace(
        get_model_info=lambda name: info,
        get_effective_generate_kwargs=lambda name: {"max_tokens": 65536},
    )
    budget = configured_model_budget(provider, "test", raw_model)
    assert budget == RequestBudget(131072, 65536)
    assert raw_model.context_size == 1000000  # no global model mutation
    wrapper = SimpleNamespace(
        _inner=raw_model, context_size=32768, _qwenpaw_request_budget=budget
    )
    assert budget_for_model(wrapper) == budget


def test_unconfigured_window_keeps_existing_model_resolution():
    info = ModelInfo(id="test", name="test")
    provider = SimpleNamespace(
        get_model_info=lambda name: info,
        get_effective_generate_kwargs=lambda name: {"max_tokens": 100},
    )
    assert configured_model_budget(
        provider, "test", CountingModel(context=1000000)
    ) == RequestBudget(1000000, 100)


def test_factory_attaches_budget_to_the_outer_wrapper(monkeypatch):
    from qwenpaw.agents import model_factory

    raw = CountingModel(context=1000000, output=65536)
    provider = SimpleNamespace(
        get_chat_model_instance=lambda name: raw,
        get_model_info=lambda name: ModelInfo(
            id=name, name=name, max_input_length=131072, max_tokens=65536
        ),
        get_effective_generate_kwargs=lambda name: {"max_tokens": 65536},
    )
    monkeypatch.setattr(
        model_factory.ProviderManager,
        "get_instance",
        lambda: SimpleNamespace(get_provider=lambda name: provider),
    )
    wrapped, formatter = SimpleNamespace(), object()
    wrapping = Mock(return_value=(wrapped, formatter))
    monkeypatch.setattr(model_factory, "_wrap_model_and_formatter", wrapping)
    result, result_formatter = (
        model_factory._create_model_and_formatter_from_slot(
            SimpleNamespace(provider_id="test-provider", model="test-model"),
        )
    )
    assert result is wrapped and result_formatter is formatter
    assert budget_for_model(result) == RequestBudget(131072, 65536)
    wrapping.assert_called_once()


@pytest.mark.parametrize(
    "alias", ["max_tokens", "max_completion_tokens", "max_output_tokens"]
)
def test_output_alias_and_wrapper_resolution(alias):
    raw = CountingModel()
    raw._extra_generate_kwargs = {alias: 700}
    wrapper = SimpleNamespace(
        _inner=SimpleNamespace(_model=raw),
        context_size=1000,
        parameters=SimpleNamespace(max_tokens=600),
    )
    assert budget_for_model(wrapper) == RequestBudget(1000, 700)
    raw._model = wrapper  # traversal has a cycle guard
    assert budget_for_model(wrapper) == RequestBudget(1000, 700)


@pytest.mark.parametrize("captured", [False, True])
@pytest.mark.parametrize("mode", [None, "off", "auto", "confirm"])
@pytest.mark.asyncio
async def test_redaction_scope_preserves_budget_and_model_call(mode, captured):
    model = CountingModel()
    expected_budget = RequestBudget(1000, 600)
    if captured:
        expected_budget = RequestBudget(900, 650)
        model._qwenpaw_request_budget = expected_budget
    manager = SimpleNamespace(compress_to_budget=AsyncMock())
    agent = Agent(
        name="test",
        model=model,
        system_prompt="system",
        toolkit=Toolkit(),
        react_config=ReActConfig(max_iters=10),
        middlewares=[RequestBudgetMiddleware(manager)],
    )
    current = message("你好啊")
    agent.state.context = [current]
    request_context = {
        "sensitive_words": {
            "patterns": [{"type": "builtin", "key": "phone"}],
        },
    }
    if mode is not None:
        request_context["model_input_redaction"] = {"mode": mode}
    scope = TurnRedactionScope(
        agent=agent, request_context=request_context,
    )
    restore = maybe_wrap_model_for_redaction_confirm(agent, turn_scope=scope)
    try:
        assert scope.enabled is (mode in {"auto", "confirm"})
        assert isinstance(agent.model, ModelInputRedactionGuard) is scope.enabled
        assert budget_for_model(agent.model) == expected_budget
        await agent._call_model(
            messages=[current], tools=[], tool_choice="none",
        )
        assert len(model.requests) == 1
        assert model.requests[0][0] == [current]
        manager.compress_to_budget.assert_not_awaited()
    finally:
        if restore is not None:
            restore()
    assert agent.model is model


def test_redaction_guard_preserves_output_alias_and_cycle_safety():
    model = CountingModel()
    model._extra_generate_kwargs = {"max_completion_tokens": 700}
    guard = ModelInputRedactionGuard(
        original_model=SimpleNamespace(_inner=SimpleNamespace(_model=model)),
        request_context={},
        sensitive_words={},
        config={},
    )
    assert budget_for_model(guard) == RequestBudget(1000, 700)
    cyclic = SimpleNamespace()
    cyclic._original_model = cyclic
    assert budget_for_model(cyclic) is None


@pytest.mark.parametrize(
    "config,patterns",
    [({}, True), ({"mode": "off"}, True), ({"mode": "auto"}, False)],
)
def test_disabled_redaction_scope_does_not_wrap_model(config, patterns):
    model = CountingModel()
    agent = SimpleNamespace(model=model, state=SimpleNamespace(context=[]))
    scope = TurnRedactionScope(
        agent=agent,
        request_context={
            "model_input_redaction": config,
            "sensitive_words": {
                "patterns": [{"type": "builtin", "key": "phone"}]
                if patterns else [],
            },
        },
    )
    assert not scope.enabled
    assert maybe_wrap_model_for_redaction_confirm(agent, turn_scope=scope) is None
    assert agent.model is model


@pytest.mark.asyncio
async def test_snapshot_preserves_per_call_prefix_suffix_and_tool_schema():
    old = message("old")
    active = message("request")
    prefix, suffix = message("system", "system"), message("extra recall")
    agent = SimpleNamespace(state=SimpleNamespace(context=[old, active]))
    tools = [{"function": {"name": "current_tool"}}]
    snapshot = RequestSnapshot.capture(
        agent, [prefix, old, active, suffix], tools
    )
    agent.state.context = [active]
    rebuilt = await snapshot.rebuild(agent)
    assert rebuilt["messages"] == [prefix, active, suffix]
    assert rebuilt["tools"] is tools
    assert rebuilt["messages"].count(active) == 1
    with pytest.raises(ValueError):
        RequestSnapshot.capture(
            SimpleNamespace(state=SimpleNamespace(context=[old, active])),
            [prefix, old, suffix, active],
            tools,
        )


@pytest.mark.asyncio
async def test_under_budget_and_unknown_configuration():
    model = CountingModel()
    manager = SimpleNamespace(compress_to_budget=AsyncMock())
    middleware = RequestBudgetMiddleware(manager)
    current = message("request")
    agent = SimpleNamespace(state=SimpleNamespace(context=[current]))
    kwargs = dict(current_model=model, messages=[current], tools=[])
    handler = AsyncMock(return_value="result")
    assert await middleware.on_model_call(agent, kwargs, handler) == "result"
    manager.compress_to_budget.assert_not_called()
    model.parameters.max_tokens = None
    with pytest.raises(ConfigurationException):
        await middleware.on_model_call(agent, kwargs, handler)


@pytest.mark.asyncio
async def test_server_defaults_do_not_require_limits_or_disable_cloud_checks():
    model = CountingModel(output=None)
    model._qwenpaw_use_model_defaults = True
    manager = SimpleNamespace(compress_to_budget=AsyncMock())
    middleware = RequestBudgetMiddleware(manager)
    current = message("request")
    agent = SimpleNamespace(state=SimpleNamespace(context=[current]))
    kwargs = dict(current_model=model, messages=[current], tools=[])
    handler = AsyncMock(return_value="result")
    assert await middleware.on_model_call(agent, kwargs, handler) == "result"
    handler.assert_awaited_once()
    manager.compress_to_budget.assert_not_called()
    assert model.parameters.max_tokens is None

    # Returning to an ordinary/cloud model must restore the strict policy.
    kwargs["current_model"] = CountingModel(output=None)
    with pytest.raises(ConfigurationException):
        await middleware.on_model_call(agent, kwargs, handler)
    assert handler.await_count == 1


@pytest.mark.asyncio
async def test_final_request_additions_are_counted_and_preserved():
    model = CountingModel()
    old, active = message("x" * 500, "assistant"), message("request")
    extra = message("late memory " * 5)
    system = message("system", "system")
    agent = SimpleNamespace(state=SimpleNamespace(context=[old, active]))

    async def compress(current_agent, **kwargs):
        current_agent.state.context = [active]

    manager = SimpleNamespace(
        compress_to_budget=AsyncMock(side_effect=compress)
    )
    middleware = RequestBudgetMiddleware(manager)
    messages = [system, old, active, extra]
    kwargs = dict(current_model=model, messages=messages, tools=[])
    handler = AsyncMock()
    await middleware.on_model_call(agent, kwargs, handler)
    assert messages == [system, active, extra]
    assert model.parameters.max_tokens == 600
    manager.compress_to_budget.assert_awaited_once()
    handler.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "unchanged",
        "disabled",
        "tools_too_large",
        "no_input_space",
        "ambiguous",
    ],
)
async def test_unfit_or_unsafe_request_never_reaches_model(mode):
    model = CountingModel()
    old, active = message("x" * 500, "assistant"), message("request")
    agent = SimpleNamespace(state=SimpleNamespace(context=[old, active]))
    manager = SimpleNamespace(compress_to_budget=AsyncMock())
    middleware = RequestBudgetMiddleware(
        manager, compression_enabled=mode != "disabled"
    )
    messages = [old, active]
    tools = []
    if mode == "tools_too_large":
        tools = [{"description": "x" * 600}]
    if mode == "no_input_space":
        model.parameters.max_tokens = 1000
    if mode == "ambiguous":
        messages.insert(1, message("interleaved extra"))
    handler = AsyncMock()
    with pytest.raises(ModelContextLengthExceededException) as caught:
        await middleware.on_model_call(
            agent,
            dict(
                current_model=model,
                messages=messages,
                tools=tools,
            ),
            handler,
        )
    assert caught.value.error_code == "MODEL_CONTEXT_LENGTH_EXCEEDED"
    assert "token" in str(caught.value)
    handler.assert_not_called()
    assert model.parameters.max_tokens == (
        1000 if mode == "no_input_space" else 600
    )


@pytest.fixture
def scroll(tmp_path):
    history = HistoryStore(tmp_path / "history.db")
    manager = ScrollContextManager(
        history=history, session_id="test", summarize_unheadlined=False
    )
    yield manager
    history.close()


def sdk_agent(model):
    return Agent(
        name="test",
        model=model,
        system_prompt="system",
        toolkit=Toolkit(),
        react_config=ReActConfig(max_iters=10),
    )


@pytest.mark.asyncio
async def test_real_scroll_evicts_history_below_eighty_percent(scroll):
    model = CountingModel(context=10000, output=6000)
    agent = sdk_agent(model)
    active = message("current request")
    old = [message("old request"), message("x" * 5500, "assistant")]
    agent.state.context = [*old, active]
    before = await model.count_tokens(**(await agent._prepare_model_input()))
    assert 4000 < before < 8000
    middleware = RequestBudgetMiddleware(scroll)
    request = await agent._prepare_model_input()
    await middleware.on_model_call(
        agent, {"current_model": model, **request}, model_call_handler(model)
    )
    assert scroll.last_compress["evicted"] > 0
    assert active in agent.state.context
    assert old[1] not in agent.state.context
    assert await model.count_tokens(*model.requests[0]) <= 4000
    assert not scroll._history.degraded


def model_call_handler(model):
    async def handler(**kwargs):
        return await model(messages=kwargs["messages"], tools=kwargs["tools"])

    return handler


@pytest.mark.asyncio
async def test_normal_scroll_ratio_still_triggers(scroll):
    model = CountingModel(context=10000, output=100)
    agent = sdk_agent(model)
    agent.state.context = [
        message("old"),
        message("x" * 8300, "assistant"),
        message("current"),
    ]
    await scroll.compress(agent)
    assert scroll.last_compress["evicted"] > 0


@pytest.mark.asyncio
async def test_active_turn_old_tool_results_fold_for_budget_only(scroll):
    model = CountingModel(context=10000, output=6000)
    agent = sdk_agent(model)
    old_result = ToolResultBlock(
        id="a", name="read_file", output="x" * 5000, state="success"
    )
    latest_result = ToolResultBlock(
        id="b", name="read_file", output="latest result", state="success"
    )
    assistant = Msg(
        name="test",
        role="assistant",
        content=[
            ToolCallBlock(id="a", name="read_file", input="{}"),
            old_result,
            ToolCallBlock(id="b", name="read_file", input="{}"),
            latest_result,
        ],
    )
    agent.state.context = [message("current request"), assistant]
    await scroll.compress(agent)
    assert scroll.last_compress["folded"] == 1
    assert "[scroll folded]" in str(old_result.output)
    assert latest_result.output == "latest result"
    assert len(assistant.get_content_blocks("tool_call")) == 2
    assert (
        await model.count_tokens(**(await agent._prepare_model_input()))
        <= 4000
    )


@pytest.mark.asyncio
async def test_protected_single_input_and_failed_persistence_do_not_send(
    scroll, monkeypatch
):
    model = CountingModel(context=10000, output=6000)
    agent = sdk_agent(model)
    original = message("x" * 6000)
    agent.state.context = [original]
    middleware = RequestBudgetMiddleware(scroll)
    handler = AsyncMock()
    with pytest.raises(ModelContextLengthExceededException):
        await middleware.on_model_call(
            agent,
            {"current_model": model, **(await agent._prepare_model_input())},
            handler,
        )
    assert original in agent.state.context
    assert original.content[0].text == "x" * 6000
    monkeypatch.setattr(
        scroll, "_persist_guarded_async", AsyncMock(return_value=False)
    )
    agent.state.context = [
        message("old"),
        message("x" * 6000, "assistant"),
        message("current"),
    ]
    unchanged = list(agent.state.context)
    with pytest.raises(ModelContextLengthExceededException):
        await middleware.on_model_call(
            agent,
            {"current_model": model, **(await agent._prepare_model_input())},
            handler,
        )
    assert agent.state.context == unchanged
    handler.assert_not_called()


def qwen_agent(
    monkeypatch,
    model,
    scroll,
    middlewares=(),
    enabled=True,
    tools=(),
    failure_gate=None,
    progress=None,
):
    from qwenpaw.agents.react_agent import QwenPawAgent
    from qwenpaw.agents import model_factory
    from qwenpaw.loop.gates import StopHandlerResult, StopAction

    monkeypatch.setattr(
        QwenPawAgent, "_register_skills", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        model_factory, "_supports_multimodal_for_current_model", lambda: True
    )
    monkeypatch.setattr(
        QwenPawAgent, "_model_rejects_media", lambda self: False
    )
    config = SimpleNamespace(
        language="zh",
        running=SimpleNamespace(
            light_context_config=SimpleNamespace(
                context_compact_config=SimpleNamespace(enabled=enabled),
            )
        ),
    )
    agent = QwenPawAgent(
        name="test",
        model=model,
        system_prompt="system",
        toolkit=Toolkit(tools=list(tools)),
        react_config=ReActConfig(max_iters=10),
        middlewares=[
            *middlewares,
            RequestBudgetMiddleware(scroll, compression_enabled=enabled),
        ],
        agent_config=config,
        context_manager=scroll,
        failure_loop_gate=failure_gate,
        progress_reminder=progress,
    )
    monkeypatch.setattr(
        agent,
        "_run_stop_handlers",
        AsyncMock(return_value=StopHandlerResult(action=StopAction.TERMINATE)),
    )
    return agent


@pytest.mark.asyncio
async def test_failure_hint_fallback_reuses_compressed_history(
    monkeypatch, scroll, tmp_path
):
    from qwenpaw.agents.context.failure_middleware import is_failure_hint
    from qwenpaw.loop.failure_config import FailureLoopConfig
    from qwenpaw.loop.gates.failure_loop import FailureLoopGate

    primary = CountingModel(context=10000, output=6000)
    primary.responses = [RuntimeError("switch to fallback")]
    fallback = CountingModel(context=8000, output=5000)
    gate = FailureLoopGate(
        FailureLoopConfig(enabled=True),
        history_path=tmp_path / "review.db",
    )
    agent = qwen_agent(monkeypatch, primary, scroll, failure_gate=gate)
    agent.model_config.max_retries = 0
    agent.model_config.fallback_model = fallback
    agent.state.reply_id = "turn"
    agent.state.context = [
        message("old"),
        message("x" * 5000, "assistant"),
        message("current request"),
    ]
    gate.begin_turn("turn")
    gate.remember_call("turn", "missing", "read_file", '{"file_path":"a.txt"}')
    gate.append_text("turn", "missing", "Error [FILE_NOT_FOUND]: missing")
    gate.complete_call("turn", "missing", "error")
    # This test isolates delivery/budget/fallback from the separate model review.
    monkeypatch.setattr(
        gate._state().inspector,
        "inspect",
        AsyncMock(return_value=("Review current results before repeating.",)),
    )
    await agent._inject_failure_recovery()
    await agent._call_model(messages=list(agent.state.context), tools=[])
    assert len(primary.requests) == len(fallback.requests) == 1
    for model in (primary, fallback):
        sent, tools = model.requests[0]
        hints = [msg for msg in sent if is_failure_hint(msg)]
        assert len(hints) == 1
        assert await model.count_tokens(hints, []) <= 512
        assert (
            await model.count_tokens(sent, tools) + model.parameters.max_tokens
            <= model.context_size
        )
    assert not any(is_failure_hint(msg) for msg in agent.state.context)
    gate.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_real_react_counts_late_memory_and_injected_hints(
    monkeypatch, scroll, retry
):
    from qwenpaw.agents.middlewares import MemoryMiddleware

    model = CountingModel(context=10000, output=6000)
    if retry:
        model.responses = [RuntimeError("temporary model failure")]
    turn_state = {"searched_turn": "", "pending": [], "seen": {}}
    memory_result = Msg(
        name="memory_search",
        role="assistant",
        content=[
            ToolCallBlock(
                id="memory", name="memory_search", input="{}", state="finished"
            ),
            ToolResultBlock(
                id="memory",
                name="memory_search",
                output="m" * 2500,
                state="success",
            ),
        ],
    )
    memory = SimpleNamespace(
        get_auto_memory_turn_state=lambda session: turn_state,
        auto_memory_search=AsyncMock(
            return_value={"messages": [memory_result]}
        ),
    )
    agent = qwen_agent(
        monkeypatch, model, scroll, [MemoryMiddleware(memory_manager=memory)]
    )
    agent.model_config.max_retries = 1 if retry else 0
    # Bypass unrelated prompt/post-reply memory behavior, keeping actual model-call injection.
    monkeypatch.setattr(
        agent,
        "_get_system_prompt",
        AsyncMock(return_value=[TextBlock(type="text", text="system")]),
    )
    monkeypatch.setattr(agent, "_reply_middlewares", [])
    agent.state.context = [message("old"), message("x" * 3000, "assistant")]
    agent._gate_pending_continue = "continue reminder"
    _ = [evt async for evt in agent._reply(inputs=message("current request"))]
    assert len(model.requests) == (2 if retry else 1)
    for request in model.requests:
        texts = str(request[0])
        assert "current request" in texts and "continue reminder" in texts
        assert "m" * 2500 in texts
        assert await model.count_tokens(*request) <= 4000
    assert memory.auto_memory_search.await_count == 1


@pytest.mark.asyncio
async def test_real_react_checks_next_inference_after_tool_results(
    monkeypatch, scroll
):
    executed = []

    async def inspect_details(which: str) -> ToolChunk:
        """Inspect details without changing state."""
        executed.append(which)
        return ToolChunk(
            content=[
                TextBlock(
                    type="text",
                    text="x" * 5000 if which == "large" else "latest result",
                )
            ],
            state="success",
            is_last=True,
        )

    model = CountingModel(context=10000, output=6000)
    model.responses = [
        ChatResponse(
            content=[
                ToolCallBlock(
                    id="large",
                    name="inspect_details",
                    input='{"which":"large"}',
                ),
                ToolCallBlock(
                    id="latest",
                    name="inspect_details",
                    input='{"which":"latest"}',
                ),
            ],
            is_last=True,
        )
    ]
    agent = qwen_agent(
        monkeypatch, model, scroll, tools=[FunctionTool(inspect_details)]
    )
    _ = [evt async for evt in agent._reply(inputs=message("inspect"))]
    assert len(model.requests) == 2
    assert sorted(executed) == ["large", "latest"]
    assert all(
        [
            await model.count_tokens(*request) <= 4000
            for request in model.requests
        ]
    )
    assert model.parameters.max_tokens == 6000


@pytest.mark.asyncio
async def test_sdk_retries_do_not_recompress_an_unchanged_unfit_request(
    monkeypatch, scroll
):
    model = CountingModel(context=10000, output=6000)
    agent = qwen_agent(monkeypatch, model, scroll)
    agent.model_config.max_retries = 2
    compression = AsyncMock(wraps=scroll.compress_to_budget)
    monkeypatch.setattr(scroll, "compress_to_budget", compression)
    with pytest.raises(ModelContextLengthExceededException):
        _ = [evt async for evt in agent._reply(inputs=message("x" * 6000))]
    assert not model.requests
    assert compression.await_count == 1


@pytest.mark.asyncio
async def test_disabled_auto_compression_does_not_change_history(
    monkeypatch, scroll
):
    model = CountingModel(context=10000, output=6000)
    agent = qwen_agent(monkeypatch, model, scroll, enabled=False)
    persist = AsyncMock(wraps=scroll._persist_guarded_async)
    monkeypatch.setattr(scroll, "_persist_guarded_async", persist)
    original = message("x" * 6000)
    with pytest.raises(ModelContextLengthExceededException) as caught:
        _ = [evt async for evt in agent._reply(inputs=original)]
    assert "自动压缩已关闭" in str(caught.value)
    assert original in agent.state.context
    persist.assert_not_called()
    assert not model.requests


@pytest.mark.asyncio
async def test_fallback_rechecks_its_own_limits_using_compacted_request():
    primary = CountingModel(context=1000, output=600)
    secondary = CountingModel(context=700, output=699)
    old, active, system = (
        message("x" * 500, "assistant"),
        message("a"),
        message("system", "system"),
    )
    agent = SimpleNamespace(
        state=SimpleNamespace(
            context=[old, active], reply_id="turn", cur_iter=0
        )
    )

    async def compress(current_agent, **kwargs):
        current_agent.state.context = [active]

    middleware = RequestBudgetMiddleware(
        SimpleNamespace(compress_to_budget=AsyncMock(side_effect=compress))
    )
    original_messages = [system, old, active]
    await middleware.on_model_call(
        agent,
        dict(
            current_model=primary, messages=list(original_messages), tools=[]
        ),
        AsyncMock(),
    )
    with pytest.raises(ModelContextLengthExceededException) as caught:
        await middleware.on_model_call(
            agent,
            dict(
                current_model=secondary,
                messages=list(original_messages),
                tools=[],
            ),
            AsyncMock(),
        )
    assert caught.value.details["input_tokens"] == 7
    assert caught.value.details["context_tokens"] == 700
    assert caught.value.details["max_output_tokens"] == 699
