"""Batch feedback must preserve normal delivery, verification and handoffs."""

import importlib
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from agentscope.message import ToolResultState

from test_final_files import body, context, ctx, file, finish
from qwenpaw.agents.tools.agent_management import format_agent_chat_text
from qwenpaw.agents.tools.send_final_files import send_final_files_to_user
from qwenpaw.runtime import final_files as delivery


@pytest.mark.parametrize("verify", [False, True])
@pytest.mark.parametrize("delegated", [False, True])
async def test_failed_batch_reports_all_paths_then_full_retry_delivers(
    ctx, tmp_path, verify, delegated
):
    request_ctx, token = ctx, None
    parent_state = delivery.current()
    if delegated:
        payload = {"session_id": "child", "request_context": {}}
        delivery.prepare_child_request(payload, "child")
        request_ctx = context(tmp_path, "child", "child", payload["request_context"])
        token = delivery.bind_request(request_ctx)
    try:
        state = delivery.current()
        state.begin_attempt("edge" if verify else "ordinary", verify=verify)
        previous = body(await send_final_files_to_user([file(tmp_path, "old.txt")]))
        existing = [file(tmp_path, "报告.pdf"), file(tmp_path, "数据.xlsx")]
        missing = ["演示.pptx", "明细.csv"]
        selected = [existing[0], missing[0], existing[1], missing[1]]
        records_before = set((tmp_path / "store" / "artifacts").iterdir())

        chunk = await send_final_files_to_user(selected)
        failed = body(chunk)
        assert chunk.state == ToolResultState.ERROR
        assert failed.keys() == previous.keys()
        assert failed["retry"].keys() == previous["retry"].keys()
        assert failed["status"] == "rejected"
        assert failed["attempt_id"] == previous["attempt_id"]
        assert failed["submitted"] is False and failed["files"] == []
        assert failed["existing_files"] == [
            {"file_path": path, "resolved_path": str(Path(path).resolve())}
            for path in existing
        ]
        assert [e["file_path"] for e in failed["errors"]] == missing
        assert [e["resolved_path"] for e in failed["errors"]] == [
            str((tmp_path / name).resolve()) for name in missing
        ]
        assert all("FILE_NOT_FOUND" in e["error"] for e in failed["errors"])
        assert failed["error"]
        assert failed["retry"]["required"] is True
        assert failed["retry"]["submission_mode"] == "replace_all"
        assert failed["retry"]["require_complete_file_list"] is True
        message = failed["retry"]["message"]
        assert "不得只提交修正项" in message and "不得因之前调用过" in message
        assert ("当前委派任务" in message) is delegated
        assert not state.submitted and not state.files and not state.verified
        assert set((tmp_path / "store" / "artifacts").iterdir()) == records_before
        response, _ = await finish(request_ctx)
        assert not response.output and not response.metadata

        for name in missing:
            file(tmp_path, name)
        chunk = await send_final_files_to_user(selected)
        success = body(chunk)
        assert chunk.state == ToolResultState.SUCCESS
        assert success.keys() == failed.keys()
        assert success["status"] == (
            "pending_verification" if verify else "pending_finalization"
        )
        assert success["submitted"] is True
        assert success["errors"] == [] and success["error"] == ""
        assert success["retry"]["required"] is False
        assert success["retry"]["require_complete_file_list"] is True
        assert [f["name"] for f in success["files"]] == [
            "报告.pdf", "演示.pptx", "数据.xlsx", "明细.csv"
        ]
        assert "snapshot" not in success["files"][0]
        assert set(state.manifest()) == {"status", "attempt_id", "submitted", "files"}

        if verify:
            response, _ = await finish(request_ctx)
            assert not response.output and not response.metadata
            state.verified = True
        response, _ = await finish(request_ctx)
        if delegated:
            manifest = response.metadata[delivery.METADATA_KEY]
            assert manifest["status"] == "ready_for_parent"
            assert len(manifest["files"]) == 4
            assert not response.output
        else:
            assert len(response.output[-1].content) == 4
    finally:
        if token is not None:
            delivery.reset_request(token)

    if delegated:
        assert delivery.current() is parent_state and not parent_state.submitted
        text = format_agent_chat_text(response.model_dump(mode="json"), "child")
        imported = json.loads(text.split("[FINAL_FILES]\n")[1])
        result = await send_final_files_to_user([f["file_path"] for f in imported])
        assert result.state == ToolResultState.SUCCESS
        response, _ = await finish(ctx)
        assert len(response.output[-1].content) == 4


async def test_all_missing_files_have_individual_feedback(ctx, tmp_path):
    paths = [str(tmp_path / "missing.pdf"), "missing.xlsx"]
    chunk = await send_final_files_to_user(paths)
    failed = body(chunk)
    assert chunk.state == ToolResultState.ERROR
    assert failed["existing_files"] == [] and failed["files"] == []
    assert [e["file_path"] for e in failed["errors"]] == paths
    assert failed["retry"]["required"] is True
    assert not (tmp_path / "store" / "artifacts").exists()


async def test_mixed_invalid_paths_preserve_all_diagnostics(ctx, tmp_path):
    good = file(tmp_path, "good.txt")
    directory = tmp_path / "directory"
    directory.mkdir()
    sensitive = file(tmp_path, ".env")
    invalid = [str(directory), sensitive, None, "bad\0path", "missing.txt"]
    chunk = await send_final_files_to_user([*invalid, good])
    failed = body(chunk)
    assert chunk.state == ToolResultState.ERROR
    assert failed["existing_files"] == [{"file_path": good, "resolved_path": good}]
    assert [e["file_path"] for e in failed["errors"]] == invalid
    assert "not a file" in failed["errors"][0]["error"]
    assert "sensitive" in failed["errors"][1]["error"]
    assert failed["errors"][2]["resolved_path"] is None
    assert "FILE_NOT_FOUND" in failed["errors"][-1]["error"]
    assert not delivery.current().submitted


async def test_encoded_aliases_deduplicate_feedback_and_snapshots(ctx, tmp_path):
    path = Path(file(tmp_path, "报告 空格.pdf"))
    raw = "  " + quote(path.name) + "  "
    chunk = await send_final_files_to_user([raw, str(path), path.name])
    success = body(chunk)
    assert chunk.state == ToolResultState.SUCCESS
    assert success["existing_files"] == [
        {"file_path": raw, "resolved_path": str(path.resolve())}
    ]
    assert len(success["files"]) == 1
    assert len(list((tmp_path / "store" / "artifacts").iterdir())) == 1


async def test_disappearing_source_reports_file_without_releasing_partial_batch(
    ctx, tmp_path, monkeypatch
):
    tool = importlib.import_module("qwenpaw.agents.tools.send_final_files")
    first, second = file(tmp_path, "first.pdf"), file(tmp_path, "second.pdf")
    snapshot = tool.snapshot_files

    def disappear(paths, state):
        # Inject the same race whether snapshots are copied in one batch or
        # one source at a time; do not depend on the tool's loop structure.
        if Path(second) in paths:
            Path(second).unlink()
        return snapshot(paths, state)

    monkeypatch.setattr(tool, "snapshot_files", disappear)
    chunk = await send_final_files_to_user([first, second])
    failed = body(chunk)
    assert chunk.state == ToolResultState.ERROR
    assert len(failed["existing_files"]) == 2
    assert failed["files"] == [] and failed["submitted"] is False
    assert failed["errors"][0]["file_path"] == second
    assert failed["errors"][0]["resolved_path"] == str(Path(second).resolve())
    assert len(failed["errors"]) == 1
    assert not delivery.current().files
    response, _ = await finish(ctx)
    assert not response.output


@pytest.mark.parametrize("paths", [[], None, "report.pdf"])
async def test_invalid_batch_uses_same_feedback_envelope(ctx, tmp_path, paths):
    success = body(await send_final_files_to_user([file(tmp_path)]))
    chunk = await send_final_files_to_user(paths)
    failed = body(chunk)
    assert chunk.state == ToolResultState.ERROR
    assert failed.keys() == success.keys()
    assert failed["status"] == "rejected" and failed["submitted"] is False
    assert failed["files"] == [] and failed["existing_files"] == []
    assert failed["errors"] == [{
        "file_path": None,
        "resolved_path": None,
        "error": failed["error"],
    }]
    assert failed["retry"]["required"] is True
    response, _ = await finish(ctx)
    assert not response.output


@pytest.mark.parametrize("unavailable", ["disabled", "invalid_context", "finished"])
async def test_unavailable_context_does_not_encourage_retry(
    ctx, tmp_path, monkeypatch, unavailable
):
    success = body(await send_final_files_to_user([file(tmp_path)]))
    token = None
    if unavailable == "disabled":
        monkeypatch.setenv("QWENPAW_FINAL_FILES_ENABLED", "0")
        token = delivery.bind_request(context(tmp_path))
    else:
        setattr(delivery.current(), unavailable, True)
    try:
        chunk = await send_final_files_to_user(["missing.pdf"])
        failed = body(chunk)
        assert chunk.state == ToolResultState.ERROR
        assert failed.keys() == success.keys()
        assert failed["submitted"] is False and failed["files"] == []
        assert failed["existing_files"] == []
        assert failed["retry"]["required"] is False
        assert failed["attempt_id"] == (
            None if unavailable == "disabled" else success["attempt_id"]
        )
    finally:
        if token is not None:
            delivery.reset_request(token)
