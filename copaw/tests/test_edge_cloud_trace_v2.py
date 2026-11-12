"""Collaborative turn bodies share the native delta contract; configs stay v2."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from qwenpaw.observability import session_delta_langfuse
from qwenpaw.observability.session_delta_langfuse import (
    _config_exists,
    _record_config,
    upload_session_delta,
)
from qwenpaw.observability.session_delta_redactor import (
    redact_session_delta_payload,
)
from qwenpaw.runtime.edge_cloud.orchestrator import (
    EdgeCloudOrchestrator,
    _StageRecorder,
)


def _snapshot(*, system, messages=(), tools=(), model="cloud"):
    return {
        "provider_id": "test",
        "model": model,
        "stream": True,
        "tool_choice": "auto",
        "messages": [{"role": "system", "content": system}, *messages],
        "tools": list(tools),
        "params": {"temperature": 0.2},
        "model_parameters": {"max_tokens": 1024},
        "generate_kwargs": {},
    }


def _recorder(stage, snapshot):
    recorder = _StageRecorder(stage)
    recorder.add(snapshot)
    return recorder


def _orchestrator_with_stages():
    orchestrator = object.__new__(EdgeCloudOrchestrator)
    orchestrator._trace = SimpleNamespace(
        flow_id="flow-1",
        final_agent="cloud",
        fallback=True,
        fallback_reason="VALIDATION_FAIL",
        usage_by_stage={
            "router": {"call_count": 1, "prompt_tokens": 10},
        },
    )
    orchestrator._cloud_handoff_used = True
    orchestrator._stage_outcomes = {
        "router": {
            "status": "ok",
            "decision": {"task_type": "edge_skill", "target_agent": "edge"},
        },
        "edge": {"status": "ok", "final_answer": "draft"},
        "verifier": {
            "status": "ok",
            "verdict": {"passed": False, "failure_reason": "missing csv"},
        },
        "cloud": {"status": "ok", "final_answer": "done"},
    }
    orchestrator._stage_recorders = {
        "router": _recorder(
            "router",
            _snapshot(
                system="ROUTER SYSTEM",
                messages=[{"role": "user", "content": "route this"}],
                tools=[{"function": {"name": "submit_route_decision"}}],
            ),
        ),
        "edge": _recorder(
            "edge",
            _snapshot(
                system="EDGE SYSTEM",
                messages=[
                    {"role": "user", "content": "edge task"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_call",
                                "name": "read_file",
                                "input": {"file_path": "invoice.pdf"},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "name": "read_file",
                        "content": "table",
                    },
                ],
                tools=[{"function": {"name": "read_file"}}],
                model="edge-model",
            ),
        ),
        "verifier": _recorder(
            "verifier",
            _snapshot(
                system="VERIFIER SYSTEM",
                messages=[{"role": "user", "content": "evidence"}],
                tools=[{"function": {"name": "submit_verification"}}],
            ),
        ),
        "cloud": _recorder(
            "cloud",
            _snapshot(
                system="DEFAULT SYSTEM",
                messages=[
                    {"role": "user", "content": "original request"},
                    {"role": "user", "content": "[运行时接管说明]"},
                    {"role": "assistant", "content": "done"},
                ],
                tools=[
                    {"function": {"name": "read_file"}},
                    {"function": {"name": "read_handoff_artifact"}},
                ],
            ),
        ),
    }
    return orchestrator


def test_v2_payload_keeps_one_model_per_role_and_existing_session_configs():
    orchestrator = _orchestrator_with_stages()
    orchestrator._stage_recorders["edge"].add(_snapshot(
        system="EDGE SYSTEM", messages=[{"role": "user", "content": "last edge request"}],
        tools=[{"function": {"name": "read_file"}}], model="edge-model-last",
    ))
    upload = orchestrator._build_edge_cloud_upload(
        SimpleNamespace(session_id="sess-1", agent_id="default"),
        SimpleNamespace(task_type="edge_skill", target_agent="edge"),
    )

    assert set(upload) >= {"model_requests", "configs", "edge_cloud"}
    assert not any(key.startswith("stage.") for key in upload)
    models = {item["agent"]: item for item in upload["model_requests"]}
    assert len(upload["model_requests"]) == len(models) == 4
    assert "DeviceAgent" not in models
    assert models["CloudAgent"]["config_ref"] == {
        "prompt": "default",
        "tools": "cloud_handoff",
    }
    assert models["EdgeAgent"]["model"] == "edge-model-last"
    assert models["RouterAgent"]["model"] == models["VerifyAgent"]["model"] == models["CloudAgent"]["model"]
    assert all("messages" not in item and "tools" not in item for item in models.values())
    assert models["RouterAgent"]["usage"]["call_count"] == 1

    configs = {
        (config["config_key"], config["record_type"])
        for config in upload["configs"]
    }
    assert ("router", "config.prompt") in configs
    assert ("router", "config.tools") in configs
    assert ("edge", "config.prompt") in configs
    assert ("edge", "config.tools") in configs
    assert ("verifier", "config.prompt") in configs
    assert ("verifier", "config.tools") in configs
    assert ("default", "config.prompt") in configs
    assert ("cloud_handoff", "config.tools") in configs
    assert "stages" not in upload["edge_cloud"]


def test_v2_redaction_covers_configs_messages_and_outcomes():
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    payload = {
        "protocol": "edge_cloud_trace.v2",
        "session_id": "sess-1",
        "agent_id": "default",
        "configs": [
            {
                "record_type": "config.prompt",
                "config_key": "edge",
                "system_prompt": f"system contains {secret}",
            },
            {
                "record_type": "config.tools",
                "config_key": "edge",
                "tools": [
                    {
                        "name": "read_file",
                        "description": f"tool contains {secret}",
                        "input_schema": {"type": "object"},
                    }
                ],
            },
        ],
        "stage.edge": {
            "messages": [{"role": "user", "content": f"message {secret}"}],
            "outcome": {
                "status": "ok",
                "final_answer": f"answer {secret}",
            },
        },
        "edge_cloud": {"flow_id": "flow-1"},
    }

    redacted = redact_session_delta_payload(payload)
    text = json.dumps(redacted, ensure_ascii=False)

    assert secret not in text
    assert "[REDACTED" in text
    assert redacted["edge_cloud"]["flow_id"] == "flow-1"


def test_config_dedupe_is_per_session_key_and_type(tmp_path):
    db_path = tmp_path / "history.db"

    assert _config_exists(db_path, "sess-1", "edge", "config.prompt") is False
    _record_config(db_path, "sess-1", "edge", "config.prompt", "hash-1")
    assert _config_exists(db_path, "sess-1", "edge", "config.prompt") is True
    assert _config_exists(db_path, "sess-1", "edge", "config.tools") is False
    assert _config_exists(db_path, "sess-2", "edge", "config.prompt") is False


def test_upload_sync_skips_configs_seen_in_earlier_turns(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(session_delta_langfuse, "_client", lambda: object())
    monkeypatch.setattr(
        session_delta_langfuse,
        "_propagate_context",
        lambda *_: nullcontext(),
    )
    monkeypatch.setattr(
        session_delta_langfuse,
        "_send_edge_cloud_config_span",
        lambda client, payload, config: calls.append(
            (config["record_type"], config["config_key"])
        ),
    )
    monkeypatch.setattr(
        session_delta_langfuse,
        "_send_edge_cloud_delta_span",
        lambda client, payload: calls.append(("delta", payload["turn_id"])),
    )

    payload = {
        "protocol": "edge_cloud_trace.v2",
        "session_id": "sess-1",
        "workspace_dir": str(tmp_path),
        "turn_id": "turn-1",
        "configs": [
            {
                "record_type": "config.prompt",
                "config_key": "router",
                "system_prompt": "router prompt",
            },
            {
                "record_type": "config.tools",
                "config_key": "router",
                "tools": [],
            },
        ],
        "edge_cloud": {"stages": ["router"]},
    }

    session_delta_langfuse._upload_edge_cloud_sync(payload)
    session_delta_langfuse._upload_edge_cloud_sync(payload)

    assert calls == [
        ("config.prompt", "router"),
        ("config.tools", "router"),
        ("delta", "turn-1"),
        ("delta", "turn-1"),
    ]

    payload["turn_id"] = "turn-2"
    payload["configs"] = [
        {
            "record_type": "config.prompt",
            "config_key": "edge",
            "system_prompt": "edge prompt",
        }
    ]
    session_delta_langfuse._upload_edge_cloud_sync(payload)

    assert calls[-2:] == [("config.prompt", "edge"), ("delta", "turn-2")]


@pytest.mark.asyncio
async def test_upload_session_delta_dispatches_v2(monkeypatch):
    captured = {}

    async def fake_upload(payload):
        captured["payload"] = payload

    monkeypatch.setattr(
        session_delta_langfuse, "_upload_edge_cloud", fake_upload
    )

    await upload_session_delta({"protocol": "edge_cloud_trace.v2"})

    assert captured["payload"]["protocol"] == "edge_cloud_trace.v2"
