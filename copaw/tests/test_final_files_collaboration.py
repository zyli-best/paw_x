"""Real runtime/tool flows with scripted models and an in-process API transport."""

import asyncio
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg, TextBlock, ToolResultState
from agentscope.tool import FunctionTool, Toolkit

from test_edge_cloud import Model, call, clean_capture, config, context, existing_skill, route, text
from test_final_files import body, ctx, file, finish
from qwenpaw.agents.tools import agent_management as management
from qwenpaw.agents.tools.file_io import write_file
from qwenpaw.agents.tools.send_final_files import send_final_files_to_user
from qwenpaw.app import agent_context
from qwenpaw.config.context import (
    current_request_context, current_session_id, current_workspace_dir,
)
from qwenpaw.runtime import final_files as delivery
from qwenpaw.runtime.builder import AgentBuilder
from qwenpaw.runtime.edge_cloud.orchestrator import EdgeCloudOrchestrator
from qwenpaw.runtime.final_files.prompts import TOOL_NAME
from qwenpaw.runtime.hooks import HookResult
from qwenpaw.runtime.phases import Phase
from qwenpaw.runtime.runtime import Runtime


def tool_results(messages, name):
    values = []
    for message in messages:
        for block in message.get_content_blocks("tool_result"):
            if block.name != name:
                continue
            outputs = [block.output] if isinstance(block.output, str) else [
                item.text for item in block.output if hasattr(item, "text")
            ]
            for output in outputs:
                try:
                    values.append(json.loads(output))
                except (ValueError, TypeError):
                    pass
    return values


class FeedbackModel(Model):
    def __init__(self, name, responses, *, read_handoff=False):
        super().__init__(name, responses)
        self.read_handoff = read_handoff
        self.seen_submissions = []
        self.handoff_feedback = []
        self.failed_submission_state = None

    async def __call__(self, messages, **kwargs):
        submissions = tool_results(messages, TOOL_NAME)
        if self.failed_submission_state is None and any(
            value.get("status") == "rejected" for value in submissions
        ):
            self.failed_submission_state = delivery.current().manifest()
            assert not self.failed_submission_state["submitted"]
            assert self.failed_submission_state["files"] == []
        self.seen_submissions.extend(submissions)
        if self.read_handoff:
            if not self.requests:
                match = re.search(
                    r"handoff://[^\s/]+/manifest@v1",
                    messages[-1].get_text_content(),
                )
                assert match, "Cloud must receive an accessible handoff manifest"
                self.responses.insert(0, call("read_handoff_artifact", {
                    "ref": match.group(), "view": "result",
                }, "read-manifest"))
            elif len(self.requests) == 1:
                manifest = tool_results(messages, "read_handoff_artifact")[-1]
                failed = next(c for c in manifest["calls"] if c["tool"] == TOOL_NAME)
                assert failed["status"] == "error"
                self.responses.insert(0, call("read_handoff_artifact", {
                    "ref": failed["result_ref"], "view": "result",
                }, "read-failure"))
            else:
                for value in tool_results(messages, "read_handoff_artifact"):
                    if value.get("tool") == TOOL_NAME:
                        self.handoff_feedback.append(
                            json.loads(value["result"]["content"][0]["text"])
                        )
        return await super().__call__(messages, **kwargs)


@pytest.mark.parametrize("background", [False, True], ids=["foreground", "background"])
@pytest.mark.parametrize("mode", [
    "disabled", "direct_cloud", "device", "edge", "device_fallback", "edge_fallback",
])
async def test_runtime_batch_repair_returns_through_real_agent_tools(
    ctx, tmp_path, monkeypatch, background, mode
):
    """Exercise real enhanced submission through Runtime and agent API tools."""
    submit_tool = send_final_files_to_user
    child_root = tmp_path / "child-output"
    child_root.mkdir()
    existing = file(child_root, "existing.pdf", "verified existing document")
    missing = child_root / "missing.xlsx"
    selected = [existing, missing.name]
    fallback = mode.endswith("fallback")
    local = mode.startswith(("device", "edge"))
    tier = "edge" if mode.startswith("edge") else "device"
    repair = [
        call("write_file", {"file_path": missing.name, "content": "repaired spreadsheet"}, "repair"),
        call(TOOL_NAME, {"file_paths": selected}, "resubmit-all"),
        text("The complete file list is submitted."),
    ]
    failed_call = call(TOOL_NAME, {"file_paths": selected}, "missing-file")
    cloud = FeedbackModel(
        "cloud", repair if fallback else [failed_call, *repair], read_handoff=fallback,
    )
    lower = FeedbackModel(
        tier, [
            *([call("Skill", {"skill": "report"}, "skill")] if tier == "edge" else []),
            failed_call,
            *([text("The file submission needs repair.")] if fallback else repair),
        ],
    )
    tools = [FunctionTool(write_file), FunctionTool(submit_tool)]
    child = context(child_root, tools, cloud)
    if tier == "edge" and local:
        skill = existing_skill(child_root, "report", "Create both files and submit the complete list.")
        child.agent.toolkit = Toolkit(tools=tools, skills_or_loaders=[str(skill)])
    child.context_injections = []
    child.agent.close = AsyncMock()
    observation = {}

    async def hook(phase, hook_ctx):
        if phase == Phase.POST_RESPONSE:
            state = delivery.current()
            observation.update(
                stage=state.stage, verified=state.verified, recipient=state.recipient,
                requires_verification=state.requires_verification,
                manifest=state.manifest(),
            )
        return HookResult()

    plugins = SimpleNamespace(
        hook_registry=SimpleNamespace(run=hook),
        slash_command_registry=SimpleNamespace(dispatch=AsyncMock(return_value=None)),
        active_mode_names=lambda _: set(),
    )
    child.workspace = SimpleNamespace(plugins=plugins)
    runtime = Runtime(workspace=child.workspace, app_services=None)

    def build_context(request):
        child.request = request
        child.session_id = request.session_id
        child.input_msgs = [Msg(
            name="user", role="user",
            content=[TextBlock(text=request.input[0].content[0].text)],
        )]
        return child

    monkeypatch.setattr(runtime, "_build_context", build_context)
    monkeypatch.setattr(AgentBuilder, "build", AsyncMock(return_value=child.agent))
    monkeypatch.setattr(AgentBuilder, "_build_request_context", staticmethod(
        lambda request_ctx: request_ctx.request.request_context
    ))
    monkeypatch.setattr(
        "qwenpaw.runtime.edge_cloud.config.load_edge_cloud_config",
        lambda: config(enabled=mode != "disabled", device_max_tool_rounds=4,
                       trace_stream_enabled=True),
    )
    decisions = [route(
        "edge_skill" if tier == "edge" else "device_tools",
        need_verifier=True, **({"skill": "report"} if tier == "edge" else {}),
    ), {"protocol_version": "edge_cloud_verifier.v1", "passed": not fallback,
        "failure_reason": "missing required file" if fallback else ""}]
    if not local:
        decisions = [route("complex_or_negative")]
    decision = AsyncMock(side_effect=decisions)
    monkeypatch.setattr(EdgeCloudOrchestrator, "_decision", decision)
    monkeypatch.setattr(
        "qwenpaw.agents.model_factory.create_openai_compatible_model_and_formatter",
        lambda **_: (lower, None),
    )
    monkeypatch.setattr(agent_context, "get_current_agent_id", lambda: "parent")
    monkeypatch.setattr(agent_context, "get_current_session_id", lambda: "session")
    monkeypatch.setattr(agent_context, "get_current_user_id", lambda: "user")
    monkeypatch.setattr(management, "agent_exists", lambda *_: True)
    loop = asyncio.get_running_loop()
    child_responses, pending = [], []

    async def run_child(payload):
        payload = {**payload, "request_context": {
            **payload["request_context"], "is_coding_mode": False,
            "sandbox_mounts": [str(child_root)],
        }}
        request_token = current_request_context.set(payload["request_context"])
        session_token = current_session_id.set(payload["session_id"])
        workspace_token = current_workspace_dir.set(child_root)
        try:
            events = [event.model_copy(deep=True) async for event in runtime.run(payload)]
            response = events[-1]
            assert response.object == "response" and response.status == "completed"
            # A delegated child must never publish user attachments itself.
            assert not [
                block for event in events for block in (getattr(event, "content", None) or [])
                if getattr(block, "type", "") in {"file", "image"}
            ]
            value = response.model_dump(mode="json")
            child_responses.append(value)
            return value
        finally:
            current_workspace_dir.reset(workspace_token)
            current_session_id.reset(session_token)
            current_request_context.reset(request_token)

    def collect(_base, payload, _agent, timeout):
        return asyncio.run_coroutine_threadsafe(run_child(payload), loop).result(timeout)

    def submit(_base, payload, _agent, _timeout, _task_timeout=None):
        pending.append(asyncio.run_coroutine_threadsafe(run_child(payload), loop))
        return {"task_id": "task-123456789abc"}

    def status(_base, task_id, **kwargs):
        assert task_id == "task-123456789abc" and pending[0].done()
        return {"status": "finished", "result": pending[0].result()}

    monkeypatch.setattr(management, "collect_final_agent_chat_response", collect)
    monkeypatch.setattr(management, "submit_agent_chat_task", submit)
    monkeypatch.setattr(management, "get_agent_chat_task_status", status)
    parent_state = delivery.current()
    parent_request = current_request_context.set({
        "session_id": "session", "user_id": "user", "is_coding_mode": False,
        "sandbox_mounts": [str(tmp_path)],
    })
    try:
        if background:
            submitted = await management.submit_to_agent("default", "Create and return both final files.")
            assert "[TASK_ID: task-123456789abc]" in submitted.content[0].text
            await asyncio.wait_for(asyncio.wrap_future(pending[0]), 15)
            returned = await management.check_agent_task("task-123456789abc")
            assert "Task completed." in returned.content[0].text
        else:
            returned = await management.chat_with_agent("default", "Create and return both final files.")
        assert delivery.current() is parent_state and not parent_state.submitted
        value = returned.content[0].text
        assert "[FINAL_FILES_ERROR]" not in value and "[FINAL_FILES]" in value
        imported = json.loads(value.split("[FINAL_FILES]\n")[1])
        assert [item["name"] for item in imported] == ["existing.pdf", "missing.xlsx"]
        for item in imported:
            contents = Path(item["file_path"]).read_bytes()
            assert hashlib.sha256(contents).hexdigest() == item["sha256"]
        manifest = child_responses[0]["metadata"][delivery.METADATA_KEY]
        assert manifest["status"] == "ready_for_parent" and len(manifest["files"]) == 2
        assert observation["recipient"] == "parent"
        assert observation["stage"] == (tier if local and not fallback else (
            "ordinary" if mode == "disabled" else "cloud"
        ))
        assert observation["verified"] is (local and not fallback)
        assert observation["requires_verification"] is (local and not fallback)
        assert decision.await_count == (2 if local else int(mode == "direct_cloud"))
        actor = lower if local else cloud
        failures = [p for p in actor.seen_submissions if p["status"] == "rejected"]
        assert failures, "The executing model must receive the missing-file feedback"
        failure = failures[0]
        assert failure["errors"][0]["file_path"] == missing.name
        assert actor.failed_submission_state["files"] == []
        assert failure["existing_files"][0]["resolved_path"] == existing
        assert failure["errors"][0]["resolved_path"] == str(missing.resolve())
        assert failure["retry"]["require_complete_file_list"] is True
        assert "当前委派任务" in failure["retry"]["message"]
        if fallback:
            assert cloud.handoff_feedback and cloud.handoff_feedback[0] == failure
        success_actor = cloud if fallback else actor
        successful = [p for p in success_actor.seen_submissions if p.get("submitted")]
        assert successful and len(successful[-1]["files"]) == 2
        assert successful[-1]["status"] == (
            "pending_verification" if local and not fallback else "pending_finalization"
        )
        accepted = await submit_tool([item["file_path"] for item in imported])
        assert accepted.state == ToolResultState.SUCCESS
        assert len(body(accepted)["files"]) == 2
        response, _ = await finish(ctx)
        assert [block.filename for block in response.output[-1].content] == [
            "existing.pdf", "missing.xlsx",
        ]
    finally:
        current_request_context.reset(parent_request)
        for future in pending:
            if not future.done():
                future.cancel()


@pytest.mark.parametrize("tier", ["device", "edge"])
@pytest.mark.parametrize("passed", [False, True], ids=["reject", "accept"])
async def test_real_enhanced_submission_waits_for_verification_gate(
    ctx, tmp_path, monkeypatch, tier, passed,
):
    """Only the model verdict is stubbed; pending/display/attachment gates run."""
    from test_edge_cloud import prepare
    from test_final_files_display import tool_contents
    from qwenpaw.runtime.envelope import Envelope

    submit_tool = send_final_files_to_user
    candidate = file(tmp_path, "candidate.pdf")
    corrected = file(tmp_path, "corrected.pdf")
    cloud = FeedbackModel("cloud", [
        call(TOOL_NAME, {"file_paths": [corrected]}, "cloud-submit"), text("cloud done"),
    ])
    tools = [FunctionTool(submit_tool)]
    request_ctx = context(tmp_path, tools, cloud)
    steps = []
    if tier == "edge":
        skill = existing_skill(tmp_path, "report")
        request_ctx.agent.toolkit = Toolkit(tools=tools, skills_or_loaders=[str(skill)])
        steps.append(call("Skill", {"skill": "report"}, "skill"))
    steps.extend([
        call(TOOL_NAME, {"file_paths": [candidate]}, "lower-submit"), text("lower done"),
    ])
    lower = FeedbackModel(tier, steps)
    orchestrator = prepare(
        monkeypatch, request_ctx, lower,
        [route("edge_skill" if tier == "edge" else "device_tools", need_verifier=True,
               **({"skill": "report"} if tier == "edge" else {})),
         {"protocol_version": "edge_cloud_verifier.v1", "passed": passed}],
        settings=config(device_max_tool_rounds=3, trace_stream_enabled=True),
        request_context={"is_coding_mode": False, "sandbox_mounts": [str(tmp_path)]},
    )
    entered, proceed = asyncio.Event(), asyncio.Event()
    decide, captured = orchestrator._decision, {}

    async def pause_verifier(ctx, stage, prompt, payload, **kwargs):
        if stage == "verifier":
            captured.update(payload)
            entered.set()
            await proceed.wait()
        return await decide(ctx, stage, prompt, payload, **kwargs)

    orchestrator._decision = AsyncMock(side_effect=pause_verifier)
    envelope, events = Envelope(), []

    async def collect():
        async for event in orchestrator.run(request_ctx, envelope):
            events.append(event.model_copy(deep=True))
        async for event in delivery.finish(request_ctx, envelope):
            events.append(event.model_copy(deep=True))
        async for event in envelope.finalize():
            events.append(event.model_copy(deep=True))

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert delivery.current().submitted and not delivery.current().verified
        assert [e async for e in delivery.finish(request_ctx, Envelope())] == []
        assert not tool_contents(events, TOOL_NAME)
        assert not cloud.requests
        assert set(captured["final_files"]) == {"status", "attempt_id", "submitted", "files"}
        visible_to_model = lower.seen_submissions[-1]
        assert visible_to_model["status"] == "pending_verification"
        assert visible_to_model["existing_files"] and visible_to_model["errors"] == []
        proceed.set()
        await asyncio.wait_for(task, 10)
        names = [
            block.filename for msg in events[-1].output for block in msg.content
            if block.type == "file"
        ]
        assert names == (["candidate.pdf"] if passed else ["corrected.pdf"])
        assert bool(cloud.requests) is not passed
        displayed = json.dumps(tool_contents(events[-1].output, TOOL_NAME))
        assert "existing_files" in displayed
        if not passed:
            assert "candidate.pdf" not in displayed
    finally:
        proceed.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
