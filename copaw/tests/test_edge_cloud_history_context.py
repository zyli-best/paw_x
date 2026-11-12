"""Collaborative history references live in user input, never system prompts."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock, ToolResultState
from agentscope.tool import FunctionTool, ToolChunk, Toolkit

from test_edge_cloud import Model, call, clean_capture, config, context, existing_skill, prepare, route, text
from qwenpaw.agents.context.scroll.history import HistoryStore
from qwenpaw.agents.context.scroll.manager import ScrollContextManager
from qwenpaw.agents.context.scroll.recall_tool import make_recall_history
from qwenpaw.agents.context.types import LogEntry
from qwenpaw.runtime.edge_cloud.function_call import consume_decision_call
from qwenpaw.runtime.edge_cloud.execution import ExecutionJournal
from qwenpaw.runtime.edge_cloud.handoff import HandoffArtifacts
from qwenpaw.runtime.edge_cloud.history_context import CollaborationHistory, CloudHistoryModel, HistoryRecall
from qwenpaw.runtime.envelope import Envelope


class CountingModel(Model):
    def __init__(self, budget=3000):
        super().__init__("cloud")
        self.context_size = budget
        self.parameters.max_tokens = 200

    async def count_tokens(self, messages, tools=None, **kwargs):
        return len(json.dumps([message.model_dump(mode="json") for message in messages], ensure_ascii=False)) + len(json.dumps(tools or []))


@pytest.fixture
def history(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    source = CollaborationHistory(store, session_id="edge-test", agent_id="default", flow_id="flow")
    original = FunctionTool(make_recall_history(
        history_db_path=str(store.path), session_id="edge-test", agent_id="default",
    ))
    yield source, original
    store.close()


def message(role, value, **kwargs):
    return Msg(name=role, role=role, content=[TextBlock(text=value)], **kwargs)


async def test_verifier_large_input_is_exactly_preserved_and_recalled_by_page(history):
    source, original = history
    model = CountingModel()
    payload = {"execution_task": "check output", "tool_results": [{"result": "a" * 30000 + "TAIL"}]}
    messages = [message("system", "UNCHANGED SYSTEM"), message("user", json.dumps(payload))]
    before = [msg.model_dump(mode="json") for msg in messages]
    fitted = await source.prepare_verifier(model, messages, [])
    assert fitted[0].get_text_content() == "UNCHANGED SYSTEM"
    assert [msg.model_dump(mode="json") for msg in messages] == before
    reference = json.loads(fitted[1].get_text_content())["history_context"]
    assert "recall_history" in reference["notice"] and reference["store"] == "conversation_history"
    seq = reference["seq"]
    assert source.rows("SELECT content FROM conversation_history WHERE seq=?", (seq,))[0]["content"] == messages[1].get_text_content()
    recall = HistoryRecall(original, source, verifier=True)
    result = await recall.call(op="expand", lo=seq, hi=seq, json_path="/tool_results/0/result", offset=30000, max_chars=10)
    page = json.loads(result.content[0].text)
    assert page["content"] == "TAIL" and page["complete"] and page["next_offset"] is None
    assert await model.count_tokens(fitted, []) + model.parameters.max_tokens <= model.context_size
    repeated = await source.prepare_verifier(model, fitted, [])
    assert repeated is fitted
    assert source.rows("SELECT COUNT(*) AS n FROM conversation_history", ())[0]["n"] == 1
    # Snapshots do not become search hits in normal conversation recall.
    if source.store._fts:
        assert source.rows("SELECT COUNT(*) AS n FROM conversation_history_fts WHERE conversation_history_fts MATCH ?", ("TAIL",))[0]["n"] == 0


async def test_fitting_verifier_input_is_not_rewritten_or_given_recall_hint(history):
    source, _original = history
    messages = [message("system", "SYSTEM"), message("user", json.dumps({
        "execution_task": "check file", "final_answer": "done", "tool_results": [],
    }))]
    fitted = await source.prepare_verifier(CountingModel(), messages, [])
    assert fitted is messages
    assert "recall_history" not in fitted[1].get_text_content()
    assert not source.rows("SELECT seq FROM conversation_history WHERE kind='edge_cloud_context'", ())


async def test_verifier_cannot_read_another_request_or_session(history):
    source, original = history
    allowed = source.snapshot("allowed", stage="verifier")
    forbidden = source.store.append(
        session_id="other-session", agent_id="default",
        entry=LogEntry(kind="context_msg", role="user", content="PRIVATE"),
    )
    recall = HistoryRecall(original, source, verifier=True)
    good = await recall.call(op="expand", lo=allowed, hi=allowed)
    bad = await recall.call(op="expand", lo=forbidden, hi=forbidden)
    assert good.state == ToolResultState.SUCCESS
    assert bad.state == ToolResultState.ERROR and "PRIVATE" not in bad.content[0].text
    assert not any(key in recall.input_schema["properties"] for key in ("all_agents", "session_id", "agent_id"))


async def test_verifier_checks_budget_again_after_skill_and_recall_results(history):
    source, original = history
    model = CountingModel()
    seq = source.snapshot("x" * 40000 + "END", stage="verifier")
    recall = HistoryRecall(original, source, verifier=True)
    result = await recall.call(op="expand", lo=seq, hi=seq, max_chars=16000)
    messages = [
        message("system", "SYSTEM"), message("user", "verify"),
        Msg(name="assistant", role="assistant", content=[ToolCallBlock(id="page", name="recall_history", input="{}")]),
        Msg(name="tools", role="assistant", content=[ToolResultBlock(id="page", name="recall_history", output=result.content, state=result.state)]),
    ]
    fitted = await source.prepare_verifier(model, messages, [])
    assert await model.count_tokens(fitted, []) + model.parameters.max_tokens <= model.context_size
    block = fitted[-1].content[0]
    page = json.loads(block.output[0].text)
    assert 0 < page["next_offset"] < 16000
    tail = await recall.call(op="expand", lo=seq, hi=seq, offset=40000, max_chars=10)
    assert json.loads(tail.content[0].text)["content"] == "END"
    # Skill output is also preserved when it cannot fit as one observation.
    messages[-1].content[0] = ToolResultBlock(id="skill", name="Skill", output="SKILL" * 20000, state=ToolResultState.SUCCESS)
    fitted = await source.prepare_verifier(model, messages, [])
    reference = json.loads(fitted[-1].content[0].output[0].text)["history_context"]
    loaded = await recall.call(**reference["recall"])
    assert json.loads(loaded.content[0].text)["content"].startswith("SKILL")


async def test_history_reads_have_separate_budget_and_do_not_change_verdict(history, tmp_path):
    source, original = history
    seq = source.snapshot("evidence", stage="verifier")
    recall = HistoryRecall(original, source, verifier=True)
    verdict = {"protocol_version": "edge_cloud_verifier.v1", "passed": True}
    model = Model("cloud", [
        call("recall_history", {"op": "expand", "lo": seq, "hi": seq}, f"read-{i}")
        for i in range(3)
    ] + [call("submit_verification", verdict)])
    result = await consume_decision_call(
        model, [message("system", "SYSTEM"), message("user", "verify")],
        stage="verifier", read_tools={"recall_history": recall}, state=context(tmp_path).agent.state,
        prepare_request=lambda messages, tools: source.prepare_verifier(model, messages, tools),
    )
    assert result == verdict and len(model.requests) == 4


@pytest.mark.parametrize("tier", ["device", "edge"])
@pytest.mark.parametrize("trace_enabled", [False, True])
async def test_orchestrator_verifier_uses_history_only_in_user_input(
    history, tmp_path, monkeypatch, tier, trace_enabled,
):
    from qwenpaw.runtime.edge_cloud.orchestrator import EdgeCloudOrchestrator
    from qwenpaw.runtime.edge_cloud.prompts import VERIFIER_PROMPT
    from agentscope.tool import ToolChunk

    source, original = history

    class VerifyingModel(CountingModel):
        async def __call__(self, messages, tools=None, **kwargs):
            assert await self.count_tokens(messages, tools) + 200 <= self.context_size
            if not self.requests:
                payload = json.loads(next(m for m in messages if m.role == "user").get_text_content())
                seq = payload["history_context"]["seq"]
                record = 1 if tier == "edge" else 0
                response = call("recall_history", {
                    "op": "expand", "lo": seq, "hi": seq,
                    "json_path": f"/tool_results/{record}/result/content/0/text",
                    "offset": 50000, "max_chars": 20,
                })
            else:
                result = next(block for m in reversed(messages) for block in m.content if isinstance(block, ToolResultBlock))
                assert "TAIL" in result.output[0].text
                response = call("submit_verification", {"protocol_version": "edge_cloud_verifier.v1", "passed": True})
            self.responses.append(response)
            return await super().__call__(messages, tools, **kwargs)

    async def read_file(file_path: str):
        return ToolChunk(content=[TextBlock(text="x" * 50000 + "TAIL")])

    cloud = VerifyingModel(16000)
    tools = [FunctionTool(read_file), original]
    ctx = context(tmp_path, tools, cloud)
    ctx.agent._context_manager = ScrollContextManager(history=source.store, session_id="edge-test", agent_id="default")
    steps = []
    if tier == "edge":
        skill = existing_skill(tmp_path, "report")
        ctx.agent.toolkit = Toolkit(tools=tools, skills_or_loaders=[str(skill)])
        steps.append(call("Skill", {"skill": "report"}, "skill"))
    steps.extend([call("read_file", {"file_path": "large.txt"}), text("done")])
    orchestrator = prepare(monkeypatch, ctx, Model(tier, steps), [], settings=config(trace_stream_enabled=trace_enabled))

    async def decide(ctx, stage, prompt, payload, **kwargs):
        if stage == "router":
            return route("edge_skill" if tier == "edge" else "device_tools", **({"skill": "report"} if tier == "edge" else {}))
        return await EdgeCloudOrchestrator._decision(orchestrator, ctx, stage, prompt, payload, **kwargs)

    orchestrator._decision = decide
    _ = [event async for event in orchestrator.run(ctx, Envelope())]
    assert len(cloud.requests) == 2
    assert all(request[0][0].get_text_content() == VERIFIER_PROMPT for request in cloud.requests)
    assert orchestrator._trace.final_agent == tier and not orchestrator._trace.fallback
    assert ctx.agent._system_prompt == "DEFAULT SYSTEM"
    assert "history_context" not in json.dumps(ctx.agent.state_dict())


async def test_cloud_large_handoff_can_be_recalled_without_changing_other_input(history):
    source, original = history
    model = CountingModel()
    source.store.append(session_id="edge-test", agent_id="default", entry=LogEntry(kind="context_msg", role="user", content="earlier agreement"))
    previous_handoff = message("user", "earlier handoff", metadata={"qwenpaw_message_kind": "edge_cloud_handoff"})
    handoff = message("user", "large lower-tier evidence " * 4000 + "END", metadata={"qwenpaw_message_kind": "edge_cloud_handoff"})
    messages = [
        message("system", "ORIGINAL SYSTEM"), message("user", "earlier agreement"),
        message("assistant", "prior reply"), previous_handoff,
        message("user", "original user request"), handoff,
    ]
    wrapped = CloudHistoryModel(model, source, handoff_ids={handoff.id})
    before = [msg.model_dump(mode="json") for msg in messages]
    await wrapped(messages, tools=[])
    actual = model.requests[0][0]
    assert all(actual[index] is messages[index] for index in range(len(messages) - 1))
    assert [msg.model_dump(mode="json") for msg in messages] == before
    assert "recall_history" in actual[-1].get_text_content()
    rows = source.rows("SELECT seq, content FROM conversation_history WHERE kind='edge_cloud_context'", ())
    assert len(rows) == 1 and rows[0]["content"] == handoff.get_text_content()
    reader = HistoryRecall(original, source)
    tail = await reader.call(op="expand", lo=rows[0]["seq"], hi=rows[0]["seq"], offset=len(rows[0]["content"]) - 3, max_chars=10)
    assert json.loads(tail.content[0].text)["content"] == "END"
    assert await wrapped.count_tokens(messages, tools=[]) + 200 <= model.context_size
    await wrapped(messages)  # Auxiliary headline/compression calls stay unchanged.
    assert model.requests[-1][0] is messages


@pytest.mark.parametrize("large_history", [False, True])
async def test_fitting_handoff_does_not_add_hint_or_change_session_history(history, large_history):
    source, _original = history
    model = CountingModel()
    handoff = message("user", "small lower-tier evidence", metadata={"qwenpaw_message_kind": "edge_cloud_handoff"})
    messages = [
        message("system", "ORIGINAL SYSTEM"),
        message("user", "prior history " * (4000 if large_history else 1)),
        message("user", "original request"), handoff,
    ]
    wrapped = CloudHistoryModel(model, source, handoff_ids={handoff.id})
    before = [msg.model_dump(mode="json") for msg in messages]
    expected = await model.count_tokens(messages, [])
    assert await wrapped.count_tokens(messages, tools=[]) == expected
    await wrapped(messages, tools=[])
    assert model.requests[-1][0] is messages
    assert [msg.model_dump(mode="json") for msg in messages] == before
    assert not source.rows("SELECT seq FROM conversation_history WHERE kind='edge_cloud_context'", ())


async def test_cloud_wrapper_never_replaces_original_user_input_or_old_handoff(history):
    source, _original = history
    model = CountingModel()
    previous_handoff = message("user", "old lower-tier evidence " * 4000, metadata={"qwenpaw_message_kind": "edge_cloud_handoff"})
    messages = [message("system", "SYSTEM"), previous_handoff, message("user", "original user text " * 4000)]
    wrapped = CloudHistoryModel(model, source, handoff_ids={"current-handoff-not-present"})
    await wrapped(messages, tools=[])
    assert model.requests[-1][0] is messages
    assert not source.rows("SELECT seq FROM conversation_history WHERE kind='edge_cloud_context'", ())


@pytest.mark.parametrize("tier", ["device", "edge"])
@pytest.mark.parametrize("large_handoff", [False, True])
async def test_cloud_takeover_adapts_only_this_lower_tier_handoff(
    history, tmp_path, monkeypatch, tier, large_handoff,
):
    source, original = history

    async def read_file(file_path: str):
        return "file content"

    cloud = CountingModel(16000)
    tools = [FunctionTool(read_file), original]
    ctx = context(tmp_path, tools, cloud)
    ctx.agent._context_manager = ScrollContextManager(history=source.store, session_id="edge-test", agent_id="default")
    original_toolkit = ctx.agent.toolkit
    steps = []
    if tier == "edge":
        skill = existing_skill(tmp_path, "report")
        ctx.agent.toolkit = Toolkit(tools=tools, skills_or_loaders=[str(skill)])
        original_toolkit = ctx.agent.toolkit
        steps.append(call("Skill", {"skill": "report"}, "skill"))
    steps.extend([call("read_file", {"file_path": "file.txt"}), text("done")])
    task = "complete task " * (4000 if large_handoff else 1)
    orchestrator = prepare(monkeypatch, ctx, Model(tier, steps), [
        route("edge_skill" if tier == "edge" else "device_tools", execution_task=task,
              **({"skill": "report"} if tier == "edge" else {})),
        {"protocol_version": "edge_cloud_verifier.v1", "passed": False,
         "failure_reason": "missing requirement"},
    ])
    _ = [event async for event in orchestrator.run(ctx, Envelope())]
    actual = cloud.requests[0][0]
    handoff = next(msg for msg in actual if (msg.metadata or {}).get("qwenpaw_message_kind") == "edge_cloud_handoff")
    assert ("history_context" in handoff.get_text_content()) is large_handoff
    expected = await ctx.agent._prepare_model_input()
    assert actual[0].get_text_content() == expected["messages"][0].get_text_content()
    assert ctx.agent._system_prompt == "DEFAULT SYSTEM"
    assert next(msg for msg in actual if msg.id == ctx.input_msgs[0].id).get_text_content() == "complete the file task"
    canonical = next(msg for msg in ctx.agent.state.context if msg.id == handoff.id)
    assert task in canonical.get_text_content() and "history_context" not in canonical.get_text_content()
    rows = source.rows("SELECT content FROM conversation_history WHERE kind='edge_cloud_context'", ())
    assert len(rows) == int(large_handoff)
    if large_handoff:
        assert rows[0]["content"] == canonical.get_text_content()
    assert orchestrator._trace.fallback_reason == "VALIDATION_FAIL"
    assert ctx.agent.model is cloud and ctx.agent.toolkit is original_toolkit


@pytest.mark.parametrize("enabled", [False, True])
async def test_direct_cloud_keeps_original_history_path_with_collaboration_on_or_off(history, tmp_path, monkeypatch, enabled):
    from qwenpaw.runtime.builder import AgentBuilder
    from qwenpaw.runtime.edge_cloud.orchestrator import EdgeCloudOrchestrator
    from qwenpaw.runtime.hooks import HookResult
    from qwenpaw.runtime.runtime import Runtime

    source, original = history
    source.store.append(session_id="edge-test", agent_id="default", entry=LogEntry(kind="context_msg", role="user", content="prior history"))
    cloud = Model("cloud", [text("done")])
    ctx = context(tmp_path, [original], cloud)
    ctx.agent._context_manager = ScrollContextManager(history=source.store, session_id="edge-test", agent_id="default")
    original_manager, original_toolkit = ctx.agent._context_manager, ctx.agent.toolkit
    ctx.agent.close = AsyncMock()
    ctx.context_injections = []
    plugins = SimpleNamespace(
        hook_registry=SimpleNamespace(run=AsyncMock(return_value=HookResult())),
        slash_command_registry=SimpleNamespace(dispatch=AsyncMock(return_value=None)),
        active_mode_names=lambda _: set(),
    )
    ctx.workspace = SimpleNamespace(plugins=plugins)
    runtime = Runtime(workspace=ctx.workspace, app_services=None)
    monkeypatch.setattr(runtime, "_normalize", lambda request: request)
    monkeypatch.setattr(runtime, "_build_context", lambda _: ctx)
    monkeypatch.setattr(AgentBuilder, "build", AsyncMock(return_value=ctx.agent))
    monkeypatch.setattr(AgentBuilder, "_build_request_context", lambda _: {})
    monkeypatch.setattr("qwenpaw.runtime.edge_cloud.config.load_edge_cloud_config", lambda: config(enabled=enabled))
    decision = AsyncMock(return_value=route("complex_or_negative"))
    monkeypatch.setattr(EdgeCloudOrchestrator, "_decision", decision)
    def unexpected_wrapper(*args, **kwargs):
        raise AssertionError("Direct cloud must keep the original model/history path")
    monkeypatch.setattr("qwenpaw.runtime.edge_cloud.history_context.CloudHistoryModel", unexpected_wrapper)
    _ = [event async for event in runtime.run(SimpleNamespace(request_context={}))]
    assert decision.await_count == int(enabled)
    assert cloud.requests[0][0][0].get_text_content() == "DEFAULT SYSTEM"
    latest = next(msg for msg in reversed(cloud.requests[0][0]) if msg.role == "user")
    assert latest.get_text_content() == "complete the file task"
    assert "history_context" not in json.dumps(ctx.agent.state_dict(), ensure_ascii=False)
    assert ctx.agent._context_manager is original_manager and ctx.agent.toolkit is original_toolkit
    assert ctx.agent.model is cloud


@pytest.mark.parametrize("selected_range", [None, (3, 80), (121, 121)])
async def test_lower_file_read_preserves_pretruncation_text_even_when_verifier_input_fits(
    history, tmp_path, monkeypatch, selected_range,
):
    from qwenpaw.agents.tools import file_io

    source, original = history
    monkeypatch.setattr(file_io, "get_current_recent_max_bytes", lambda: 128)
    lines = [f"line-{index}:" + "内容" * 30 for index in range(120)] + ["x" * 2000 + "TAIL"]
    path = tmp_path / "long.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    arguments = {"file_path": str(path)}
    start, end = selected_range or (1, len(lines))
    if selected_range:
        arguments.update(start_line=start, end_line=end)
    expected = await file_io.read_file(**arguments)
    journal = ExecutionJournal()
    journal.history = source
    wrapped = journal.wrap(FunctionTool(file_io.read_file), lower=True)
    chunks = [chunk async for chunk in await wrapped(**arguments)]
    assert chunks[-1].content[0].text == expected.content[0].text
    assert journal.records[0]["status"] == "success"
    reference = journal.records[0]["text_evidence"][0]
    assert reference["available"] and reference["source"]["start_line"] == start
    assert reference["source"]["text_lines"] == end - start + 1
    saved = json.loads(source.rows("SELECT content FROM conversation_history WHERE seq=?", (reference["seq"],))[0]["content"])
    selected = "\n".join(lines[start - 1:end])
    assert saved["text"] == selected
    assert "already_partial" in saved["source"] and not saved["source"]["already_partial"]
    messages = [message("system", "SYSTEM"), message("user", json.dumps({"tool_results": journal.records}, ensure_ascii=False))]
    assert await source.prepare_verifier(CountingModel(16000), messages, []) is messages
    reader = HistoryRecall(original, source, verifier=True)
    tail = await reader.call(**{**reference["recall"], "offset": len(selected) - 10})
    assert json.loads(tail.content[0].text)["content"] == selected[-10:]
    # The durable snapshot is the text from that invocation, not a later reread.
    path.write_text("changed later", encoding="utf-8")
    tail_again = await reader.call(**{**reference["recall"], "offset": len(selected) - 10})
    assert tail_again.content[0].text == tail.content[0].text


@pytest.mark.parametrize("lower,has_history", [(False, True), (True, False), (True, True)])
@pytest.mark.parametrize("full", ["short", "inside\n" * 100])
async def test_text_capture_does_not_escape_lower_tool_invocation(history, lower, has_history, full):
    from qwenpaw.agents.tools.utils import truncate_text_output

    source, _original = history

    async def read_file(file_path: str):
        return ToolChunk(is_last=True, state=ToolResultState.SUCCESS, content=[
            TextBlock(text=truncate_text_output(full, total_lines=101, max_bytes=20)),
        ])

    journal = ExecutionJournal()
    journal.history = source if has_history else None
    wrapped = journal.wrap(FunctionTool(read_file), lower=lower)
    stream = await wrapped(file_path="ignored")
    first = await anext(stream)
    assert first.state == ToolResultState.SUCCESS
    # The observer is reset before yielding to the surrounding middleware.
    truncate_text_output("OUTSIDE\n" * 100, total_lines=101, max_bytes=20)
    _ = [chunk async for chunk in stream]
    rows = source.rows("SELECT content FROM conversation_history WHERE kind='edge_cloud_context'", ())
    assert len(rows) == int(lower and has_history and full != "short")
    assert all("OUTSIDE" not in row["content"] for row in rows)
    if rows:
        assert json.loads(rows[0]["content"])["text"] == full


async def test_concurrent_lower_requests_keep_text_evidence_separate(history):
    from qwenpaw.agents.tools.utils import truncate_text_output

    source, original = history
    other = CollaborationHistory(source.store, session_id="other", agent_id="default", flow_id="other-flow")

    async def read_file(file_path: str):
        await asyncio.sleep(0)
        return ToolChunk(is_last=True, state=ToolResultState.SUCCESS, content=[
            TextBlock(text=truncate_text_output((file_path + "\n") * 100, total_lines=101, max_bytes=20)),
        ])

    async def execute(history, value):
        journal = ExecutionJournal()
        journal.history = history
        _ = [chunk async for chunk in await journal.wrap(FunctionTool(read_file), lower=True)(file_path=value)]
        return journal.records[0]["text_evidence"][0]

    first, second = await asyncio.gather(execute(source, "FIRST"), execute(other, "SECOND"))
    reader = HistoryRecall(original, source, verifier=True)
    allowed = await reader.call(**first["recall"])
    forbidden = await reader.call(**second["recall"])
    assert json.loads(allowed.content[0].text)["content"] == "FIRST\n" * 100
    assert forbidden.state == ToolResultState.ERROR and "SECOND" not in forbidden.content[0].text


async def test_failed_text_snapshot_does_not_change_tool_success(history, monkeypatch):
    from qwenpaw.agents.tools.utils import truncate_text_output

    source, _original = history
    full = "line\n" * 100
    expected = truncate_text_output(full, total_lines=101, max_bytes=20)

    async def read_file(file_path: str):
        return ToolChunk(is_last=True, state=ToolResultState.SUCCESS, content=[
            TextBlock(text=truncate_text_output(full, total_lines=101, max_bytes=20)),
        ])

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("simulated unavailable history")

    monkeypatch.setattr(source, "snapshot", fail)
    journal = ExecutionJournal()
    journal.history = source
    chunks = [chunk async for chunk in await journal.wrap(FunctionTool(read_file), lower=True)(file_path="ignored")]
    assert chunks[-1].state == ToolResultState.SUCCESS and chunks[-1].content[0].text == expected
    assert journal.records[0]["status"] == "success"
    evidence = journal.records[0]["text_evidence"][0]
    assert not evidence["available"] and "seq" not in evidence


async def test_unavailable_handoff_database_keeps_original_artifact_reader(history, monkeypatch):
    from qwenpaw.runtime.edge_cloud.orchestrator import EdgeCloudOrchestrator

    source, _original = history
    journal = ExecutionJournal()
    journal.records.append({
        "call_seq": 1, "tool": "write_file", "status": "success",
        "arguments": {"file_path": "out.txt", "content": "x" * 4000 + "TAIL"},
        "result": {"state": "success", "content": [{"type": "text", "text": "done"}]},
    })
    artifacts = HandoffArtifacts("flow", request="task", reason="VALIDATION_FAIL", journal=journal, conditions=[])

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("simulated unavailable history")

    monkeypatch.setattr(source, "snapshot", fail)
    handoff = EdgeCloudOrchestrator._handoff_message(
        reason="VALIDATION_FAIL", detail="missing", artifacts=artifacts,
        executed_by="edge", execution_task="task", history=source,
    )
    assert "read_handoff_artifact" in handoff.get_text_content()
    loaded = await artifacts("trace://flow/call-1@v1", view="result")
    assert json.loads(loaded.content[0].text)["arguments"]["content"] == "x" * 4000 + "TAIL"
    assert journal.records[0]["status"] == "success"


@pytest.mark.parametrize("tool,arguments,result,pointer,expected", [
    ("write_file", {"file_path": "out.txt", "content": "w" * 4000 + "WRITE-TAIL"}, "written", "/arguments/content", "WRITE-TAIL"),
    ("edit_file", {"file_path": "out.txt", "old_text": "old", "new_text": "e" * 4000 + "EDIT-TAIL"}, "edited", "/arguments/new_text", "EDIT-TAIL"),
    ("read_file", {"file_path": "out.txt"}, "r" * 4000 + "READ-TAIL", "/result/content/0/text", "READ-TAIL"),
])
async def test_abbreviated_handoff_call_has_database_reference_without_overflow(
    history, tool, arguments, result, pointer, expected,
):
    from qwenpaw.runtime.edge_cloud.orchestrator import EdgeCloudOrchestrator

    source, original = history
    journal = ExecutionJournal()
    journal.records.append({
        "call_seq": 1, "tool": tool, "arguments": arguments, "status": "success",
        "result": {"state": "success", "content": [{"type": "text", "text": result}]},
    })
    artifacts = HandoffArtifacts("flow", request="task", reason="VALIDATION_FAIL", journal=journal, conditions=[])
    handoff = EdgeCloudOrchestrator._handoff_message(
        reason="VALIDATION_FAIL", detail="missing requirement", artifacts=artifacts,
        executed_by="device", execution_task="task", history=source,
    )
    reference_line = next(line for line in handoff.get_text_content().splitlines() if "完整工具入参和返回：" in line)
    reference = json.loads(reference_line.split("：", 1)[1])
    model = CountingModel(16000)
    messages = [message("system", "SYSTEM"), handoff]
    wrapped = CloudHistoryModel(model, source, handoff_ids={handoff.id})
    assert await wrapped._input(messages, []) is messages
    assert "history_context" not in handoff.get_text_content()
    value = arguments.get("content") or arguments.get("new_text") or result
    reader = HistoryRecall(original, source)
    loaded = await reader.call(**{**reference["recall"], "json_path": pointer, "offset": len(value) - len(expected)})
    assert json.loads(loaded.content[0].text)["content"] == expected
    assert artifacts.artifacts[next(ref for ref in artifacts.artifacts if ref.startswith("trace://"))] == journal.records[0]


@pytest.mark.parametrize("tier", ["device", "edge"])
@pytest.mark.parametrize("passed", [True, False])
async def test_verifier_and_cloud_can_recall_truncated_lower_read_under_budget(
    history, tmp_path, monkeypatch, tier, passed,
):
    from qwenpaw.agents.tools import file_io
    from qwenpaw.runtime.edge_cloud.orchestrator import EdgeCloudOrchestrator
    from qwenpaw.runtime.edge_cloud.prompts import VERIFIER_PROMPT

    source, original = history
    monkeypatch.setattr(file_io, "get_current_recent_max_bytes", lambda: 128)
    path = tmp_path / "long.txt"
    path.write_text("line\n" * 10000 + "TAIL", encoding="utf-8")

    class EvidenceModel(CountingModel):
        async def __call__(self, messages, tools=None, **kwargs):
            assert await self.count_tokens(messages, tools) + 200 <= self.context_size
            if not self.requests:
                assert messages[0].get_text_content() == VERIFIER_PROMPT
                payload = json.loads(next(msg for msg in messages if msg.role == "user").get_text_content())
                assert "history_context" not in payload
                record = next(item for item in payload["tool_results"] if item["tool"] == "read_file")
                ref = record["text_evidence"][0]
                response = call("recall_history", {**ref["recall"], "offset": 50000})
            elif len(self.requests) == 1:
                assert "TAIL" in next(block for msg in reversed(messages) for block in msg.content if isinstance(block, ToolResultBlock)).output[0].text
                response = call("submit_verification", {"protocol_version": "edge_cloud_verifier.v1", "passed": passed, "failure_reason": "" if passed else "another requirement is missing"})
            elif len(self.requests) == 2:
                handoff = next(msg for msg in messages if (msg.metadata or {}).get("qwenpaw_message_kind") == "edge_cloud_handoff")
                assert "history_context" not in handoff.get_text_content()
                line = next(line for line in handoff.get_text_content().splitlines() if "截断前文本读取入口：" in line)
                ref = json.loads(line.split("：", 1)[1])[0]
                response = call("recall_history", {**ref["recall"], "offset": 50000}, "cloud-recall")
            else:
                assert "TAIL" in next(block for msg in reversed(messages) for block in msg.content if isinstance(block, ToolResultBlock)).output[0].text
                response = text("cloud completed remaining work")
            self.responses.append(response)
            return await super().__call__(messages, tools, **kwargs)

    cloud = EvidenceModel(20000)
    tools = [FunctionTool(file_io.read_file), original]
    ctx = context(tmp_path, tools, cloud)
    ctx.agent._context_manager = ScrollContextManager(history=source.store, session_id="edge-test", agent_id="default")
    steps = []
    if tier == "edge":
        skill = existing_skill(tmp_path, "report")
        ctx.agent.toolkit = Toolkit(tools=tools, skills_or_loaders=[str(skill)])
        steps.append(call("Skill", {"skill": "report"}, "skill"))
    steps.extend([call("read_file", {"file_path": str(path)}), text("lower done")])
    orchestrator = prepare(monkeypatch, ctx, Model(tier, steps), [])

    async def decide(ctx, stage, prompt, payload, **kwargs):
        if stage == "router":
            return route("edge_skill" if tier == "edge" else "device_tools", **({"skill": "report"} if tier == "edge" else {}))
        return await EdgeCloudOrchestrator._decision(orchestrator, ctx, stage, prompt, payload, **kwargs)

    orchestrator._decision = decide
    _ = [event async for event in orchestrator.run(ctx, Envelope())]
    assert len(cloud.requests) == (2 if passed else 4)
    assert orchestrator._trace.final_agent == (tier if passed else "cloud")
    assert orchestrator._trace.fallback is (not passed)
    read_record = next(item for item in orchestrator._journal.records if item["tool"] == "read_file")
    assert "TAIL" not in json.dumps(read_record["result"])
    assert read_record["status"] == "success"
    assert ctx.agent._system_prompt == "DEFAULT SYSTEM" and ctx.agent.model is cloud
