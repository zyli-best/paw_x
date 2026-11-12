"""First agent inputs include real prompts without changing model requests."""

import json
import logging
from copy import deepcopy
from types import SimpleNamespace

import pytest
from agentscope.tool import Toolkit

from qwenpaw.app.agent_context import set_current_session_id
from qwenpaw.observability.model_request_capture import (
    clear_model_request_captures,
    pop_model_requests_for_session,
    record_model_request,
)
from qwenpaw.runtime.edge_cloud.orchestrator import EdgeCloudOrchestrator
from qwenpaw.runtime.edge_cloud.roles import AGENT_NAMES
from qwenpaw.runtime.edge_cloud.trace import EdgeCloudTrace
from test_edge_cloud import (
    Model, call, config, consume, context, existing_skill, prepare_real_route,
    route, text,
)


@pytest.fixture
def input_logs(caplog, monkeypatch):
    logger = logging.getLogger("qwenpaw.runtime.edge_cloud.logging")
    caplog.set_level(logging.INFO, logger=logger.name)
    monkeypatch.setattr(logger, "propagate", False)
    logger.addHandler(caplog.handler)
    clear_model_request_captures()
    set_current_session_id("edge-test")
    yield caplog
    logger.removeHandler(caplog.handler)
    clear_model_request_captures()
    set_current_session_id("")


def first_inputs(logs):
    return [
        (record.args[0], record.args[2], json.loads(record.args[-1]))
        for record in logs.records
        if record.name == "qwenpaw.runtime.edge_cloud.logging"
        and "event=first_model_input " in record.getMessage()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["device_text", "edge_skill", "complex_or_negative"])
async def test_real_flows_print_each_participating_role_first_input(
    monkeypatch, tmp_path, input_logs, kind,
):
    execution = Model("cloud", [text("cloud result")])
    ctx = context(tmp_path, cloud=execution)
    decisions, local_replies = [], [text("local result")]
    if kind == "edge_skill":
        directory = existing_skill(tmp_path / "skills", "report")
        ctx.agent.toolkit = Toolkit(skills_or_loaders=[str(directory)])
        decisions.append(call("Skill", {"skill": "report"}, "inspect"))
        local_replies.insert(0, call("Skill", {"skill": "report"}, "execute"))
    decisions.append(call(
        "submit_route_decision",
        route(kind, skill="report" if kind == "edge_skill" else "", need_verifier=True),
        "route",
    ))
    if kind == "edge_skill":
        decisions.append(call("submit_verification", {
            "protocol_version": "edge_cloud_verifier.v1",
            "passed": False,
            "failure_reason": "needs cloud work",
        }, "verify"))
    control, local = Model("control", decisions), Model("local", local_replies)
    orchestrator = prepare_real_route(monkeypatch, ctx, local, control)
    orchestrator.config = config(log_detail="full")
    await consume(orchestrator, ctx)

    logged = {stage: payload for _, stage, payload in first_inputs(input_logs)}
    expected = {
        "device_text": {"router", "device"},
        "edge_skill": {"router", "edge", "verifier", "cloud"},
        "complex_or_negative": {"router", "cloud"},
    }[kind]
    assert set(logged) == expected
    assert len(first_inputs(input_logs)) == len(expected)
    assert orchestrator._trace.final_agent == ("device" if kind == "device_text" else "cloud")
    for stage, payload in logged.items():
        assert payload["agent_name"] == AGENT_NAMES[stage]
        first = orchestrator._stage_recorders[stage].snapshots[0]
        assert payload["messages"] == first["messages"]
        assert payload["tools"] == first["tools"]
        assert payload["messages"][0]["role"] == "system"
        assert payload["model"] == first["model"]
    assert "RouterAgent" in json.dumps(logged["router"]["messages"])
    if "verifier" in logged:
        assert "VerifyAgent" in json.dumps(logged["verifier"]["messages"])
        assert "local result" in json.dumps(logged["verifier"]["messages"])
    saved = pop_model_requests_for_session(ctx.session_id)
    assert all(item["messages"][0]["content"] == "DEFAULT SYSTEM" for item in saved)
    assert ctx.agent._system_prompt == "DEFAULT SYSTEM"


@pytest.mark.asyncio
async def test_first_input_is_printed_again_in_next_flow(
    monkeypatch, tmp_path, input_logs,
):
    ctx = context(tmp_path)
    control = Model("control", [
        call("submit_route_decision", route("complex_or_negative"), str(index))
        for index in range(2)
    ])
    orchestrator = prepare_real_route(monkeypatch, ctx, Model("unused"), control)
    orchestrator.config = config(log_detail="full")
    for _ in range(2):
        await consume(orchestrator, ctx)
    logged = first_inputs(input_logs)
    assert [stage for _, stage, _ in logged] == ["router", "cloud", "router", "cloud"]
    assert logged[0][0] == logged[1][0]
    assert logged[2][0] == logged[3][0]
    assert logged[0][0] != logged[2][0]


def orchestrator_for_stage(detail="full"):
    orchestrator = EdgeCloudOrchestrator(
        workspace=None, app_services=None, config=config(log_detail=detail),
    )
    orchestrator._trace = EdgeCloudTrace(base_agent_id="default")
    orchestrator._default_prompt = "DEFAULT SYSTEM"
    orchestrator._stage_recorders = {}
    return orchestrator


def capture(messages, tools):
    record_model_request(
        provider_id="test", model=SimpleNamespace(model="actual-model"),
        messages=messages, tools=tools, tool_choice=None, params={},
    )


@pytest.mark.parametrize("has_session", [True, False])
def test_skip_auxiliary_requests_keep_full_first_input_and_do_not_mutate(
    input_logs, has_session,
):
    if not has_session:
        set_current_session_id("")
    orchestrator = orchestrator_for_stage()
    ctx = SimpleNamespace(agent_id="default")
    messages = [
        {"role": "system", "content": "设备提示词\n" + "x" * 20000 + "\nEND"},
        {"role": "developer", "content": "developer instructions"},
        {"role": "user", "content": "task; api_key=test-secret"},
    ]
    before = deepcopy(messages)
    # Re-entering the same stage (or retrying) must not print another input.
    for _ in range(2):
        with orchestrator._model_request_stage(ctx, "device"):
            capture([{"role": "system", "content": "auxiliary headline"}], None)
            capture(messages, [])
            capture([{"role": "system", "content": "next ReAct round"}], [])
    logged = first_inputs(input_logs)
    assert len(logged) == 1
    payload = logged[0][2]
    assert payload["messages"][0] == messages[0]
    assert payload["messages"][1] == messages[1]
    assert "test-secret" not in input_logs.text
    assert payload["tools"] == []
    assert messages == before
    # The context callback must not leak into later, unrelated model calls.
    capture(messages, [])
    assert len(first_inputs(input_logs)) == 1
    saved = pop_model_requests_for_session("edge-test")
    assert len(saved) == (7 if has_session else 0)


@pytest.mark.parametrize("detail", ["off", "summary"])
def test_input_logging_respects_log_detail(input_logs, detail):
    orchestrator = orchestrator_for_stage(detail)
    with orchestrator._model_request_stage(SimpleNamespace(agent_id="default"), "cloud"):
        capture([{"role": "system", "content": "private prompt"}], [])
    assert not first_inputs(input_logs)
    assert len(pop_model_requests_for_session("edge-test")) == 1


def test_logging_failure_preserves_existing_request_captures(monkeypatch, input_logs):
    orchestrator = orchestrator_for_stage()

    def fail(*args, **kwargs):
        raise RuntimeError("diagnostic failure")

    monkeypatch.setattr(orchestrator, "_log", fail)
    with orchestrator._model_request_stage(SimpleNamespace(agent_id="default"), "edge"):
        capture([{"role": "system", "content": "real role prompt"}], [])
    assert len(orchestrator._stage_recorders["edge"].snapshots) == 1
    saved = pop_model_requests_for_session("edge-test")
    assert saved[0]["messages"] == [{"role": "system", "content": "DEFAULT SYSTEM"}]
