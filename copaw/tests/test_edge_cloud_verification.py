"""VerifyAgent alone accepts lower-tier work; failures retain cloud handoff."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import FunctionTool, ToolChunk, Toolkit

from test_edge_cloud import (
    Model, call, clean_capture, config, context, existing_skill, prepare, route, text,
)
from test_final_files import ctx
from qwenpaw.agents.tools.file_io import edit_file, read_file, write_file
from qwenpaw.agents.tools.send_final_files import send_final_files_to_user
from qwenpaw.runtime import final_files as delivery
from qwenpaw.runtime.edge_cloud.execution import ExecutionJournal
from qwenpaw.runtime.envelope import Envelope
from qwenpaw.runtime.final_files.prompts import TOOL_NAME
from qwenpaw.tool_calls import ToolCoordinator, ToolCoordinatorMiddleware


@pytest.mark.parametrize("tier", ["device", "edge"])
@pytest.mark.parametrize("trace_enabled", [False, True])
@pytest.mark.parametrize("passed", [False, True])
@pytest.mark.parametrize("failure_kind", ["denied_write", "edit_miss"])
async def test_real_path_denial_then_recovery_is_judged_by_final_evidence(
    ctx, tmp_path, monkeypatch, tier, trace_enabled, passed, failure_kind,
):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    forbidden, target = tmp_path / "hello.txt", scratch / "hello.txt"
    tools = [FunctionTool(fn) for fn in (write_file, edit_file, read_file, send_final_files_to_user)]
    request_ctx = context(tmp_path, tools)
    request_ctx.agent._acting_middlewares = [ToolCoordinatorMiddleware(ToolCoordinator())]
    steps = []
    if tier == "edge":
        skill = existing_skill(tmp_path, "report", "Write hello.txt, read it and submit it.")
        request_ctx.agent.toolkit = Toolkit(tools=tools, skills_or_loaders=[str(skill)])
        steps.append(call("Skill", {"skill": "report"}, "skill"))
    if failure_kind == "edit_miss":
        target.write_text("old", encoding="utf-8")
        steps.extend([
            call("edit_file", {"file_path": str(target), "old_text": "missing", "new_text": "hello"}, "error"),
            call("edit_file", {"file_path": str(target), "old_text": "old", "new_text": "hello"}, "edit"),
        ])
    else:
        steps.extend([
            call("write_file", {"file_path": str(forbidden), "content": "hello"}, "denied"),
            call("write_file", {"file_path": "hello.txt", "content": "hello"}, "write"),
        ])
    steps.extend([
        call("read_file", {"file_path": "hello.txt"}, "read"),
        call(TOOL_NAME, {"file_paths": [str(target)]}, "submit"),
        text("Created hello.txt containing hello."),
    ])
    orchestrator = prepare(
        monkeypatch, request_ctx, Model(tier, steps),
        [route("edge_skill" if tier == "edge" else "device_tools",
               **({"skill": "report"} if tier == "edge" else {})),
         {"protocol_version": "edge_cloud_verifier.v1", "passed": passed,
          "failure_reason": "" if passed else "missing required content"}],
        settings=config(device_max_tool_rounds=4, trace_stream_enabled=trace_enabled),
        request_context={"is_coding_mode": False, "sandbox_mounts": [str(scratch)]},
    )
    envelope = Envelope()
    events = [e async for e in orchestrator.run(request_ctx, envelope)]
    events.extend([e async for e in delivery.finish(request_ctx, envelope)])
    events.extend([e async for e in envelope.finalize()])

    assert not forbidden.exists()
    assert target.read_text(encoding="utf-8-sig") == "hello"
    records = orchestrator._decision.await_args_list[1].args[-1]["tool_results"]
    writes = [r for r in records if r["tool"] == ("edit_file" if failure_kind == "edit_miss" else "write_file")]
    assert [(r["status"], r["file_operation_status"]) for r in writes] == [
        ("error", "unknown") if failure_kind == "edit_miss" else ("denied", "not_executed"),
        ("success", "succeeded"),
    ]
    expected_error = "not found" if failure_kind == "edit_miss" else "restricted to the first"
    assert expected_error in json.dumps(writes[0]["result"])
    assert orchestrator._trace.fallback is not passed
    assert bool(request_ctx.agent.model.requests) is not passed
    assert orchestrator._stage_outcomes["verifier"]["accepted"] is passed
    assert orchestrator._trace.final_agent == (tier if passed else "cloud")
    files = [c for m in events[-1].output for c in m.content if c.type == "file"]
    assert [c.filename for c in files] == (["hello.txt"] if passed else [])
    assert delivery.current().verified is passed
    if not passed:
        handoff = request_ctx.agent.model.requests[0][0][-1].get_text_content()
        assert "missing required content" in handoff
        assert orchestrator._trace.fallback_reason == "VALIDATION_FAIL"


@pytest.mark.parametrize("state", [ToolResultState.ERROR, ToolResultState.DENIED])
@pytest.mark.parametrize("tier", ["device", "edge"])
@pytest.mark.parametrize("passed", [False, True])
async def test_unknown_write_evidence_does_not_override_verifier_verdict(
    tmp_path, monkeypatch, state, tier, passed,
):
    calls = 0
    target = tmp_path / "result.txt"

    async def write_file(file_path: str, content: str):
        nonlocal calls
        calls += 1
        Path(file_path).write_text(content)
        if calls == 1:
            (tmp_path / "unexpected.txt").write_text("partial side effect")
        return ToolChunk(
            state=state if calls == 1 else ToolResultState.SUCCESS,
            content=[TextBlock(text="not executed" if calls == 1 else "written")],
        )

    arguments = {"file_path": str(target), "content": "correct"}
    tools = [FunctionTool(write_file)]
    request_ctx = context(tmp_path, tools)
    steps = []
    if tier == "edge":
        skill = existing_skill(tmp_path, "report")
        request_ctx.agent.toolkit = Toolkit(tools=tools, skills_or_loaders=[str(skill)])
        steps.append(call("Skill", {"skill": "report"}, "skill"))
    steps.extend([call("write_file", arguments, "first"),
                  call("write_file", arguments, "retry"), text("finished")])
    orchestrator = prepare(
        monkeypatch, request_ctx,
        Model(tier, steps),
        [route("edge_skill" if tier == "edge" else "device_tools",
               **({"skill": "report"} if tier == "edge" else {})),
         {"protocol_version": "edge_cloud_verifier.v1", "passed": passed,
          "confidence": 0, "missing_requirements": ["model-supplied note"]}],
    )
    envelope = Envelope()
    _ = [e async for e in orchestrator.run(request_ctx, envelope)]
    assert calls == 2 and target.read_text() == "correct"
    operation = next(iter(orchestrator._journal.operations.values()))
    assert operation["status"] == "unknown" and operation["previous_outcome_unknown"]
    assert bool(request_ctx.agent.model.requests) is not passed
    assert orchestrator._trace.fallback is not passed
    assert orchestrator._stage_outcomes["verifier"]["accepted"] is passed
    payload = orchestrator._decision.await_args_list[1].args[-1]
    writes = [r for r in payload["tool_results"] if r["tool"] == "write_file"]
    assert [r["status"] for r in writes] == [state.value, "success"]
    assert writes[0]["file_operation_status"] == "unknown"


@pytest.mark.parametrize("tier", ["device", "edge"])
@pytest.mark.parametrize("trace_enabled", [False, True])
@pytest.mark.parametrize("error", [TimeoutError("timeout"), {"passed": "invalid"}])
async def test_invalid_or_unavailable_verification_retains_cloud_handoff(
    tmp_path, monkeypatch, tier, trace_enabled, error,
):
    async def read_file(file_path: str):
        return ToolChunk(content=[TextBlock(text="current evidence")])

    tools = [FunctionTool(read_file)]
    request_ctx = context(tmp_path, tools)
    steps = []
    if tier == "edge":
        skill = existing_skill(tmp_path, "report")
        request_ctx.agent.toolkit = Toolkit(tools=tools, skills_or_loaders=[str(skill)])
        steps.append(call("Skill", {"skill": "report"}, "skill"))
    steps.extend([call("read_file", {"file_path": "input.txt"}), text("claimed success")])
    orchestrator = prepare(
        monkeypatch, request_ctx, Model(tier, steps),
        [route("edge_skill" if tier == "edge" else "device_tools",
               **({"skill": "report"} if tier == "edge" else {})), error],
        settings=config(trace_stream_enabled=trace_enabled),
    )
    envelope = Envelope()
    events = [e.model_copy(deep=True) async for e in orchestrator.run(request_ctx, envelope)]
    events.extend([e.model_copy(deep=True) async for e in envelope.finalize()])
    assert request_ctx.agent.model.requests
    assert orchestrator._trace.fallback and orchestrator._trace.final_agent == "cloud"
    assert orchestrator._trace.fallback_reason == "VALIDATION_UNCERTAIN"
    assert orchestrator._decision.await_count == 2
    assert orchestrator._stage_outcomes["verifier"]["status"] == "error"
    handoff = request_ctx.agent.model.requests[0][0][-1].get_text_content()
    assert "current evidence" in handoff and "VALIDATION_UNCERTAIN" in handoff
    if trace_enabled:
        final = next(e for e in reversed(events) if e.object == "message" and e.name == "VerifyAgent")
        assert "CloudAgent" in final.content[0].text
    sequences = [e.sequence_number for e in events]
    assert sequences == sorted(set(sequences))


async def test_terminal_denial_is_recorded_before_stream_is_closed(tmp_path):
    from qwenpaw.config.context import current_request_context

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    token = current_request_context.set({"is_coding_mode": False, "sandbox_mounts": [str(allowed)]})
    journal = ExecutionJournal()
    try:
        stream = await journal.wrap(FunctionTool(write_file), lower=True)(
            file_path=str(tmp_path / "blocked.txt"), content="hello",
        )
        result = await anext(stream)
        assert result.state == ToolResultState.DENIED
        assert journal.records[0]["file_operation_status"] == "not_executed"
        assert next(iter(journal.operations.values()))["status"] == "not_executed"
        await stream.aclose()
        assert not journal.stop_reason and not journal._lock.locked()
    finally:
        current_request_context.reset(token)


@pytest.mark.parametrize("trace_enabled", [False, True])
@pytest.mark.parametrize("passed", [False, True])
async def test_verification_summary_failure_cannot_change_verifier_verdict(
    tmp_path, monkeypatch, trace_enabled, passed,
):
    request_ctx = context(tmp_path)
    orchestrator = prepare(
        monkeypatch, request_ctx, Model("device", [text("candidate answer")]),
        [route("device_tools"),
         {"protocol_version": "edge_cloud_verifier.v1", "passed": passed}],
        settings=config(trace_stream_enabled=trace_enabled),
    )
    formatter = Mock(side_effect=RuntimeError("summary unavailable"))
    monkeypatch.setattr("qwenpaw.runtime.edge_cloud.orchestrator.verifier_summary", formatter)
    envelope = Envelope()
    events = [e.model_copy(deep=True) async for e in orchestrator.run(request_ctx, envelope)]
    events.extend([e.model_copy(deep=True) async for e in envelope.finalize()])
    assert orchestrator._stage_outcomes["verifier"]["accepted"] is passed
    assert orchestrator._trace.fallback is not passed
    assert bool(request_ctx.agent.model.requests) is not passed
    assert formatter.call_count == int(trace_enabled)
