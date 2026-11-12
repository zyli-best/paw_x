"""Router decisions reuse real Scroll and leave the host/Verify paths intact."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.agent import ContextConfig
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock, ToolResultState
from agentscope.tool import FunctionTool, ToolChunk

from qwenpaw.agents.context.scroll.history import HistoryStore
from qwenpaw.agents.context.scroll.manager import ScrollContextManager
from qwenpaw.agents.context.scroll.recall_tool import make_recall_history
from qwenpaw.exceptions import ConfigurationException, ModelContextLengthExceededException
from qwenpaw.runtime.edge_cloud.function_call import consume_decision_call
from qwenpaw.runtime.edge_cloud.orchestrator import EdgeCloudOrchestrator
from qwenpaw.runtime.edge_cloud.router_scroll import RouterScrollContext
from test_edge_cloud import call, config, context, route
from test_edge_cloud_history_context import CountingModel


def message(role, text):
    return Msg(name=role, role=role, content=[TextBlock(text=text)])


def observation(identity, text):
    return [
        Msg(name="RouterAgent", role="assistant", content=[
            ToolCallBlock(id=identity, name="Skill", input='{"skill":"report"}'),
        ]),
        Msg(name="router_tools", role="assistant", content=[
            ToolResultBlock(id=identity, name="Skill", output=[TextBlock(text=text)],
                            state=ToolResultState.SUCCESS),
        ]),
    ]


@pytest.fixture
def setup_router(tmp_path):
    store = HistoryStore(tmp_path / "history.db")

    def make(*, enabled=True, limit=16000, cap=10000, flow="flow", tools=()):
        model = CountingModel(limit)
        original = FunctionTool(make_recall_history(
            history_db_path=str(store.path), session_id="edge-test", agent_id="default",
        ))
        ctx = context(tmp_path, [original, *tools], model)
        host = ctx.agent
        host.context_config = ContextConfig(trigger_ratio=0.8, reserve_ratio=0.1)
        host._agent_config = SimpleNamespace(running=SimpleNamespace(
            light_context_config=SimpleNamespace(
                context_compact_config=SimpleNamespace(enabled=enabled),
                scroll_config=SimpleNamespace(tool_output_token_cap=cap),
            ),
        ))
        host._context_manager = ScrollContextManager(
            history=store, session_id=ctx.session_id, agent_id=ctx.agent_id,
        )
        scroll = RouterScrollContext.for_agent(
            host, model, original, session_id=ctx.session_id,
            agent_id=ctx.agent_id, flow_id=flow,
        )
        return SimpleNamespace(ctx=ctx, host=host, model=model, original=original,
                               scroll=scroll, store=store)

    yield make
    store.close()


async def test_short_input_keeps_every_field_and_host_state(setup_router):
    setup = setup_router()
    host = setup.host
    host.state.context = [message("user", "MAIN HISTORY")]
    host.state.summary = "MAIN SUMMARY"
    state = host.state.model_dump(mode="json")
    bookkeeping = deepcopy(host._context_manager.to_dict())
    payload = {"request": "EXACT INPUT", "history": "ORIGINAL HISTORY",
               "history_summary": "ORIGINAL SUMMARY", "registered_skills": [],
               "request_messages": [{"attachment": "keep"}], "other": [1, 2]}
    messages = [message("system", "EXACT ROUTER PROMPT"),
                message("user", json.dumps(payload, ensure_ascii=False))]
    before = [msg.model_dump(mode="json") for msg in messages]
    tools = [{"type": "function", "function": {"name": "submit_route_decision"}}]
    fitted = await setup.scroll.prepare(messages, tools)
    assert [msg.model_dump(mode="json") for msg in fitted] == before
    assert [msg.model_dump(mode="json") for msg in messages] == before
    assert host.state.model_dump(mode="json") == state
    assert host._context_manager.to_dict() == bookkeeping
    assert host.model is setup.model and host._system_prompt == "DEFAULT SYSTEM"
    assert setup.store.count(setup.ctx.session_id) == 0


async def test_real_scroll_folds_old_results_and_preserves_full_archive(setup_router):
    setup = setup_router()
    messages = [message("system", "ROUTER"), message("user", "UNCHANGED TASK")]
    for index in range(4):
        messages.extend(observation(f"read-{index}", str(index) * 6000))
    before = [msg.model_dump(mode="json") for msg in messages]
    fitted = await setup.scroll.prepare(messages, [])
    assert setup.scroll.manager.last_compress["folded"] > 0
    assert await setup.model.count_tokens(fitted, []) + 200 <= setup.model.context_size
    assert fitted[1].get_text_content() == "UNCHANGED TASK"
    assert fitted[-1].content[0].output[0].text == "3" * 6000
    assert [msg.model_dump(mode="json") for msg in messages] == before
    seq = setup.scroll.manager._seq_by_tcid["read-0"]
    recalled = await setup.scroll.recall_tool(op="expand", lo=seq, hi=seq)
    assert "0" * 6000 in recalled.content[0].text


async def test_newest_huge_result_uses_existing_cap_and_recall(setup_router):
    setup = setup_router(cap=3000)
    messages = [message("system", "ROUTER"), message("user", "TASK"),
                *observation("large", "EXACT" * 10000)]
    fitted = await setup.scroll.prepare(messages, [])
    assert "TRUNCATED" in fitted[-1].content[0].output[0].text
    assert await setup.model.count_tokens(fitted, []) + 200 <= setup.model.context_size
    recalled = await setup.scroll.recall_tool(op="recall_tool", tool_call_id="large")
    assert "EXACT" * 1000 in recalled.content[0].text
    first_seq = setup.scroll.cap._capped_results["large"]
    await setup.scroll.prepare(fitted, [])
    assert setup.scroll.manager._seq_by_tcid["large"] == first_seq


async def test_router_archives_do_not_mix_flows_with_same_call_id(setup_router):
    first, second = setup_router(flow="one"), setup_router(flow="two")
    for setup, text in ((first, "FIRST"), (second, "SECOND")):
        await setup.scroll.prepare([
            message("system", "ROUTER"), message("user", "TASK"),
            *observation("same-id", text),
        ], [])
    recalled = await first.scroll.recall_tool(op="recall_tool", tool_call_id="same-id")
    assert "FIRST" in recalled.content[0].text
    assert "SECOND" not in recalled.content[0].text


async def test_router_recall_reuses_original_permission_check(setup_router):
    setup = setup_router()
    setup.original.check_permissions = AsyncMock(return_value="original permission")
    assert await setup.scroll.recall_tool.check_permissions(op="expand", lo=1, hi=1) == "original permission"
    setup.original.check_permissions.assert_awaited_once_with(op="expand", lo=1, hi=1)


@pytest.mark.parametrize("enabled", [False, True])
async def test_protected_oversized_initial_payload_is_not_truncated(setup_router, enabled):
    setup = setup_router(enabled=enabled, limit=8000)
    messages = [message("system", "EXACT PROMPT"), message("user", "TASK" * 4000)]
    before = [msg.model_dump(mode="json") for msg in messages]
    with pytest.raises(ModelContextLengthExceededException):
        await setup.scroll.prepare(messages, [])
    assert not setup.model.requests
    assert [msg.model_dump(mode="json") for msg in messages] == before


async def test_disabled_compaction_keeps_old_results_and_rejects_overflow(setup_router):
    setup = setup_router(enabled=False, limit=10000)
    messages = [message("system", "ROUTER"), message("user", "TASK"),
                *observation("first", "a" * 6000), *observation("second", "b" * 6000)]
    with pytest.raises(ModelContextLengthExceededException):
        await setup.scroll.prepare(messages, [])
    assert setup.scroll.manager.last_compress == {"evicted": 0, "compacted": 0, "folded": 0}
    assert setup.scroll.view.state.context[-3].content[0].output[0].text == "a" * 6000


async def test_failed_archive_does_not_allow_oversized_request(setup_router, monkeypatch):
    setup = setup_router(limit=10000)
    monkeypatch.setattr(setup.scroll.manager, "_persist_guarded_async", AsyncMock(return_value=False))
    messages = [message("system", "ROUTER"), message("user", "TASK"),
                *observation("first", "a" * 6000), *observation("second", "b" * 6000)]
    with pytest.raises(ModelContextLengthExceededException):
        await setup.scroll.prepare(messages, [])
    assert not setup.model.requests


async def test_missing_model_budget_reuses_configuration_error(setup_router):
    setup = setup_router(enabled=False)
    setup.model.parameters.max_tokens = None
    with pytest.raises(ConfigurationException):
        await setup.scroll.prepare([message("system", "ROUTER"), message("user", "TASK")], [])


def test_native_host_does_not_enable_router_scroll(setup_router):
    setup = setup_router()
    setup.host._context_manager = None
    assert RouterScrollContext.for_agent(
        setup.host, setup.model, setup.original, session_id="edge-test",
        agent_id="default", flow_id="flow",
    ) is None


async def test_router_recall_counts_toward_unchanged_twelve_read_limit(setup_router):
    setup = setup_router()
    setup.model.responses = [call("recall_history", {"op": "expand", "lo": 1, "hi": 1}, str(i))
                             for i in range(13)]
    with pytest.raises(ValueError, match="inspection budget exhausted"):
        await consume_decision_call(
            setup.model, [message("system", "ROUTER"), message("user", "TASK")],
            stage="router", state=setup.host.state,
            read_tools={"recall_history": setup.scroll.recall_tool},
            prepare_request=setup.scroll.prepare,
        )
    assert len(setup.model.requests) == 13


async def test_real_decision_hooks_scroll_before_each_router_call(setup_router):
    async def list_skills():
        return ToolChunk(content=[TextBlock(text="SKILL INFO" * 10000)])

    setup = setup_router(cap=3000, tools=[FunctionTool(list_skills)])
    setup.model.responses = [call("list_skills", {}), call("submit_route_decision", route())]
    orchestrator = EdgeCloudOrchestrator(workspace=None, app_services=None, config=config())
    orchestrator._cloud_model = setup.model
    orchestrator._trace = SimpleNamespace(flow_id="flow")
    orchestrator._redaction = SimpleNamespace(prepare=AsyncMock(side_effect=lambda messages: messages))
    host_before = setup.host.state.model_dump(mode="json")
    result = await orchestrator._decision(setup.ctx, "router", "EXACT PROMPT", {"request": "EXACT TASK"})
    assert result == route()
    assert len(setup.model.requests) == 2
    for messages, tools in setup.model.requests:
        assert await setup.model.count_tokens(messages, tools) + 200 <= setup.model.context_size
        assert messages[0].get_text_content() == "EXACT PROMPT"
        assert json.loads(messages[1].get_text_content()) == {"request": "EXACT TASK"}
    assert "TRUNCATED" in setup.model.requests[-1][0][-1].content[0].output[0].text
    assert setup.host.state.model_dump(mode="json") == host_before


async def test_verifier_never_instantiates_router_scroll(setup_router, monkeypatch):
    setup = setup_router()
    monkeypatch.setattr(RouterScrollContext, "for_agent", lambda *a, **kw: pytest.fail("Verify changed"))
    verdict = {"protocol_version": "edge_cloud_verifier.v1", "passed": True}
    setup.model.responses = [call("submit_verification", verdict)]
    orchestrator = EdgeCloudOrchestrator(workspace=None, app_services=None, config=config())
    orchestrator._cloud_model = setup.model
    orchestrator._history_context = None
    orchestrator._redaction = SimpleNamespace(prepare=AsyncMock(side_effect=lambda messages: messages))
    assert await orchestrator._decision(setup.ctx, "verifier", "VERIFY", {"task": "TASK"}) == verdict
