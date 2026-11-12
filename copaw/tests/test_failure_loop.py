"""Model-review configuration, evidence collection and failure isolation."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.agent import ReActConfig
from agentscope.event import ToolResultEndEvent
from agentscope.message import Msg
from agentscope.tool import Toolkit

from qwenpaw.loop.failure_config import FailureLoopConfig
from qwenpaw.loop.gates import StopAction, StopHandlerResult
from qwenpaw.loop.gates.failure_loop import (
    FailureLoopGate,
    build_failure_loop_gate,
)


@pytest.fixture
def gate(tmp_path):
    gate = FailureLoopGate(
        FailureLoopConfig(enabled=True),
        history_path=tmp_path / "review.db",
    )
    gate.begin_turn("turn")
    yield gate
    gate.close()


def record(gate, call_id="call", *, reply_id="turn"):
    gate.remember_call(reply_id, call_id, "read_file", '{"path":"a.txt"}')
    gate.append_text(reply_id, call_id, "actual result")
    gate.complete_call(reply_id, call_id, "success")


@pytest.mark.parametrize("invalid", [0, -1, True, "512"])
def test_hint_budget_rejects_invalid_configuration(invalid):
    with pytest.raises(ValueError):
        FailureLoopConfig(max_hint_tokens=invalid)


@pytest.mark.parametrize("invalid", [0, -1, True, "5"])
def test_interval_rejects_invalid_configuration(invalid):
    with pytest.raises(ValueError):
        FailureLoopConfig(check_interval=invalid)


@pytest.mark.parametrize(
    "platform,coding,enabled,expected",
    [
        ("win32", False, True, True),
        ("win32", True, True, False),
        ("linux", False, True, False),
        ("darwin", False, True, False),
        ("win32", False, False, False),
    ],
)
def test_factory_scope(
    monkeypatch, tmp_path, platform, coding, enabled, expected
):
    import qwenpaw.loop.gates.failure_loop as module

    monkeypatch.setattr(module.sys, "platform", platform)
    gate = build_failure_loop_gate(
        workspace_dir=tmp_path,
        request_context={"is_coding_mode": coding},
        config=FailureLoopConfig(enabled=enabled),
    )
    assert (gate is not None) is expected
    if gate is not None:
        assert gate.history_path == tmp_path / "failure_loop" / "history.db"
        gate.close()
    assert not (tmp_path / "failure_loop").exists()  # opened lazily


def test_reply_boundary_dedup_and_late_results(gate):
    record(gate)
    record(gate)
    assert gate._state().inspector.count == 1
    gate.begin_turn("new")
    record(gate, "late", reply_id="turn")
    assert gate._state().inspector.count == 0
    gate.reset()
    assert gate._state() is None


def test_session_isolation(gate, monkeypatch):
    import qwenpaw.loop.gates.loop_gate as module

    record(gate)
    previous = gate._state()
    monkeypatch.setattr(module, "_session_id", lambda: "another")
    gate.begin_turn("turn")
    assert gate._state() is not previous
    assert gate._state().inspector.count == 0


def test_full_stream_arguments_and_metadata_are_kept(gate):
    arguments = '{"path":"' + "p" * 70000 + '"}'
    output = "HEAD" + "x" * 100000 + "TAIL"
    gate.remember_call("turn", "large", "read_file", arguments, iteration=7)
    gate.append_text("turn", "large", output[:50000])
    gate.append_text("turn", "large", output[50000:])
    gate.complete_call("turn", "large", "success", {"offloaded": True})
    record = gate._state().inspector._latest[1]
    assert record["arguments"] == json.loads(arguments)
    assert record["text"] == output
    assert record["iteration"] == 7 and record["offloaded"] is True


def test_disabled_gate_never_collects(tmp_path):
    gate = FailureLoopGate(
        FailureLoopConfig(enabled=False), history_path=tmp_path / "unused.db"
    )
    gate.begin_turn("turn")
    record(gate)
    assert gate._state() is None
    assert not (tmp_path / "unused.db").exists()


def test_observer_error_suspends_review_without_raising(monkeypatch, gate):
    agent = make_agent(monkeypatch, gate)
    gate.remember_call("turn", "call", "read_file", "{}")

    def fail(*args, **kwargs):
        raise OSError("archive unavailable")

    monkeypatch.setattr(gate._state().inspector, "record", fail)
    event = ToolResultEndEvent(
        reply_id="turn", tool_call_id="call", state="error"
    )
    agent._observe_failure_event(event)
    assert event.state == "error"
    assert gate._state().suspended
    assert gate._turn("turn") is None


@pytest.mark.asyncio
async def test_prepare_exception_cannot_stop_main_model(monkeypatch, gate):
    agent = make_agent(monkeypatch, gate)
    agent.state.reply_id = "turn"
    agent.model_config.max_retries = 0
    agent.model_config.fallback_model = None
    monkeypatch.setattr(
        agent._failure_recovery_middleware,
        "prepare",
        AsyncMock(side_effect=RuntimeError("checker failed")),
    )
    await agent._inject_failure_recovery()
    request = AsyncMock()
    await agent._failure_recovery_middleware.on_model_call(
        agent,
        {"current_model": agent.model, "messages": [], "tools": []},
        request,
    )
    request.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_stop_prevents_review(monkeypatch, gate):
    agent = make_agent(monkeypatch, gate)
    agent.state.reply_id = "turn"
    record(gate)
    review = AsyncMock()
    monkeypatch.setattr(gate._state().inspector, "inspect", review)
    agent._gate_pending_stop = StopHandlerResult(reason="iteration limit")
    call = AsyncMock()
    monkeypatch.setattr(agent, "_call_model", call)
    events = [event async for event in agent._reasoning()]
    call.assert_not_called()
    review.assert_not_called()
    assert any(isinstance(event, Msg) for event in events)


def make_agent(monkeypatch, gate, tools=(), progress=None):
    from qwenpaw.agents.react_agent import QwenPawAgent
    from qwenpaw.agents import model_factory

    monkeypatch.setattr(
        QwenPawAgent, "_register_skills", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        model_factory, "_supports_multimodal_for_current_model", lambda: True
    )
    monkeypatch.setattr(
        QwenPawAgent, "_model_rejects_media", lambda self: False
    )
    agent = QwenPawAgent(
        name="test",
        model=SimpleNamespace(
            model="fake",
            context_size=99999,
            count_tokens=AsyncMock(return_value=10),
        ),
        system_prompt="test",
        toolkit=Toolkit(tools=list(tools)),
        react_config=ReActConfig(max_iters=20),
        middlewares=[],
        agent_config=SimpleNamespace(language="zh"),
        failure_loop_gate=gate,
        progress_reminder=progress,
    )
    monkeypatch.setattr(agent, "compress_context", AsyncMock())
    monkeypatch.setattr(
        agent,
        "_run_stop_handlers",
        AsyncMock(
            return_value=StopHandlerResult(action=StopAction.TERMINATE),
        ),
    )
    return agent


async def request_with_hint(agent, callback=None):
    captured = []

    async def next_handler(**kwargs):
        captured.append(kwargs["messages"])
        if callback is not None:
            await callback(**kwargs)

    await agent._failure_recovery_middleware.on_model_call(
        agent,
        {
            "current_model": agent.model,
            "messages": agent.state.context,
            "tools": [],
        },
        next_handler,
    )
    return captured[0]


def entry_functions():
    # Execute only entry parser/env functions, avoiding unrelated startup IO.
    source = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"parse_entry_cli_args", "apply_devco_bundle_env"}
    ]
    import argparse
    import os
    import sys

    namespace = {
        "argparse": argparse,
        "os": os,
        "sys": sys,
        "_INIT_ONLY_ENV": "TEST_INIT_ONLY",
    }
    exec(
        compile(
            ast.Module(body=selected, type_ignores=[]), str(source), "exec"
        ),
        namespace,
    )
    return namespace


def test_main_removes_failure_loop_settings_and_forwards_app_flags(monkeypatch):
    import os

    monkeypatch.setattr(os, "environ", {})
    entry = entry_functions()
    old_flags = [
        "--failure-loop-enabled", "1",
        "--failure-loop-check-interval", "3",
        "--failure-loop-max-hint-tokens", "256",
    ]
    args = [*old_flags, "--port", "9001", "--reload"]
    assert entry["parse_entry_cli_args"](args) == [
        "--port", "9001", *old_flags, "--reload",
    ]
    assert not any(key.startswith("QWENPAW_FAILURE_LOOP_") for key in os.environ)
