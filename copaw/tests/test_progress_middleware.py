"""Real SDK delivery, retries, Scroll, independent switches and session IO."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.event import (
    ConfirmResult,
    ExternalExecutionResultEvent,
    ReplyStartEvent,
    ToolResultEndEvent,
    UserConfirmResultEvent,
)
from agentscope.message import (
    Msg,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
)
from agentscope.model import ChatResponse
from agentscope.tool import FunctionTool, ToolChunk

from qwenpaw.agents.context.budget_middleware import RequestBudgetMiddleware
from qwenpaw.agents.context.failure_middleware import is_failure_hint
from qwenpaw.agents.context.progress_middleware import (
    ProgressReminderMiddleware,
)
from qwenpaw.agents.context.request_hints import (
    is_optional_hint,
    is_progress_hint,
)
from qwenpaw.loop.failure_config import FailureLoopConfig
from qwenpaw.loop.gates import StopHandlerResult
from qwenpaw.loop.gates.failure_loop import FailureLoopGate
from qwenpaw.loop.progress_config import ProgressReminderConfig
from qwenpaw.loop.progress_reminder import ProgressReminderController
from qwenpaw.runtime.prompt_contributors import ProgressExecutionContributor

from test_failure_loop import entry_functions, make_agent
from test_model_loop import ReviewModel, finding
from test_progress_reminder import Clock, clock, controller, record
from test_request_budget import (
    CountingModel,
    failure_message,
    message,
    qwen_agent,
    scroll,
)


def progress_agent(monkeypatch, controller, gate=None, model=None):
    agent = make_agent(monkeypatch, gate, progress=controller)
    agent.model = model or ReviewModel()
    agent.model_config.max_retries = 0
    agent.model_config.fallback_model = None
    agent.state.reply_id = "turn"
    agent.state.context = [message("current user task")]
    return agent


async def deliver(agent):
    await agent._call_model(messages=list(agent.state.context), tools=[])
    return agent.model.requests[-1]["messages"]


@pytest.mark.parametrize(
    "failure_on,progress_on",
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_real_sdk_four_combinations(
    monkeypatch, tmp_path, clock, failure_on, progress_on
):
    gate = (
        FailureLoopGate(
            FailureLoopConfig(enabled=True, check_interval=1),
            history_path=tmp_path / "failure_loop" / "history.db",
        )
        if failure_on
        else None
    )
    controller = (
        ProgressReminderController(
            ProgressReminderConfig(enabled=True), clock=clock
        )
        if progress_on
        else None
    )

    async def inspect_file(path: str) -> ToolChunk:
        """Inspect a confirmed target."""
        clock.now += 700
        return ToolChunk(
            content=[TextBlock(type="text", text="same actual result")],
            state="success",
            is_last=True,
        )

    agent = make_agent(
        monkeypatch, gate, [FunctionTool(inspect_file)], progress=controller
    )
    agent.model = ReviewModel()
    agent.model_config.max_retries = 0
    agent.model_config.fallback_model = None
    agent.model.review_response = finding()
    agent.model.responses = [
        ChatResponse(
            content=[
                ToolCallBlock(
                    id=call_id, name="inspect_file", input='{"path":"a"}'
                )
            ],
            is_last=True,
        )
        for call_id in ("one", "two")
    ]
    try:
        events = [
            event
            async for event in agent._reply(inputs=message("inspect target"))
        ]
        assert (
            sum(isinstance(event, ToolResultEndEvent) for event in events) == 2
        )
        requests = agent.model.requests
        assert sum(request["review"] for request in requests) == (
            2 if failure_on else 0
        )
        main = [request for request in requests if not request["review"]]
        assert len(main) == 3
        assert sum(
            is_progress_hint(msg)
            for request in main
            for msg in request["messages"]
        ) == (2 if progress_on else 0)
        assert (
            any(is_failure_hint(msg) for msg in main[-1]["messages"])
            is failure_on
        )
        assert not any(is_optional_hint(msg) for msg in agent.state.context)
        assert ("progress_reminder" in agent.state_dict()) is progress_on
        if progress_on:
            block = controller.to_dict()
            assert block["statuses"] == {"success": 2}
            assert block["execution_elapsed_seconds"] == 1400
            assert block["checkpoint_seq"] == 2
        assert (
            tmp_path / "failure_loop" / "history.db"
        ).exists() is failure_on
        prompt_ctx = SimpleNamespace(
            extras={
                "language": "zh",
                "progress_reminder_config": ProgressReminderConfig(
                    enabled=progress_on
                ),
            }
        )
        assert (
            bool(ProgressExecutionContributor().contribute_sync(prompt_ctx))
            is progress_on
        )
        if failure_on and progress_on:
            assert [
                type(mw).__name__ for mw in agent._model_call_middlewares
            ] == ["FailureRecoveryMiddleware", "ProgressReminderMiddleware"]
            assert [
                is_failure_hint(msg) for msg in main[-1]["messages"][-2:]
            ] == [True, False]
    finally:
        await agent.close()


async def test_review_time_counts_before_progress_preparation(
    monkeypatch, tmp_path, controller, clock
):
    gate = FailureLoopGate(
        FailureLoopConfig(enabled=True, check_interval=1),
        history_path=tmp_path / "review.db",
    )
    agent = progress_agent(monkeypatch, controller, gate=gate)
    gate.begin_turn("turn")
    for call_id in ("one", "two"):
        gate.remember_call("turn", call_id, "read_file", "{}")
        gate.append_text("turn", call_id, "same output")
        gate.complete_call("turn", call_id, "success")

    async def review():
        clock.now += 2
        return finding()

    agent.model.review_callback = review
    clock.now = 599
    try:
        _ = [event async for event in agent._reasoning()]
        assert [request["review"] for request in agent.model.requests] == [
            True,
            False,
        ]
        assert any(
            is_progress_hint(msg)
            for msg in agent.model.requests[-1]["messages"]
        )
        assert not any(
            is_progress_hint(msg)
            for msg in agent.model.requests[0]["messages"]
        )
    finally:
        await agent.close()


async def test_stop_skips_both_preparations(
    monkeypatch, tmp_path, controller, clock
):
    gate = FailureLoopGate(
        FailureLoopConfig(enabled=True), history_path=tmp_path / "review.db"
    )
    agent = progress_agent(monkeypatch, controller, gate=gate)
    failure = AsyncMock()
    progress = AsyncMock()
    monkeypatch.setattr(agent._failure_recovery_middleware, "prepare", failure)
    monkeypatch.setattr(
        agent._progress_reminder_middleware, "prepare", progress
    )
    agent._gate_pending_stop = StopHandlerResult(reason="existing limit")
    clock.now = 600
    _ = [event async for event in agent._reasoning()]
    failure.assert_not_called()
    progress.assert_not_called()
    assert not agent.model.requests and controller.checkpoint_seq == 0
    gate.close()


async def test_sdk_retries_stable_message_and_no_next_iteration_replay(
    monkeypatch, controller, clock
):
    class RetryModel(ReviewModel):
        async def __call__(self, *args, **kwargs):
            response = await super().__call__(*args, **kwargs)
            if len(self.requests) == 1:
                raise RuntimeError("transient")
            return response

    agent = progress_agent(monkeypatch, controller, model=RetryModel())
    clock.now = 600
    await agent._progress_reminder_middleware.prepare(agent)
    hints = []
    original_fit = agent._progress_reminder_middleware._fit

    async def fit(*args):
        result = await original_fit(*args)
        hints.append(result)
        return result

    monkeypatch.setattr(agent._progress_reminder_middleware, "_fit", fit)
    agent.model_config.max_retries = 1
    await deliver(agent)
    assert len(hints) == 2 and hints[0] is hints[1]
    assert controller.checkpoint_seq == 1
    agent.state.cur_iter += 1
    await agent._progress_reminder_middleware.prepare(agent)
    assert not any(is_progress_hint(msg) for msg in await deliver(agent))


@pytest.mark.parametrize(
    "change", ["reply", "iteration", "evidence", "session"]
)
async def test_stale_hint_is_not_delivered(
    monkeypatch, controller, clock, change
):
    agent = progress_agent(monkeypatch, controller)
    clock.now = 600
    await agent._progress_reminder_middleware.prepare(agent)
    if change == "reply":
        agent.state.reply_id = "next"
    elif change == "iteration":
        agent.state.cur_iter += 1
    elif change == "evidence":
        record(controller)
    else:
        monkeypatch.setattr(
            "qwenpaw.agents.context.progress_middleware.get_current_session_id",
            lambda: "other",
        )
    assert not any(is_progress_hint(msg) for msg in await deliver(agent))
    assert controller.checkpoint_seq == 1


@pytest.mark.parametrize("mode", ["tiny", "count_error", "cancel"])
async def test_sizing_errors_and_cancellation(
    monkeypatch, controller, clock, mode
):
    agent = progress_agent(monkeypatch, controller)
    clock.now = 600
    await agent._progress_reminder_middleware.prepare(agent)
    if mode == "tiny":
        agent.model.count_tokens = AsyncMock(return_value=513)
    else:
        agent.model.count_tokens = AsyncMock(
            side_effect=(
                asyncio.CancelledError()
                if mode == "cancel"
                else ValueError("count failed")
            )
        )
    if mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await deliver(agent)
        assert not agent.model.requests
    else:
        assert not any(is_progress_hint(msg) for msg in await deliver(agent))
    assert controller.checkpoint_seq == 1


@pytest.mark.parametrize("language", ["zh", "en", "ru"])
async def test_bundled_tokenizer_and_complete_short_template(
    monkeypatch, controller, clock, language
):
    from pathlib import Path
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(
        str(
            Path(__file__).resolve().parents[1]
            / "src/qwenpaw/tokenizer/tokenizer.json"
        )
    )
    agent = progress_agent(monkeypatch, controller)
    agent._language = language

    async def count(messages, tools):
        return sum(
            len(tokenizer.encode(msg.get_text_content()).ids) + 280
            for msg in messages
        )

    agent.model.count_tokens = count
    clock.now = 600
    await agent._progress_reminder_middleware.prepare(agent)
    sent = await deliver(agent)
    hint = next(msg for msg in sent if is_progress_hint(msg))
    assert await count([hint], []) <= 512
    assert hint.get_text_content().endswith("</system-reminder>")
    assert (
        "不降低验收标准"
        if language == "zh"
        else "Do not lower acceptance criteria"
    ) in hint.get_text_content()


async def test_budget_omits_progress_before_failure(
    monkeypatch, controller, clock
):
    model = CountingModel(context=1500, output=600)
    agent = progress_agent(monkeypatch, controller, model=model)
    agent.state.context = [message("x" * 800)]
    clock.now = 600
    progress = agent._progress_reminder_middleware
    await progress.prepare(agent)
    # The reminder itself fits, but the full request fits only without it.
    failure = failure_message("f" * 40)
    budget = RequestBudgetMiddleware(None, compression_enabled=False)
    handler = AsyncMock()

    async def check(**kwargs):
        return await budget.on_model_call(agent, kwargs, handler)

    await progress.on_model_call(
        agent,
        {
            "current_model": model,
            "messages": [*agent.state.context, failure],
            "tools": [],
        },
        check,
    )
    assert handler.await_args.kwargs["messages"] == [
        *agent.state.context,
        failure,
    ]
    assert agent.state.context[0].get_text_content() == "x" * 800
    assert controller.last_outcome == "omitted"
    # Both hints can be omitted if retaining failure alone is still too big.
    agent.state.context = [message("x" * 890)]
    await progress.on_model_call(
        agent,
        {
            "current_model": model,
            "messages": [*agent.state.context, failure],
            "tools": [],
        },
        check,
    )
    assert handler.await_args.kwargs["messages"] == agent.state.context


async def test_scroll_retry_fallback_keeps_suffix_and_refits(
    monkeypatch, controller, clock, scroll
):
    class RetryCountingModel(CountingModel):
        async def __call__(self, messages, **kwargs):
            hints.extend(msg for msg in messages if is_progress_hint(msg))
            return await super().__call__(messages, **kwargs)

    hints = []
    primary = RetryCountingModel(context=10000, output=6000)
    primary.responses = [RuntimeError("retry"), RuntimeError("fallback")]
    fallback = CountingModel(context=8000, output=5000)
    agent = qwen_agent(monkeypatch, primary, scroll, progress=controller)
    agent.model_config.max_retries = 1
    agent.model_config.fallback_model = fallback
    agent.state.reply_id = "turn"
    agent.state.context = [
        message("old"),
        message("x" * 5000, "assistant"),
        message("current request"),
    ]
    clock.now = 600
    await agent._progress_reminder_middleware.prepare(agent)
    await agent._call_model(messages=list(agent.state.context), tools=[])
    assert len(primary.requests) == 2 and len(fallback.requests) == 1
    assert hints[0] is hints[1]
    for model in (primary, fallback):
        for sent, tools in model.requests:
            assert sum(is_progress_hint(msg) for msg in sent) == 1
            assert sent[-1].name == "progress_reminder"
            assert (
                await model.count_tokens(sent, tools)
                + model.parameters.max_tokens
                <= model.context_size
            )
            assert any(
                msg.get_text_content() == "current request" for msg in sent
            )
    assert controller.last_outcome == "included_in_request"
    assert controller.checkpoint_seq == 1
    assert not any(is_progress_hint(msg) for msg in agent.state.context)
    agent.state.cur_iter += 1
    await agent._progress_reminder_middleware.prepare(agent)
    await agent._call_model(messages=list(agent.state.context), tools=[])
    assert not any(is_progress_hint(msg) for msg in primary.requests[-1][0])


@pytest.mark.parametrize("kind", ["denied", "external"])
async def test_fresh_agent_resume_preserves_time_and_dedup(
    monkeypatch, controller, clock, kind
):
    previous = progress_agent(monkeypatch, controller)
    call = ToolCallBlock(
        id="resumed",
        name="read_file",
        input='{"path":"a"}',
        state=(
            ToolCallState.ASKING
            if kind == "denied"
            else ToolCallState.SUBMITTED
        ),
    )
    previous.state.context = [
        Msg(id="turn", name="test", role="assistant", content=[call])
    ]
    record(controller, "old")
    controller.remember_call("turn", call.id, call.name, call.input)
    clock.now = 250
    controller.pause()
    snapshot = json.loads(json.dumps(previous.state_dict()))
    restored = ProgressReminderController(controller.config, clock=clock)
    agent = progress_agent(monkeypatch, restored)
    agent.load_state_dict(snapshot)
    clock.now = 50000
    if kind == "denied":
        event = UserConfirmResultEvent(
            reply_id="turn",
            confirm_results=[ConfirmResult(confirmed=False, tool_call=call)],
        )
    else:
        event = ExternalExecutionResultEvent(
            reply_id="turn",
            execution_results=[
                ToolResultBlock(
                    id="resumed",
                    name="read_file",
                    output="actual external result",
                    state="success",
                )
            ],
        )
    _ = [event async for event in agent._reply(inputs=event)]
    assert restored.to_dict()["execution_elapsed_seconds"] == 250
    assert len(restored.to_dict()["completed_call_ids"]) == 2
    assert not any(
        is_progress_hint(msg)
        for request in agent.model.requests
        for msg in request["messages"]
    )


async def test_reply_cancellation_pauses_without_extra_call(
    monkeypatch, controller, clock
):
    class CancelModel(ReviewModel):
        async def __call__(self, *args, **kwargs):
            clock.now += 700
            raise asyncio.CancelledError()

    agent = progress_agent(monkeypatch, controller, model=CancelModel())
    agent.react_config.interruption_raise_cancelled_error = True
    with pytest.raises(asyncio.CancelledError):
        _ = [
            event
            async for event in agent._reply(inputs=message("new user task"))
        ]
    assert controller.current_elapsed() == 700
    clock.now += 10000
    assert controller.current_elapsed() == 700
    assert controller.checkpoint_seq == 0


def test_main_has_no_progress_reminder_cli_or_environment_settings(monkeypatch):
    import os

    monkeypatch.setattr(os, "environ", {})
    entry = entry_functions()
    entry["apply_devco_bundle_env"]()
    old_flags = [
        "--progress-reminder-enabled", "0",
        "--progress-reminder-interval-seconds", "30",
    ]
    assert entry["parse_entry_cli_args"](
        [*old_flags, "--react-timing-enabled", "0"]
    ) == old_flags
    assert os.environ["QWENPAW_REACT_TIMING_ENABLED"] == "0"
    assert not any(key.startswith("QWENPAW_PROGRESS_REMINDER_") for key in os.environ)


@pytest.mark.parametrize(
    "command,preserved",
    [
        ("compact", True),
        ("history", True),
        ("clear", False),
        ("new", False),
        ("load_history", False),
    ],
)
async def test_conversation_commands_preserve_only_same_task_checkpoint(
    monkeypatch, controller, command, preserved
):
    import qwenpaw.runtime.builtin_commands as commands
    from agentscope.state import AgentState

    state = AgentState()
    state.context = [message("existing")]
    block = controller.to_dict()
    payload = {
        "scroll": {"checkpoint": 3},
        "session_skills": ["skill"],
        "progress_reminder": block,
    }
    monkeypatch.setattr(
        commands, "_load_agent_state", AsyncMock(return_value=(state, payload))
    )

    async def run(self, query):
        return message("command result")

    monkeypatch.setattr(
        "qwenpaw.agents.command_handler.CommandHandler.handle_command", run
    )
    session = SimpleNamespace(save_session_state=AsyncMock())
    ctx = SimpleNamespace(
        workspace=SimpleNamespace(session=session),
        session_id="default",
        agent_id="default",
    )
    await commands._make_conversation_adapter(command).handler(ctx, "")
    saved = session.save_session_state.await_args.kwargs["agent"].data
    assert ("progress_reminder" in saved) is preserved
    assert saved["scroll"] == payload["scroll"]
    assert saved["session_skills"] == ["skill"]


@pytest.mark.parametrize(
    "platform,coding", [("win32", False), ("linux", False), ("win32", True)]
)
async def test_builder_effective_components_and_prompt(
    monkeypatch, tmp_path, caplog, platform, coding
):
    import logging
    from qwenpaw.agents.react_agent import QwenPawAgent
    from qwenpaw.runtime.builder import AgentBuilder, AgentBuildOverride

    monkeypatch.setenv("QWENPAW_FAILURE_LOOP_ENABLED", "1")
    monkeypatch.setenv("QWENPAW_PROGRESS_REMINDER_ENABLED", "0")
    monkeypatch.setenv("QWENPAW_PROGRESS_REMINDER_INTERVAL_SECONDS", "30")
    monkeypatch.setattr(
        "qwenpaw.loop.gates.failure_loop.sys.platform", platform
    )
    config = SimpleNamespace(
        id="default",
        name="test",
        language="zh",
        active_model=SimpleNamespace(provider_id="fake", model="fake"),
        coding_mode=SimpleNamespace(
            enabled=coding, project_dir=str(tmp_path) if coding else None
        ),
        heartbeat=None,
        running=SimpleNamespace(
            light_context_config=SimpleNamespace(strategy="native")
        ),
    )
    monkeypatch.setattr(
        "qwenpaw.config.config.load_agent_config", lambda *a: config
    )
    monkeypatch.setattr(
        "qwenpaw.agents.skill_system.ensure_skills_initialized",
        lambda *a: None,
    )
    monkeypatch.setattr(
        "qwenpaw.agents.skill_system.resolve_effective_skills", lambda *a: []
    )
    monkeypatch.setattr(
        "qwenpaw.utils.terminal_runtime.detect_runtime_environments",
        lambda: {},
    )
    monkeypatch.setattr(
        QwenPawAgent, "_register_skills", lambda *a, **kw: None
    )
    builder = AgentBuilder()
    monkeypatch.setattr(builder, "_build_request_context", lambda ctx: {})
    monkeypatch.setattr(
        builder, "_apply_request_coding_project", lambda cfg, request: cfg
    )
    monkeypatch.setattr(builder, "_init_governor", lambda *a, **kw: None)
    monkeypatch.setattr(
        builder, "_collect_coding_mode_tools", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        builder,
        "_collect_driver_tools_and_prompts",
        AsyncMock(return_value=([], [])),
    )
    monkeypatch.setattr(builder, "_build_offloader", lambda *a: None)
    monkeypatch.setattr(
        builder, "_build_scroll_components", lambda *a, **kw: None
    )
    monkeypatch.setattr(builder, "_build_context_config", lambda *a: None)
    monkeypatch.setattr(builder, "_build_middlewares", lambda *a: [])
    monkeypatch.setattr(builder, "_get_memory_manager", lambda *a: None)
    monkeypatch.setattr(
        builder, "_build_env_context", lambda *a: "environment"
    )
    monkeypatch.setattr(builder, "_get_driver_prompt_hints", lambda *a: [])
    from agentscope.tool import Toolkit

    monkeypatch.setattr(
        builder,
        "build_toolkit",
        AsyncMock(side_effect=lambda *a, **kw: Toolkit()),
    )
    ctx = SimpleNamespace(
        workspace=None,
        workspace_dir=tmp_path,
        session_id="default",
        session_state=None,
        agent_id="default",
        extras={},
    )
    override = AgentBuildOverride(model=ReviewModel(), max_iters=10)
    with caplog.at_level(logging.INFO):
        agent = await builder.build(ctx, override)
    assert agent._failure_loop_gate is None
    assert agent._progress_reminder.config.enabled
    assert agent._progress_reminder.config.interval_seconds == 300
    assert "定时提醒不改变权限" in ctx.extras["system_prompt"]
    assert len(agent._model_call_middlewares) == 1
    assert not list(tmp_path.iterdir())
    assert "progress_interval_seconds=300" in caplog.text
    # Explicit prompt overrides remain exact, including when P is enabled.
    override.system_prompt = "EXPLICIT USER SYSTEM PROMPT"
    custom_agent = await builder.build(ctx, override)
    assert ctx.extras["system_prompt"] == override.system_prompt
    assert custom_agent._progress_reminder_middleware is not None
    await agent.close()
    await custom_agent.close()


@pytest.mark.parametrize("enabled", [False, True])
async def test_agent_snapshots_are_optional_and_invalid_blocks_are_ignored(
    monkeypatch, controller, clock, enabled
):
    agent = progress_agent(monkeypatch, controller)
    clock.now = 650
    await agent._progress_reminder_middleware.prepare(agent)
    snapshot = json.loads(json.dumps(agent.state_dict()))
    restored = (
        ProgressReminderController(controller.config, clock=clock)
        if enabled
        else None
    )
    target = progress_agent(monkeypatch, restored)
    target.load_state_dict(snapshot)
    assert ("progress_reminder" in target.state_dict()) is enabled
    snapshot["progress_reminder"]["execution_elapsed_seconds"] = float("nan")
    target.load_state_dict(snapshot)
    assert target.state.context == agent.state.context
    assert "progress_reminder" not in target.state_dict()
    snapshot.pop("progress_reminder")
    target.load_state_dict(snapshot)
    assert target.state.context == agent.state.context


@pytest.mark.parametrize("broken", ["failure", "progress"])
async def test_both_observers_fail_independently(
    monkeypatch, tmp_path, clock, broken
):
    controller = ProgressReminderController(
        ProgressReminderConfig(enabled=True), clock=clock
    )
    controller.begin_segment("default", "turn")
    gate = FailureLoopGate(
        FailureLoopConfig(enabled=True, check_interval=1),
        history_path=tmp_path / "review.db",
    )
    gate.begin_turn("turn")
    agent = progress_agent(monkeypatch, controller, gate=gate)
    for observer in (controller, gate):
        observer.remember_call("turn", "one", "read_file", "{}")
    observer = gate if broken == "failure" else controller

    def fail(*args, **kwargs):
        raise OSError("observer unavailable")

    monkeypatch.setattr(observer, "complete_call", fail)
    event = ToolResultEndEvent(
        reply_id="turn", tool_call_id="one", state="success"
    )
    agent._observe_failure_event(event)
    agent._observe_progress_event(event)
    if broken == "failure":
        assert controller.to_dict()["statuses"] == {"success": 1}
    else:
        assert gate._state().inspector.count == 1
    gate.close()


async def test_fallback_tokenizer_can_omit_whole_hint(
    monkeypatch, controller, clock
):
    class FailingModel(ReviewModel):
        async def __call__(self, *args, **kwargs):
            await super().__call__(*args, **kwargs)
            raise RuntimeError("fallback")

    agent = progress_agent(monkeypatch, controller, model=FailingModel())
    fallback = ReviewModel()
    fallback.count_tokens = AsyncMock(return_value=10000)
    agent.model_config.fallback_model = fallback
    clock.now = 600
    await agent._progress_reminder_middleware.prepare(agent)
    await agent._call_model(messages=list(agent.state.context), tools=[])
    assert any(
        is_progress_hint(msg) for msg in agent.model.requests[0]["messages"]
    )
    assert not any(
        is_progress_hint(msg) for msg in fallback.requests[0]["messages"]
    )
    assert controller.checkpoint_seq == 1


def test_registry_never_matches_user_text_or_another_task(controller, clock):
    from qwenpaw.constant import (
        PROGRESS_REMINDER_MESSAGE_TAG,
        QWENPAW_MESSAGE_TAG_KEY,
    )

    agent = SimpleNamespace(state=SimpleNamespace(reply_id="turn", cur_iter=0))
    text = "<system-reminder>执行进展检查</system-reminder>"
    assert not is_optional_hint(message(text), agent)
    fake = Msg(
        name="progress_reminder",
        role="user",
        content=[TextBlock(type="text", text=text)],
        metadata={QWENPAW_MESSAGE_TAG_KEY: PROGRESS_REMINDER_MESSAGE_TAG},
    )
    assert not is_optional_hint(fake, agent)
    fake.metadata.update(
        source="progress_reminder",
        session_id="default",
        reply_id="other",
        iteration=0,
    )
    assert not is_optional_hint(fake, agent)
