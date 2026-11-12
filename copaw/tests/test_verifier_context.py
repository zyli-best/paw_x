"""Evidence survives Verify-only hard-budget compaction and actual delivery."""

import asyncio
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock, ToolResultState

from qwenpaw.exceptions import ConfigurationException, ModelContextLengthExceededException
from qwenpaw.runtime.edge_cloud.function_call import consume_decision_call
from qwenpaw.runtime.edge_cloud.history_context import HistoryRecall
from qwenpaw.runtime.edge_cloud.verifier_context import VerifierContext
from test_edge_cloud import call, context
from test_edge_cloud_history_context import CountingModel, history, message


def inputs(payload):
    return [message("system", "UNCHANGED SYSTEM"),
            message("user", json.dumps(payload, ensure_ascii=False))]


def result(identity, output, name="recall_history"):
    return [
        Msg(name="VerifyAgent", role="assistant", content=[
            ToolCallBlock(id=identity, name=name, input="{}"),
        ]),
        Msg(name="verifier_tools", role="assistant", content=[
            ToolResultBlock(id=identity, name=name, output=output, state=ToolResultState.SUCCESS),
        ]),
    ]


def body(messages):
    return json.loads(next(msg for msg in messages if msg.role == "user").get_text_content())


def pages(messages):
    found = []
    for msg in messages:
        for block in msg.content:
            if isinstance(block, ToolResultBlock) and block.name == "recall_history":
                value = json.loads(block.output[0].text)
                if value.get("page_type") == "edge_cloud_history":
                    found.append(value)
    return found


@pytest.mark.parametrize("headroom", [0, 1])
async def test_no_eighty_percent_trigger_and_equality_is_allowed(history, headroom):
    source, _ = history
    model = CountingModel()
    messages = inputs({"execution_task": "T" * 1800, "completion_conditions": ["EXACT"]})
    tokens = await model.count_tokens(messages, [])
    model.context_size = tokens + model.parameters.max_tokens + headroom
    assert tokens > model.context_size * 0.8
    assert await source.prepare_verifier(model, messages, []) is messages
    assert source.store.count(source.session_id) == 0


async def test_compaction_keeps_task_conditions_and_failed_call_directory(history):
    source, _ = history
    model = CountingModel(8000)
    payload = {
        "execution_task": "Write the exact report to output.txt",
        "completion_conditions": ["Footer is END", "No extra files"],
        "executed_by": "device", "final_answer": "finished", "skill": {},
        "tool_results": [
            {"call_seq": 1, "tool": "write_file", "status": "error",
             "arguments": {"file_path": "output.txt"}, "file_operation_status": "unknown",
             "result": "FAILED" * 3000},
            {"call_seq": 2, "tool": "read_file", "status": "success",
             "arguments": {"file_path": "output.txt"}, "result": "report" * 3000},
        ],
    }
    messages = inputs(payload)
    before = deepcopy(messages)
    fitted = await source.prepare_verifier(model, messages, [])
    actual = body(fitted)
    for key in ("execution_task", "completion_conditions", "executed_by", "final_answer", "skill"):
        assert actual[key] == payload[key]
    assert [(row["call_seq"], row["tool"], row["status"]) for row in actual["tool_results"]] == [
        (1, "write_file", "error"), (2, "read_file", "success"),
    ]
    assert actual["tool_results"][0]["file_operation_status"] == "unknown"
    assert actual["tool_results"][0]["arguments"] == {"file_path": "output.txt"}
    assert messages == before
    seq = actual["history_context"]["seq"]
    assert json.loads(source.rows("SELECT content FROM conversation_history WHERE seq=?", (seq,))[0]["content"]) == payload


async def test_parallel_unseen_pages_keep_both_bodies_and_exact_pending_ranges(history):
    source, original = history
    model = CountingModel(8000)
    recall = HistoryRecall(original, source, verifier=True)
    messages = inputs({"execution_task": "check both outputs", "completion_conditions": ["both valid"]})
    for index, letter in enumerate(("A", "B")):
        seq = source.snapshot(letter * 10000, stage="verifier")
        output = await recall.call(op="expand", lo=seq, hi=seq, max_chars=4000)
        messages += result(str(index), output.content)
    fitted = await source.prepare_verifier(model, messages, [])
    actual = pages(fitted)
    assert len(actual) == 2
    assert all(0 < len(page["content"]) < 4000 for page in actual)
    assert all(page["next_offset"] == len(page["content"]) and not page["complete"] for page in actual)
    tracker = source._verifier_context
    assert not tracker.delivered and not tracker.delivered_blocks
    source.record_verifier_delivery(fitted)
    for page in actual:
        key = (page["seq"], page["json_path"])
        assert tracker.delivered[key] == [(0, len(page["content"]))]
    with pytest.raises(ValueError, match="不能判定通过"):
        source.validate_verifier_decision({"protocol_version": "edge_cloud_verifier.v1", "passed": True})
    assert await model.count_tokens(fitted, []) + 200 <= model.context_size


async def test_followup_pages_close_gaps_without_nested_references(history):
    source, original = history
    model = CountingModel(6000)
    recall = HistoryRecall(original, source, verifier=True)
    seq = source.snapshot("0123456789" * 2000, stage="verifier")
    output = await recall.call(op="expand", lo=seq, hi=seq, max_chars=10000)
    messages = inputs({"execution_task": "check requested range"}) + result("first", output.content)
    fitted = await source.prepare_verifier(model, messages, [])
    source.record_verifier_delivery(fitted)
    first = pages(fitted)[0]
    offset = first["next_offset"]
    assert 0 < offset < 10000
    # Ask only for the undelivered portion of the original requested page.
    for index in range(12):
        remaining = 10000 - offset
        if remaining == 0:
            break
        output = await recall.call(op="expand", lo=seq, hi=seq, offset=offset,
                                   max_chars=min(remaining, 1000))
        fitted += result(f"next-{index}", output.content)
        fitted = await source.prepare_verifier(model, fitted, [])
        source.record_verifier_delivery(fitted)
        page = pages(fitted)[-1]
        assert page["offset"] == offset
        assert page["seq"] == seq
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert offset == 10000
    source.validate_verifier_decision({"protocol_version": "edge_cloud_verifier.v1", "passed": True})
    for msg in fitted:
        for block in msg.content:
            if isinstance(block, ToolResultBlock) and block.metadata.get("edge_cloud_history_ref"):
                assert json.loads(block.output[0].text)["history_context"]["seq"] == seq


async def test_index_only_execution_result_blocks_pass_until_actual_body_is_delivered(history):
    source, original = history
    model = CountingModel(16000)
    seq = source.snapshot("HEADER" + "x" * 10000 + "END", stage="tool_evidence")
    payload = {"execution_task": "check footer", "completion_conditions": ["footer END"],
               "tool_results": [{"call_seq": 1, "tool": "read_file", "status": "success",
                                 "result": {"history_context": source.reference(seq)}}]}
    messages = inputs(payload)
    assert await source.prepare_verifier(model, messages, []) is messages
    source.record_verifier_delivery(messages)
    with pytest.raises(ValueError, match="不能判定通过"):
        source.validate_verifier_decision({"protocol_version": "edge_cloud_verifier.v1", "passed": True})
    source.validate_verifier_decision({"protocol_version": "edge_cloud_verifier.v1", "passed": False})
    reader = HistoryRecall(original, source, verifier=True)
    output = await reader.call(op="expand", lo=seq, hi=seq, offset=10006, max_chars=3)
    messages += result("footer", output.content)
    fitted = await source.prepare_verifier(model, messages, [])
    source.record_verifier_delivery(fitted)
    assert pages(fitted)[0]["content"] == "END"
    # Relevance is assessed by the verifier: unrelated leading file bytes do
    # not have to be read merely to validate this footer condition.
    source.validate_verifier_decision({"protocol_version": "edge_cloud_verifier.v1", "passed": True})


async def test_oversized_new_skill_is_pending_and_keeps_original_text(history):
    source, original = history
    model = CountingModel(4000)
    text = "SKILL" * 10000
    messages = inputs({"execution_task": "check skill"}) + result("skill", [TextBlock(text=text)], "Skill")
    fitted = await source.prepare_verifier(model, messages, [])
    ref = json.loads(fitted[-1].content[0].output[0].text)["history_context"]
    source.record_verifier_delivery(fitted)
    with pytest.raises(ValueError, match="不能判定通过"):
        source.validate_verifier_decision({"protocol_version": "edge_cloud_verifier.v1", "passed": True})
    reader = HistoryRecall(original, source, verifier=True)
    output = await reader.call(**ref["recall"])
    assert json.loads(output.content[0].text)["content"].startswith("SKILL")
    assert source.rows("SELECT content FROM conversation_history WHERE seq=?", (ref["seq"],))[0]["content"] == text


async def test_large_directory_is_paged_with_total_and_unseen_call_tracking(history):
    source, original = history
    model = CountingModel(6000)
    records = [{"call_seq": i, "tool": "read_file", "status": "success" if i % 2 else "error",
                "arguments": {"file_path": f"{i}.txt"}, "result": str(i) * 1000} for i in range(1, 31)]
    messages = inputs({"execution_task": "check all calls", "completion_conditions": ["no unresolved errors"],
                       "tool_results": records})
    fitted = await source.prepare_verifier(model, messages, [])
    payload = body(fitted)
    directory = payload["tool_catalog"]
    assert directory["total"] == 30
    assert directory["remaining"] == 30 - directory["shown"] > 0
    source.record_verifier_delivery(fitted)
    with pytest.raises(ValueError, match="不能判定通过"):
        source.validate_verifier_decision({"protocol_version": "edge_cloud_verifier.v1", "passed": True})
    reader = HistoryRecall(original, source, verifier=True)
    output = await reader.call(**directory["history_context"]["recall"])
    page = json.loads(output.content[0].text)
    row = json.loads(page["content"])
    assert row["call_seq"] == directory["next_index"] + 1
    assert row["status"] in {"error", "success"}
    assert await model.count_tokens(fitted, []) + 200 <= model.context_size


@pytest.mark.parametrize("error", [RuntimeError("model failed"), asyncio.CancelledError()])
async def test_failed_or_cancelled_model_request_does_not_mark_delivery(history, tmp_path, error):
    source, original = history
    model = CountingModel(16000)
    seq = source.snapshot("evidence", stage="verifier")
    reader = HistoryRecall(original, source, verifier=True)
    output = await reader.call(op="expand", lo=seq, hi=seq)
    messages = inputs({"task": "check"}) + result("read", output.content)
    model.responses = [error]
    completed = Mock(wraps=source.record_verifier_delivery)
    with pytest.raises(type(error)):
        await consume_decision_call(
            model, messages, stage="verifier", state=context(tmp_path).agent.state,
            read_tools={"recall_history": reader},
            prepare_request=lambda msgs, tools: source.prepare_verifier(model, msgs, tools),
            on_request_complete=completed, validate_decision=source.validate_verifier_decision,
        )
    completed.assert_not_called()
    assert not source._verifier_context.delivered


async def test_successful_model_call_still_cannot_pass_with_unprovided_page_tail(history, tmp_path):
    source, original = history
    model = CountingModel(4000)
    seq = source.snapshot("evidence" * 3000, stage="verifier")
    reader = HistoryRecall(original, source, verifier=True)
    output = await reader.call(op="expand", lo=seq, hi=seq, max_chars=10000)
    messages = inputs({"task": "check"}) + result("read", output.content)
    model.responses = [call("submit_verification", {"protocol_version": "edge_cloud_verifier.v1", "passed": True})]
    with pytest.raises(ValueError, match="不能判定通过"):
        await consume_decision_call(
            model, messages, stage="verifier", state=context(tmp_path).agent.state,
            read_tools={"recall_history": reader},
            prepare_request=lambda msgs, tools: source.prepare_verifier(model, msgs, tools),
            on_request_complete=source.record_verifier_delivery,
            validate_decision=source.validate_verifier_decision,
        )
    page = pages(model.requests[0][0])[0]
    assert source._verifier_context.delivered[(seq, "")] == [(0, len(page["content"]))]
    assert len(page["content"]) < 10000


async def test_uncompressible_task_is_preserved_and_request_is_not_sent(history):
    source, _ = history
    model = CountingModel(3000)
    messages = inputs({"execution_task": "EXACT" * 2000, "completion_conditions": ["never omit"]})
    before = deepcopy(messages)
    with pytest.raises(ModelContextLengthExceededException):
        await source.prepare_verifier(model, messages, [])
    assert messages == before and not model.requests


async def test_missing_budget_fails_closed(history):
    source, _ = history
    model = CountingModel()
    model.parameters.max_tokens = None
    with pytest.raises(ConfigurationException):
        await source.prepare_verifier(model, inputs({"execution_task": "check"}), [])


async def test_archive_write_failure_never_discards_evidence(history, monkeypatch):
    source, _ = history
    model = CountingModel(3000)
    messages = inputs({"execution_task": "check", "tool_results": [{"result": "x" * 20000}]})
    before = deepcopy(messages)
    monkeypatch.setattr(source, "snapshot", Mock(side_effect=OSError("disk unavailable")))
    with pytest.raises(OSError, match="disk unavailable"):
        await source.prepare_verifier(model, messages, [])
    assert messages == before and not model.requests


@pytest.mark.parametrize("passed", [True, 1, "true", "yes"])
async def test_pass_guard_uses_the_same_boolean_coercion_as_verifier_schema(history, passed):
    source, _ = history
    seq = source.snapshot("unseen evidence", stage="tool_evidence")
    messages = inputs({"tool_results": [{"result": {"history_context": source.reference(seq)}}]})
    await source.prepare_verifier(CountingModel(16000), messages, [])
    source.record_verifier_delivery(messages)
    with pytest.raises(ValueError, match="不能判定通过"):
        source.validate_verifier_decision({"protocol_version": "edge_cloud_verifier.v1", "passed": passed})


async def test_large_write_arguments_keep_exact_target_and_all_original_parameters(history):
    source, _ = history
    target = "工作目录/输出文件.txt"
    parameters = {"file_path": target, "content": "EXACT CONTENT" * 10000, "mode": "append"}
    payload = {"execution_task": "check write", "executed_by": "edge",
               "tool_results": [{"call_seq": 7, "tool": "write_file", "status": "error",
                                 "previous_outcome_unknown": True,
                                 "file_operation_status": "unknown", "arguments": parameters,
                                 "result": "interrupted"}]}
    fitted = await source.prepare_verifier(CountingModel(6000), inputs(payload), [])
    row = body(fitted)["tool_results"][0]
    assert row["arguments"]["file_path"] == target
    assert row["arguments"]["mode"] == "append"
    assert row["previous_outcome_unknown"] is True
    assert row["result"] == "interrupted"
    seq = body(fitted)["history_context"]["seq"]
    saved = json.loads(source.rows("SELECT content FROM conversation_history WHERE seq=?", (seq,))[0]["content"])
    assert saved["tool_results"][0]["arguments"] == parameters


async def test_input_hint_excludes_the_page_body_currently_being_provided(history):
    source, original = history
    model = CountingModel(4000)
    seq = source.snapshot("evidence" * 2000, stage="verifier")
    reader = HistoryRecall(original, source, verifier=True)
    output = await reader.call(op="expand", lo=seq, hi=seq, max_chars=8000)
    fitted = await source.prepare_verifier(model, inputs({"task": "check"}) + result("read", output.content), [])
    page = pages(fitted)[0]
    hint = body(fitted)["verification_reads"]
    assert hint["next_reads"][0]["recall"]["offset"] == page["next_offset"]
    assert not source._verifier_context.delivered


async def test_runtime_delivery_labels_refresh_after_required_body_is_received(history):
    source, original = history
    model = CountingModel(6000)
    fitted = await source.prepare_verifier(model, inputs({
        "execution_task": "check tail", "tool_results": [{"result": "x" * 10000 + "END"}],
    }), [])
    row = body(fitted)["tool_results"][0]
    assert row["result"]["delivery"] == "pending"
    ref = row["result"]["history_context"]["recall"]
    reader = HistoryRecall(original, source, verifier=True)
    output = await reader.call(**{**ref, "offset": 10000, "max_chars": 3})
    fitted += result("tail", output.content)
    fitted = await source.prepare_verifier(model, fitted, [])
    assert body(fitted)["tool_results"][0]["result"]["delivery"] == "provided"
    assert body(fitted)["verification_reads"]["pending_count"] == 0
    # Preparing the request can describe its prospective delivery, but must
    # not commit that delivery before the provider succeeds.
    with pytest.raises(ValueError, match="不能判定通过"):
        source.validate_verifier_decision({"protocol_version": "edge_cloud_verifier.v1", "passed": True})
    source.record_verifier_delivery(fitted)
    source.validate_verifier_decision({"protocol_version": "edge_cloud_verifier.v1", "passed": True})


async def test_interrupted_response_does_not_commit_delivery(history, tmp_path):
    source, original = history
    model = CountingModel(16000)
    model.responses = [{"finished_reason": "interrupted", "content": []}]
    seq = source.snapshot("original", stage="verifier")
    reader = HistoryRecall(original, source, verifier=True)
    output = await reader.call(op="expand", lo=seq, hi=seq)
    completed = Mock(wraps=source.record_verifier_delivery)
    with pytest.raises(RuntimeError, match="interrupted"):
        await consume_decision_call(
            model, inputs({"task": "check"}) + result("read", output.content),
            stage="verifier", read_tools={"recall_history": reader}, state=context(tmp_path).agent.state,
            prepare_request=lambda msgs, tools: source.prepare_verifier(model, msgs, tools),
            on_request_complete=completed, validate_decision=source.validate_verifier_decision,
        )
    completed.assert_not_called()


async def test_only_a_verifier_model_response_can_commit_delivered_call_batches(history):
    source, _ = history
    model = CountingModel(16000)
    messages = inputs({"task": "check"}) + result("read", [TextBlock(text="actual result")], "Skill")
    await source.prepare_verifier(model, messages, [])
    tracker = source._verifier_context
    assert not tracker.tool_messages and not tracker.delivered_blocks
    source.record_verifier_delivery(messages)
    expected = [msg.model_dump(mode="json") for msg in messages[2:]]
    archived = deepcopy(messages)
    tracker._archive_delivered_pairs(archived)
    record = next(msg for msg in archived if msg.metadata.get("verifier_read_archive"))
    seq = json.loads(record.get_text_content())["history_context"]["seq"]
    snapshot = json.loads(source.rows("SELECT content FROM conversation_history WHERE seq=?", (seq,))[0]["content"])
    assert snapshot["read_batches"] == [expected]
    assert not any(isinstance(block, (ToolCallBlock, ToolResultBlock)) for msg in archived for block in msg.content)


@pytest.mark.parametrize("oversized", [False, True])
async def test_budget_is_checked_even_when_history_store_is_unavailable(oversized):
    verifier = VerifierContext(None)
    model = CountingModel(3000)
    messages = inputs({"execution_task": "x" * (10000 if oversized else 10)})
    if oversized:
        with pytest.raises(ModelContextLengthExceededException, match="历史库不可用"):
            await verifier.prepare(model, messages, [])
    else:
        assert await verifier.prepare(model, messages, []) is messages
    assert not model.requests


async def test_missing_history_cannot_turn_an_index_into_verified_evidence():
    verifier = VerifierContext(None)
    messages = inputs({"tool_results": [{"result": {"history_context": {
        "seq": 1, "recall": {"op": "expand", "lo": 1, "hi": 1},
    }}}]})
    await verifier.prepare(CountingModel(16000), messages, [])
    verifier.record_delivery(messages)
    with pytest.raises(ValueError, match="不能判定通过"):
        verifier.validate_decision({"protocol_version": "edge_cloud_verifier.v1", "passed": True})
