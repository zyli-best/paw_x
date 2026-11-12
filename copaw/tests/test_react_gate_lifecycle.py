"""Real loop gates across replies, sessions, Scroll and execution tiers."""

import asyncio
import json
import sqlite3
from copy import deepcopy
from types import SimpleNamespace

import pytest
from agentscope.agent import ReActConfig
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultState
from agentscope.tool import FunctionTool, ToolChunk, Toolkit

from qwenpaw.app import agent_context
from qwenpaw.config.config import AgentProfileConfig, DoomLoopConfig, ModelSlotConfig
from qwenpaw.loop.gates import StopAction
from qwenpaw.loop.gates.doom_loop import DoomLoopGate
from qwenpaw.loop.react_gates import register_react_gates


@pytest.fixture
def session_scope(monkeypatch):
    current = {"id": "first"}
    monkeypatch.setattr(agent_context, "get_current_session_id", lambda: current["id"])
    return current


def doom_gate():
    config = DoomLoopConfig()
    return DoomLoopGate(window_size=config.window_size,
                        similarity_threshold=config.similarity_threshold,
                        stages=config.stages)


def active_reply(identity="reply"):
    message = Msg(name="agent", role="assistant", content=[], id=identity)
    agent = SimpleNamespace(state=SimpleNamespace(reply_id=identity, context=[message]))
    return agent, message


def add_call(message, identity, value="same"):
    message.content.append(ToolCallBlock(
        id=identity, name="read_file", input=json.dumps({"file_path": value}),
    ))


async def evaluate(gate, agent, iteration):
    return await gate.check({"agent": agent, "iteration": iteration})


async def test_text_and_repeated_checks_do_not_record_old_calls(session_scope):
    gate = doom_gate()
    agent, message = active_reply()
    for index in range(2):
        add_call(message, str(index))
        assert (await evaluate(gate, agent, index)).action == StopAction.BYPASS
    message.content.append(TextBlock(text="done"))
    for index in range(2, 8):
        assert (await evaluate(gate, agent, index)).action == StopAction.BYPASS
    assert len(gate._state().history) == 2
    assert gate._state().consecutive_hits == 0


async def test_real_repetition_still_warns_and_stops(session_scope):
    gate = doom_gate()
    agent, message = active_reply()
    results = []
    for index in range(6):
        add_call(message, str(index))
        results.append((await evaluate(gate, agent, index)).action)
    assert results == (
        [StopAction.BYPASS] * 2
        + [StopAction.INTERRUPT_AND_CONTINUE] * 3
        + [StopAction.TERMINATE]
    )
    assert gate._state().consecutive_hits == 6


async def test_reply_switch_resets_repetition_but_text_does_not_escalate(session_scope):
    gate = doom_gate()
    lower, message = active_reply("lower")
    for index in range(3):
        add_call(message, f"lower-{index}")
        await evaluate(gate, lower, index)
    assert gate._state().consecutive_hits == 3
    message.content.append(TextBlock(text="incomplete"))
    assert (await evaluate(gate, lower, 3)).action == StopAction.BYPASS
    assert gate._state().consecutive_hits == 3
    cloud, message = active_reply("cloud")
    for index in range(3):
        add_call(message, f"cloud-{index}", f"different-{index}")
        assert (await evaluate(gate, cloud, index)).action == StopAction.BYPASS
    assert gate._state().consecutive_hits == 0
    assert len(gate._state().history) == 3


async def test_scroll_copy_and_rebuild_does_not_duplicate_call_ids(session_scope):
    gate = doom_gate()
    agent, message = active_reply()
    add_call(message, "one")
    await evaluate(gate, agent, 0)
    agent.state.context = [
        Msg(name="memory", role="user", content=[TextBlock(text="index")]),
        deepcopy(message),
    ]
    assert (await evaluate(gate, agent, 1)).action == StopAction.BYPASS
    assert len(gate._state().history) == 1
    add_call(agent.state.context[-1], "two")
    await evaluate(gate, agent, 2)
    assert len(gate._state().history) == 2


async def test_manual_records_are_evaluated_once(session_scope):
    gate = doom_gate()
    for _ in range(3):
        gate.record("read_file", "same")
    assert (await gate.check(None)).action == StopAction.INTERRUPT_AND_CONTINUE
    assert (await gate.check(None)).action == StopAction.BYPASS
    assert gate._state().consecutive_hits == 3


def running_config(limit):
    profile = AgentProfileConfig(id="default", name="default")
    profile.running.max_iters = limit
    profile.running.loop.iteration.max_iterations = limit
    return profile.running


def workspace():
    return SimpleNamespace(plugins=SimpleNamespace(stop_handlers=[]))


async def test_iteration_gate_is_rearmed_for_new_sessions_and_after_exhaustion(session_scope):
    ws = workspace()
    register_react_gates(ws, running_config(2))
    session_scope["id"] = "second"
    handler = register_react_gates(ws, running_config(2))
    assert (await handler({"iteration": 0})).reason == ""
    assert "Max iterations (2)" in (await handler({"iteration": 1})).reason
    assert len(ws.plugins.stop_handlers) == 1
    handler = register_react_gates(ws, running_config(3))
    for index in range(2):
        assert (await handler({"iteration": index})).reason == ""
    assert "Max iterations (3)" in (await handler({"iteration": 2})).reason


async def test_iteration_budget_is_shared_across_replies_and_separate_between_agents(session_scope):
    first, second = workspace(), workspace()
    first_handler = register_react_gates(first, running_config(3))
    second_handler = register_react_gates(second, running_config(5))
    agent, message = active_reply("device")
    add_call(message, "lower")
    await first_handler({"agent": agent, "iteration": 0})
    cloud, message = active_reply("cloud")
    add_call(message, "cloud", "different")
    await first_handler({"agent": cloud, "iteration": 0})
    assert "Max iterations (3)" in (await first_handler({"agent": cloud, "iteration": 1})).reason
    assert (await second_handler({"iteration": 0})).reason == ""
    other_gate = next(g for g in second_handler.gates if g.name == "iteration")
    assert other_gate._state().iteration == 1


async def test_iteration_config_changes_apply_only_to_current_session(session_scope):
    ws = workspace()
    handler = register_react_gates(ws, running_config(3))
    await handler({"iteration": 0})
    session_scope["id"] = "second"
    register_react_gates(ws, running_config(7))
    await handler({"iteration": 0})
    gate = next(g for g in handler.gates if g.name == "iteration")
    assert gate._state().max_iterations == 7
    session_scope["id"] = "first"
    assert gate._state().max_iterations == 3 and gate._state().iteration == 1


async def test_exhausted_budget_blocks_a_new_reply_before_reasoning(session_scope):
    from qwenpaw.loop.gates.runner import check_pending_gates

    ws = workspace()
    handler = register_react_gates(ws, running_config(1))
    agent, _message = active_reply("lower")
    agent._get_stop_handlers = lambda: ws.plugins.stop_handlers
    assert "Max iterations (1)" in (await handler({"agent": agent})).reason
    agent.state.reply_id = "cloud"
    assert "Max iterations (1)" in check_pending_gates(agent).reason
    assert "Max iterations (1)" in check_pending_gates(agent).reason
    gate = next(g for g in handler.gates if g.name == "iteration")
    assert gate._state().iteration == 1
    register_react_gates(ws, running_config(3))
    assert check_pending_gates(agent) is None


async def test_a_real_doom_stop_still_charges_its_last_round(session_scope):
    ws = workspace()
    handler = register_react_gates(ws, running_config(10))
    agent, message = active_reply()
    for index in range(6):
        add_call(message, str(index))
        result = await handler({"agent": agent, "iteration": index})
    assert result.action == StopAction.TERMINATE and "Doom loop" in result.reason
    iteration = next(g for g in handler.gates if g.name == "iteration")
    assert iteration._state().iteration == 6


async def test_iteration_can_be_enabled_after_initial_registration(session_scope):
    ws = workspace()
    disabled = running_config(2)
    disabled.loop.iteration.enabled = False
    handler = register_react_gates(ws, disabled)
    assert not any(g.name == "iteration" for g in handler.gates)
    register_react_gates(ws, running_config(2))
    assert (await handler({"iteration": 0})).reason == ""
    assert "Max iterations (2)" in (await handler({"iteration": 1})).reason
    register_react_gates(ws, disabled)
    for index in range(3):
        assert (await handler({"iteration": index})).reason == ""


async def test_repetition_scope_is_separate_between_sessions(session_scope):
    gate = doom_gate()
    first, first_message = active_reply("first-reply")
    second, second_message = active_reply("second-reply")
    for index in range(3):
        session_scope["id"] = "first"
        add_call(first_message, f"first-{index}")
        await evaluate(gate, first, index)
        session_scope["id"] = "second"
        add_call(second_message, f"second-{index}", f"different-{index}")
        assert (await evaluate(gate, second, index)).action == StopAction.BYPASS
    assert gate._state().consecutive_hits == 0
    session_scope["id"] = "first"
    assert gate._state().consecutive_hits == 3


@pytest.mark.parametrize("limit", [3, 4, 5, 6, 20])
@pytest.mark.parametrize("scroll", [False, True], ids=["native", "scroll"])
@pytest.mark.parametrize("tier", ["disabled", "direct_cloud", "device", "edge"])
async def test_real_stop_handlers_preserve_scroll_and_enforce_shared_budget(
    monkeypatch, tmp_path, session_scope, scroll, tier, limit,
):
    from qwenpaw.agents.react_agent import QwenPawAgent
    from qwenpaw.agents.context.budget_middleware import RequestBudgetMiddleware
    from qwenpaw.agents.context.scroll.history import HistoryStore
    from qwenpaw.agents.context.scroll.manager import ScrollContextManager
    from qwenpaw.plugins.registry import PluginRegistry
    from qwenpaw.runtime.envelope import Envelope
    from qwenpaw.runtime.executor import AgentExecutor
    from test_edge_cloud import (
        Model, call, config, context, existing_skill, prepare, route, text,
    )

    session_scope["id"] = "edge-test"
    monkeypatch.setattr(agent_context, "get_current_agent_id", lambda: "default")
    ws = workspace()
    monkeypatch.setattr(PluginRegistry, "_instance", SimpleNamespace(
        get_workspace_manager=lambda: SimpleNamespace(agents={"default": ws}),
    ))
    executed = []

    async def read_file(file_path: str):
        """Read task evidence."""
        executed.append(file_path)
        return ToolChunk(
            state=ToolResultState.SUCCESS,
            content=[TextBlock(text=f"evidence: {file_path}")],
        )

    cloud = Model("cloud", [
        *[call("read_file", {"file_path": f"cloud-{i}"}, f"cloud-{i}") for i in range(3)],
        text("cloud finished"),
    ])
    profile = AgentProfileConfig(
        id="default", name="default",
        active_model=ModelSlotConfig(provider_id="test", model="cloud"),
    )
    profile.running.max_iters = limit
    profile.running.loop.iteration.max_iterations = limit
    history = HistoryStore(tmp_path / "history.db") if scroll else None
    manager = (
        ScrollContextManager(history=history, session_id="edge-test", agent_id="default")
        if scroll else None
    )
    toolkit = Toolkit(tools=[FunctionTool(read_file)])
    if tier == "edge":
        skill = existing_skill(tmp_path, "report", "Read the evidence and report the result.")
        toolkit = Toolkit(tools=[FunctionTool(read_file)], skills_or_loaders=[str(skill)])
    agent = QwenPawAgent(
        name="default", model=cloud, system_prompt="DEFAULT SYSTEM", toolkit=toolkit,
        react_config=ReActConfig(max_iters=limit),
        middlewares=[RequestBudgetMiddleware(manager)] if scroll else [],
        agent_config=profile, workspace_dir=tmp_path, context_manager=manager,
    )
    register_react_gates(ws, profile.running)
    ctx = context(tmp_path)
    ctx.agent, ctx.agent_config, ctx.workspace = agent, profile, ws
    lower = Model(tier, [
        *([call("Skill", {"skill": "report"}, "skill")] if tier == "edge" else []),
        *[call("read_file", {"file_path": "same"}, f"lower-{i}") for i in range(3)],
        text("lower finished"),
    ])
    try:
        envelope = Envelope(session_id=ctx.session_id)
        if tier == "disabled":
            events = [event async for event in AgentExecutor(agent, envelope).run(ctx.input_msgs)]
        else:
            decisions = [route("complex_or_negative")] if tier == "direct_cloud" else [
                route("edge_skill" if tier == "edge" else "device_tools", need_verifier=True,
                      **({"skill": "report"} if tier == "edge" else {})),
                {"protocol_version": "edge_cloud_verifier.v1", "passed": False},
            ]
            orchestrator = prepare(monkeypatch, ctx, lower, decisions,
                                   settings=config(device_max_tool_rounds=4))
            events = [event async for event in orchestrator.run(ctx, envelope)]
            assert orchestrator._trace.final_agent == "cloud"
        lower_checks = min(limit, {"device": 4, "edge": 5}.get(tier, 0))
        cloud_checks = min(4, limit - lower_checks)
        assert len(cloud.requests) == cloud_checks
        expected_cloud_reads = [f"cloud-{i}" for i in range(min(3, cloud_checks))]
        if expected_cloud_reads:
            assert executed[-len(expected_cloud_reads):] == expected_cloud_reads
        assert not any("Doom loop: agent stuck" in str(event) for event in events)
        assert agent._context_manager is manager
        gate = next(g for g in ws._stop_handler.gates if g.name == "iteration")
        assert gate._state().iteration == lower_checks + cloud_checks
        if lower_checks + cloud_checks == limit:
            assert gate.exhausted_result() is not None
            if tier in {"device", "edge"} and cloud_checks < 4:
                assert any(f"Max iterations ({limit})" in str(event) for event in events)
            if cloud_checks < 4:
                assert not any("cloud finished" in str(event) for event in events)
        else:
            assert gate.exhausted_result() is None
        if scroll:
            assert set(expected_cloud_reads) <= manager._persisted_tcids
            if tier in {"device", "edge"}:
                seq = manager._seq_by_tcid["lower-0"]
                with sqlite3.connect(history.path) as connection:
                    archived = connection.execute(
                        "SELECT content FROM conversation_history WHERE seq=?", (seq,),
                    ).fetchone()[0]
                assert "evidence: same" in archived
            assert agent.state_dict()["scroll"] == manager.to_dict()
    finally:
        if history is not None:
            history.close()


async def test_independent_agents_keep_their_own_real_gates_and_scroll(monkeypatch, tmp_path):
    from qwenpaw.agents.react_agent import QwenPawAgent
    from qwenpaw.agents.context.budget_middleware import RequestBudgetMiddleware
    from qwenpaw.agents.context.scroll.history import HistoryStore
    from qwenpaw.agents.context.scroll.manager import ScrollContextManager
    from qwenpaw.plugins.registry import PluginRegistry
    from qwenpaw.runtime.envelope import Envelope
    from qwenpaw.runtime.executor import AgentExecutor
    from test_edge_cloud import Model, call, text

    workspaces = {name: workspace() for name in ("first", "second")}
    monkeypatch.setattr(PluginRegistry, "_instance", SimpleNamespace(
        get_workspace_manager=lambda: SimpleNamespace(agents=workspaces),
    ))
    executions = {}

    async def run(name, limit):
        agent_token = agent_context._current_agent_id.set(name)
        session_token = agent_context._current_session_id.set("shared-session")
        directory = tmp_path / name
        directory.mkdir()
        history = HistoryStore(directory / "history.db")
        try:
            manager = ScrollContextManager(
                history=history, session_id="shared-session", agent_id=name,
            )
            executed = []

            async def read_file(file_path: str):
                """Read this agent's evidence."""
                await asyncio.sleep(0)
                executed.append(file_path)
                return ToolChunk(content=[TextBlock(text=f"{name}: {file_path}")])

            model = Model(name, [
                *[call("read_file", {"file_path": "same"}, f"{name}-{i}")
                  for i in range(3)],
                text(f"{name} finished"),
            ])
            profile = AgentProfileConfig(id=name, name=name)
            profile.running.max_iters = limit
            profile.running.loop.iteration.max_iterations = limit
            register_react_gates(workspaces[name], profile.running)
            agent = QwenPawAgent(
                name=name, model=model, system_prompt="SYSTEM",
                toolkit=Toolkit(tools=[FunctionTool(read_file)]),
                react_config=ReActConfig(max_iters=limit), agent_config=profile,
                workspace_dir=directory, context_manager=manager,
                middlewares=[RequestBudgetMiddleware(manager)],
            )
            events = [event async for event in AgentExecutor(
                agent, Envelope(session_id="shared-session"),
            ).run([Msg(name="user", role="user", content=[TextBlock(text="read evidence")])])]
            assert not any("Doom loop: agent stuck" in str(event) for event in events)
            assert executed == ["same"] * 3
            assert manager._persisted_tcids == {f"{name}-{i}" for i in range(3)}
            assert agent.state_dict()["scroll"] == manager.to_dict()
            gate = next(g for g in workspaces[name]._stop_handler.gates
                        if g.name == "iteration")
            executions[name] = (
                "exhausted" if gate.exhausted_result() is not None
                else gate._state().iteration
            )
        finally:
            history.close()
            agent_context._current_session_id.reset(session_token)
            agent_context._current_agent_id.reset(agent_token)

    await asyncio.gather(run("first", 4), run("second", 8))
    assert executions == {"first": "exhausted", "second": 4}
