"""Cloud decisions, execution handoff and default-session compatibility."""

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.agent import Agent, ReActConfig
from agentscope.message import (
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultState,
)
from agentscope.model import ChatResponse
from agentscope.permission import PermissionMode
from agentscope.tool import FunctionTool, ToolChunk, Toolkit

from qwenpaw.app.agent_context import set_current_session_id
from qwenpaw.hooks.session.session_hook import _build_session_delta_payload
from qwenpaw.observability.model_request_capture import (
    clear_model_request_captures,
    edge_cloud_model_request_context,
    pop_model_requests_for_session,
    record_model_request,
)
from qwenpaw.runtime.builder import AgentBuilder
from qwenpaw.runtime.edge_cloud.config import (
    EdgeCloudConfig,
    detach_edge_cloud_override,
    load_edge_cloud_config,
    resolve_edge_cloud_config,
)
from qwenpaw.runtime.edge_cloud.skills import (
    DEVICE_TOOLS,
    RouteSkillReader,
    make_edge_toolkit,
)
from qwenpaw.runtime.edge_cloud.execution import (
    ExecutionJournal,
    ExecutionModel,
    LocalExecutionHandoff,
    TaskInputView,
)
from qwenpaw.runtime.edge_cloud.handoff import HandoffArtifacts
from qwenpaw.runtime.edge_cloud.function_call import consume_decision_call
from qwenpaw.runtime.edge_cloud.orchestrator import EdgeCloudOrchestrator
from qwenpaw.runtime.edge_cloud.schemas import RouterDecision
from qwenpaw.runtime.envelope import Envelope


def text(value):
    return ChatResponse(content=[TextBlock(text=value)], is_last=True)


def call(name, arguments, identity="call-1"):
    return ChatResponse(
        content=[ToolCallBlock(id=identity, name=name, input=json.dumps(arguments))],
        is_last=True,
    )


class Model:
    def __init__(self, name, responses=()):
        self.model = name
        self.context_size = 32000
        self.parameters = SimpleNamespace(max_tokens=1000)
        self.responses = list(responses)
        self.requests = []
        self.tool_choices = []

    async def count_tokens(self, *args, **kwargs):
        return 100

    async def __call__(self, messages, tools=None, **kwargs):
        self.requests.append((messages, tools))
        self.tool_choices.append(kwargs.get("tool_choice"))
        record_model_request(
            provider_id="test",
            model=self,
            messages=[m.model_dump(mode="json") for m in messages],
            tools=tools,
            tool_choice=None,
            params={},
        )
        response = self.responses.pop(0) if self.responses else text("done")
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture(autouse=True)
def clean_capture():
    clear_model_request_captures()
    set_current_session_id("edge-test")
    yield
    clear_model_request_captures()
    set_current_session_id("")


def config(**changes):
    return replace(
        EdgeCloudConfig(
            True,
            frozenset({"default"}),
            "http://localhost/v1",
            "test-key",
            "local-model",
            log_detail="off",
            device_base_url="http://device.local/v1",
            device_api_key="test-device-key",
            device_model="device-model",
        ),
        **changes,
    )


def route(kind="device_text", **extra):
    return {
        "protocol_version": "edge_cloud_router.v1",
        "task_type": kind,
        "target_agent": (
            "edge"
            if kind.startswith("edge")
            else ("cloud" if kind == "complex_or_negative" else "device")
        ),
        "need_verifier": False,
        "estimated_tool_rounds": 2 if kind == "device_tools" else 0,
        "execution_task": "Complete the file task with the provided inputs and preserve all stated requirements.",
        **extra,
    }


def context(tmp_path, tools=(), cloud=None):
    agent = Agent(
        name="default",
        model=cloud or Model("cloud"),
        system_prompt="DEFAULT SYSTEM",
        toolkit=Toolkit(tools=list(tools)),
        react_config=ReActConfig(max_iters=10),
    )
    agent.state.permission_context.mode = PermissionMode.BYPASS
    marker = object()
    agent._context_manager = marker
    agent.state_dict = lambda: {
        "state": agent.state.model_dump(mode="json"),
        "scroll": {"marker": "unchanged"},
    }
    return SimpleNamespace(
        agent=agent,
        agent_id="default",
        workspace_dir=tmp_path,
        workspace=None,
        session_id="edge-test",
        extras={"system_prompt": "DEFAULT SYSTEM"},
        input_msgs=[
            Msg(
                name="user",
                role="user",
                content=[TextBlock(text="complete the file task")],
            )
        ],
        request=SimpleNamespace(request_context={}),
        agent_config=SimpleNamespace(
            active_model=SimpleNamespace(provider_id="test", model="cloud")
        ),
    )


def prepare(monkeypatch, ctx, lower, decisions, settings=None, request_context=None):
    request_context = {} if request_context is None else request_context
    monkeypatch.setattr(
        AgentBuilder,
        "_build_request_context",
        staticmethod(lambda _: request_context),
    )
    monkeypatch.setattr(
        "qwenpaw.agents.model_factory.create_openai_compatible_model_and_formatter",
        lambda **_: (lower, None),
    )
    orchestrator = EdgeCloudOrchestrator(
        workspace=None, app_services=None, config=settings or config()
    )
    orchestrator._decision = AsyncMock(side_effect=decisions)
    return orchestrator


async def consume(orchestrator, ctx):
    return [
        event
        async for event in orchestrator.run(ctx, Envelope(session_id=ctx.session_id))
    ]


@pytest.mark.asyncio
async def test_text_execution_has_no_tools_and_keeps_default_session(
    monkeypatch, tmp_path
):
    async def write_file(file_path: str, content: str):
        raise AssertionError("Text route must have no tool access")

    ctx = context(tmp_path, [FunctionTool(write_file)])
    agent, manager = ctx.agent, ctx.agent._context_manager
    lower = Model("local", [text("translation")])
    orchestrator = prepare(monkeypatch, ctx, lower, [route()])
    events = await consume(orchestrator, ctx)
    assert len(events) > 2
    assert orchestrator._decision.await_count == 1
    assert not lower.requests[0][1]
    assert ctx.agent is agent and agent._context_manager is manager
    assert ctx.agent._system_prompt == ctx.extras["system_prompt"] == "DEFAULT SYSTEM"
    assert ctx.agent.react_config.max_iters == 10
    payload = _build_session_delta_payload(
        ctx, agent.state_dict(), user_id="u", channel="console"
    )
    # The collaborative delta uses ordinary message/config fields while
    # session-level prompt/tool records retain their existing lifecycle.
    assert payload["protocol"] == "edge_cloud_trace.v2"
    assert payload["record_type"] == "delta"
    assert payload["delta"]
    assert payload["agent_calls"] == []
    assert "system_prompt" not in payload
    assert not any(key.startswith("stage.") for key in payload)
    model = next(item for item in payload["model_requests"] if item["agent"] == "DeviceAgent")
    assert model["config_ref"] == {
        "prompt": "device_text",
        "tools": "device_text",
    }
    assert all(message["role"] != "system" for message in payload["delta"])
    assert payload["delta"][0]["role"] == "user"
    prompt_config = next(
        config
        for config in payload["configs"]
        if config["record_type"] == "config.prompt"
        and config["config_key"] == "device_text"
    )
    assert "DeviceAgent" in prompt_config["system_prompt"]
    assert payload["edge_cloud"]["final_agent"] == "device"


@pytest.mark.asyncio
async def test_device_prompt_is_self_contained_and_skill_free(monkeypatch, tmp_path):
    """DeviceAgent must not learn about Skills, VerifyAgent or CloudAgent."""
    ctx = context(tmp_path)
    lower = Model("local", [text("translation")])
    orchestrator = prepare(monkeypatch, ctx, lower, [route()])
    await consume(orchestrator, ctx)

    system = lower.requests[0][0][0].get_text_content()
    assert system.startswith("你是 DeviceAgent")
    assert "DEFAULT SYSTEM" not in system
    for forbidden in ("Skill", "VerifyAgent", "CloudAgent", "云端"):
        assert forbidden not in system
    assert "工作目录" in system and str(tmp_path) in system
    assert "External mounted directories: none" in system
    assert "Session scratch directory: none (session_id missing)" in system

    task = json.loads(lower.requests[0][0][1].get_text_content())
    assert task["agent"] == "DeviceAgent"
    assert "skill" not in task


@pytest.mark.asyncio
async def test_device_prompt_includes_env_and_write_targets(monkeypatch, tmp_path):
    rw, ro = tmp_path / "rw", tmp_path / "ro"
    ctx = context(tmp_path)
    ctx.extras["runtime_environments"] = {
        "python": {
            "executable": "C:/Python/python.exe",
            "directory": "C:/Python",
        },
        "nodejs": {
            "executable": "C:/node/node.exe",
            "directory": "C:/node",
        },
    }
    lower = Model("local", [text("done")])
    orchestrator = prepare(
        monkeypatch,
        ctx,
        lower,
        [route()],
        request_context={
            "session_id": "edge-test",
            "sandbox_mounts": [str(rw)],
            "sandbox_rx_mounts": [str(ro)],
            "is_coding_mode": False,
        },
    )
    await consume(orchestrator, ctx)

    system = lower.requests[0][0][0].get_text_content()
    assert "- Default Shell:" in system
    assert "Python（非虚拟环境）" in system
    assert "Node.js 可执行文件" in system
    assert f"- External mounted directories: {rw}" in system
    assert "- Read/execute-only external directories" in system
    assert str(ro) in system
    assert (
        f"- Session scratch directory: {tmp_path / 'session_tmp' / 'edge-test'}"
        in system
    )
    assert "文件只能写入该行列出的额外挂载目录" in system
    assert "文件只能写入 Session scratch directory" in system


@pytest.mark.asyncio
async def test_edge_task_keeps_the_selected_skill(monkeypatch, tmp_path):
    ctx = context(tmp_path)
    directory = existing_skill(tmp_path, "report")
    ctx.agent.toolkit = Toolkit(skills_or_loaders=[str(directory)])
    lower = Model("local", [call("Skill", {"skill": "report"}), text("done")])
    orchestrator = prepare(
        monkeypatch,
        ctx,
        lower,
        [
            route("edge_skill", skill="report"),
            {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
        ],
    )
    await consume(orchestrator, ctx)

    task = json.loads(lower.requests[0][0][1].get_text_content())
    assert task["agent"] == "EdgeAgent"
    assert task["skill"] == "report"


@pytest.mark.asyncio
async def test_execution_handoff_continues_with_existing_calls_and_scroll(
    monkeypatch, tmp_path
):
    target = tmp_path / "result.txt"
    writes = []

    async def write_file(file_path: str, content: str):
        """Write the given content."""
        Path(file_path).write_text(content, encoding="utf-8")
        writes.append(content)
        return ToolChunk(
            state=ToolResultState.SUCCESS, content=[TextBlock(text="written")]
        )

    lower = Model(
        "local",
        [
            call("write_file", {"file_path": str(target), "content": "draft"}),
            LocalExecutionHandoff("DEVICE_TOOL_ROUND_LIMIT"),
        ],
    )
    cloud = Model(
        "cloud",
        [
            call(
                "write_file",
                {"file_path": str(target), "content": "corrected"},
                "call-2",
            ),
            text("complete"),
        ],
    )
    ctx = context(tmp_path, [FunctionTool(write_file)], cloud)
    agent, manager = ctx.agent, ctx.agent._context_manager
    orchestrator = prepare(
        monkeypatch,
        ctx,
        lower,
        [
            route("device_tools"),
        ],
    )
    await consume(orchestrator, ctx)
    assert writes == ["draft", "corrected"]
    assert target.read_text(encoding="utf-8") == "corrected"
    assert ctx.agent is agent and agent._context_manager is manager
    assert orchestrator._decision.await_count == 1
    assert orchestrator._trace.fallback_reason == "DEVICE_TOOL_ROUND_LIMIT"
    saved = json.dumps(agent.state_dict(), ensure_ascii=False)
    assert "call-1" in saved and "call-2" in saved and "written" in saved
    # The original request already sits in the context; the handoff must not
    # repeat it as a second copy.
    assert saved.count("complete the file task") == 1
    cloud_input = json.dumps([m.model_dump(mode="json") for m in cloud.requests[0][0]])
    assert "call-1" in cloud_input and "written" in cloud_input
    assert "handoff://" in cloud_input
    assert cloud.requests[0][0][-1].role == "user"
    handoff = json.dumps(
        cloud.requests[0][0][-1].model_dump(mode="json"), ensure_ascii=False
    )
    assert "[运行时接管说明]" in handoff
    # The role statement lives in the newest user turn, not the system prompt.
    assert "You are CloudAgent" in handoff
    assert cloud.requests[0][0][0].role == "system"
    assert "CloudAgent" not in cloud.requests[0][0][0].get_text_content()
    # The lower-tier turn is folded into the handoff: only the user request,
    # the handoff and the cloud answer remain in the session context.
    assert [m.role for m in agent.state.context] == ["user", "user", "assistant"]
    assert agent.state.context[-1].id == agent.state.reply_id
    assert "submit_verification" not in saved and "submit_route_decision" not in saved
    captures = pop_model_requests_for_session(ctx.session_id)
    assert len(captures) == 4
    assert all(item["messages"][0]["content"] == "DEFAULT SYSTEM" for item in captures)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_coordinator", [False, True])
async def test_three_failures_stop_a_batch_before_fourth_operation(
    monkeypatch, tmp_path, with_coordinator,
):
    invoked = []

    async def read_file(file_path: str):
        """Read a file."""
        invoked.append(file_path)
        return ToolChunk(
            state=ToolResultState.ERROR, content=[TextBlock(text="not found")]
        )

    batch = ChatResponse(
        content=[
            ToolCallBlock(
                id=f"call-{i}",
                name="read_file",
                input=json.dumps({"file_path": str(i)}),
            )
            for i in range(4)
        ],
        is_last=True,
    )
    lower = Model("local", [batch])
    ctx = context(tmp_path, [FunctionTool(read_file)])
    if with_coordinator:
        from qwenpaw.tool_calls import ToolCoordinator, ToolCoordinatorMiddleware

        ctx.agent._acting_middlewares = [ToolCoordinatorMiddleware(ToolCoordinator())]
    orchestrator = prepare(monkeypatch, ctx, lower, [route("device_tools")])
    await asyncio.wait_for(consume(orchestrator, ctx), 3)
    assert len(invoked) == 3
    assert len(lower.requests) == 1
    assert orchestrator._decision.await_count == 1  # no verifier after forced handoff
    assert orchestrator._trace.fallback_reason == "SAME_TOOL_3_FAILURES"
    assert len(orchestrator._journal.records) == 3
    assert all(record["status"] == "error" for record in orchestrator._journal.records)
    assert "not found" in json.dumps(ctx.agent.state_dict())


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_in_tool", [False, True])
async def test_cancel_does_not_start_cloud_or_verifier(monkeypatch, tmp_path, wait_in_tool):
    started = asyncio.Event()

    class WaitingModel(Model):
        async def __call__(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()

    async def read_file(file_path: str):
        started.set()
        await asyncio.Event().wait()

    cloud = Model("cloud")
    ctx = context(tmp_path, [FunctionTool(read_file)], cloud=cloud)
    lower = (
        Model("local", [call("read_file", {"file_path": "a.txt"})])
        if wait_in_tool else WaitingModel("local")
    )
    orchestrator = prepare(
        monkeypatch, ctx, lower, [route("device_tools" if wait_in_tool else "device_text")],
    )
    task = asyncio.create_task(consume(orchestrator, ctx))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not cloud.requests
    assert orchestrator._decision.await_count == 1
    assert ctx.agent.model is cloud and ctx.agent._system_prompt == "DEFAULT SYSTEM"
    if wait_in_tool:
        assert orchestrator._journal.stop_reason == "UNKNOWN_TOOL_OUTCOME"


@pytest.mark.asyncio
async def test_control_model_requests_are_not_persisted():
    model = Model("cloud")
    for stage in ("router", "verifier", "device", "edge", "cloud"):
        with edge_cloud_model_request_context(
            stage=stage,
            base_agent_id="default",
            flow_id="flow",
            session_system_prompt="DEFAULT",
        ):
            await model(
                [
                    Msg(
                        name="system",
                        role="system",
                        content=[TextBlock(text=stage)],
                    )
                ]
            )
    saved = pop_model_requests_for_session("edge-test")
    assert len(saved) == 3
    assert all(
        request["messages"] == [{"role": "system", "content": "DEFAULT"}]
        for request in saved
    )
    # Other users of model request capture retain their old behavior.
    await model(
        [
            Msg(
                name="system",
                role="system",
                content=[TextBlock(text="ordinary")],
            )
        ]
    )
    assert (
        pop_model_requests_for_session("edge-test")[0]["messages"][0]["content"][0][
            "text"
        ]
        == "ordinary"
    )


@pytest.mark.parametrize(
    "sequence,expected",
    [
        ([("a", "error"), ("a", "error"), ("a", "error")], 3),
        ([("a", "error"), ("b", "error"), ("a", "error")], 1),
        ([("a", "error"), ("a", "success"), ("a", "error")], 1),
    ],
)
def test_failure_streak_is_consecutive_not_cumulative(sequence, expected):
    journal = ExecutionJournal()
    for name, state in sequence:
        journal.complete({"tool": name}, ToolChunk(state=state, content=[]))
    assert journal.failure_streak == expected
    assert bool(journal.stop_reason) == (expected == 3)


@pytest.mark.asyncio
async def test_handoff_references_are_scoped_readonly_snapshots():
    journal = ExecutionJournal()
    journal.records.append(
        {
            "call_seq": 1,
            "tool": "read_file",
            "status": "error",
            "result": "evidence",
        }
    )
    artifacts = HandoffArtifacts(
        "flow",
        request="original",
        reason="failed",
        journal=journal,
        conditions=["complete"],
    )
    journal.records[0]["result"] = "changed"
    result = await artifacts(ref="trace://flow/call-1@v1", view="result")
    assert (
        "evidence" in result.content[0].text and "changed" not in result.content[0].text
    )
    with pytest.raises(ValueError):
        await artifacts(ref="handoff://another/manifest@v1")


def test_handoff_inlines_short_results_and_only_points_to_long_ones():
    journal = ExecutionJournal()
    journal.records = [
        {
            "call_seq": 1,
            "tool": "read_file",
            "arguments": {"file_path": "short.txt"},
            "status": "success",
            "result": {
                "content": [{"type": "text", "text": "SHORT-RESULT"}],
                "state": "success",
            },
        },
        {
            "call_seq": 2,
            "tool": "read_file",
            "arguments": {"file_path": "huge.txt"},
            "status": "success",
            "result": {
                "content": [{"type": "text", "text": "X" * 5000}],
                "state": "success",
            },
        },
    ]
    artifacts = HandoffArtifacts(
        "flow",
        request="req",
        reason="failed",
        journal=journal,
        conditions=[],
    )
    text = EdgeCloudOrchestrator._handoff_message(
        reason="VALIDATION_FAIL",
        detail="",
        artifacts=artifacts,
        executed_by="edge",
        execution_task="task",
    ).get_text_content()
    assert "SHORT-RESULT" in text
    assert "X" * 100 not in text
    assert "实际结果过长（5000 字符），已省略" in text
    assert "trace://flow/call-2@v1" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_streamed_tool_evidence_uses_sdk_terminal_semantics(failure):
    async def read_file(file_path: str):
        """Stream file contents."""
        yield ToolChunk(
            content=[TextBlock(text="first part")],
            is_last=False,
            state=(ToolResultState.ERROR if failure else ToolResultState.RUNNING),
        )
        yield ToolChunk(content=[TextBlock(text="second part")])

    journal = ExecutionJournal()
    wrapped = journal.wrap(FunctionTool(read_file), lower=True)
    result = await wrapped(file_path="a.txt")
    chunks = [chunk async for chunk in result]
    assert len(chunks) == 2
    record = journal.records[0]
    assert record["status"] == ("error" if failure else "success")
    assert "first part" in json.dumps(record["result"])
    assert "second part" in json.dumps(record["result"])
    assert journal.failure_streak == int(failure)


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["device", "edge"])
@pytest.mark.parametrize("passed", [True, False])
async def test_coordinator_read_error_allows_write_and_normal_verification(
    monkeypatch, tmp_path, tier, passed,
):
    import gc

    from qwenpaw.tool_calls import ToolCoordinator, ToolCoordinatorMiddleware

    target = tmp_path / "hello.html"

    async def read_file(file_path: str):
        return ToolChunk(
            state=ToolResultState.ERROR,
            content=[TextBlock(text="Error [FILE_NOT_FOUND]: hello.html")],
        )

    async def write_file(file_path: str, content: str):
        Path(file_path).write_text(content, encoding="utf-8")
        return ToolChunk(content=[TextBlock(text="Wrote hello.html")])

    class CollectingModel(Model):
        async def __call__(self, *args, **kwargs):
            # The coordinator stops consuming on ERROR. Close abandoned SDK
            # streams before the next model response, as can happen in service.
            gc.collect()
            await asyncio.sleep(0)
            return await super().__call__(*args, **kwargs)

    cloud = Model("cloud", [text("cloud continued from the existing file")])
    tools = [FunctionTool(read_file), FunctionTool(write_file)]
    ctx = context(tmp_path, tools, cloud)
    steps = []
    if tier == "edge":
        directory = existing_skill(tmp_path, "report")
        ctx.agent.toolkit = Toolkit(tools=tools, skills_or_loaders=[str(directory)])
        steps.append(call("Skill", {"skill": "report"}, "load"))
    ctx.agent._acting_middlewares = [ToolCoordinatorMiddleware(ToolCoordinator())]
    steps.extend([
        call("read_file", {"file_path": str(target)}, "read"),
        call("write_file", {"file_path": str(target), "content": "hello"}, "write"),
        text("local complete"),
    ])
    lower = CollectingModel("local", steps)
    orchestrator = prepare(
        monkeypatch, ctx, lower,
        [
            route(
                "edge_skill" if tier == "edge" else "device_tools",
                **({"skill": "report"} if tier == "edge" else {}),
            ),
            {"protocol_version": "edge_cloud_verifier.v1", "passed": passed},
        ],
        settings=config(device_max_tool_rounds=3),
    )
    events = await asyncio.wait_for(consume(orchestrator, ctx), 10)
    assert target.read_text(encoding="utf-8") == "hello"
    assert orchestrator._decision.await_count == 2
    assert orchestrator._trace.final_agent == (tier if passed else "cloud")
    assert orchestrator._trace.fallback is not passed
    assert bool(cloud.requests) is not passed
    records = orchestrator._journal.records
    assert [(r["tool"], r["status"]) for r in records[-2:]] == [
        ("read_file", "error"), ("write_file", "success"),
    ]
    assert "FILE_NOT_FOUND" in json.dumps(records[-2]["result"])
    assert "interrupted by the user" not in json.dumps(
        [event.model_dump(mode="json") for event in events],
    )
    assert ctx.agent.model is cloud
    assert ctx.agent._system_prompt == "DEFAULT SYSTEM"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [ToolResultState.ERROR, ToolResultState.INTERRUPTED])
async def test_terminal_tool_error_is_recorded_before_consumer_closes(state):
    async def read_file(file_path: str):
        yield ToolChunk(
            state=ToolResultState.RUNNING,
            is_last=False,
            content=[TextBlock(text="partial result")],
        )
        yield ToolChunk(state=state, content=[TextBlock(text="terminal result")])

    journal = ExecutionJournal()
    stream = await journal.wrap(FunctionTool(read_file), lower=True)(file_path="a.txt")
    try:
        await anext(stream)
        await anext(stream)
        record = journal.records[0]
        assert record["status"] == state.value
        assert "partial result" in json.dumps(record["result"])
        assert "terminal result" in json.dumps(record["result"])
    finally:
        await stream.aclose()
    assert journal.stop_reason == (
        "UNKNOWN_TOOL_OUTCOME" if state == ToolResultState.INTERRUPTED else ""
    )
    assert journal.failure_streak == int(state == ToolResultState.ERROR)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "close", "cancel"])
async def test_write_outcome_remains_safe_on_close_cancel_and_cloud_replay(ending):
    writes = []

    async def write_file(file_path: str, content: str):
        writes.append(file_path)
        if ending == "success":
            return ToolChunk(content=[TextBlock(text="written")])

        async def partial():
            yield ToolChunk(
                is_last=False, state=ToolResultState.RUNNING,
                content=[TextBlock(text="write started")],
            )
            await asyncio.Event().wait()

        return partial()

    journal = ExecutionJournal()
    tool = FunctionTool(write_file)
    arguments = {"file_path": "a.txt", "content": "hello"}
    stream = await journal.wrap(tool, lower=True)(**arguments)
    await anext(stream)
    if ending == "cancel":
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    await stream.aclose()
    expected = "succeeded" if ending == "success" else "unknown"
    assert journal.operations[journal.key("write_file", arguments)]["status"] == expected
    assert journal.stop_reason == ("" if ending == "success" else "UNKNOWN_TOOL_OUTCOME")
    assert journal.records[0]["status"] == ("success" if ending == "success" else "unknown")
    assert not journal._lock.locked()

    replay = await journal.wrap(tool, lower=False)(**arguments)
    chunks = [chunk async for chunk in replay]
    assert len(chunks) == 1 and chunks[0].state == ToolResultState.ERROR
    assert f"already attempted ({expected})" in chunks[0].content[0].text
    assert writes == ["a.txt"]


def test_configuration_and_request_overrides_are_role_scoped(
    monkeypatch,
):
    for name in list(__import__("os").environ):
        if name.startswith("QWENPAW_EDGE_CLOUD_"):
            monkeypatch.delenv(name)
    loaded = load_edge_cloud_config()
    assert not loaded.enabled and loaded.device_max_tool_rounds == 2
    original = config()
    merged = resolve_edge_cloud_config(original, {"edge": {"model": "new"}})
    assert merged.edge_model == "new" and original.edge_model == "local-model"
    request = SimpleNamespace(
        request_context={
            "edge_cloud": {"edge": {"api_key": "private"}},
            "user_id": "u",
        },
        channel_meta={},
    )
    detached, override = detach_edge_cloud_override(request)
    assert (
        "edge_cloud" not in detached.request_context
        and override["edge"]["api_key"] == "private"
    )
    assert "edge_cloud" in request.request_context
    for changes in (
        {"enabled": "true"},
        {"edge": {"model": ""}},
        {"edge": {"api_base": "file:///private"}},
    ):
        with pytest.raises(ValueError):
            resolve_edge_cloud_config(original, changes)


@pytest.mark.asyncio
async def test_existing_skill_runs_without_extra_configuration(monkeypatch, tmp_path):
    directory = tmp_path / "skills" / "report"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: report\ndescription: Generate a fixed report\n---\nOriginal workflow",
        encoding="utf-8",
    )
    ctx = context(tmp_path)
    ctx.agent.toolkit = Toolkit(skills_or_loaders=[str(directory)])
    lower = Model(
        "local", [call("Skill", {"skill": "report"}), text("report complete")]
    )
    orchestrator = prepare(
        monkeypatch,
        ctx,
        lower,
        [
            route("edge_skill", skill="report"),
            {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
        ],
    )
    await consume(orchestrator, ctx)
    assert orchestrator._trace.final_agent == "edge", (
        orchestrator._trace.fallback_reason,
        orchestrator._journal.records,
        ctx.agent.state_dict(),
    )
    assert orchestrator._decision.await_count == 2
    assert not (directory / "edge-cloud.json").exists()
    assert orchestrator._journal.required_skill == "report"
    assert "Original workflow" in json.dumps(ctx.agent.state_dict())


def test_unavailable_skill_loader_falls_back_before_local_execution():
    orchestrator = EdgeCloudOrchestrator(
        workspace=None, app_services=None, config=config()
    )
    decision = RouterDecision.model_validate(route("edge_skill", skill="unknown"))
    orchestrator._normalize_route(decision, {})
    assert decision.target_agent == "cloud"


@pytest.mark.asyncio
async def test_model_construction_failure_preserves_original_request(
    monkeypatch, tmp_path
):
    ctx = context(tmp_path)
    orchestrator = prepare(monkeypatch, ctx, Model("local"), [route()])

    def unavailable(**kwargs):
        raise RuntimeError("model service unavailable")

    monkeypatch.setattr(
        "qwenpaw.agents.model_factory.create_openai_compatible_model_and_formatter",
        unavailable,
    )
    await consume(orchestrator, ctx)
    assert orchestrator._trace.final_agent == "cloud"
    assert "complete the file task" in json.dumps(ctx.agent.state_dict())
    assert orchestrator._decision.await_count == 1


@pytest.mark.asyncio
async def test_production_agent_keeps_real_scroll_archive_on_handoff(
    monkeypatch, tmp_path
):
    from qwenpaw.agents.react_agent import QwenPawAgent
    from qwenpaw.agents.context.scroll.history import HistoryStore
    from qwenpaw.agents.context.scroll.manager import ScrollContextManager
    from qwenpaw.agents.context.budget_middleware import (
        RequestBudgetMiddleware,
    )
    from qwenpaw.config.config import AgentProfileConfig, ModelSlotConfig
    from qwenpaw.loop.gates import StopAction, StopHandlerResult

    history = HistoryStore(tmp_path / "history.db")
    manager = ScrollContextManager(history=history, session_id="edge-test")
    profile = AgentProfileConfig(
        id="default",
        name="default",
        active_model=ModelSlotConfig(provider_id="test", model="cloud"),
    )
    cloud = Model("cloud", [text("fixed")])

    async def read_file(file_path: str):
        """Read a file."""
        return ToolChunk(
            state=ToolResultState.SUCCESS,
            content=[TextBlock(text="original evidence")],
        )

    agent = QwenPawAgent(
        name="default",
        model=cloud,
        system_prompt="DEFAULT SYSTEM",
        toolkit=Toolkit(tools=[FunctionTool(read_file)]),
        react_config=ReActConfig(max_iters=10),
        middlewares=[RequestBudgetMiddleware(manager, compression_enabled=True)],
        agent_config=profile,
        workspace_dir=tmp_path,
        context_manager=manager,
    )
    monkeypatch.setattr(
        agent,
        "_run_stop_handlers",
        AsyncMock(return_value=StopHandlerResult(action=StopAction.TERMINATE)),
    )
    monkeypatch.setattr(agent, "_model_rejects_media", lambda: False)
    ctx = context(tmp_path)
    ctx.agent, ctx.agent_config = agent, profile
    lower = Model(
        "local",
        [call("read_file", {"file_path": "a.txt"}), text("incomplete")],
    )
    orchestrator = prepare(
        monkeypatch,
        ctx,
        lower,
        [
            route("device_tools"),
            {"protocol_version": "edge_cloud_verifier.v1", "passed": False},
        ],
    )
    try:
        await consume(orchestrator, ctx)
        assert ctx.agent is agent and agent._context_manager is manager
        assert agent.state_dict()["scroll"] == manager.to_dict()
        assert manager._persisted_tcids == {"call-1"}
        assert "original evidence" in json.dumps(agent.state_dict())
        assert orchestrator._trace.final_agent == "cloud"
    finally:
        history.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["append_file", "move_file_or_dir"])
async def test_device_retains_previous_simple_file_operations(
    monkeypatch, tmp_path, operation
):
    source = tmp_path / "a.txt"
    destination = tmp_path / "b.txt"
    source.write_text("original", encoding="utf-8")

    async def append_file(file_path: str, content: str):
        """Append content to a file."""
        with Path(file_path).open("a", encoding="utf-8") as stream:
            stream.write(content)
        return ToolChunk(
            state=ToolResultState.SUCCESS, content=[TextBlock(text="appended")]
        )

    async def move_file_or_dir(source_path: str, destination_path: str):
        """Move a file to its destination."""
        Path(source_path).rename(destination_path)
        return ToolChunk(
            state=ToolResultState.SUCCESS, content=[TextBlock(text="moved")]
        )

    arguments = (
        {"file_path": str(source), "content": " added"}
        if operation == "append_file"
        else {"source_path": str(source), "destination_path": str(destination)}
    )
    ctx = context(tmp_path, [FunctionTool(append_file), FunctionTool(move_file_or_dir)])
    lower = Model("local", [call(operation, arguments), text("done")])
    orchestrator = prepare(
        monkeypatch,
        ctx,
        lower,
        [
            route("device_tools", required_tools=[operation]),
            {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
        ],
    )
    await consume(orchestrator, ctx)
    assert DEVICE_TOOLS == {
        "read_file",
        "write_file",
        "edit_file",
        "append_file",
        "copy_file_or_dir",
        "move_file_or_dir",
        "create_directory",
    }
    assert orchestrator._trace.final_agent == "device"
    assert orchestrator._decision.await_count == 2
    if operation == "append_file":
        assert source.read_text(encoding="utf-8") == "original added"
    else:
        assert (
            not source.exists()
            and destination.read_text(encoding="utf-8") == "original"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,skill", [("edge_skill", ""), ("edge_tools", "")])
async def test_edge_without_one_skill_never_executes_local_tools(
    monkeypatch, tmp_path, kind, skill
):
    async def execute_shell_command(command: str):
        raise AssertionError("Standalone tools must not enter edge execution")

    ctx = context(tmp_path, [FunctionTool(execute_shell_command)])
    lower = Model("local")
    orchestrator = prepare(
        monkeypatch,
        ctx,
        lower,
        [route(kind, skill=skill, required_tools=["execute_shell_command"])],
    )
    await consume(orchestrator, ctx)
    assert not lower.requests
    assert orchestrator._trace.final_agent == "cloud"
    assert orchestrator._decision.await_count == 1
    assert (
        "edge_tools"
        not in RouterDecision.model_json_schema()["properties"]["task_type"]["enum"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("load_skill", [True, False])
async def test_edge_uses_existing_tools_after_reading_one_skill(
    monkeypatch, tmp_path, load_skill
):
    directory = tmp_path / "skills" / "report"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: report\ndescription: Fixed report\n---\nRead input using read_file and generate the fixed report.",
        encoding="utf-8",
    )
    reads = []
    surfaces = []

    async def read_file(file_path: str):
        """Read skill input."""
        reads.append(file_path)
        return ToolChunk(
            state=ToolResultState.SUCCESS,
            content=[TextBlock(text="report data")],
        )

    async def write_file(file_path: str, content: str):
        raise AssertionError("The skill did not ask for this operation")

    ctx = context(tmp_path, [FunctionTool(read_file), FunctionTool(write_file)])
    ctx.agent.toolkit = Toolkit(
        tools=ctx.agent.toolkit.tool_groups[0].tools,
        skills_or_loaders=[str(directory)],
    )

    class InspectingModel(Model):
        async def __call__(self, *args, **kwargs):
            surfaces.append(
                set(
                    await ctx.agent.toolkit._get_available_tools(
                        ctx.agent.state.tool_context.activated_groups
                    )
                )
            )
            return await super().__call__(*args, **kwargs)

    responses = [call("Skill", {"skill": "report"}, "skill-read")] if load_skill else []
    responses += [
        call("read_file", {"file_path": "input.txt"}),
        text("report completed"),
    ]
    lower = InspectingModel("local", responses)
    decisions = [route("edge_skill", skill="report", required_tools=["read_file"])]
    if load_skill:
        decisions.append({"protocol_version": "edge_cloud_verifier.v1", "passed": True})
    orchestrator = prepare(
        monkeypatch,
        ctx,
        lower,
        decisions,
    )
    await consume(orchestrator, ctx)
    assert all(surface == {"Skill", "read_file", "write_file"} for surface in surfaces)
    if load_skill:
        assert reads == ["input.txt"]
        assert orchestrator._trace.final_agent == "edge"
    else:
        assert reads == []
        assert orchestrator._trace.fallback_reason == "EDGE_SKILL_NOT_LOADED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_request", ["分析并修复仓库中的复杂错误", "刚才的报告不对，重新完成"]
)
async def test_cloud_route_for_complex_or_feedback_uses_no_local_model(
    monkeypatch, tmp_path, user_request
):
    ctx = context(tmp_path)
    ctx.input_msgs = [
        Msg(name="user", role="user", content=[TextBlock(text=user_request)])
    ]
    lower = Model("local")
    orchestrator = prepare(monkeypatch, ctx, lower, [route("complex_or_negative")])
    await consume(orchestrator, ctx)
    assert orchestrator._trace.final_agent == "cloud"
    assert not lower.requests and orchestrator._decision.await_count == 1


def existing_skill(root, name, body="Use the original workflow."):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Existing {name} workflow\n---\n{body}",
        encoding="utf-8",
    )
    return directory


@pytest.mark.asyncio
async def test_edge_discovers_pool_skill_and_preserves_existing_bindings(
    monkeypatch, tmp_path
):
    from qwenpaw.agents.tools.pool_skills import list_skills
    from qwenpaw.agents.skill_system.process_tool import (
        install_process_tool_skill_viewer,
    )

    pool = tmp_path / "pool"
    directory = existing_skill(pool, "report")
    # This optional file belongs to the existing driver framework; it is not
    # a new edge dependency declaration and must keep working unchanged.
    (directory / "process-tool.json").write_text(
        json.dumps(
            {
                "inputSchema": {
                    "type": "object",
                    "properties": {"report_id": {"type": "string"}},
                },
                "bindings": {
                    "report": {"source": "request_context", "key": "report_id"}
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "qwenpaw.agents.skill_system.store.get_skill_pool_dirs", lambda: [pool]
    )
    monkeypatch.setattr(
        "qwenpaw.agents.tools.pool_skills.get_skill_pool_dirs", lambda: [pool]
    )
    toolkit = Toolkit(tools=[FunctionTool(list_skills)])
    install_process_tool_skill_viewer(toolkit, {"report_id": "known-report"})
    toolkit.tool_groups[0].tools.append(toolkit.builtin_skill_viewer.tool)
    ctx = context(tmp_path)
    ctx.agent.toolkit = toolkit
    lower = Model(
        "local",
        [
            call("list_skills", {}, "discover"),
            call("Skill", {"skill": "report"}),
            text("done"),
        ],
    )
    orchestrator = prepare(
        monkeypatch,
        ctx,
        lower,
        [
            route("edge_skill"),  # No configured/selected skill is required up front.
            {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
        ],
    )
    await consume(orchestrator, ctx)
    assert orchestrator._trace.final_agent == "edge"
    assert orchestrator._journal.required_skill == "report"
    assert [record["tool"] for record in orchestrator._journal.records] == [
        "list_skills",
        "Skill",
    ]
    assert "known-report" in json.dumps(ctx.agent.state_dict(), ensure_ascii=False)
    assert not (directory / "edge-cloud.json").exists()


@pytest.mark.asyncio
async def test_second_skill_hands_off_before_its_body_is_exposed(monkeypatch, tmp_path):
    report = existing_skill(tmp_path / "skills", "report", "Report procedure")
    chart = existing_skill(tmp_path / "skills", "chart", "Chart private procedure")
    ctx = context(tmp_path)
    ctx.agent.toolkit = Toolkit(skills_or_loaders=[str(report), str(chart)])
    lower = Model(
        "local",
        [
            call("Skill", {"skill": "report"}, "first"),
            call("Skill", {"skill": "chart"}, "second"),
        ],
    )
    orchestrator = prepare(monkeypatch, ctx, lower, [route("edge_skill")])
    await consume(orchestrator, ctx)
    assert orchestrator._trace.fallback_reason == "MULTIPLE_SKILLS_REQUIRED"
    assert orchestrator._journal.required_skill == "report"
    assert len(orchestrator._journal.records) == 1
    assert "Chart private procedure" not in json.dumps(ctx.agent.state_dict())
    assert orchestrator._decision.await_count == 1


@pytest.mark.asyncio
async def test_edge_keeps_native_dynamic_tool_groups_and_permissions(tmp_path):
    from agentscope.tool import ToolGroup
    from agentscope.permission import PermissionBehavior, PermissionDecision

    directory = existing_skill(tmp_path, "report")

    async def scripts(value: str):
        """Run the existing skill's script."""
        return ToolChunk(state=ToolResultState.SUCCESS, content=[TextBlock(text=value)])

    async def chat_with_agent(to_agent: str, text: str):
        raise AssertionError("Delegation is not available in edge mode")

    tool = FunctionTool(scripts)
    denied = PermissionDecision(
        behavior=PermissionBehavior.DENY, message="Original policy"
    )
    tool.check_permissions = AsyncMock(return_value=denied)
    group = ToolGroup(
        name="driver",
        description="Existing driver tools",
        tools=[tool, FunctionTool(chat_with_agent)],
    )
    original = Toolkit(skills_or_loaders=[str(directory)], tool_groups=[group])
    journal = ExecutionJournal()
    edge = make_edge_toolkit(original, journal)
    initial = await edge._get_available_tools([])
    activated = await edge._get_available_tools(["driver"])
    assert "scripts" not in initial and "scripts" in activated
    assert "reset_tools" in initial
    assert "chat_with_agent" not in activated
    assert "chat_with_agent" in await original._get_available_tools(["driver"])
    assert (
        await activated["scripts"].tool.check_permissions({"value": "input"}, None)
        is denied
    )
    tool.check_permissions.assert_awaited_once_with({"value": "input"}, None)


def prepare_real_route(monkeypatch, ctx, lower, control):
    orchestrator = prepare(monkeypatch, ctx, lower, [])
    del orchestrator._decision
    from qwenpaw.observability.model_request_capture import _edge_cloud_context

    execution = ctx.agent.model

    class ConfiguredCloudModel:
        def __init__(self):
            self._inner = execution

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def __call__(self, messages, **kwargs):
            stage = (_edge_cloud_context.get() or {}).get("stage")
            model = control if stage in {"router", "verifier"} else self._inner
            return await model(messages, **kwargs)

    ctx.agent.model = ConfiguredCloudModel()
    return orchestrator


@pytest.mark.asyncio
@pytest.mark.parametrize("dependent", [False, True])
async def test_route_reads_real_skill_before_selecting_execution(
    monkeypatch, tmp_path, dependent
):
    body = (
        "Mandatory: delegate analysis to another Agent before generating the report."
        if dependent
        else "Generate the fixed report. Check its output format."
    )
    directory = existing_skill(tmp_path / "skills", "report", body)
    ctx = context(tmp_path)
    ctx.agent.toolkit = Toolkit(skills_or_loaders=[str(directory)])
    original_state = ctx.agent.state
    control_responses = [
        call("Skill", {"skill": "report"}, "route-inspection"),
        call(
            "submit_route_decision",
            route("complex_or_negative" if dependent else "edge_skill", skill="report"),
            "route-submit",
        ),
    ]
    if not dependent:
        control_responses.append(call("Skill", {"skill": "report"}, "verify-skill"))
        control_responses.append(
            call(
                "submit_verification",
                {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
                "validation-submit",
            )
        )
    control = Model("cloud-control", control_responses)
    lower = Model("local", [call("Skill", {"skill": "report"}), text("done")])
    orchestrator = prepare_real_route(monkeypatch, ctx, lower, control)
    await consume(orchestrator, ctx)
    assert orchestrator._trace.final_agent == ("cloud" if dependent else "edge")
    assert bool(lower.requests) == (not dependent)
    assert body in json.dumps(
        [m.model_dump(mode="json") for m in control.requests[1][0]]
    )
    tool_names = {t["function"]["name"] for t in control.requests[0][1]}
    assert tool_names == {"Skill", "submit_route_decision"}
    assert len(control.requests[0][0]) == 2
    assert len(control.requests[1][0]) == 4
    assert ctx.agent.state is original_state
    saved = json.dumps(ctx.agent.state_dict())
    assert "route-inspection" not in saved and "route-submit" not in saved
    captures = pop_model_requests_for_session(ctx.session_id)
    assert len(captures) == (1 if dependent else 2)
    assert all(item["messages"][0]["content"] == "DEFAULT SYSTEM" for item in captures)


@pytest.mark.asyncio
async def test_v2_payload_captures_real_router_and_verifier_requests(
    monkeypatch, tmp_path
):
    """The control stages must be captured through the real model wrapper."""

    directory = existing_skill(
        tmp_path / "skills", "report", "Generate the fixed report."
    )
    ctx = context(tmp_path)
    ctx.agent.toolkit = Toolkit(skills_or_loaders=[str(directory)])
    control = Model(
        "cloud-control",
        [
            call("Skill", {"skill": "report"}, "route-inspection"),
            call(
                "submit_route_decision",
                route("edge_skill", skill="report"),
                "route-submit",
            ),
            call("Skill", {"skill": "report"}, "verify-skill"),
            call(
                "submit_verification",
                {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
                "validation-submit",
            ),
        ],
    )
    lower = Model("local", [call("Skill", {"skill": "report"}), text("done")])
    orchestrator = prepare_real_route(monkeypatch, ctx, lower, control)
    await consume(orchestrator, ctx)

    payload = _build_session_delta_payload(
        ctx, ctx.agent.state_dict(), user_id="u", channel="console"
    )
    assert payload["protocol"] == "edge_cloud_trace.v2"
    models = {item["agent"]: item for item in payload["model_requests"]}
    assert models["RouterAgent"]["config_ref"] == {
        "prompt": "router",
        "tools": "router",
    }
    assert models["VerifyAgent"]["config_ref"] == {
        "prompt": "verifier",
        "tools": "verifier",
    }
    configs = {
        (config["config_key"], config["record_type"]): config
        for config in payload["configs"]
    }
    assert "RouterAgent" in configs[("router", "config.prompt")]["system_prompt"]
    assert "VerifyAgent" in configs[("verifier", "config.prompt")]["system_prompt"]
    router_tools = {
        tool["function"]["name"]
        for tool in configs[("router", "config.tools")]["tools"]
    }
    assert router_tools == {"Skill", "submit_route_decision"}
    verifier_tools = {
        tool["function"]["name"]
        for tool in configs[("verifier", "config.tools")]["tools"]
    }
    assert verifier_tools == {"Skill", "submit_verification"}


def test_non_edge_cloud_agents_keep_legacy_payload(tmp_path):
    """Requests without the orchestrator overlay must keep the v1 shape."""

    ctx = SimpleNamespace(
        agent_id="legacy-agent",
        root_agent_id="legacy-agent",
        session_id="legacy-session",
        root_session_id="legacy-session",
        workspace_dir=str(tmp_path),
        extras={},
    )
    state = {
        "state": {
            "context": [
                {"id": "m1", "role": "user", "content": "hello"},
                {"id": "m2", "role": "assistant", "content": "hi"},
            ]
        }
    }

    payload = _build_session_delta_payload(
        ctx, state, user_id="u", channel="console"
    )

    assert payload is not None
    assert "protocol" not in payload
    assert "edge_cloud_upload" not in payload
    assert "stage." not in json.dumps(list(payload))
    assert [message["id"] for message in payload["delta"]] == ["m1", "m2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("passed", [False, True])
async def test_verifier_verdict_is_final_without_runtime_skill_inspection_veto(
    monkeypatch, tmp_path, passed,
):
    directory = existing_skill(tmp_path, "report")
    ctx = context(tmp_path)
    ctx.agent.toolkit = Toolkit(skills_or_loaders=[str(directory)])
    control = Model(
        "control",
        [
            call("Skill", {"skill": "report"}, "route-inspection"),
            call(
                "submit_route_decision",
                route("edge_skill", skill="report"),
                "route-submit",
            ),
            call(
                "submit_verification",
                {"protocol_version": "edge_cloud_verifier.v1", "passed": passed},
                "validation-submit",
            ),
        ],
    )
    lower = Model("local", [call("Skill", {"skill": "report"}), text("done")])
    orchestrator = prepare_real_route(monkeypatch, ctx, lower, control)
    await consume(orchestrator, ctx)
    assert orchestrator._trace.final_agent == ("edge" if passed else "cloud")
    assert orchestrator._trace.fallback is not passed
    assert len(control.requests) == 3
    assert orchestrator._stage_outcomes["verifier"]["accepted"] is passed
    tools = {tool["function"]["name"] for tool in control.requests[-1][1]}
    assert tools == {"Skill", "submit_verification"}


@pytest.mark.asyncio
async def test_verifier_text_answer_falls_back_without_retry(monkeypatch, tmp_path):
    async def read_file(file_path: str):
        return ToolChunk(content=[TextBlock(text="CURRENT-EVIDENCE: 42")])

    ctx = context(tmp_path, [FunctionTool(read_file)])
    lower = Model(
        "local",
        [
            call("read_file", {"file_path": "input.txt"}),
            text("The value is 42."),
            text("cloud completion"),
        ],
    )
    control = Model(
        "control",
        [
            call("submit_route_decision", route("device_tools")),
            text("passed without submitting"),
        ],
    )
    orchestrator = prepare_real_route(monkeypatch, ctx, lower, control)
    await consume(orchestrator, ctx)
    assert len(control.requests) == 2
    assert control.tool_choices[-1].mode == "auto"
    assert orchestrator._trace.fallback
    assert orchestrator._trace.final_agent == "cloud"
    assert orchestrator._trace.fallback_reason == "VALIDATION_UNCERTAIN"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inspection,selected",
    [(None, "report"), ("report", "other"), ("missing", "missing")],
)
async def test_unread_or_failed_skill_inspection_cannot_route_to_edge(
    monkeypatch, tmp_path, inspection, selected
):
    directory = existing_skill(tmp_path / "skills", "report")
    ctx = context(tmp_path)
    ctx.agent.toolkit = Toolkit(skills_or_loaders=[str(directory)])
    responses = []
    if inspection:
        responses.append(call("Skill", {"skill": inspection}))
    responses.append(call("submit_route_decision", route("edge_skill", skill=selected)))
    control = Model("control", responses)
    lower = Model("local")
    orchestrator = prepare_real_route(monkeypatch, ctx, lower, control)
    await consume(orchestrator, ctx)
    assert not lower.requests and orchestrator._trace.final_agent == "cloud"
    if inspection == "missing":
        result = control.requests[-1][0][-1].content[0]
        assert result.state == ToolResultState.ERROR and "not found" in result.output


@pytest.mark.asyncio
async def test_router_discovery_does_not_activate_pool_process_bindings(
    monkeypatch, tmp_path
):
    from qwenpaw.agents.tools.pool_skills import list_skills
    from qwenpaw.agents.skill_system.process_tool import (
        active_process_tool_specs,
        clear_active_process_tool_specs,
        install_process_tool_skill_viewer,
    )

    pool = tmp_path / "pool"
    directory = existing_skill(pool, "report", "First call an analysis Agent.")
    (directory / "process-tool.json").write_text(
        json.dumps(
            {"inputSchema": {"type": "object", "properties": {}}, "bindings": {}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "qwenpaw.agents.skill_system.store.get_skill_pool_dirs", lambda: [pool]
    )
    monkeypatch.setattr(
        "qwenpaw.agents.tools.pool_skills.get_skill_pool_dirs", lambda: [pool]
    )
    toolkit = Toolkit(tools=[FunctionTool(list_skills)])
    install_process_tool_skill_viewer(toolkit, {})
    toolkit.tool_groups[0].tools.append(toolkit.builtin_skill_viewer.tool)
    ctx = context(tmp_path)
    ctx.agent.toolkit = toolkit
    control = Model(
        "control",
        [
            call("list_skills", {"offset": 0, "limit": 1}, "route-discover"),
            call("Skill", {"skill": "report"}, "route-inspect"),
            call("submit_route_decision", route("complex_or_negative")),
        ],
    )
    lower = Model("local")
    clear_active_process_tool_specs()
    try:
        orchestrator = prepare_real_route(monkeypatch, ctx, lower, control)
        await consume(orchestrator, ctx)
        assert not active_process_tool_specs()
        assert not lower.requests and orchestrator._trace.final_agent == "cloud"
        result = control.requests[1][0][-1].content[0]
        assert result.state == ToolResultState.SUCCESS, result.output
        assert "report" in result.output[0].text
        assert "analysis Agent" in control.requests[2][0][-1].content[0].output[0].text
        saved = json.dumps(ctx.agent.state_dict())
        assert "route-discover" not in saved and "route-inspect" not in saved
    finally:
        clear_active_process_tool_specs()


@pytest.mark.asyncio
async def test_router_skill_reader_honors_original_permissions(tmp_path):
    from agentscope.permission import PermissionBehavior, PermissionDecision

    directory = existing_skill(tmp_path, "report", "Confidential procedure")
    toolkit = Toolkit(skills_or_loaders=[str(directory)])
    viewer = toolkit.builtin_skill_viewer.tool
    viewer.check_permissions = AsyncMock(
        return_value=PermissionDecision(
            behavior=PermissionBehavior.DENY,
            message="Original permission denial",
        )
    )
    reader = RouteSkillReader(toolkit, viewer, [])
    ctx = context(tmp_path)
    model = Model(
        "control",
        [
            call("Skill", {"skill": "report"}),
            call("submit_route_decision", route("complex_or_negative")),
        ],
    )
    await consume_decision_call(
        model, [], stage="router", read_tools={"Skill": reader}, state=ctx.agent.state
    )
    assert not reader.inspected
    output = model.requests[1][0][-1].content[0]
    assert output.state == ToolResultState.ERROR
    assert "Original permission denial" in output.output
    assert "Confidential procedure" not in str(model.requests)


@pytest.mark.asyncio
async def test_router_redacts_new_skill_tool_results_before_next_model_call(tmp_path):
    directory = existing_skill(tmp_path, "report", "private-marker")
    toolkit = Toolkit(skills_or_loaders=[str(directory)])
    reader = RouteSkillReader(toolkit, toolkit.builtin_skill_viewer.tool, [])
    ctx = context(tmp_path)

    async def redact(messages):
        result = []
        for message in messages:
            copied = message.model_copy(deep=True)
            for block in copied.content:
                if getattr(block, "type", None) == "tool_result":
                    for item in block.output:
                        item.text = item.text.replace("private-marker", "[redacted]")
            result.append(copied)
        return result

    model = Model(
        "control",
        [
            call("Skill", {"skill": "report"}),
            call("submit_route_decision", route("edge_skill", skill="report")),
        ],
    )
    await consume_decision_call(
        model,
        [],
        stage="router",
        read_tools={"Skill": reader},
        state=ctx.agent.state,
        prepare_messages=redact,
    )
    sent = json.dumps([m.model_dump(mode="json") for m in model.requests[1][0]])
    assert "private-marker" not in sent and "[redacted]" in sent


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["business_tool", "mixed_submit", "exhausted"])
async def test_invalid_router_calls_fall_back_without_business_execution(
    monkeypatch, tmp_path, mode
):
    directory = existing_skill(tmp_path / "skills", "report")
    writes = []

    async def write_file(file_path: str, content: str):
        writes.append(file_path)

    ctx = context(tmp_path)
    ctx.agent.toolkit = Toolkit(
        tools=[FunctionTool(write_file)], skills_or_loaders=[str(directory)]
    )
    if mode == "business_tool":
        responses = [call("write_file", {"file_path": "x", "content": "x"})]
    elif mode == "mixed_submit":
        response = call("Skill", {"skill": "report"})
        response.content.extend(
            call(
                "submit_route_decision", route("edge_skill", skill="report"), "submit"
            ).content
        )
        responses = [response]
    else:
        responses = [call("Skill", {"skill": "report"}, f"read-{i}") for i in range(13)]
    lower = Model("local")
    orchestrator = prepare_real_route(
        monkeypatch, ctx, lower, Model("control", responses)
    )
    await consume(orchestrator, ctx)
    assert not writes and not lower.requests
    assert orchestrator._trace.final_agent == "cloud"


@pytest.mark.asyncio
async def test_edge_cannot_replace_route_selected_skill(monkeypatch, tmp_path):
    report = existing_skill(tmp_path / "skills", "report")
    other = existing_skill(tmp_path / "skills", "other", "Unselected workflow")
    ctx = context(tmp_path)
    ctx.agent.toolkit = Toolkit(skills_or_loaders=[str(report), str(other)])
    lower = Model("local", [call("Skill", {"skill": "other"})])
    orchestrator = prepare(
        monkeypatch, ctx, lower, [route("edge_skill", skill="report")]
    )
    await consume(orchestrator, ctx)
    assert orchestrator._trace.fallback_reason == "EDGE_SKILL_MISMATCH"
    assert not orchestrator._journal.records
    assert "Unselected workflow" not in json.dumps(ctx.agent.state_dict())


@pytest.mark.asyncio
async def test_router_preserves_streaming_envelopes_and_configured_generation(tmp_path):
    directory = existing_skill(tmp_path, "report")
    toolkit = Toolkit(skills_or_loaders=[str(directory)])
    reader = RouteSkillReader(toolkit, toolkit.builtin_skill_viewer.tool, [])
    ctx = context(tmp_path)
    submitted = route("edge_skill", skill="report")
    requests = []

    async def model(messages, **kwargs):
        requests.append((list(messages), dict(kwargs)))
        if len(requests) == 1:

            async def stream():
                yield {"content": [], "is_last": False}
                yield call("Skill", {"skill": "report"}, "stream-inspect")

            return stream()
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "final",
                                "function": {
                                    "name": "submit_route_decision",
                                    "arguments": json.dumps(submitted),
                                },
                            }
                        ]
                    }
                }
            ]
        }

    result = await consume_decision_call(
        model,
        [],
        stage="router",
        read_tools={"Skill": reader},
        state=ctx.agent.state,
    )
    assert result == submitted and reader.inspected == {"report"}
    assert all(
        "extra_body" not in kwargs and "disable_thinking" not in kwargs
        for _, kwargs in requests
    )
    block = requests[1][0][-1].content[0]
    assert block.id == "stream-inspect" and block.state == ToolResultState.SUCCESS


@pytest.mark.asyncio
async def test_cancel_during_router_inspection_never_starts_execution(
    monkeypatch, tmp_path
):
    directory = existing_skill(tmp_path, "report")
    ctx = context(tmp_path)
    ctx.agent.toolkit = Toolkit(skills_or_loaders=[str(directory)])
    started = asyncio.Event()

    class WaitingControl(Model):
        async def __call__(self, *args, **kwargs):
            if not self.requests:
                return await super().__call__(*args, **kwargs)
            started.set()
            await asyncio.Event().wait()

    control = WaitingControl("control", [call("Skill", {"skill": "report"})])
    lower = Model("local")
    orchestrator = prepare_real_route(monkeypatch, ctx, lower, control)
    task = asyncio.create_task(consume(orchestrator, ctx))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not lower.requests and not ctx.agent.model.requests
    assert not ctx.agent.state.context
    assert ctx.agent._system_prompt == "DEFAULT SYSTEM"


@pytest.mark.asyncio
async def test_router_cannot_read_pool_when_original_viewer_disallows_it(
    monkeypatch, tmp_path
):
    from qwenpaw.agents.skill_system.process_tool import (
        install_process_tool_skill_viewer,
    )

    pool = tmp_path / "pool"
    existing_skill(pool, "report", "Pool-only instructions")
    monkeypatch.setattr(
        "qwenpaw.agents.skill_system.store.get_skill_pool_dirs", lambda: [pool]
    )
    toolkit = Toolkit()
    install_process_tool_skill_viewer(toolkit, {}, allow_pool_skills=False)
    reader = RouteSkillReader(toolkit, toolkit.builtin_skill_viewer.tool, [])
    with pytest.raises(ValueError, match="not found"):
        await reader.call(skill="report")
    assert not reader.inspected


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["device", "edge"])
@pytest.mark.parametrize("handoff", [False, True])
async def test_lower_receives_complete_task_and_cloud_keeps_original_context(
    monkeypatch, tmp_path, tier, handoff
):
    original = "RAW-LATEST: use the previously agreed path and report format"
    old = Msg(
        name="user",
        role="user",
        content=[TextBlock(text="OLD-PRIVATE: irrelevant previous conversation")],
    )
    prior_answer = Msg(
        name="default",
        role="assistant",
        content=[TextBlock(text="OLD-ANSWER: unrelated")],
    )
    task = "Read input.csv, then produce the agreed fixed report at output/report.txt with two columns."

    async def read_file(file_path: str):
        return ToolChunk(
            content=[TextBlock(text="CURRENT-RESULT: verified input data")]
        )

    ctx = context(tmp_path, [FunctionTool(read_file)])
    ctx.agent.state.context = [old, prior_answer]
    ctx.agent.state.summary = "OLD-SUMMARY: historical facts for the cloud only"
    ctx.input_msgs = [Msg(name="user", role="user", content=[TextBlock(text=original)])]
    if tier == "edge":
        directory = existing_skill(tmp_path, "report")
        ctx.agent.toolkit = Toolkit(
            tools=[FunctionTool(read_file)], skills_or_loaders=[str(directory)]
        )
    responses = [call("Skill", {"skill": "report"}, "load")] if tier == "edge" else []
    responses.extend(
        [call("read_file", {"file_path": "input.csv"}),
         LocalExecutionHandoff("DEVICE_TOOL_ROUND_LIMIT") if handoff else text("draft finished")]
    )
    lower = Model("local", responses)
    orchestrator = prepare(
        monkeypatch,
        ctx,
        lower,
        [
            route(
                "edge_skill" if tier == "edge" else "device_tools",
                skill="report" if tier == "edge" else "",
                execution_task=task,
            ),
            {
                "protocol_version": "edge_cloud_verifier.v1",
                "passed": False,
                "failure_reason": "report missing",
            },
        ],
    )
    agent, manager = ctx.agent, ctx.agent._context_manager
    cloud = agent.model
    await consume(orchestrator, ctx)
    for messages, _ in lower.requests:
        sent = json.dumps([m.model_dump(mode="json") for m in messages])
        assert task in sent
        assert not any(
            value in sent
            for value in [original, "OLD-PRIVATE", "OLD-ANSWER", "OLD-SUMMARY"]
        )
    final_input = json.dumps([m.model_dump(mode="json") for m in lower.requests[-1][0]])
    assert "CURRENT-RESULT" in final_input and "read_file" in final_input
    cloud_input = json.dumps([m.model_dump(mode="json") for m in cloud.requests[0][0]])
    assert all(
        value in cloud_input
        for value in [original, "OLD-PRIVATE", "OLD-ANSWER", "OLD-SUMMARY",
                      "CURRENT-RESULT", "read_file", task]
    )
    assert {"device": "DeviceAgent", "edge": "EdgeAgent"}[tier] in cloud_input
    assert "证据：trace://" in json.dumps(
        cloud.requests[0][0][-1].model_dump(mode="json"), ensure_ascii=False
    )
    saved = json.dumps(agent.state_dict())
    # The handoff carries the task book; the original request is already in
    # context as the current user turn and must not be duplicated.
    assert saved.count(original) == 1
    assert task in saved
    assert "call-1" in saved and "CURRENT-RESULT" in saved
    assert ctx.agent is agent and agent._context_manager is manager
    payload = orchestrator._decision.await_args_list[0].args[-1]
    assert (
        "OLD-PRIVATE" in payload["history"]
        and "OLD-SUMMARY" in payload["history_summary"]
    )
    assert original in str(payload["request_messages"])
    if handoff:
        assert orchestrator._decision.await_count == 1
        return
    verification = orchestrator._decision.await_args_list[1].args[-1]
    assert verification["execution_task"] == task
    assert verification["final_answer"] == "draft finished"
    assert not {"request", "history", "history_summary"} & verification.keys()
    assert not any(
        value in json.dumps(verification)
        for value in [original, "OLD-PRIVATE", "OLD-ANSWER", "OLD-SUMMARY"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["device", "edge"])
async def test_verifier_receives_task_answer_and_evidence_without_session_history(
    monkeypatch, tmp_path, tier
):
    async def read_file(file_path: str):
        return ToolChunk(content=[TextBlock(text="CURRENT-EVIDENCE: 42")])

    task = "Read input.txt and report its value exactly."
    conditions = ["Report the value read from input.txt."]
    skill_body = "Read input.txt with read_file and report its exact value."
    ctx = context(tmp_path, [FunctionTool(read_file)])
    ctx.agent.state.context = [
        Msg(
            name="user", role="user",
            content=[TextBlock(text="OLD-PRIVATE: previous task")],
        ),
        Msg(
            name="default", role="assistant",
            content=[TextBlock(text="OLD-ANSWER: previous result")],
        ),
    ]
    ctx.agent.state.summary = "OLD-SUMMARY: earlier requirements"
    ctx.input_msgs = [
        Msg(
            name="user", role="user",
            content=[TextBlock(text="RAW-LATEST: use the earlier file")],
        )
    ]
    responses = []
    decisions = []
    if tier == "edge":
        directory = existing_skill(tmp_path, "report", skill_body)
        (directory / "references").mkdir()
        (directory / "references" / "format.md").write_text(
            "Use the fixed format.", encoding="utf-8"
        )
        (directory / "scripts").mkdir()
        (directory / "scripts" / "run.py").write_text(
            "print('ok')", encoding="utf-8"
        )
        ctx.agent.toolkit = Toolkit(
            tools=[FunctionTool(read_file)], skills_or_loaders=[str(directory)]
        )
        decisions.append(call("Skill", {"skill": "report"}, "inspect-skill"))
        responses.append(call("Skill", {"skill": "report"}, "load-skill"))
    responses.extend(
        [
            ChatResponse(
                content=[
                    TextBlock(text="PROGRESS-ONLY: checking the file"),
                    ToolCallBlock(
                        id="read-input",
                        name="read_file",
                        input=json.dumps({"file_path": "input.txt"}),
                    ),
                ],
                is_last=True,
            ),
            ChatResponse(
                content=[
                    ThinkingBlock(thinking="PRIVATE-CURRENT-REASONING"),
                    TextBlock(text="The value in input.txt is 42."),
                ],
                is_last=True,
            ),
        ]
    )
    decisions.append(
        call(
            "submit_route_decision",
            route(
                "edge_skill" if tier == "edge" else "device_tools",
                execution_task=task,
                completion_conditions=conditions,
                skill="report" if tier == "edge" else "",
            ),
        )
    )
    if tier == "edge":
        decisions.append(call("Skill", {"skill": "report"}, "verify-skill"))
    decisions.append(
        call(
            "submit_verification",
            {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
        )
    )
    control = Model("control", decisions)
    orchestrator = prepare_real_route(
        monkeypatch, ctx, Model("local", responses), control
    )
    await consume(orchestrator, ctx)
    messages, tools = control.requests[-1]
    payload = json.loads(messages[1].get_text_content())
    assert set(payload) == {
        "execution_task", "completion_conditions", "final_answer",
        "executed_by", "skill", "tool_results",
    }
    assert payload["execution_task"] == task
    assert payload["completion_conditions"] == conditions
    assert payload["final_answer"] == "The value in input.txt is 42."
    assert payload["executed_by"] == tier
    if tier == "edge":
        skill = payload["skill"]
        assert skill["name"] == "report"
        assert skill["source"] == "registered"
        assert skill["directory"] == str(directory)
        assert skill["instructions_path"] == str(directory / "SKILL.md")
        assert skill["files"] == ["references/format.md", "scripts/run.py"]
        assert skill["files_truncated"] is False
        assert skill_body in skill["instructions"]
        skill_record = next(
            item for item in payload["tool_results"] if item["tool"] == "Skill"
        )
        assert skill["evidence_call_seq"] == skill_record["call_seq"]
        assert skill_record["skill_evidence_ref"] == "skill.instructions"
        assert skill_body not in json.dumps(skill_record["result"])
    else:
        assert payload["skill"] == {}
    result = next(r for r in payload["tool_results"] if r["tool"] == "read_file")
    assert result["arguments"] == {"file_path": "input.txt"}
    assert result["status"] == "success"
    assert "CURRENT-EVIDENCE: 42" in json.dumps(result["result"])
    wire = json.dumps([m.model_dump(mode="json") for m in messages])
    assert not any(
        marker in wire
        for marker in (
            "OLD-PRIVATE", "OLD-ANSWER", "OLD-SUMMARY", "RAW-LATEST",
            "PROGRESS-ONLY", "PRIVATE-CURRENT-REASONING",
        )
    )
    assert {tool["function"]["name"] for tool in tools} == (
        {"Skill", "submit_verification"}
        if tier == "edge"
        else {"submit_verification"}
    )
    assert control.tool_choices[-1].mode == "auto"
    if tier == "edge":
        assert any(message.name == "verifier_tools" for message in messages)
        assert wire.count(skill_body) >= 2
    else:
        assert all(message.name != "verifier_tools" for message in messages)
    assert orchestrator._trace.final_agent == tier
    assert not orchestrator._trace.fallback


@pytest.mark.asyncio
async def test_next_lower_request_does_not_receive_previous_reply(
    monkeypatch, tmp_path
):
    ctx = context(tmp_path)
    first = Model("local", [text("PREVIOUS-LOWER-ANSWER")])
    await consume(
        prepare(monkeypatch, ctx, first, [route(execution_task="FIRST-COMPLETE-TASK")]),
        ctx,
    )
    ctx.input_msgs = [
        Msg(name="user", role="user", content=[TextBlock(text="RAW-SECOND-QUESTION")])
    ]
    second = Model("local", [text("second answer")])
    await consume(
        prepare(
            monkeypatch, ctx, second, [route(execution_task="SECOND-COMPLETE-TASK")]
        ),
        ctx,
    )
    sent = json.dumps([m.model_dump(mode="json") for m in second.requests[0][0]])
    assert "SECOND-COMPLETE-TASK" in sent
    assert not any(
        value in sent
        for value in [
            "FIRST-COMPLETE-TASK",
            "PREVIOUS-LOWER-ANSWER",
            "RAW-SECOND-QUESTION",
        ]
    )
    assert "PREVIOUS-LOWER-ANSWER" in json.dumps(ctx.agent.state_dict())


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["device_text", "edge_skill"])
async def test_missing_complete_task_uses_cloud_instead_of_raw_lower_input(
    monkeypatch, tmp_path, kind
):
    ctx = context(tmp_path)
    lower = Model("local")
    orchestrator = prepare(monkeypatch, ctx, lower, [route(kind, execution_task="   ")])
    await consume(orchestrator, ctx)
    assert not lower.requests and orchestrator._trace.final_agent == "cloud"
    sent = json.dumps(
        [m.model_dump(mode="json") for m in ctx.agent.model.requests[0][0]]
    )
    assert "complete the file task" in sent


@pytest.mark.asyncio
async def test_route_receives_available_history_without_character_truncation(
    monkeypatch, tmp_path
):
    ctx = context(tmp_path)
    ctx.agent.state.context = [
        Msg(
            name="user",
            role="user",
            content=[TextBlock(text="IMPORTANT-EARLY-FACT " + "x" * 14000)],
        )
    ]
    orchestrator = prepare(
        monkeypatch, ctx, Model("local"), [route("complex_or_negative")]
    )
    await consume(orchestrator, ctx)
    history = orchestrator._decision.await_args.args[-1]["history"]
    assert "IMPORTANT-EARLY-FACT" in history and json.loads(history)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "recall",
        "recall_history",
        "recall_history_python",
        "get_historical_tool_result",
        "memory_search",
        "material_skill",
        "materialize_skill",
    ],
)
async def test_edge_filters_history_and_skill_authoring_tools_after_activation(
    tmp_path, name
):
    from agentscope.tool import ToolGroup

    async def forbidden():
        raise AssertionError("This tool cannot enter edge execution")

    forbidden.__name__ = name
    tool = FunctionTool(forbidden)
    original = Toolkit(
        tool_groups=[ToolGroup(name="extra", description="Extra tools", tools=[tool])]
    )
    edge = make_edge_toolkit(original, ExecutionJournal())
    assert name not in await edge._get_available_tools([])
    assert name not in await edge._get_available_tools(["extra"])
    assert name in await original._get_available_tools(["extra"])


@pytest.mark.asyncio
async def test_execution_counting_and_dispatch_share_task_view_and_auxiliary_history_stays_cloud(
    tmp_path,
):
    from agentscope.message import ToolResultBlock

    ctx = context(tmp_path)
    reply = ctx.agent.state.reply_id
    task = Msg(
        name="execution_task", role="user", content=[TextBlock(text="COMPLETE-TASK")]
    )
    system = Msg(name="system", role="system", content=[TextBlock(text="SYSTEM")])
    old = Msg(name="user", role="user", content=[TextBlock(text="PRIVATE-HISTORY")])
    index = Msg(
        name="memory", role="user", content=[TextBlock(text="PRIVATE-SCROLL-INDEX")]
    )
    current = Msg(
        id=reply,
        name="default",
        role="assistant",
        content=[
            ToolCallBlock(id="read", name="read_file", input="{}"),
            ToolResultBlock(id="read", name="read_file", output="OWN-TOOL-RESULT"),
        ],
    )
    lower = Model("local")
    counts = []

    async def count(messages, tools):
        counts.append([m.model_dump(mode="json") for m in messages])
        return 100

    lower.count_tokens = count
    cloud = Model("cloud")
    cloud.generate_structured_output = AsyncMock(return_value="summary")
    model = ExecutionModel(
        lower,
        ExecutionJournal(),
        task_view=TaskInputView(ctx.agent, task),
        cloud_model=cloud,
    )
    full = [system, old, index, current]
    await model.count_tokens(messages=full, tools=[])
    await model(messages=full, tools=[])
    sent = [m.model_dump(mode="json") for m in lower.requests[0][0]]
    assert counts[0] == sent
    assert "OWN-TOOL-RESULT" in str(sent) and "COMPLETE-TASK" in str(sent)
    assert "PRIVATE-HISTORY" not in str(sent) and "PRIVATE-SCROLL-INDEX" not in str(
        sent
    )
    auxiliary = [system, old]
    await model(auxiliary)  # Scroll headline generation uses its existing call shape.
    await model.generate_structured_output(
        messages=auxiliary, structured_model=object()
    )
    assert len(lower.requests) == 1
    assert cloud.requests[0][0] == auxiliary
    assert cloud.generate_structured_output.await_args.kwargs["messages"] == auxiliary


@pytest.mark.asyncio
@pytest.mark.parametrize("summarize_history", [False, True])
@pytest.mark.parametrize("handoff", [False, True])
async def test_real_scroll_pressure_keeps_hooks_and_lower_task_isolation(
    monkeypatch, tmp_path, summarize_history, handoff
):
    from qwenpaw.agents.react_agent import QwenPawAgent
    from qwenpaw.agents.context.scroll.history import HistoryStore
    from qwenpaw.agents.context.scroll.manager import ScrollContextManager
    from qwenpaw.agents.context.budget_middleware import RequestBudgetMiddleware
    from qwenpaw.config.config import AgentProfileConfig, ModelSlotConfig
    from qwenpaw.loop.gates import StopAction, StopHandlerResult

    history = HistoryStore(tmp_path / "history.db")
    manager = ScrollContextManager(
        history=history, session_id="edge-test", summarize_unheadlined=summarize_history
    )
    compressed = []
    original_compress = manager.compress

    async def compress(*args, **kwargs):
        await original_compress(*args, **kwargs)
        compressed.append(dict(manager.last_compress))

    manager.compress = AsyncMock(side_effect=compress)
    observations = []
    auxiliary = []
    lower_counts = []

    class CountingModel(Model):
        async def count_tokens(self, messages, tools=None):
            value = json.dumps([m.model_dump(mode="json") for m in messages])
            if self.model == "local":
                lower_counts.append(value)
            return len(value) + len(json.dumps(tools or []))

        async def __call__(self, messages, **kwargs):
            if kwargs.get("tools") is None:
                auxiliary.append(
                    (
                        self.model,
                        json.dumps([m.model_dump(mode="json") for m in messages]),
                    )
                )
                return await super().__call__(messages, **kwargs)
            observations.append(
                (
                    self.model,
                    manager.compress.await_count,
                    json.dumps([m.model_dump(mode="json") for m in messages]),
                )
            )
            return await super().__call__(messages, **kwargs)

    cloud = CountingModel(
        "cloud",
        ([text("1. Previous task context")] if summarize_history else [])
        + [text("completed by cloud")],
    )
    cloud.context_size = 60000
    lower = CountingModel(
        "local",
        [
            call("read_file", {"file_path": "large"}, "large-call"),
            call("read_file", {"file_path": "small"}, "small-call"),
            LocalExecutionHandoff("DEVICE_TOOL_ROUND_LIMIT") if handoff else text("draft"),
        ],
    )
    lower.context_size = 20000

    async def read_file(file_path: str):
        value = "BIG-RESULT " + "x" * 14000 if file_path == "large" else "SMALL-RESULT"
        return ToolChunk(content=[TextBlock(text=value)])

    profile = AgentProfileConfig(
        id="default",
        name="default",
        active_model=ModelSlotConfig(provider_id="test", model="cloud"),
    )
    agent = QwenPawAgent(
        name="default",
        model=cloud,
        system_prompt="DEFAULT SYSTEM",
        toolkit=Toolkit(tools=[FunctionTool(read_file)]),
        react_config=ReActConfig(max_iters=10),
        middlewares=[RequestBudgetMiddleware(manager)],
        agent_config=profile,
        workspace_dir=tmp_path,
        context_manager=manager,
    )
    monkeypatch.setattr(
        agent,
        "_run_stop_handlers",
        AsyncMock(return_value=StopHandlerResult(action=StopAction.TERMINATE)),
    )
    monkeypatch.setattr(agent, "_model_rejects_media", lambda: False)
    agent.state.context = [
        Msg(
            name="user",
            role="user",
            content=[TextBlock(text="ORIGINAL-HISTORY " + "y" * 6000)],
        ),
        Msg(
            name="default", role="assistant", content=[TextBlock(text="ORIGINAL-REPLY")]
        ),
    ]
    ctx = context(tmp_path)
    ctx.agent, ctx.agent_config = agent, profile
    ctx.input_msgs = [
        Msg(name="user", role="user", content=[TextBlock(text="RAW-LATEST-REQUEST")])
    ]
    orchestrator = prepare(
        monkeypatch,
        ctx,
        lower,
        [
            route(
                "device_tools",
                execution_task="Read large and small input files, then finish the fixed task.",
            ),
            {"protocol_version": "edge_cloud_verifier.v1", "passed": False},
        ],
    )
    try:
        await consume(orchestrator, ctx)
        assert len(observations) == 4
        assert all(checks >= i + 1 for i, (_, checks, _) in enumerate(observations))
        assert any(item["evicted"] or item["folded"] for item in compressed), compressed
        assert manager._persisted_tcids == {"large-call", "small-call"}
        assert agent._context_manager is manager and ctx.agent is agent
        if not handoff:
            verification = orchestrator._decision.await_args_list[1].args[-1]
            assert verification["final_answer"] == "draft"
            assert "BIG-RESULT" in json.dumps(verification["tool_results"])
            assert "SMALL-RESULT" in json.dumps(verification["tool_results"])
            assert not any(
                marker in json.dumps(verification)
                for marker in (
                    "ORIGINAL-HISTORY", "ORIGINAL-REPLY", "RAW-LATEST-REQUEST",
                    "context compressed", "1. Previous task context",
                )
            )
        for model, _, wire in observations:
            if model == "local":
                assert not any(
                    value in wire
                    for value in [
                        "ORIGINAL-HISTORY",
                        "ORIGINAL-REPLY",
                        "RAW-LATEST-REQUEST",
                        "context compressed",
                    ]
                )
        assert all(
            "ORIGINAL-HISTORY" not in counted and "RAW-LATEST-REQUEST" not in counted
            for counted in lower_counts
        )
        assert observations[-1][0] == "cloud"
        assert "RAW-LATEST-REQUEST" in observations[-1][2]
        # Short results stay inline; oversized ones retain a trace reference.
        cloud_wire = json.dumps(json.loads(observations[-1][2]), ensure_ascii=False)
        assert "SMALL-RESULT" in cloud_wire
        assert "BIG-RESULT" not in cloud_wire
        assert "实际结果过长" in cloud_wire
        assert "trace://" in cloud_wire
        assert "handoff://" in observations[-1][2]
        assert bool(auxiliary) is summarize_history
        assert all(
            model == "cloud" and "ORIGINAL-HISTORY" in wire for model, wire in auxiliary
        )
    finally:
        history.close()


@pytest.mark.asyncio
async def test_generated_execution_task_uses_existing_redaction(monkeypatch, tmp_path):
    from qwenpaw.runtime.edge_cloud.safety import EdgeCloudRedaction

    original_prepare = EdgeCloudRedaction.prepare

    async def prepare_inputs(self, messages):
        prepared = await original_prepare(self, messages)
        for message in prepared:
            if message.name == "execution_task":
                message.content[0].text = message.content[0].text.replace(
                    "TASK-SECRET", "[redacted]"
                )
        return prepared

    monkeypatch.setattr(EdgeCloudRedaction, "prepare", prepare_inputs)
    ctx = context(tmp_path)
    lower = Model("local", [text("done")])
    await consume(
        prepare(
            monkeypatch,
            ctx,
            lower,
            [route(execution_task="Transform TASK-SECRET as requested")],
        ),
        ctx,
    )
    sent = json.dumps([m.model_dump(mode="json") for m in lower.requests[0][0]])
    assert "TASK-SECRET" not in sent and "[redacted]" in sent


@pytest.mark.parametrize("limit", [1, 2, 4])
@pytest.mark.parametrize("estimate", [0, 1, 2, 3, 4, 5])
def test_device_file_route_uses_configured_tool_round_budget(estimate, limit):
    orchestrator = EdgeCloudOrchestrator(
        workspace=None, app_services=None, config=config(device_max_tool_rounds=limit)
    )
    decision = RouterDecision.model_validate(
        route("device_tools", estimated_tool_rounds=estimate)
    )
    orchestrator._normalize_route(decision, {})
    accepted = 1 <= estimate <= limit
    assert decision.target_agent == ("device" if accepted else "cloud")
    assert decision.need_verifier is accepted


@pytest.mark.asyncio
async def test_text_only_device_never_verifies_even_if_route_requested_it(
    monkeypatch, tmp_path
):
    ctx = context(tmp_path)
    orchestrator = prepare(
        monkeypatch,
        ctx,
        Model("local", [text("translation")]),
        [route(need_verifier=True)],
    )
    await consume(orchestrator, ctx)
    assert orchestrator._trace.final_agent == "device"
    assert orchestrator._decision.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2, 4])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("third_tool", [False, True])
async def test_device_budget_counts_batches_and_blocks_excess_rounds(
    monkeypatch, tmp_path, streaming, third_tool, limit
):
    invoked = []

    async def read_file(file_path: str):
        invoked.append(file_path)
        return ToolChunk(content=[TextBlock(text="read " + file_path)])

    batch = call("read_file", {"file_path": "a"}, "a")
    batch.content.extend(call("read_file", {"file_path": "b"}, "b").content)
    additional = [f"round-{index}" for index in range(1, limit)]
    responses = [batch] + [
        call("read_file", {"file_path": path}, path) for path in additional
    ]
    responses.append(
        call("read_file", {"file_path": "never"}, "third")
        if third_tool
        else text("finished")
    )
    choices = []

    class StreamingModel(Model):
        async def __call__(self, *args, **kwargs):
            choices.append(kwargs.get("tool_choice"))
            result = await super().__call__(*args, **kwargs)
            if not streaming:
                return result

            async def chunks():
                yield ChatResponse(content=deepcopy(result.content), is_last=False)
                yield result

            return chunks()

    lower = StreamingModel("local", responses)
    ctx = context(tmp_path, [FunctionTool(read_file)])
    decisions = [route("device_tools", estimated_tool_rounds=limit)]
    if not third_tool:
        decisions.append({"protocol_version": "edge_cloud_verifier.v1", "passed": True})
    orchestrator = prepare(
        monkeypatch, ctx, lower, decisions, config(device_max_tool_rounds=limit)
    )
    errors = []
    orchestrator._log = lambda event, **kwargs: errors.append((event, kwargs))
    await consume(orchestrator, ctx)
    assert invoked == ["a", "b", *additional], errors
    assert orchestrator._journal.tool_rounds == limit
    assert choices[-1].mode == "none"
    assert (
        f"最多 {limit} 轮工具调用"
        in lower.requests[0][0][0].get_text_content()
    )
    _, stage, prompt, payload = orchestrator._decision.call_args_list[0].args
    assert stage == "router"
    assert f"不超过 {limit} 轮工具调用" in prompt
    assert payload["device_max_tool_rounds"] == limit
    if third_tool:
        assert orchestrator._trace.fallback_reason == "DEVICE_TOOL_ROUND_LIMIT"
        assert orchestrator._decision.await_count == 1
        assert '"id": "third"' not in json.dumps(ctx.agent.state_dict())
    else:
        assert orchestrator._trace.final_agent == "device"
        assert orchestrator._decision.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["device", "edge"])
async def test_each_execution_agent_uses_its_own_connection(
    monkeypatch, tmp_path, tier
):
    ctx = context(tmp_path)
    if tier == "edge":
        directory = existing_skill(tmp_path, "report")
        ctx.agent.toolkit = Toolkit(skills_or_loaders=[str(directory)])
    lower = Model(
        "local",
        ([call("Skill", {"skill": "report"})] if tier == "edge" else [])
        + [text("done")],
    )
    decisions = [
        route(
            "edge_skill" if tier == "edge" else "device_text",
            skill="report" if tier == "edge" else "",
        )
    ]
    if tier == "edge":
        decisions.append({"protocol_version": "edge_cloud_verifier.v1", "passed": True})
    settings = config()
    orchestrator = prepare(monkeypatch, ctx, lower, decisions, settings)
    received = []

    def factory(**kwargs):
        received.append(kwargs)
        return lower, None

    monkeypatch.setattr(
        "qwenpaw.agents.model_factory.create_openai_compatible_model_and_formatter",
        factory,
    )
    await consume(orchestrator, ctx)
    connection = settings.connection_for(tier)
    assert received == [
        {
            "base_url": connection.api_base,
            "api_key": connection.api_key,
            "model": connection.model,
            "agent_id": "default",
            "execution_role": tier,
            "max_tokens": connection.max_tokens,
        }
    ]
    assert (
        settings.connection_for("device").model != settings.connection_for("edge").model
    )
    assert settings.device_api_key not in json.dumps(ctx.agent.state_dict())
    assert settings.edge_api_key not in json.dumps(ctx.agent.state_dict())


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["device", "edge"])
@pytest.mark.parametrize("max_tokens", [None, 2048])
async def test_model_defaults_execute_with_real_factory_and_scroll(
    monkeypatch, tmp_path, tier, max_tokens
):
    from inspect import signature
    from agentscope.model import OpenAIChatModel
    from qwenpaw.agents import model_factory
    from qwenpaw.agents.context.request_budget import budget_for_model
    from qwenpaw.agents.context.scroll.history import HistoryStore
    from qwenpaw.agents.context.scroll.manager import ScrollContextManager
    from qwenpaw.providers import openai_chat_model_compat
    from test_request_budget import qwen_agent

    async def read_file(file_path: str):
        return ToolChunk(content=[TextBlock(text="confirmed file content")])

    responses = (
        [call("Skill", {"skill": "report"}, "load-skill")]
        if tier == "edge" else []
    ) + [call("read_file", {"file_path": "input.txt"}), text("completed")]
    sent = []
    created = []
    constructor = openai_chat_model_compat.OpenAIChatModelCompat
    factory = model_factory.create_openai_compatible_model_and_formatter

    def create(**kwargs):
        raw = constructor(**kwargs)
        created.append((kwargs, raw))
        return raw

    async def call_api(self, *args, **kwargs):
        sent.append(kwargs)
        return responses.pop(0)

    async def count_tokens(self, *args, **kwargs):
        return 100

    monkeypatch.setattr(constructor, "_call_api", call_api)
    monkeypatch.setattr(constructor, "count_tokens", count_tokens)
    monkeypatch.setattr(openai_chat_model_compat, "OpenAIChatModelCompat", create)
    monkeypatch.setattr(
        model_factory, "_runtime_model_wrapping_config", lambda _: (None, None, None)
    )
    history = HistoryStore(tmp_path / "history.db")
    manager = ScrollContextManager(
        history=history, session_id="edge-test", summarize_unheadlined=False
    )
    cloud = Model("original-cloud")
    tools = [FunctionTool(read_file)]
    ctx = context(tmp_path)
    ctx.agent = qwen_agent(monkeypatch, cloud, manager, tools=tools)
    ctx.agent.state.permission_context.mode = PermissionMode.BYPASS
    if tier == "edge":
        directory = existing_skill(tmp_path, "report")
        ctx.agent.toolkit = Toolkit(tools=tools, skills_or_loaders=[str(directory)])
    decisions = [
        route(
            "edge_skill" if tier == "edge" else "device_tools",
            skill="report" if tier == "edge" else "",
        ),
        {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
    ]
    settings = config(
        device_max_tokens=max_tokens,
        edge_max_tokens=max_tokens * 2 if max_tokens is not None else None,
    )
    orchestrator = prepare(monkeypatch, ctx, None, decisions, settings)
    monkeypatch.setattr(
        model_factory, "create_openai_compatible_model_and_formatter", factory
    )
    try:
        await consume(orchestrator, ctx)
        assert orchestrator._trace.final_agent == tier
        assert not orchestrator._trace.fallback
        assert not responses and len(sent) == (3 if tier == "edge" else 2)
        kwargs, raw = created[0]
        assert "context_size" not in kwargs and "parameters" not in kwargs
        assert raw.context_size == signature(OpenAIChatModel).parameters[
            "context_size"
        ].default
        expected_tokens = settings.connection_for(tier).max_tokens
        assert raw.parameters == OpenAIChatModel.Parameters(max_tokens=expected_tokens)
        assert raw.parameters.max_tokens == expected_tokens
        if expected_tokens is None:
            assert budget_for_model(raw) is None
        assert "call-1" in manager._persisted_tcids
        assert ctx.agent._context_manager is manager and ctx.agent.model is cloud
    finally:
        history.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("trace_enabled", [False, True])
async def test_router_and_verify_reuse_exact_original_agent_model(
    monkeypatch, tmp_path, trace_enabled
):
    async def read_file(file_path: str):
        return ToolChunk(content=[TextBlock(text="actual evidence")])

    original = Model(
        "original-secret-provider-model",
        [
            call("submit_route_decision", route("device_tools")),
            call(
                "submit_verification",
                {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
            ),
        ],
    )
    original.parameters.temperature = 0.17
    ctx = context(tmp_path, [FunctionTool(read_file)], original)
    lower = Model("device", [call("read_file", {"file_path": "a"}), text("done")])
    orchestrator = prepare(
        monkeypatch, ctx, lower, [], settings=config(trace_stream_enabled=trace_enabled)
    )
    del orchestrator._decision
    factory = AsyncMock(
        side_effect=AssertionError("No separate Router/Verify model factory")
    )
    monkeypatch.setattr(
        "qwenpaw.agents.model_factory.create_model_and_formatter_for_slot", factory
    )
    await consume(orchestrator, ctx)
    assert orchestrator._cloud_model is ctx.agent.model is original
    assert original.parameters.temperature == 0.17
    assert [messages[0].name for messages, _ in original.requests] == [
        "RouterAgent",
        "VerifyAgent",
    ]
    factory.assert_not_called()


def test_chat_device_and_edge_configuration_never_fall_back_to_each_other():
    base = config()
    override = {
        "enabled": True,
        "device": {
            "api_base": "http://device-box/v1",
            "api_key": "device-private",
            "model": "device-only",
        },
        "edge": {
            "api_base": "http://edge-box/v1",
            "api_key": "edge-private",
            "model": "edge-only",
        },
    }
    configured = resolve_edge_cloud_config(base, override)
    assert configured.connection_for("device").model == "device-only"
    assert configured.connection_for("edge").model == "edge-only"
    assert base.device_model == "device-model" and base.edge_model == "local-model"
    edge_only = replace(
        configured, device_base_url="", device_api_key="", device_model=""
    )
    assert edge_only.ready and not edge_only.connection_for("device").ready
    device_only = replace(configured, edge_base_url="", edge_api_key="", edge_model="")
    assert device_only.ready and not device_only.connection_for("edge").ready


@pytest.mark.parametrize(
    "override",
    [
        {"device": {"api_key": "sensitive", "unexpected": "x"}},
        {"device": {"api_base": "file:///sensitive"}},
        {"edge": {"model": ""}},
        {"router": {"model": "override-original"}},
        {"verify": {"api_key": "sensitive"}},
    ],
)
def test_invalid_role_connection_is_rejected_without_echoing_values(override):
    with pytest.raises(ValueError) as error:
        resolve_edge_cloud_config(config(), override)
    assert "sensitive" not in str(error.value)


def test_console_chat_accepts_distinct_roles_and_runtime_detaches_secrets(monkeypatch):
    from qwenpaw.security.tool_guard.guardians import file_guardian

    monkeypatch.setattr(file_guardian, "_load_sensitive_files_from_config", lambda: [])
    from qwenpaw.app.routers import console

    monkeypatch.setattr(console, "_skill_pool_dir", lambda: None)
    override = {
        "enabled": True,
        "device": {
            "api_base": "http://device/v1",
            "api_key": "device-private",
            "model": "device",
        },
        "edge": {
            "api_base": "http://edge/v1",
            "api_key": "edge-private",
            "model": "edge",
        },
    }
    payload = console._extract_session_and_payload(
        {
            "user_id": "u",
            "session_id": "s",
            "input": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            "request_context": {"edge_cloud": override},
        }
    )
    request = SimpleNamespace(
        request_context=payload["meta"]["request_context"], channel_meta=payload["meta"]
    )
    detached, received = detach_edge_cloud_override(request)
    assert received == override
    assert "edge_cloud" not in detached.request_context
    assert "device-private" not in str(detached.__dict__) and "edge-private" not in str(
        detached.__dict__
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["device", "edge"])
async def test_unconfigured_tier_uses_cloud_without_borrowing_other_model(
    monkeypatch, tmp_path, missing
):
    settings = config(
        **{f"{missing}_base_url": "", f"{missing}_api_key": "", f"{missing}_model": ""}
    )
    ctx = context(tmp_path)
    lower = Model("must-not-be-used")
    orchestrator = prepare(
        monkeypatch,
        ctx,
        lower,
        [route("device_text" if missing == "device" else "edge_skill")],
        settings,
    )
    assert orchestrator.should_handle(ctx)
    await consume(orchestrator, ctx)
    assert not lower.requests and orchestrator._trace.final_agent == "cloud"


def test_each_model_environment_is_independent(monkeypatch):
    for tier in ("device", "edge"):
        for suffix, value in (
            ("BASE_URL", f"http://{tier}/v1"),
            ("API_KEY", f"{tier}-private"),
            ("MODEL", f"{tier}-model"),
        ):
            monkeypatch.setenv(f"QWENPAW_EDGE_CLOUD_{tier.upper()}_{suffix}", value)
    loaded = load_edge_cloud_config()
    assert loaded.connection_for("device").api_key == "device-private"
    assert loaded.connection_for("edge").api_key == "edge-private"
    assert loaded.device_model != loaded.edge_model


@pytest.mark.parametrize("tier", ["device", "edge"])
@pytest.mark.parametrize("invalid", ["0", "-1", "1.5", "invalid"])
def test_output_token_environment_requires_positive_integer(monkeypatch, tier, invalid):
    name = f"QWENPAW_EDGE_CLOUD_{tier.upper()}_MAX_TOKENS"
    monkeypatch.setenv(name, invalid)
    with pytest.raises(ValueError, match=name):
        load_edge_cloud_config()


def test_router_function_schema_exposes_only_named_architecture_protocol():
    from qwenpaw.runtime.edge_cloud.function_call import decision_call_kwargs
    from qwenpaw.runtime.edge_cloud.roles import AGENT_NAMES

    schema = decision_call_kwargs("router")["tools"][0]["function"]["parameters"][
        "properties"
    ]
    assert schema["target_agent"]["enum"] == ["device", "edge", "cloud"]
    assert "single_simple" not in schema["task_type"]["enum"]
    assert set(AGENT_NAMES.values()) == {
        "RouterAgent",
        "VerifyAgent",
        "CloudAgent",
        "EdgeAgent",
        "DeviceAgent",
    }


def test_decision_calls_always_use_auto_choice():
    from qwenpaw.runtime.edge_cloud.function_call import decision_call_kwargs

    reader = RouteSkillReader(None, None, [], allowed_skill="report")
    for stage in ("router", "verifier"):
        for read_tools in ([], [reader]):
            kwargs = decision_call_kwargs(stage, read_tools)
            assert kwargs["tool_choice"].mode == "auto"
    kwargs = decision_call_kwargs("verifier", [reader])
    assert [item["function"]["name"] for item in kwargs["tools"]] == [
        "submit_verification",
        "Skill",
    ]


@pytest.mark.asyncio
async def test_route_skill_reader_can_be_limited_to_selected_skill():
    class Viewer:
        async def resolve_skill(self, name, groups):
            return SimpleNamespace(markdown="BODY-" + name)

    reader = RouteSkillReader(None, Viewer(), [], allowed_skill="report")
    result = await reader.call("report")
    assert "BODY-report" in result.content[0].text
    assert reader.inspected == {"report"}
    with pytest.raises(ValueError, match="selected Skill"):
        await reader.call("other")


@pytest.mark.parametrize(
    "override",
    [
        {"api_base": "http://old/v1"},
        {"api_key": "private"},
        {"model": "old"},
        {"simple_max_tool_rounds": 10},
        {"fail_open_to_complex": False},
        {"simple": {"model": "old"}},
        {"complex": {"model": "old"}},
        {"edge": {"max_tool_rounds": 2}},
    ],
)
def test_removed_configuration_fields_are_rejected(override):
    with pytest.raises(ValueError):
        resolve_edge_cloud_config(config(), override)


@pytest.mark.parametrize(
    "fields",
    [
        {"task_type": "single_simple"},
        {"task_type": "simple_multiturn"},
        {"target_agent": "simple"},
        {"target_agent": "complex"},
    ],
)
def test_removed_router_values_are_rejected(fields):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RouterDecision.model_validate({**route(), **fields})


@pytest.mark.parametrize("limit", [1, 2, 4])
def test_device_round_budget_request_override_is_not_persisted(limit):
    base = config(device_max_tool_rounds=2)
    merged = resolve_edge_cloud_config(base, {"device": {"max_tool_rounds": limit}})
    assert merged.device_max_tool_rounds == limit
    assert base.device_max_tool_rounds == 2
    assert merged.connection_for("device") == base.connection_for("device")
    assert merged.connection_for("edge") == base.connection_for("edge")


@pytest.mark.parametrize("invalid", [0, -1, True, False, 1.5, "2", None])
def test_device_round_budget_requires_positive_integer(invalid):
    with pytest.raises(ValueError):
        resolve_edge_cloud_config(config(), {"device": {"max_tool_rounds": invalid}})


def test_environment_uses_current_enable_flag_and_device_budget_only(monkeypatch):
    import os

    monkeypatch.setattr(
        os,
        "environ",
        {
            "QWENPAW_EDGE_CLOUD_DEFAULT_AGENT_ENABLED": "1",
            "QWENPAW_EDGE_CLOUD_SIMPLE_MAX_TOOL_ROUNDS": "10",
            "QWENPAW_EDGE_CLOUD_FAIL_OPEN_TO_COMPLEX": "0",
        },
    )
    loaded = load_edge_cloud_config()
    assert not loaded.enabled and loaded.device_max_tool_rounds == 2
    monkeypatch.setenv("QWENPAW_EDGE_CLOUD_ENABLED", "1")
    monkeypatch.setenv("QWENPAW_EDGE_CLOUD_DEVICE_MAX_TOOL_ROUNDS", "4")
    loaded = load_edge_cloud_config()
    assert loaded.enabled and loaded.device_max_tool_rounds == 4


@pytest.mark.parametrize("invalid", ["0", "-1", "x", "1.5"])
def test_invalid_environment_device_round_budget_is_rejected(monkeypatch, invalid):
    monkeypatch.setenv("QWENPAW_EDGE_CLOUD_DEVICE_MAX_TOOL_ROUNDS", invalid)
    with pytest.raises(ValueError):
        load_edge_cloud_config()


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [None, True, False])
@pytest.mark.parametrize("with_tools", [False, True])
async def test_runtime_enable_switch_uses_original_default_path_when_off(
    monkeypatch, tmp_path, override, with_tools,
):
    from qwenpaw.runtime.hooks import HookResult
    from qwenpaw.runtime.runtime import Runtime

    from qwenpaw.tool_calls import ToolCoordinator, ToolCoordinatorMiddleware

    writes = []

    async def read_file(file_path: str):
        return ToolChunk(
            state=ToolResultState.ERROR,
            content=[TextBlock(text="FILE_NOT_FOUND")],
        )

    async def write_file(file_path: str, content: str):
        writes.append(file_path)
        return ToolChunk(content=[TextBlock(text="written")])

    def steps(answer):
        return [
            *([
                call("read_file", {"file_path": "hello.html"}, "read"),
                call("write_file", {"file_path": "hello.html", "content": "hello"}, "write"),
            ] if with_tools else []),
            text(answer),
        ]

    cloud = Model("original-default", steps("ordinary answer"))
    lower = Model("device", steps("device answer"))
    ctx = context(
        tmp_path, [FunctionTool(read_file), FunctionTool(write_file)], cloud=cloud,
    )
    original_toolkit = ctx.agent.toolkit
    ctx.agent._acting_middlewares = [ToolCoordinatorMiddleware(ToolCoordinator())]
    wrap = ExecutionJournal.wrap
    wrapped_tools = []

    def checked_wrap(self, tool, *, lower):
        assert override is True, "Disabled collaboration must not wrap any tool"
        wrapped_tools.append(tool.name)
        return wrap(self, tool, lower=lower)

    monkeypatch.setattr(ExecutionJournal, "wrap", checked_wrap)
    ctx.context_injections = []
    ctx.agent.close = AsyncMock()
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
    monkeypatch.setattr(
        "qwenpaw.runtime.edge_cloud.config.load_edge_cloud_config",
        lambda: config(enabled=override is False),
    )
    decision = AsyncMock(side_effect=[
        route("device_tools" if with_tools else "device_text"),
        {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
    ])
    monkeypatch.setattr(EdgeCloudOrchestrator, "_decision", decision)
    monkeypatch.setattr(
        "qwenpaw.agents.model_factory.create_openai_compatible_model_and_formatter",
        lambda **_: (lower, None),
    )
    request = SimpleNamespace(
        request_context=(
            {} if override is None else {"edge_cloud": {"enabled": override}}
        )
    )
    events = [event async for event in runtime.run(request)]
    assert events
    assert decision.await_count == ((2 if with_tools else 1) if override else 0)
    assert bool(lower.requests) is (override is True)
    assert bool(cloud.requests) is (override is not True)
    if cloud.requests:
        assert cloud.requests[0][0][0].get_text_content() == "DEFAULT SYSTEM"
    assert ctx.agent.model is cloud and ctx.agent._system_prompt == "DEFAULT SYSTEM"
    assert ctx.agent.toolkit is original_toolkit
    assert writes == (["hello.html"] if with_tools else [])
    assert bool(wrapped_tools) is (with_tools and override is True)
    AgentBuilder.build.assert_awaited_once()


def test_entry_cli_preserves_env_and_overrides_enable_and_device_budget(monkeypatch):
    import argparse
    import ast
    import os
    import sys

    source = Path(__file__).resolve().parents[1] / "main.py"
    selected = [
        node
        for node in ast.parse(source.read_text(encoding="utf-8-sig")).body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"parse_entry_cli_args", "apply_devco_bundle_env"}
    ]
    entry = {
        "argparse": argparse,
        "os": os,
        "sys": sys,
        "_INIT_ONLY_ENV": "TEST_INIT_ONLY",
    }
    exec(
        compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), entry
    )
    monkeypatch.setattr(
        os,
        "environ",
        {
            "QWENPAW_EDGE_CLOUD_ENABLED": "1",
            "QWENPAW_EDGE_CLOUD_TRACE_STREAM_ENABLED": "1",
            "QWENPAW_EDGE_CLOUD_DEVICE_MAX_TOOL_ROUNDS": "4",
            "QWENPAW_EDGE_CLOUD_DEVICE_MAX_TOKENS": "512",
            "QWENPAW_EDGE_CLOUD_EDGE_MAX_TOKENS": "1024",
        },
    )
    assert entry["parse_entry_cli_args"]([]) == []
    assert load_edge_cloud_config().enabled
    assert load_edge_cloud_config().trace_stream_enabled
    assert load_edge_cloud_config().device_max_tool_rounds == 4
    assert load_edge_cloud_config().connection_for("device").max_tokens == 512
    assert load_edge_cloud_config().connection_for("edge").max_tokens == 1024
    assert entry["parse_entry_cli_args"](
        [
            "--edge-cloud-enabled",
            "0",
            "--edge-cloud-trace-stream-enabled",
            "0",
            "--edge-cloud-device-max-tool-rounds",
            "1",
            "--edge-cloud-device-max-tokens",
            "2048",
            "--edge-cloud-edge-max-tokens",
            "4096",
            "--reload",
        ]
    ) == ["--reload"]
    assert not load_edge_cloud_config().enabled
    assert not load_edge_cloud_config().trace_stream_enabled
    assert load_edge_cloud_config().device_max_tool_rounds == 1
    assert load_edge_cloud_config().connection_for("device").max_tokens == 2048
    assert load_edge_cloud_config().connection_for("edge").max_tokens == 4096


def test_cloud_usage_counts_current_cloud_stages():
    from qwenpaw.runtime.edge_cloud.trace import EdgeCloudTrace

    trace = EdgeCloudTrace(base_agent_id="default")
    for stage in ("router", "verifier", "cloud", "device", "edge"):
        with edge_cloud_model_request_context(
            stage=stage, base_agent_id="default", flow_id=trace.flow_id
        ):
            from qwenpaw.observability.model_request_capture import (
                is_edge_cloud_cloud_stage,
            )

            assert is_edge_cloud_cloud_stage() is (
                stage in {"router", "verifier", "cloud"}
            )
        trace.record_usage(
            stage=stage,
            provider_id="p",
            model="m",
            prompt_tokens=10,
            completion_tokens=2,
        )
    assert set(trace.usage_by_stage) == {"router", "verifier", "cloud"}
    assert trace.cloud_usage_total() == {
        "counted": True,
        "call_count": 3,
        "prompt_tokens": 30,
        "completion_tokens": 6,
        "total_tokens": 36,
    }


def test_client_stream_config_is_opt_in_and_can_be_overridden(monkeypatch):
    monkeypatch.delenv("QWENPAW_EDGE_CLOUD_TRACE_STREAM_ENABLED", raising=False)
    assert not load_edge_cloud_config().trace_stream_enabled
    monkeypatch.setenv("QWENPAW_EDGE_CLOUD_TRACE_STREAM_ENABLED", "1")
    assert load_edge_cloud_config().trace_stream_enabled
    settings = config(trace_stream_enabled=True)
    assert not resolve_edge_cloud_config(
        settings, {"trace_stream_enabled": False}
    ).trace_stream_enabled
    assert settings.trace_stream_enabled
    with pytest.raises(ValueError, match="trace_stream_enabled must be a boolean"):
        resolve_edge_cloud_config(settings, {"trace_stream_enabled": "true"})


@pytest.mark.asyncio
async def test_decisions_stream_before_completion_without_changing_session_or_upload(
    monkeypatch, tmp_path
):
    gates = {name: asyncio.Event() for name in ("RouterAgent", "VerifyAgent")}
    visible = {name: asyncio.Event() for name in gates}

    async def read_file(file_path: str):
        return ToolChunk(content=[TextBlock(text="actual evidence")])

    class StreamingControl(Model):
        async def __call__(self, messages, **kwargs):
            response = await super().__call__(messages, **kwargs)
            stage = messages[0].name

            async def chunks():
                yield ChatResponse(
                    content=[
                        ThinkingBlock(id="thought", thinking=f"live-{stage}"),
                        ToolCallBlock(
                            id="call-1", name=response.content[0].name, input="{",
                        ),
                    ],
                    is_last=False,
                )
                await gates[stage].wait()
                yield ChatResponse(
                    content=[
                        ThinkingBlock(id="thought", thinking=f"live-{stage}"),
                        *response.content,
                    ],
                    is_last=True,
                )

            return chunks()

    control = StreamingControl("same-cloud-model", [
        call("submit_route_decision", route("device_tools")),
        call("submit_verification", {
            "protocol_version": "edge_cloud_verifier.v1", "passed": True,
        }),
    ])
    ctx = context(tmp_path, [FunctionTool(read_file)], control)
    lower = Model("device", [call("read_file", {"file_path": "a"}), text("done")])
    orchestrator = prepare(
        monkeypatch, ctx, lower, [], settings=config(trace_stream_enabled=True)
    )
    del orchestrator._decision
    envelope = Envelope(session_id=ctx.session_id)
    events = []

    async def collect():
        async for event in orchestrator.run(ctx, envelope):
            events.append(event.model_copy(deep=True))
            if getattr(event, "object", "") == "message" and event.type == "reasoning":
                for stage in gates:
                    if f"live-{stage}" in event.content[0].text:
                        visible[stage].set()
        async for event in envelope.finalize():
            events.append(event.model_copy(deep=True))

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(visible["RouterAgent"].wait(), 10)
        assert not lower.requests and not task.done()
        gates["RouterAgent"].set()
        await asyncio.wait_for(visible["VerifyAgent"].wait(), 10)
        assert len(lower.requests) == 2 and not task.done()
        gates["VerifyAgent"].set()
        await asyncio.wait_for(task, 10)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    trace_messages = [
        event for event in events
        if getattr(event, "object", "") == "message"
        and event.name in gates and event.status == "completed"
        and event.type == "message"
    ]
    assert len(trace_messages) == 4
    for event in trace_messages:
        displayed = event.content[0].text
        assert displayed.startswith(f"【{event.name} | same-cloud-model】")
        assert event.type == "message"
        assert "live-" not in displayed
        assert "provider" not in displayed
    assert trace_messages[0].content[0].text == "【RouterAgent | same-cloud-model】"
    assert "已选择 DeviceAgent" in trace_messages[1].content[0].text
    assert trace_messages[2].content[0].text == "【VerifyAgent | same-cloud-model】"
    assert "校验结论：通过" in trace_messages[3].content[0].text
    for stage, start, conclusion in (
        ("RouterAgent", trace_messages[0], trace_messages[1]),
        ("VerifyAgent", trace_messages[2], trace_messages[3]),
    ):
        thinking_index = next(i for i,e in enumerate(events)
                              if e.object == "message" and e.type == "reasoning"
                              and e.content[0].text == f"live-{stage}")
        result_index = next(i for i,e in enumerate(events)
                            if e.object == "message" and e.type == "plugin_call_output"
                            and e.name == stage and e.status == "completed")
        assert events.index(start) < thinking_index < result_index < events.index(conclusion)
    sequences = [event.sequence_number for event in events]
    assert sequences == sorted(set(sequences))
    assert {event.object for event in events} <= {"message", "content", "response"}
    assert len([e for e in events if e.object == "response" and e.status == "completed"]) == 1
    trace_ids = {event.id for event in trace_messages}
    assert trace_ids <= {event.id for event in events[-1].output}
    assert events[-1].output[0].id == trace_messages[0].id
    detailed = [
        message for message in events[-1].output
        if message.type == "reasoning"
        or (message.name in gates and message.type in {"plugin_call", "plugin_call_output"})
    ]
    assert [m.content[0].text for m in detailed if m.type == "reasoning"] == [
        "live-RouterAgent",
        "live-VerifyAgent",
    ]
    assert [m.content[0].data["name"] for m in detailed if m.type == "plugin_call"] == [
        "submit_route_decision", "submit_verification",
    ]
    results = [m for m in detailed if m.type == "plugin_call_output"]
    assert len(results) == 2
    assert all(m.status == "completed" and m.content[0].data["state"] == "success" for m in results)
    assert not {m.id for m in detailed} & {m.id for m in envelope.response.output}
    assert not trace_ids & {event.id for event in envelope.response.output}
    assert all("live-" not in value for _, value in envelope.collect_partial_blocks())
    saved = json.dumps(ctx.agent.state_dict())
    upload = json.dumps(ctx.extras["edge_cloud_upload"])
    assert "live-RouterAgent" not in saved + upload
    assert "live-VerifyAgent" not in saved + upload
    assert "【RouterAgent" not in saved + upload
    assert orchestrator._trace.final_agent == "device"
    assert ctx.agent.model is control
    assert [msgs[0].name for msgs, _ in control.requests] == list(gates)
    assert len(control.requests) == 2
    assert set(orchestrator._stage_recorders) == {"router", "device", "verifier"}


@pytest.mark.asyncio
@pytest.mark.parametrize("close_stream", [False, True])
async def test_client_stream_cleans_up_decision_task_on_stop(
    monkeypatch, tmp_path, close_stream
):
    closed = asyncio.Event()

    class WaitingControl(Model):
        async def __call__(self, messages, **kwargs):
            await super().__call__(messages, **kwargs)

            async def chunks():
                try:
                    yield ChatResponse(
                        content=[
                            ThinkingBlock(thinking="waiting for route"),
                            ToolCallBlock(
                                id="call-1", name="submit_route_decision", input="{",
                            ),
                        ], is_last=False,
                    )
                    await asyncio.Event().wait()
                finally:
                    closed.set()

            return chunks()

    control = WaitingControl("cloud", [call("submit_route_decision", route())])
    ctx = context(tmp_path, cloud=control)
    lower = Model("device")
    orchestrator = prepare(
        monkeypatch, ctx, lower, [], settings=config(trace_stream_enabled=True)
    )
    del orchestrator._decision
    envelope = Envelope()
    visible, stop = asyncio.Event(), asyncio.Event()

    async def collect():
        from contextlib import aclosing

        async with aclosing(orchestrator.run(ctx, envelope)) as stream:
            async for event in stream:
                if (getattr(event, "object", "") == "message" and event.type == "reasoning"
                        and event.content[0].text == "waiting for route"):
                    visible.set()
                    if close_stream:
                        await stop.wait()
                        break

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(visible.wait(), 10)
        if close_stream:
            stop.set()
            await asyncio.wait_for(task, 10)
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert closed.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not lower.requests and not ctx.agent.state.context
    assert envelope.collect_partial_blocks() == []
    assert ctx.agent.model is control and ctx.agent._system_prompt == "DEFAULT SYSTEM"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_client_stream_handles_function_arguments_and_full_final_snapshot(streaming):
    from qwenpaw.runtime.edge_cloud.client_stream import ClientDecisionStream

    decision = route()
    arguments = json.dumps(decision)
    final = call("submit_route_decision", decision)
    kwargs_seen = []

    async def model(messages, **kwargs):
        kwargs_seen.append(kwargs)
        if not streaming:
            return {"choices": [{"message": {"tool_calls": [{
                "id": "call-1", "function": {
                    "name": "submit_route_decision", "arguments": arguments,
                },
            }]}}]}

        async def chunks():
            for fragment in (arguments[:25], arguments[25:]):
                yield ChatResponse(content=[ToolCallBlock(
                    id="call-1", name="submit_route_decision", input=fragment,
                )], is_last=False)
            yield final

        return chunks()

    stream = ClientDecisionStream("RouterAgent", "cloud-model")

    async def decide(observer_factory):
        return await consume_decision_call(
            model, [], stage="router", response_observer_factory=observer_factory,
        )

    events = [event async for event in stream.run(decide)]
    assert stream.result == decision
    start = stream.start()
    conclusion = stream.complete("已选择 DeviceAgent 处理这次请求。")
    assert start.id != conclusion.id
    assert start.content[0].text == "【RouterAgent | cloud-model】"
    assert "已选择 DeviceAgent" in conclusion.content[0].text
    displayed = json.dumps([e.model_dump(mode="json") for e in events])
    assert "submit_route_decision" not in displayed
    assert "protocol_version" not in displayed
    assert "response_observer_factory" not in kwargs_seen[0]
    from qwenpaw.runtime.edge_cloud.function_call import decision_call_kwargs
    assert kwargs_seen == [decision_call_kwargs("router")]
    assert final.content[0].input == arguments
    deltas = [event.text for event in events if event.object == "content"]
    assert deltas == []
    assert events == []


@pytest.mark.asyncio
async def test_client_stream_errors_preserve_cloud_fallback(monkeypatch, tmp_path):
    cloud = Model("cloud", [RuntimeError("route failed"), text("cloud answer")])
    ctx = context(tmp_path, cloud=cloud)
    lower = Model("device")
    orchestrator = prepare(
        monkeypatch, ctx, lower, [], settings=config(trace_stream_enabled=True)
    )
    del orchestrator._decision
    events = await consume(orchestrator, ctx)
    assert orchestrator._trace.final_agent == "cloud"
    assert not lower.requests
    assert len(cloud.requests) == 2
    completed = [e for e in events if e.object == "message" and e.status == "completed"]
    router_messages = [e for e in completed if e.name == "RouterAgent"]
    assert len(router_messages) == 2
    assert router_messages[0].content[0].text == "【RouterAgent | cloud】"
    assert "未取得有效路由结果" in router_messages[1].content[0].text
    assert router_messages[0].id != router_messages[1].id
    assert "【RouterAgent" not in json.dumps(ctx.agent.state_dict())


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_router_conclusion_reports_normalized_target_after_start(
    monkeypatch, tmp_path, enabled,
):
    ctx = context(tmp_path, cloud=Model("cloud", [text("cloud answer")]))
    lower = Model("device")
    orchestrator = prepare(
        monkeypatch, ctx, lower,
        [route(reason="简单问候", execution_task="回复用户的问候")],
        settings=config(trace_stream_enabled=enabled, device_model=""),
    )
    envelope = Envelope()
    events = [e async for e in orchestrator.run(ctx, envelope)]
    events.extend([e async for e in envelope.finalize()])
    summaries = [e for e in events if e.object == "message" and e.name == "RouterAgent"]
    assert len(summaries) == 2 * int(enabled)
    assert (envelope._client_messages is not None) is enabled
    if enabled:
        assert summaries[0].content[0].text == "【RouterAgent | cloud】"
        assert "已选择 CloudAgent" in summaries[1].content[0].text
        assert "原建议交给 DeviceAgent" in summaries[1].content[0].text
        assert "简单问候" in summaries[1].content[0].text
        assert "回复用户的问候" not in summaries[1].content[0].text
        assert events[-1].output[0].name == "RouterAgent"
    assert orchestrator._stage_outcomes["router"]["decision"]["target_agent"] == "cloud"
    assert not lower.requests and orchestrator._trace.final_agent == "cloud"
    assert "【RouterAgent" not in json.dumps(ctx.agent.state_dict())


@pytest.mark.asyncio
@pytest.mark.parametrize("passed", [False, True])
async def test_client_verification_reports_verifier_verdict_without_runtime_veto(
    monkeypatch, tmp_path, passed,
):
    ctx = context(tmp_path, cloud=Model("cloud", [text("cloud continuation")]))
    lower = Model("device", [text("device result")])
    orchestrator = prepare(
        monkeypatch, ctx, lower, [], settings=config(trace_stream_enabled=True),
    )

    async def decide(_ctx, stage, *args, **kwargs):
        if stage == "router":
            return route("device_tools")
        orchestrator._journal.operations["failed-write"] = {"status": "unknown"}
        return {
            "protocol_version": "edge_cloud_verifier.v1", "passed": passed,
            "failure_reason": "" if passed else "结果缺少要求的内容",
            "missing_requirements": [] if passed else ["缺少统计结果"],
        }

    orchestrator._decision = decide
    envelope = Envelope()
    events = [e.model_copy(deep=True) async for e in orchestrator.run(ctx, envelope)]
    events.extend([e.model_copy(deep=True) async for e in envelope.finalize()])
    output = events[-1].output
    assert output[0].name == "RouterAgent"
    verifier_index = next(i for i, m in enumerate(output)
                          if m.name == "VerifyAgent" and "校验结论" in m.content[0].text)
    summary = output[verifier_index].content[0].text
    if passed:
        assert "校验结论：通过" in summary
        assert "运行时执行记录检查" not in summary
    else:
        assert "结果缺少要求的内容" in summary and "缺少统计结果" in summary
        assert "校验结论：未通过" in summary and "CloudAgent" in summary
    assert "device result" in json.dumps(
        [m.model_dump(mode="json") for m in output[:verifier_index]]
    )
    assert bool(ctx.agent.model.requests) is not passed
    assert orchestrator._trace.fallback is not passed
    assert orchestrator._trace.final_agent == ("device" if passed else "cloud")
    assert orchestrator._stage_outcomes["verifier"]["accepted"] is passed


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failure", ["factory", "callback"])
async def test_client_observer_failure_does_not_change_decision(streaming, failure):
    from qwenpaw.runtime.edge_cloud.function_call import decision_call_kwargs

    response = call("submit_route_decision", route())
    original = asdict(response)
    seen = []

    async def model(messages, **kwargs):
        seen.append(kwargs)
        if not streaming:
            return response

        async def chunks():
            yield text("private reasoning")
            yield response

        return chunks()

    async def observe(_):
        raise ValueError("display unavailable")

    def factory():
        if failure == "factory":
            raise ValueError("display unavailable")
        return observe

    result = await consume_decision_call(
        model, [], stage="router", response_observer_factory=factory,
    )
    assert result == route()
    assert seen == [decision_call_kwargs("router")]
    assert asdict(response) == original


@pytest.mark.asyncio
async def test_client_observer_cancellation_is_not_swallowed():
    async def model(*args, **kwargs):
        return call("submit_route_decision", route())

    async def observe(_):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await consume_decision_call(
            model, [], stage="router", response_observer_factory=lambda: observe,
        )


@pytest.mark.asyncio
async def test_client_display_redacts_summary_without_touching_tool_payload(monkeypatch):
    from qwenpaw.runtime.edge_cloud.client_stream import ClientDecisionStream

    secret = "CLIENT_PRIVATE_VALUE"
    decision = {"passed": False, "failure_reason": f"联系 {secret} 13800138000"}
    response = call("submit_verification", decision)
    original = asdict(response)
    stream = ClientDecisionStream(
        "VerifyAgent", "cloud",
        sensitive_words={"patterns": [{"type": "custom", "keyword": secret}]},
    )

    async def model(*args, **kwargs):
        return response

    async def decide(factory):
        return await consume_decision_call(
            model, [], stage="verifier", response_observer_factory=factory,
        )

    events = [e async for e in stream.run(decide)]
    events.append(stream.complete(decision["failure_reason"]))
    displayed = json.dumps([e.model_dump(mode="json") for e in events])
    assert secret not in displayed and "13800138000" not in displayed
    assert "submit_verification" not in displayed
    assert stream.result == decision
    assert asdict(response) == original

    def fail(*args, **kwargs):
        raise ValueError("redactor failed")

    monkeypatch.setattr(
        "qwenpaw.runtime.edge_cloud.client_stream.redact_sensitive_value", fail,
    )
    assert secret not in stream.complete(secret).content[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["initialization", "label", "execution_start"])
async def test_client_display_initialization_failure_keeps_original_route(
    monkeypatch, tmp_path, failure,
):
    from qwenpaw.runtime.edge_cloud.client_stream import client_agent_header

    def fail(*args, **kwargs):
        if failure == "label" and args[0] != "RouterAgent":
            return client_agent_header(*args, **kwargs)
        raise ValueError("display unavailable")

    monkeypatch.setattr({
        "initialization": "qwenpaw.runtime.edge_cloud.client_stream.ClientDecisionStream",
        "label": "qwenpaw.runtime.edge_cloud.client_stream.client_agent_header",
        "execution_start": "qwenpaw.runtime.edge_cloud.client_stream.client_execution_start",
    }[failure], fail)
    ctx = context(tmp_path)
    lower = Model("device", [text("done")])
    orchestrator = prepare(
        monkeypatch, ctx, lower, [route()], settings=config(trace_stream_enabled=True),
    )
    events = await consume(orchestrator, ctx)
    assert orchestrator._trace.final_agent == "device"
    assert not orchestrator._trace.fallback
    assert len(lower.requests) == 1
    orchestrator._decision.assert_awaited_once()
    assert any(e.object == "message" and e.name == "RouterAgent" for e in events) is (
        failure == "execution_start"
    )


def test_trace_flag_alone_does_not_enable_collaboration(tmp_path):
    orchestrator = EdgeCloudOrchestrator(
        workspace=None, app_services=None,
        config=config(enabled=False, trace_stream_enabled=True),
    )
    assert not orchestrator.should_handle(context(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_state", [ToolResultState.SUCCESS, ToolResultState.ERROR])
@pytest.mark.parametrize("tier", ["device", "edge", "cloud"])
async def test_client_trace_preserves_tool_results_in_stream_and_final_response(
    monkeypatch, tmp_path, tool_state, tier,
):
    body = json.dumps({
        "result": {"text": "原始工具结果\n13800138000", "values": [1, None, False]},
    }, ensure_ascii=False)
    snapshots = []

    async def read_file(file_path: str):
        return ToolChunk(content=[TextBlock(text=body)], state=tool_state)

    directory = existing_skill(tmp_path, "report") if tier == "edge" else None
    for enabled in (False, True):
        steps = [call("read_file", {"file_path": "a"}), ChatResponse(content=[
            ThinkingBlock(id="original-thinking", thinking="original model thinking"),
            TextBlock(text="done"),
        ], is_last=True)]
        cloud = Model("cloud", steps if tier == "cloud" else [])
        ctx = context(tmp_path, [FunctionTool(read_file)], cloud=cloud)
        if tier == "edge":
            ctx.agent.toolkit = Toolkit(
                tools=[FunctionTool(read_file)], skills_or_loaders=[str(directory)],
            )
            steps.insert(0, call("Skill", {"skill": "report"}, "load"))
        lower = Model(tier, steps)
        orchestrator = prepare(
            monkeypatch, ctx, lower, [
                route({
                    "device": "device_tools", "edge": "edge_skill",
                    "cloud": "complex_or_negative",
                }[tier], **({"skill": "report"} if tier == "edge" else {})),
                {"protocol_version": "edge_cloud_verifier.v1", "passed": True},
            ], settings=config(trace_stream_enabled=enabled),
        )
        envelope = Envelope()
        events = [e.model_copy(deep=True) async for e in orchestrator.run(ctx, envelope)]
        events.extend([e.model_copy(deep=True) async for e in envelope.finalize()])
        streamed = [
            e.data for e in events
            if e.object == "content" and isinstance(getattr(e, "data", None), dict)
            and e.data.get("name") == "read_file"
        ]
        completed = [
            c.data for m in events[-1].output if m.type == "plugin_call_output"
            for c in m.content if c.data.get("name") == "read_file"
        ]
        inputs = [
            c.data for m in events[-1].output if m.type == "plugin_call"
            for c in m.content if c.data.get("name") == "read_file"
        ]
        assert len(inputs) == 1
        assert inputs[0]["arguments"] == json.dumps({"file_path": "a"})
        assert len(completed) == 1
        assert completed[0]["output"] == body
        assert completed[0]["state"] == tool_state.value
        assert json.loads(completed[0]["output"]) == json.loads(body)
        assert "【" not in json.dumps((inputs, completed), ensure_ascii=False)
        thinking = [m for m in events[-1].output if m.type == "reasoning"]
        assert len(thinking) == 1
        assert thinking[0].content[0].text == "original model thinking"
        assert thinking[0].name == "assistant"
        thinking_chunks = [
            e.text for e in events
            if e.object == "content" and e.msg_id == thinking[0].id
        ]
        assert thinking_chunks and all("【" not in chunk for chunk in thinking_chunks)
        snapshots.append((streamed, inputs, completed, thinking_chunks))
    # Sequence numbers grow when independent trace events are inserted;
    # the actual tool payloads, including result status, remain identical.
    assert snapshots[0] == snapshots[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("tier", ["device", "edge", "cloud"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_client_execution_labels_only_cover_answer_text_without_saving_labels(
    monkeypatch, tmp_path, enabled, tier, streaming,
):
    from qwenpaw.runtime.edge_cloud.roles import AGENT_NAMES

    class ExecutionOutput(Model):
        async def __call__(self, *args, **kwargs):
            response = await super().__call__(*args, **kwargs)
            if not streaming:
                return response

            async def chunks():
                # Streaming chunks carry deltas; the last chunk is a full
                # snapshot saved by the SDK, rather than another delta.
                if any(isinstance(block, ThinkingBlock) for block in response.content):
                    yield ChatResponse(content=[
                        ThinkingBlock(id="execution-thinking", thinking="model "),
                    ], is_last=False)
                    yield ChatResponse(content=[
                        ThinkingBlock(id="execution-thinking", thinking="thinking"),
                    ], is_last=False)
                    yield ChatResponse(content=[
                        TextBlock(text="model "),
                    ], is_last=False)
                    yield ChatResponse(content=[TextBlock(text="answer")], is_last=False)
                else:
                    yield ChatResponse(content=deepcopy(response.content), is_last=False)
                yield response

            return chunks()

    answer = ChatResponse(content=[
        ThinkingBlock(id="execution-thinking", thinking="model thinking"),
        TextBlock(text="model answer"),
    ], is_last=True)
    cloud = ExecutionOutput("actual-cloud-model", [answer] if tier == "cloud" else [])
    ctx = context(tmp_path, cloud=cloud)
    local_responses = []
    if tier == "edge":
        directory = existing_skill(tmp_path, "report")
        ctx.agent.toolkit = Toolkit(skills_or_loaders=[str(directory)])
        local_responses.append(call("Skill", {"skill": "report"}, "load"))
    local_responses.append(answer)
    lower = ExecutionOutput(f"actual-{tier}-model", local_responses)
    decision = route({
        "device": "device_text", "edge": "edge_skill", "cloud": "complex_or_negative",
    }[tier], **({"skill": "report"} if tier == "edge" else {}))
    orchestrator = prepare(
        monkeypatch, ctx, lower,
        [decision, {"protocol_version": "edge_cloud_verifier.v1", "passed": True}],
        settings=config(trace_stream_enabled=enabled),
    )
    envelope = Envelope()
    events = [e.model_copy(deep=True) async for e in orchestrator.run(ctx, envelope)]
    events.extend([e.model_copy(deep=True) async for e in envelope.finalize()])
    expected = f"【{AGENT_NAMES[tier]} | actual-{tier}-model】"
    execution_messages = [
        m for m in events[-1].output
        if m.type in {"message", "reasoning"}
        and any(getattr(c, "text", "").endswith(("model answer", "model thinking"))
                for c in m.content)
    ]
    assert {m.type for m in execution_messages} == {"message", "reasoning"}
    for message in execution_messages:
        labelled = enabled and message.type == "message"
        assert message.content[0].text.count(expected) == int(labelled)
        assert message.name == (AGENT_NAMES[tier] if labelled else "assistant")
        deltas = [e.text for e in events if e.object == "content"
                  and e.msg_id == message.id and e.type == "text" and e.delta]
        assert "".join(deltas).count(expected) == int(labelled)
        if message.type == "reasoning":
            assert message.content[0].text == "model thinking"
            assert "".join(deltas) == "model thinking"
    internal = json.dumps(envelope.response.model_dump(mode="json"), ensure_ascii=False)
    saved = json.dumps(ctx.agent.state_dict(), ensure_ascii=False)
    uploaded = json.dumps(ctx.extras["edge_cloud_upload"], ensure_ascii=False)
    assert expected not in internal + saved + uploaded
    assert "model answer" in internal + saved
    assert orchestrator._trace.final_agent == tier
    if enabled:
        decisions = [m for m in events[-1].output if m.name in {"RouterAgent", "VerifyAgent"}]
        assert {m.name for m in decisions} == (
            {"RouterAgent", "VerifyAgent"} if tier == "edge" else {"RouterAgent"}
        )
        assert all(m.type == "message" for m in decisions)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("source", ["device", "edge", "cloud", "fallback"])
async def test_client_execution_start_precedes_tools_without_waiting_for_answer(
    monkeypatch, tmp_path, enabled, source,
):
    from qwenpaw.runtime.edge_cloud.roles import AGENT_NAMES

    waiting, release = asyncio.Event(), asyncio.Event()

    class PausingExecution(Model):
        async def __call__(self, *args, **kwargs):
            response = await super().__call__(*args, **kwargs)
            if not any(isinstance(block, ToolCallBlock) for block in response.content):
                waiting.set()
                await release.wait()
            return response

    async def read_file(file_path: str):
        return ToolChunk(content=[TextBlock(text="unchanged tool result")])

    tier = "cloud" if source == "fallback" else source
    cloud_steps = [call("read_file", {"file_path": "cloud.txt"}), text("cloud answer")]
    cloud = PausingExecution("actual-cloud-model", cloud_steps)
    ctx = context(tmp_path, [FunctionTool(read_file)], cloud)
    if source == "edge":
        directory = existing_skill(tmp_path, "report")
        ctx.agent.toolkit = Toolkit(skills_or_loaders=[str(directory)])
        local_steps = [call("Skill", {"skill": "report"}, "load"), text("local answer")]
    else:
        local_steps = [call("read_file", {"file_path": "local.txt"}), text("local answer")]
    if source == "fallback":
        local_steps[-1] = LocalExecutionHandoff("DEVICE_TOOL_ROUND_LIMIT")
    lower_type = Model if source == "fallback" else PausingExecution
    lower = lower_type(f"actual-{source}-model", local_steps)
    decisions = [route({
        "device": "device_tools", "edge": "edge_skill",
        "cloud": "complex_or_negative", "fallback": "device_tools",
    }[source], **({"skill": "report"} if source == "edge" else {}))]
    if source != "cloud":
        decisions.append({
            "protocol_version": "edge_cloud_verifier.v1",
            "passed": source != "fallback",
        })
    orchestrator = prepare(
        monkeypatch, ctx, lower, decisions, settings=config(trace_stream_enabled=enabled),
    )
    envelope = Envelope()
    events = []

    async def collect():
        async for event in orchestrator.run(ctx, envelope):
            events.append(event.model_copy(deep=True))
        async for event in envelope.finalize():
            events.append(event.model_copy(deep=True))

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(waiting.wait(), 10)
        assert not task.done()
        starts = [
            event for event in events
            if event.object == "message" and event.type == "message"
            and event.name == AGENT_NAMES[tier] and event.content
            and "开始执行任务" in event.content[0].text
        ]
        assert len(starts) == int(enabled)
        if enabled:
            start = starts[0]
            assert start.content[0].text.startswith(
                f"【{AGENT_NAMES[tier]} | actual-{tier}-model】"
            )
            assert start.status == "completed"
            start_index = events.index(start)
            tool_indices = [
                index for index, event in enumerate(events)
                if event.object == "message" and event.type == "plugin_call"
            ]
            if source == "fallback":
                assert not any(event.name == "VerifyAgent" for event in events
                               if event.object == "message")
                assert min(tool_indices) < start_index < max(tool_indices)
            else:
                assert start_index < min(tool_indices)
            internal = json.dumps(envelope.response.model_dump(mode="json"), ensure_ascii=False)
            partial = json.dumps(envelope.collect_partial_blocks(), ensure_ascii=False)
            assert "开始执行任务" not in internal + partial
        release.set()
        await asyncio.wait_for(task, 10)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    output = events[-1].output
    if enabled:
        assert sum(message.id == start.id for message in output) == 1
        assert output[0].name == "RouterAgent"
    else:
        assert all(message.name == "assistant" for message in output)
        assert "【" not in json.dumps([message.model_dump() for message in output])
    internal = json.dumps(envelope.response.model_dump(mode="json"), ensure_ascii=False)
    saved = json.dumps(ctx.agent.state_dict(), ensure_ascii=False)
    uploaded = json.dumps(ctx.extras["edge_cloud_upload"], ensure_ascii=False)
    assert "开始执行任务" not in internal + saved + uploaded
    sequences = [event.sequence_number for event in events]
    assert sequences == sorted(set(sequences))
