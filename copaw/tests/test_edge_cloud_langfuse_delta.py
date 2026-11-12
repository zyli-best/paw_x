"""Flat collaborative uploads preserve native storage and upload boundaries."""

import json
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg, TextBlock, ThinkingBlock
from agentscope.model import ChatResponse
from agentscope.tool import FunctionTool, ToolChunk, Toolkit

from test_edge_cloud import (
    Model,
    call,
    clean_capture,
    config,
    context,
    existing_skill,
    route,
    text,
)
from test_edge_cloud_session_process import runtime_for, wire_message
from test_final_files import ctx
from qwenpaw.observability import session_delta_langfuse as uploader
from qwenpaw.runtime.edge_cloud.langfuse_delta import LangfuseDeltaRecorder
from qwenpaw.runtime.session_views import canonical_session_state
from qwenpaw.schemas import FileContent, TextContent

COMMON_INPUT = {
    "session_id",
    "agent_calls",
    "delta",
    "session_skills",
    "model_requests",
    "edge_cloud",
}


@pytest.mark.parametrize("store", [False, True])
@pytest.mark.parametrize("trace", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
async def test_runtime_upload_records_visible_turn_and_one_config_per_actual_role(
    monkeypatch,
    tmp_path,
    store,
    trace,
    fallback,
):
    async def read_file(file_path: str):
        return ToolChunk(content=[TextBlock(text="real file result")])

    choice = route("edge_skill", skill="report", reason="one existing Skill")
    verdict = {
        "protocol_version": "edge_cloud_verifier.v1",
        "passed": not fallback,
        "failure_reason": "missing requirement" if fallback else "",
    }
    cloud = Model(
        "same-cloud-model",
        [
            call("Skill", {"skill": "report"}, "router-read"),
            ChatResponse(
                content=[
                    ThinkingBlock(thinking="real route thinking"),
                    *call("submit_route_decision", choice).content,
                ],
                is_last=True,
            ),
            call("Skill", {"skill": "report"}, "verify-read"),
            call("submit_verification", verdict),
            *([text("cloud repaired answer")] if fallback else []),
        ],
    )
    edge = Model(
        "edge-model",
        [
            call("Skill", {"skill": "report"}, "edge-skill"),
            call("read_file", {"file_path": "report.txt"}, "edge-read"),
            text("edge answer </think>"),
        ],
    )
    ctx = context(tmp_path, cloud=cloud)
    directory = existing_skill(
        tmp_path / "skills", "report", "Read report.txt and report the result."
    )
    ctx.agent.toolkit = Toolkit(
        tools=[FunctionTool(read_file)], skills_or_loaders=[str(directory)]
    )
    runtime, session, uploads = runtime_for(
        monkeypatch,
        ctx,
        config(session_process_enabled=store, trace_stream_enabled=trace),
    )
    monkeypatch.setattr(
        "qwenpaw.agents.model_factory.create_openai_compatible_model_and_formatter",
        lambda **_: (edge, None),
    )
    events = [event.model_copy(deep=True) async for event in runtime.run(ctx.request)]
    assert len(uploads) == 1
    payload = uploads[0]
    models = {item["agent"]: item for item in payload["model_requests"]}
    expected = {"RouterAgent", "EdgeAgent", "VerifyAgent"} | (
        {"CloudAgent"} if fallback else set()
    )
    assert set(models) == expected and len(models) == len(payload["model_requests"])
    assert (
        models["RouterAgent"]["model"]
        == models["VerifyAgent"]["model"]
        == "same-cloud-model"
    )
    assert len(edge.requests) == 3  # All ReAct rounds still produce one role config.
    assert models["EdgeAgent"]["model"] == "edge-model"
    assert not any(key.startswith("stage.") for key in payload)
    assert "stages" not in payload["edge_cloud"]
    assert payload["delta_count"] == len(payload["delta"])
    delta = [Msg.model_validate(message) for message in payload["delta"]]
    blocks = [block for message in delta for block in message.content]
    body = "\n".join(block.text for block in blocks if block.type == "text")
    assert ("【RouterAgent | same-cloud-model】" in body) is trace
    assert ("【EdgeAgent | edge-model】" in body) is trace
    assert ("【VerifyAgent | same-cloud-model】" in body) is trace
    assert body.count("edge answer </think>") == 1
    if fallback:
        assert body.count("cloud repaired answer") == 1
        assert ("【CloudAgent | same-cloud-model】" in body) is trace
    if trace:
        route_index = next(
            i for i, m in enumerate(delta) if "已选择" in (m.get_text_content() or "")
        )
        edge_index = next(
            i
            for i, m in enumerate(delta)
            if "【EdgeAgent" in (m.get_text_content() or "")
        )
        verify_index = next(
            i
            for i, m in enumerate(delta)
            if "【VerifyAgent" in (m.get_text_content() or "")
        )
        assert route_index < edge_index < verify_index
        assert any(
            block.type == "thinking" and block.thinking == "real route thinking"
            for block in blocks
        )
        assert all(
            m.metadata.get("model_ref") in expected
            for m in delta
            if m.role == "assistant"
        )
    for block in (item for item in blocks if item.type == "tool_call"):
        assert (
            sum(item.id == block.id for item in blocks if item.type == "tool_result")
            == 1
        )
    assert sum(m.role == "user" for m in delta) == 1
    assert payload["agent_calls"] == []
    assert canonical_session_state(session.saved) == ctx.agent.state_dict()
    if not store:
        assert session.saved == ctx.agent.state_dict()
        assert "session_context_views" not in ctx.extras
    # Upload annotations and local recovery data never cross the save boundary.
    assert "model_ref" not in json.dumps(session.saved)
    assert "qwenpaw_session_context_view" not in json.dumps(payload)
    assert not any(
        m.get("metadata", {}).get("model_ref")
        for m in ctx.agent.state_dict()["state"]["context"]
    )
    assert events[-1].status == "completed"


def test_upload_collector_merges_chunks_and_deliveries_without_changing_events_or_session(
    tmp_path,
):
    ctx = context(tmp_path)
    ctx.extras["edge_cloud_upload"] = {
        "protocol": "edge_cloud_trace.v2",
        "model_requests": [],
    }
    recorder = LangfuseDeltaRecorder(ctx.agent.name)
    header = wire_message(
        "answer", content=[TextContent(text="【EdgeAgent | model】\n\n")]
    )
    header.name = "EdgeAgent"
    original = header.model_dump(mode="json")
    recorder.record(header)
    recorder.record(TextContent(msg_id="answer", index=0, delta=True, text="partial"))
    recorder.record(
        TextContent(
            msg_id="answer",
            index=0,
            delta=False,
            text="【EdgeAgent | model】\n\ncomplete",
        )
    )
    delivered = wire_message(
        "answer",
        content=[
            TextContent(text="【EdgeAgent | model】\n\ncomplete"),
            FileContent(
                filename="report.txt", file_url=(tmp_path / "report.txt").as_uri()
            ),
        ],
    )
    delivered.name = "EdgeAgent"
    recorder.record(delivered)
    native = deepcopy(ctx.agent.state_dict())
    recorder.attach(ctx)
    assert ctx.agent.state_dict() == native
    assert header.model_dump(mode="json") == original
    assert "session_context_views" not in ctx.extras
    messages = ctx.extras["edge_cloud_upload"]["delta"]
    assert len(messages) == 2 and messages[0]["role"] == "user"
    assert messages[-1]["content"][0]["text"] == "【EdgeAgent | model】\n\ncomplete"
    assert messages[-1]["content"][1]["name"] == "report.txt"


class FakeClient:
    def __init__(self):
        self.spans = []

    def start_observation(self, **kwargs):
        span = SimpleNamespace(**kwargs)
        span.update = lambda **update: setattr(span, "output", update["output"])
        span.end = lambda: None
        self.spans.append(span)
        return span

    def flush(self):
        pass


async def test_real_upload_preserves_config_dedupe_redaction_and_common_input(
    monkeypatch, tmp_path
):
    from test_edge_cloud_trace_v2 import _orchestrator_with_stages

    orchestrator = _orchestrator_with_stages()
    payload = orchestrator._build_edge_cloud_upload(
        SimpleNamespace(session_id="s", agent_id="default"),
        SimpleNamespace(task_type="edge_skill", target_agent="edge"),
    )
    private = "CUSTOM_PRIVATE_RESULT"
    payload.update(
        {
            "session_id": "s",
            "workspace_dir": str(tmp_path),
            "user_id": "u",
            "agent_id": "default",
            "delta": [
                {
                    "id": "stable-msg",
                    "name": "Default",
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": f"【EdgeAgent | edge】 {private}"},
                        {
                            "type": "thinking",
                            "thinking": private,
                            "id": "stable-thinking",
                        },
                        {
                            "type": "tool_call",
                            "id": "stable-call",
                            "name": "read_file",
                            "input": "{}",
                            "state": "finished",
                        },
                        {
                            "type": "tool_result",
                            "id": "stable-call",
                            "name": "read_file",
                            "output": private,
                            "state": "success",
                        },
                    ],
                }
            ],
            "delta_count": 1,
            "agent_calls": [],
            "session_skills": [],
            "_sensitive_words": {"patterns": [{"type": "custom", "keyword": private}]},
        }
    )
    payload["configs"][0]["system_prompt"] += private
    payload["model_requests"][0]["params"]["note"] = private
    original = deepcopy(payload)
    client = FakeClient()
    monkeypatch.setattr(uploader, "_client", lambda: client)
    monkeypatch.setattr(uploader, "_propagate_context", lambda *_: nullcontext())
    monkeypatch.setattr(uploader, "is_session_delta_langfuse_enabled", lambda: True)
    await uploader.upload_session_delta(payload)
    await uploader.upload_session_delta(payload)
    configs = [span for span in client.spans if span.name != "qwenpaw.session_delta"]
    turns = [span for span in client.spans if span.name == "qwenpaw.session_delta"]
    assert len(configs) == len(payload["configs"]) and len(turns) == 2
    assert all(
        span.name
        in {"qwenpaw.session_delta.config.prompt", "qwenpaw.session_delta.config.tools"}
        for span in configs
    )
    assert set(turns[0].input) == COMMON_INPUT
    assert len(turns[0].input["model_requests"]) == 4
    assert private not in json.dumps([span.input for span in client.spans])
    assert any(
        block["type"] == "thinking" for block in turns[0].input["delta"][0]["content"]
    )
    assert turns[0].input["delta"][0]["content"][2]["id"] == "stable-call"
    assert turns[0].input["delta"][0]["content"][2]["input"] == "{}"
    assert turns[0].metadata["content_format"] == "session_messages.v1"
    assert "stage_count" not in turns[0].metadata and "stages" not in turns[0].output
    assert payload == original


async def test_non_collaborative_upload_keeps_latest_config_and_thinking_filter(
    monkeypatch,
):
    client = FakeClient()
    monkeypatch.setattr(uploader, "_client", lambda: client)
    monkeypatch.setattr(uploader, "_propagate_context", lambda *_: nullcontext())
    monkeypatch.setattr(uploader, "is_session_delta_langfuse_enabled", lambda: True)
    payload = {
        "session_id": "ordinary",
        "user_id": "u",
        "agent_calls": [],
        "session_skills": [],
        "delta": [
            {
                "role": "assistant",
                "name": "Default",
                "content": [
                    {"type": "thinking", "thinking": "original thought"},
                    {"type": "text", "text": "ordinary answer"},
                ],
            }
        ],
        "model_requests": [
            {"model": "earlier", "params": {}},
            {"model": "latest", "params": {"temperature": 0.2}},
        ],
    }
    original = deepcopy(payload)
    await uploader.upload_session_delta(payload)
    main = next(span for span in client.spans if "delta" in span.input)
    assert set(main.input) == COMMON_INPUT
    assert main.input["edge_cloud"] is None
    assert len(main.input["model_requests"]) == 1
    assert main.input["model_requests"][0]["model"] == "latest"
    assert main.input["delta"][0]["content"] == [
        {"type": "text", "text": "ordinary answer"}
    ]
    assert "content_format" not in main.metadata
    assert payload == original


async def test_redaction_failure_skips_upload_without_mutating_source(monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(uploader, "is_session_delta_langfuse_enabled", lambda: True)
    monkeypatch.setattr(
        "qwenpaw.observability.session_delta_redactor.redact_session_delta_payload",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("redaction failed")),
    )
    monkeypatch.setattr(uploader, "_upload_edge_cloud_sync", sent)
    payload = {"protocol": "edge_cloud_trace.v2", "delta": [{"content": "private"}]}
    original = deepcopy(payload)
    await uploader.upload_session_delta(payload)
    sent.assert_not_called()
    assert payload == original


@pytest.mark.parametrize("passed", [False, True])
async def test_upload_observes_final_file_gate_and_includes_released_attachments(
    ctx, monkeypatch, tmp_path, passed
):
    from qwenpaw.agents.tools.send_final_files import send_final_files_to_user
    from qwenpaw.runtime.final_files.prompts import TOOL_NAME

    candidate = tmp_path / "candidate.txt"
    candidate.write_text("candidate", encoding="utf-8")
    repaired = tmp_path / "repaired.txt"
    repaired.write_text("repaired", encoding="utf-8")
    cloud = Model(
        "cloud",
        [
            call("submit_route_decision", route("device_tools")),
            call(
                "submit_verification",
                {"protocol_version": "edge_cloud_verifier.v1", "passed": passed},
            ),
            *(
                []
                if passed
                else [
                    call(TOOL_NAME, {"file_paths": [str(repaired)]}, "same-submit-id"),
                    text("cloud finished"),
                ]
            ),
        ],
    )
    device = Model(
        "device",
        [
            call(TOOL_NAME, {"file_paths": [str(candidate)]}, "same-submit-id"),
            text("device finished"),
        ],
    )
    request_ctx = context(
        tmp_path, [FunctionTool(send_final_files_to_user)], cloud=cloud
    )
    runtime, session, uploads = runtime_for(
        monkeypatch,
        request_ctx,
        config(trace_stream_enabled=True, session_process_enabled=False),
    )
    monkeypatch.setattr(
        "qwenpaw.agents.model_factory.create_openai_compatible_model_and_formatter",
        lambda **_: (device, None),
    )
    _ = [event async for event in runtime.run(request_ctx.request)]
    assert len(uploads) == 1
    messages = [Msg.model_validate(message) for message in uploads[0]["delta"]]
    content = [block for message in messages for block in message.content]
    files = [block.name for block in content if block.type == "data"]
    assert files == ["candidate.txt" if passed else "repaired.txt"]
    body = json.dumps(uploads[0]["delta"], ensure_ascii=False)
    assert ("candidate.txt" in body) is passed
    assert ("repaired.txt" in body) is not passed
    assert session.saved == request_ctx.agent.state_dict()


@pytest.mark.parametrize("lookup_error", [False, True])
async def test_disabled_upload_does_not_collect_or_change_session(
    monkeypatch, tmp_path, lookup_error
):
    cloud = Model("cloud", [call("submit_route_decision", route())])
    device = Model("device", [text("ordinary device answer")])
    request_ctx = context(tmp_path, cloud=cloud)
    runtime, session, _ = runtime_for(
        monkeypatch,
        request_ctx,
        config(trace_stream_enabled=True, session_process_enabled=False),
    )

    def enabled():
        if lookup_error:
            raise RuntimeError("SDK detection unavailable")
        return False

    monkeypatch.setattr(uploader, "is_session_delta_langfuse_enabled", enabled)
    monkeypatch.setattr(
        "qwenpaw.runtime.edge_cloud.langfuse_delta.LangfuseDeltaRecorder.__init__",
        lambda *_: pytest.fail(
            "Disabled uploads must not create a transcript recorder"
        ),
    )
    monkeypatch.setattr(
        "qwenpaw.agents.model_factory.create_openai_compatible_model_and_formatter",
        lambda **_: (device, None),
    )
    _ = [event async for event in runtime.run(request_ctx.request)]
    assert session.saved == request_ctx.agent.state_dict()
    assert "session_context_views" not in request_ctx.extras
