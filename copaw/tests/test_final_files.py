"""Final delivery is opt-in, verified, scoped and compatible with existing UI."""

import asyncio
import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg, TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior
from agentscope.tool import FunctionTool

from qwenpaw.config.context import (
    current_request_context,
    current_workspace_dir,
)
from qwenpaw.runtime import final_files as delivery
from qwenpaw.runtime.envelope import Envelope
from qwenpaw.runtime.final_files.prompts import TOOL_NAME, append_prompt
from qwenpaw.agents.tools.send_final_files import send_final_files_to_user
from qwenpaw.agents.tools.agent_management import format_agent_chat_text


def context(tmp_path, agent="parent", session="session", rc=None):
    return SimpleNamespace(
        agent_id=agent,
        session_id=session,
        user_id="user",
        workspace_dir=tmp_path,
        extras={},
        request=SimpleNamespace(request_context=rc or {}, user_id="user"),
        agent=SimpleNamespace(
            state=SimpleNamespace(
                context=[
                    Msg(
                        name="assistant",
                        role="assistant",
                        content=[TextBlock(text="完成。")],
                    ),
                ]
            )
        ),
    )


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    monkeypatch.setenv("QWENPAW_FINAL_FILES_ENABLED", "1")
    monkeypatch.setattr(delivery, "store_root", lambda: tmp_path / "store")
    monkeypatch.setattr(
        "qwenpaw.security.tool_guard.guardians.file_guardian._is_file_guard_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "qwenpaw.security.tool_guard.guardians.file_guardian._load_sensitive_files_from_config",
        lambda: [],
    )
    workspace_token = current_workspace_dir.set(tmp_path)
    request_token = current_request_context.set(
        {
            "session_id": "session",
            "is_coding_mode": True,
        }
    )
    value = context(tmp_path)
    token = delivery.bind_request(value)
    yield value
    delivery.reset_request(token)
    current_request_context.reset(request_token)
    current_workspace_dir.reset(workspace_token)


def file(tmp_path, name="报告.txt", text="final"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


async def finish(ctx):
    envelope = Envelope(session_id=ctx.session_id)
    events = [e async for e in delivery.finish(ctx, envelope)]
    events.extend([e async for e in envelope.finalize()])
    return envelope.response, events


def body(chunk):
    return json.loads(chunk.content[0].text)


async def test_batch_delivered_as_existing_message_types_and_persisted(
    ctx, tmp_path
):
    paths = [
        file(tmp_path, "图.png"),
        file(tmp_path, "报告.pdf"),
        file(tmp_path, "表.xlsx"),
    ]
    result = await send_final_files_to_user(paths)
    assert result.state == ToolResultState.SUCCESS
    assert all(block.type == "text" for block in result.content)
    assert body(result)["status"] == "pending_finalization"
    response, events = await finish(ctx)
    blocks = response.output[-1].content
    assert [b.type.value for b in blocks] == ["image", "file", "file"]
    assert blocks[1].file_name == blocks[1].filename == "报告.pdf"
    assert blocks[1].file_url.startswith("file://")
    saved = ctx.agent.state.context[-1]
    assert saved.get_text_content() == "完成。"
    assert len(saved.get_content_blocks("data")) == 3
    assert saved.metadata[delivery.METADATA_KEY] is True
    assert len([e for e in events if e.object == "response"]) == 1
    assert [e async for e in delivery.finish(ctx, Envelope())] == []


async def test_new_submission_replaces_previous_and_failed_batch_invalidates_it(
    ctx, tmp_path
):
    first = file(tmp_path, "old.txt")
    second = file(tmp_path, "new.txt")
    await send_final_files_to_user([first])
    result = await send_final_files_to_user([second, second])
    assert len(body(result)["files"]) == 1
    assert delivery.current().files[0]["name"] == "new.txt"
    result = await send_final_files_to_user(
        [second, str(tmp_path / "missing.txt")]
    )
    assert result.state == ToolResultState.ERROR
    assert not delivery.current().submitted
    response, _ = await finish(ctx)
    assert not response.output


async def test_snapshot_survives_deleted_source_and_handles_record_filename(
    ctx, tmp_path
):
    path = Path(file(tmp_path, "record.json", '{"ok": true}'))
    await send_final_files_to_user([str(path)])
    snapshot = Path(delivery.current().files[0]["snapshot"])
    path.unlink()
    response, _ = await finish(ctx)
    assert response.output[-1].content[0].filename == "record.json"
    assert snapshot.read_text() == '{"ok": true}'


async def test_edited_snapshot_never_released(ctx, tmp_path):
    await send_final_files_to_user([file(tmp_path)])
    Path(delivery.current().files[0]["snapshot"]).write_text("tampered")
    response, _ = await finish(ctx)
    assert all(c.type.value == "text" for c in response.output[-1].content)
    assert "交付失败" in ctx.agent.state.context[-1].get_text_content()


@pytest.mark.parametrize(
    "kind",
    ["EXCEED_MAX_ITERS", "USER_INTERRUPT", "REQUIRE_EXTERNAL_EXECUTION"],
)
async def test_abnormal_stop_never_releases(ctx, tmp_path, kind):
    await send_final_files_to_user([file(tmp_path)])
    delivery.observe_event(SimpleNamespace(type=kind))
    response, _ = await finish(ctx)
    assert not response.output


@pytest.mark.parametrize(
    "reason,allowed",
    [
        ("Token budget exceeded", False),
        ("Goal completed: blocked", False),
        ("Mission max_iterations_reached", False),
        ("Goal completed: satisfied", True),
        ("All stories passed", True),
        ("", True),
    ],
)
async def test_existing_gate_outcome_controls_delivery(
    ctx, tmp_path, reason, allowed
):
    await send_final_files_to_user([file(tmp_path)])
    delivery.observe_stop(SimpleNamespace(action="terminate", reason=reason))
    response, _ = await finish(ctx)
    assert bool(response.output) is allowed


async def test_verified_child_returns_manifest_and_polling_does_not_overwrite(
    ctx, tmp_path
):
    payload = {"session_id": "child-session", "request_context": {}}
    delivery.prepare_child_request(payload, "child")
    child = context(
        tmp_path, "child", "child-session", payload["request_context"]
    )
    token = delivery.bind_request(child)
    try:
        delivery.current().begin_attempt("edge", verify=True)
        result = await send_final_files_to_user([file(tmp_path, "child.pdf")])
        assert body(result)["status"] == "pending_verification"
        response, _ = await finish(child)
        assert not response.output and not response.metadata
        delivery.current().verified = True
        response, _ = await finish(child)
        assert not response.output
        assert (
            response.metadata[delivery.METADATA_KEY]["status"]
            == "ready_for_parent"
        )
    finally:
        delivery.reset_request(token)
    payload = response.model_dump(mode="json")
    result = format_agent_chat_text(payload, session_id="child-session")
    assert "[SESSION: child-session]" in result and "[FINAL_FILES]" in result
    entries = json.loads(result.split("[FINAL_FILES]\n")[1])
    imported = Path(entries[0]["file_path"])
    imported.write_text("parent edited")
    assert format_agent_chat_text(payload, "child-session") == result
    assert imported.read_text() == "parent edited"
    await send_final_files_to_user([str(imported)])
    parent_response, _ = await finish(ctx)
    assert parent_response.output[-1].content[0].filename == "child.pdf"


async def test_handoff_cannot_be_imported_by_another_session(ctx, tmp_path):
    payload = {"session_id": "child", "request_context": {}}
    delivery.prepare_child_request(payload, "child")
    child = context(tmp_path, "child", "child", payload["request_context"])
    token = delivery.bind_request(child)
    try:
        await send_final_files_to_user([file(tmp_path)])
        response, _ = await finish(child)
    finally:
        delivery.reset_request(token)
    other = context(tmp_path, session="another-session")
    token = delivery.bind_request(other)
    try:
        result = delivery.import_parent_files(response.model_dump(mode="json"))
        assert "[FINAL_FILES_ERROR]" in result
        assert not list(tmp_path.glob("received_final_files/*/*"))
    finally:
        delivery.reset_request(token)


async def test_nested_and_parallel_requests_have_separate_state(ctx, tmp_path):
    parent = delivery.current()

    async def child(name):
        child_ctx = context(tmp_path, name, name)
        token = delivery.bind_request(child_ctx)
        try:
            await send_final_files_to_user([file(tmp_path, name + ".txt")])
            await asyncio.sleep(0)
            assert delivery.current().files[0]["name"] == name + ".txt"
        finally:
            delivery.reset_request(token)

    await asyncio.gather(child("one"), child("two"))
    assert delivery.current() is parent
    assert not parent.submitted


async def test_invalid_handoff_fails_closed(ctx, tmp_path):
    child_ctx = context(tmp_path, rc={delivery.HANDOFF_KEY: "bad"})
    token = delivery.bind_request(child_ctx)
    try:
        assert delivery.current().recipient == "parent"
        result = await send_final_files_to_user([file(tmp_path)])
        assert result.state == ToolResultState.ERROR
    finally:
        delivery.reset_request(token)


async def test_sensitive_item_rejects_whole_batch(ctx, tmp_path):
    result = await send_final_files_to_user(
        [
            file(tmp_path, "ok.txt"),
            file(tmp_path, ".env"),
        ]
    )
    assert result.state == ToolResultState.ERROR
    assert not delivery.current().files


def test_disabled_and_denied_prompts_are_identical(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPAW_FINAL_FILES_ENABLED", "0")
    off = context(tmp_path)
    token = delivery.bind_request(off)
    try:
        assert delivery.current() is None
        assert delivery.FEATURE not in off.extras
        assert append_prompt("original", None, {TOOL_NAME}) == "original"
        payload = {"session_id": "child", "request_context": {}}
        delivery.prepare_child_request(payload, "child")
        assert payload["request_context"] == {}
    finally:
        delivery.reset_request(token)
    assert append_prompt("original", delivery.current(), set()) == "original"


def test_tool_registry_opt_in_and_deny():
    from qwenpaw.runtime.tool_registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(send_final_files_to_user._tool_descriptor)
    assert registry.filter() == []
    assert len(registry.filter(enabled_features={"final_files"})) == 1
    assert (
        registry.filter(enabled_features={"final_files"}, denied={TOOL_NAME})
        == []
    )


@pytest.mark.parametrize("env_value,cli_value", [("0", "1"), ("1", "0")])
def test_entry_cli_defaults_and_precedence(monkeypatch, env_value, cli_value):
    # Import only the two entry helpers: main.py has intentional startup effects.
    main = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(main.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"parse_entry_cli_args", "apply_devco_bundle_env"}
    ]
    import argparse
    import sys

    namespace = {
        "os": os,
        "argparse": argparse,
        "sys": sys,
        "_INIT_ONLY_ENV": "TEST_INIT_ONLY",
    }
    exec(
        compile(ast.Module(body=selected, type_ignores=[]), str(main), "exec"),
        namespace,
    )
    monkeypatch.setattr(os, "environ", {})
    namespace["parse_entry_cli_args"]([])
    assert os.environ["QWENPAW_FINAL_FILES_ENABLED"] == "1"
    os.environ["QWENPAW_FINAL_FILES_ENABLED"] = env_value
    namespace["parse_entry_cli_args"]([])
    assert os.environ["QWENPAW_FINAL_FILES_ENABLED"] == env_value
    rest = namespace["parse_entry_cli_args"](
        ["--final-files-enabled", cli_value, "--other"]
    )
    assert os.environ["QWENPAW_FINAL_FILES_ENABLED"] == cli_value
    assert rest == ["--other"]


@pytest.mark.parametrize("trace_enabled", [False, True])
async def test_device_verification_accepts_pass_and_hands_failure_to_cloud(ctx, tmp_path, monkeypatch, trace_enabled):
    from test_edge_cloud import (
        Model,
        call,
        config,
        context as edge_context,
        prepare,
        route,
        text,
    )
    from qwenpaw.runtime.edge_cloud.safety import DEVICE_TOOLS, WRITE_TOOLS

    assert TOOL_NAME not in DEVICE_TOOLS | WRITE_TOOLS
    path = file(tmp_path, "device.txt")
    lower = Model(
        "device",
        [
            call(TOOL_NAME, {"file_paths": [path]}),
            text("done"),
        ],
    )
    edge_ctx = edge_context(tmp_path, [FunctionTool(send_final_files_to_user)])
    orchestrator = prepare(
        monkeypatch,
        edge_ctx,
        lower,
        [
            route("device_tools"),
            {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
        ],
        settings=config(device_max_tool_rounds=3, trace_stream_enabled=trace_enabled),
    )
    envelope = Envelope()
    events = [e async for e in orchestrator.run(edge_ctx, envelope)]
    assert delivery.current().verified
    verifier_args = orchestrator._decision.await_args_list[1].args
    assert verifier_args[3]["final_files"]["submitted"]
    assert (
        "final_files" not in orchestrator._decision.await_args_list[0].args[3]
    )
    assert not any(
        getattr(c, "type", "") in {"file", "image"}
        for e in events
        for c in (getattr(e, "content", None) or [])
    )
    events.extend([e async for e in delivery.finish(edge_ctx, envelope)])
    assert any(
        getattr(c, "filename", "") == "device.txt"
        for e in events
        for c in (getattr(e, "content", None) or [])
    )
    events.extend([e async for e in envelope.finalize()])
    assert events[-1].metadata == envelope.response.metadata
    assert not any(m.name in {"RouterAgent", "VerifyAgent"} for m in envelope.response.output)
    assert any(m.name == "RouterAgent" for m in events[-1].output) is trace_enabled
    assert any(m.name == "VerifyAgent" for m in events[-1].output) is trace_enabled
    assert "【RouterAgent" not in json.dumps(edge_ctx.agent.state_dict())

    lower = Model(
        "device", [call(TOOL_NAME, {"file_paths": [path]}), text("done")]
    )
    edge_ctx = edge_context(
        tmp_path,
        [FunctionTool(send_final_files_to_user)],
        Model("cloud", [text("needs repair")]),
    )
    # Separate runtime request, like production.
    token = delivery.bind_request(edge_ctx)
    try:
        orchestrator = prepare(
            monkeypatch,
            edge_ctx,
            lower,
            [
                route("device_tools"),
                {
                    "protocol_version": "edge_cloud_verifier.v1",
                    "passed": False,
                },
            ],
            settings=config(device_max_tool_rounds=3, trace_stream_enabled=trace_enabled),
        )
        envelope = Envelope()
        _ = [e async for e in orchestrator.run(edge_ctx, envelope)]
        assert not delivery.current().submitted
        assert not delivery.current().requires_verification
        assert delivery.current().stage == "cloud"
        assert [e async for e in delivery.finish(edge_ctx, envelope)] == []
        assert edge_ctx.agent.model.requests
        assert orchestrator._trace.fallback_reason == "VALIDATION_FAIL"
    finally:
        delivery.reset_request(token)


async def test_history_and_channel_keep_all_files_and_names(ctx, tmp_path):
    from qwenpaw.app.chats.utils import agentscope_msg_to_message
    from qwenpaw.app.channels.renderer import MessageRenderer

    await send_final_files_to_user(
        [
            file(tmp_path, "中文 文件.pdf"),
            file(tmp_path, "picture.png"),
        ]
    )
    response, _ = await finish(ctx)
    history = agentscope_msg_to_message(ctx.agent.state.context[-1])
    content = history[-1].content
    assert content[0].text == "完成。"
    assert content[1].filename == "中文 文件.pdf"
    assert content[2].type.value == "image"
    # Existing channel rendering must see both attachments, not tool JSON.
    rendered = MessageRenderer().message_to_parts(response.output[-1])
    assert any(getattr(p, "filename", "") == "中文 文件.pdf" for p in rendered)
    assert any(getattr(p, "type", "") == "image" for p in rendered)


async def test_permission_checks_all_canonical_paths_and_preserves_denies(
    ctx, tmp_path, monkeypatch
):
    from unittest.mock import Mock
    from urllib.parse import quote
    from qwenpaw.governance import PolicyGuardedTool, tool_adapter
    from qwenpaw.governance.policy import GovernanceAction, GovernanceDecision

    first = file(tmp_path, "one.txt")
    denied = file(tmp_path, "deny file.txt")
    decisions = []

    def evaluate(spec):
        decisions.append(spec)
        action = (
            GovernanceAction.DENY
            if spec.target == str(Path(denied))
            else GovernanceAction.ALLOW
        )
        return GovernanceDecision(action=action, reason="test")

    governor = SimpleNamespace(
        workspace_dir=tmp_path,
        assert_policy=evaluate,
        audit=Mock(),
    )
    monkeypatch.setattr(
        tool_adapter, "_resolve_effective_approval_level", lambda _: None
    )
    tool = PolicyGuardedTool(
        send_final_files_to_user,
        governor=governor,
        request_context={},
    )
    await send_final_files_to_user([first])
    result = await tool.check_permissions(
        {"file_paths": [first, quote(denied)]}
    )
    assert result.behavior == PermissionBehavior.DENY
    assert [d.target for d in decisions] == [first, denied]
    assert all(d.tool_name == "SendFileToUser" for d in decisions)
    assert not delivery.current().submitted


@pytest.mark.parametrize("outcome", ["normal", "cancelled", "error"])
async def test_runtime_finishes_before_save_and_never_releases_on_error(
    ctx, tmp_path, monkeypatch, outcome
):
    from qwenpaw.runtime.runtime import Runtime
    from qwenpaw.runtime.builder import AgentBuilder
    from qwenpaw.runtime.executor import AgentExecutor
    from qwenpaw.runtime.hooks import HookResult
    from qwenpaw.runtime.phases import Phase
    from qwenpaw.runtime.edge_cloud.orchestrator import EdgeCloudOrchestrator

    path = file(tmp_path)
    saved = []
    parent_state = delivery.current()
    agent = ctx.agent
    agent.close = AsyncMock()

    async def hook(phase, hook_ctx):
        if phase == Phase.POST_RESPONSE:
            saved.append(agent.state.context[-1].model_dump(mode="json"))
        return HookResult()

    async def execute(_executor, _msgs):
        assert delivery.current() is not parent_state
        await send_final_files_to_user([path])
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        if outcome == "error":
            raise RuntimeError("execution failed")
        if False:
            yield None

    plugins = SimpleNamespace(
        hook_registry=SimpleNamespace(run=hook),
        slash_command_registry=SimpleNamespace(
            dispatch=AsyncMock(return_value=None)
        ),
    )
    runtime = Runtime(
        workspace=SimpleNamespace(
            agent_id="parent", workspace_dir=tmp_path, plugins=plugins
        ),
        app_services=None,
    )
    monkeypatch.setattr(AgentBuilder, "build", AsyncMock(return_value=agent))
    monkeypatch.setattr(
        EdgeCloudOrchestrator, "should_handle", lambda *a: False
    )
    monkeypatch.setattr(AgentExecutor, "run", execute)
    monkeypatch.setattr(runtime, "_try_save_on_cancel", AsyncMock())
    request = {
        "session_id": "runtime-session",
        "user_id": "user",
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "make report"}],
            }
        ],
    }
    events = []
    try:
        async for event in runtime.run(request):
            events.append(event)
    except (asyncio.CancelledError, RuntimeError):
        assert outcome != "normal"
    assert delivery.current() is parent_state
    if outcome == "normal":
        assert any(b["type"] == "data" for b in saved[0]["content"])
        assert events[-1].output[-1].content[0].type.value == "file"
    else:
        assert not saved
        assert not agent.state.context[-1].get_content_blocks("data")


@pytest.mark.parametrize(
    "background,fork",
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_spawn_entries_bind_child_to_caller(
    ctx, tmp_path, monkeypatch, background, fork
):
    from qwenpaw.agents.tools import agent_management as management
    import qwenpaw.app.agent_context as agent_context

    monkeypatch.setattr(
        agent_context, "get_current_agent_id", lambda: "parent"
    )
    monkeypatch.setattr(
        agent_context, "get_current_session_id", lambda: "session"
    )
    monkeypatch.setattr(agent_context, "get_current_user_id", lambda: "user")
    monkeypatch.setattr(
        management, "_current_request_context_for_agent_transfer", lambda: {}
    )
    monkeypatch.setattr(
        management,
        "_call_fork_api",
        AsyncMock(
            return_value={
                "fork_session_id": "fork-child",
            }
        ),
    )
    captured = []

    def collect(_base, payload, _agent, _timeout):
        captured.append(payload)
        return {"output": [], "status": "completed"}

    def submit(_base, payload, _agent, _timeout):
        captured.append(payload)
        return {"task_id": "task-123456789abc"}

    monkeypatch.setattr(
        management, "collect_final_agent_chat_response", collect
    )
    monkeypatch.setattr(management, "submit_agent_chat_task", submit)
    await management.spawn_subagent(
        task="make report", background=background, fork=fork
    )
    ticket = captured[0]["request_context"][delivery.HANDOFF_KEY]
    grant = json.loads(
        (delivery.store_root() / "handoffs" / (ticket + ".json")).read_text()
    )
    assert grant["parent_agent"] == "parent"
    assert grant["parent_session"] == "session"
    assert grant["child_session"] == captured[0]["session_id"]


async def test_edge_skill_verification_releases_to_parent_only(
    ctx, tmp_path, monkeypatch
):
    from test_edge_cloud import (
        Model,
        call,
        config,
        context as edge_context,
        existing_skill,
        prepare,
        route,
        text,
    )
    from agentscope.tool import Toolkit

    payload = {"session_id": "edge-test", "request_context": {}}
    delivery.prepare_child_request(payload, "default")
    edge_ctx = edge_context(tmp_path)
    edge_ctx.request.request_context = payload["request_context"]
    directory = existing_skill(tmp_path, "report")
    edge_ctx.agent.toolkit = Toolkit(
        tools=[FunctionTool(send_final_files_to_user)],
        skills_or_loaders=[str(directory)],
    )
    path = file(tmp_path, "edge.pdf")
    lower = Model(
        "edge",
        [
            call("Skill", {"skill": "report"}, "load"),
            call(TOOL_NAME, {"file_paths": [path]}, "submit"),
            text("done"),
        ],
    )
    token = delivery.bind_request(edge_ctx)
    try:
        orchestrator = prepare(
            monkeypatch,
            edge_ctx,
            lower,
            [
                route("edge_skill", skill="report"),
                {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
            ],
            settings=config(),
        )
        envelope = Envelope()
        _ = [e async for e in orchestrator.run(edge_ctx, envelope)]
        assert (
            delivery.current().verified
            and delivery.current().recipient == "parent"
        )
        _ = [e async for e in delivery.finish(edge_ctx, envelope)]
        assert (
            envelope.response.metadata[delivery.METADATA_KEY]["status"]
            == "ready_for_parent"
        )
        assert not edge_ctx.agent.state.context[-1].get_content_blocks("data")
    finally:
        delivery.reset_request(token)


@pytest.mark.parametrize("trace_enabled", [False, True])
async def test_execution_handoff_cloud_resubmission_replaces_device_files(
    ctx, tmp_path, monkeypatch, trace_enabled,
):
    from test_edge_cloud import (
        Model,
        call,
        config,
        context as edge_context,
        prepare,
        route,
        text,
    )
    from qwenpaw.runtime.edge_cloud.execution import LocalExecutionHandoff

    rejected = file(tmp_path, "unverified.txt")
    repaired = file(tmp_path, "repaired.txt")
    lower = Model(
        "device",
        [
            call(TOOL_NAME, {"file_paths": [rejected]}),
            LocalExecutionHandoff("DEVICE_TOOL_ROUND_LIMIT"),
        ],
    )
    cloud = Model(
        "cloud",
        [
            call(TOOL_NAME, {"file_paths": [repaired]}, "cloud-submit"),
            text("repaired"),
        ],
    )
    edge_ctx = edge_context(
        tmp_path, [FunctionTool(send_final_files_to_user)], cloud
    )
    orchestrator = prepare(
        monkeypatch,
        edge_ctx,
        lower,
        [
            route("device_tools"),
        ],
        settings=config(device_max_tool_rounds=3, trace_stream_enabled=trace_enabled),
    )
    envelope = Envelope()
    _ = [e async for e in orchestrator.run(edge_ctx, envelope)]
    assert delivery.current().stage == "cloud"
    assert delivery.current().submitted
    assert not delivery.current().requires_verification
    _ = [e async for e in delivery.finish(edge_ctx, envelope)]
    _ = [e async for e in envelope.finalize()]
    files = [
        c.filename
        for msg in envelope.response.output
        for c in msg.content
        if getattr(c, "type", "") == "file"
    ]
    assert files == ["repaired.txt"]


async def test_parent_handoff_write_failure_does_not_fail_completed_task(
    ctx, tmp_path, monkeypatch
):
    payload = {"session_id": "child", "request_context": {}}
    delivery.prepare_child_request(payload, "child")
    child_ctx = context(tmp_path, "child", "child", payload["request_context"])
    token = delivery.bind_request(child_ctx)
    try:
        await send_final_files_to_user([file(tmp_path)])

        def fail_write(*args):
            raise OSError("storage unavailable")

        monkeypatch.setattr(delivery, "_write_json", fail_write)
        response, _ = await finish(child_ctx)
        assert not response.metadata
        assert (
            "交付失败" in child_ctx.agent.state.context[-1].get_text_content()
        )
        assert not child_ctx.agent.state.context[-1].get_content_blocks("data")
        assert [e async for e in delivery.finish(child_ctx, Envelope())] == []
    finally:
        delivery.reset_request(token)


async def test_permission_and_tool_resolve_the_same_encoded_relative_path(
    ctx, tmp_path
):
    from urllib.parse import quote

    path = file(tmp_path, "文件 空格.txt")
    raw = "  " + quote(Path(path).name) + "  "
    result = await send_final_files_to_user([raw])
    assert result.state == ToolResultState.SUCCESS
    assert delivery.source_path(raw) == Path(
        body(result)["files"][0]["source_path"]
    )


@pytest.mark.parametrize("role", ["ordinary", "device", "edge"])
@pytest.mark.parametrize("mode", ["coding", "session", "mounted"])
def test_delivery_prompt_uses_request_path_base_without_creating_it(
    ctx, tmp_path, role, mode
):
    from qwenpaw.config.context import current_session_id

    root = {
        "coding": tmp_path,
        "session": tmp_path / "session_tmp" / "path-test",
        "mounted": tmp_path / "external-output",
    }[mode]
    session_token = current_session_id.set("path-test")
    request_token = current_request_context.set({
        "is_coding_mode": mode == "coding",
        "sandbox_mounts": [str(root)] if mode == "mounted" else [],
    })
    try:
        prompt = append_prompt("original", delivery.current(), {TOOL_NAME}, role=role)
        assert str(root.resolve()) in prompt
        if mode != "coding":
            assert not root.exists()
        assert delivery.source_path("report.pptx").parent == root.resolve()
    finally:
        current_request_context.reset(request_token)
        current_session_id.reset(session_token)


@pytest.mark.parametrize("absolute", [False, True])
async def test_final_root_submission_replaces_same_named_work_file(
    ctx, tmp_path, absolute
):
    import hashlib

    from qwenpaw.config.context import current_session_id

    root = tmp_path / "session_tmp" / "path-test"
    work = root / "weather_ppt_work"
    work.mkdir(parents=True)
    old = Path(file(work, "报告.pptx", "old working version"))
    final = Path(file(root, "报告.pptx", "verified final version"))
    session_token = current_session_id.set("path-test")
    request_token = current_request_context.set({"is_coding_mode": False})
    try:
        # The same name does not authorize substituting a different file.
        previous = body(await send_final_files_to_user([str(old)]))
        assert previous["files"][0]["source_path"] == str(old.resolve())
        selected = str(final) if absolute else final.name
        result = await send_final_files_to_user([selected])
        assert result.state == ToolResultState.SUCCESS
        item = body(result)["files"][0]
        expected = final.read_bytes()
        assert item["source_path"] == str(final.resolve())
        assert item["size"] == len(expected)
        assert item["sha256"] == hashlib.sha256(expected).hexdigest()
        snapshot = Path(delivery.current().files[0]["snapshot"])
        assert snapshot.read_bytes() == expected
        response, _ = await finish(ctx)
        assert response.output[-1].content[0].file_url == snapshot.as_uri()
        assert old.read_text(encoding="utf-8") == "old working version"
    finally:
        current_request_context.reset(request_token)
        current_session_id.reset(session_token)
