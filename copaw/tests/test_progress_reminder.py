"""Execution segments, factual observations and resumable checkpoints."""

import hashlib
import json

import pytest

from qwenpaw.loop.progress_config import ProgressReminderConfig
from qwenpaw.loop.progress_reminder import ProgressReminderController


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def controller(clock):
    controller = ProgressReminderController(ProgressReminderConfig(), clock=clock)
    controller.begin_segment("default", "turn")
    return controller


def record(
    controller,
    call_id="call",
    *,
    reply_id="turn",
    text="actual result",
    arguments='{"path":"a"}',
    status="success",
):
    controller.remember_call(
        reply_id, call_id, "read_file", arguments, iteration=1
    )
    controller.append_text(reply_id, call_id, text)
    controller.complete_call(reply_id, call_id, status)


@pytest.mark.parametrize("value", [0, -1, True, "600", 1.5])
def test_positive_integer_validation(value):
    with pytest.raises(ValueError):
        ProgressReminderConfig(interval_seconds=value)


def test_progress_configuration_only_exposes_enable_and_interval():
    from dataclasses import fields

    assert [field.name for field in fields(ProgressReminderConfig)] == [
        "enabled",
        "interval_seconds",
    ]


def test_boundaries_and_no_catchup(controller, clock):
    clock.now = 299
    assert controller.prepare("default", "turn", 0) is None
    clock.now = 300
    first = controller.prepare("default", "turn", 1)
    assert first.elapsed == 300 and first.checkpoint_seq == 1
    clock.now = 900
    assert controller.prepare("default", "turn", 1) is first
    second = controller.prepare("default", "turn", 2)
    assert second.checkpoint_seq == 2 and second.since_checkpoint == 600
    assert controller.prepare("default", "turn", 3) is None
    clock.now = 1200
    assert controller.prepare("default", "turn", 4).checkpoint_seq == 3


def test_long_first_inference_prepares_only_one(controller, clock):
    clock.now = 1500
    candidate = controller.prepare("default", "turn", 0)
    assert candidate.checkpoint_seq == 1
    assert controller.prepare("default", "turn", 1) is None


def test_pause_resume_and_process_restart(controller, clock):
    clock.now = 400
    controller.pause()
    block = json.loads(json.dumps(controller.to_dict()))
    clock.now = 50000
    controller.begin_segment("default", "turn")
    clock.now += 100
    controller.pause()
    assert controller.current_elapsed() == 500
    restored = ProgressReminderController(controller.config, clock=clock)
    restored.load_state(block, session_id="default", reply_id="turn")
    restored.begin_segment("default", "turn")
    clock.now += 200
    assert restored.current_elapsed() == 600
    assert restored.prepare("default", "turn", 0).elapsed == 600
    restored.close()
    assert restored.to_dict()["checkpoint_seq"] == 1


def test_new_reply_session_and_late_events(controller, clock):
    record(controller)
    clock.now = 700
    controller.begin_segment("default", "next")
    record(controller, "late")
    assert controller.to_dict()["completed_call_ids"] == []
    assert controller.current_elapsed() == 0
    controller.begin_segment("other-session", "next")
    assert controller.prepare("default", "next", 0) is None
    assert controller.prepare("other-session", "turn", 0) is None


def test_dedup_normalization_incremental_hash_and_bounded_records(
    controller, clock
):
    for index in range(20):
        call_id = str(index)
        controller.remember_call(
            "turn", call_id, "read_file", '{"b":2,"a":1}', iteration=index
        )
        controller.remember_call("turn", call_id, "read_file", '{"ignored":1}')
        controller.append_text("turn", call_id, "actual ")
        controller.append_text("turn", call_id, "result")
        controller.complete_call("turn", call_id, "success")
        record(controller, call_id, text="duplicate terminal event")
    block = controller.to_dict()
    assert len(block["completed_call_ids"]) == 20
    assert block["statuses"] == {"success": 20}
    assert len(block["recent"]) == 16
    assert (
        block["recent"][-1]["result_signature"]
        == hashlib.sha256(b"actual result").hexdigest()
    )
    record(controller, "canonical", arguments={"a": 1, "b": 2})
    assert (
        controller.to_dict()["recent"][-1]["operation_signature"]
        == block["recent"][-1]["operation_signature"]
    )
    clock.now = 600
    hint = controller.prepare("default", "turn", 1)
    assert "same operation/result observed 16 times" in hint.stats
    assert "actual result" not in json.dumps(controller.to_dict())


def test_pending_offloaded_and_nontext_are_not_success(controller, clock):
    controller.remember_call("turn", "pending", "write_file", "{}")
    clock.now = 600
    assert controller.prepare("default", "turn", 0) is None
    controller.complete_call("turn", "pending", "success", {"offloaded": True})
    controller.complete_call("turn", "pending", "success", {"offloaded": True})
    block = controller.to_dict()
    assert not block["completed_call_ids"] and not block["statuses"]
    candidate = controller.prepare("default", "turn", 1)
    assert (
        "offloaded=1" in candidate.stats and "completed=0" in candidate.stats
    )
    controller.note_data("turn", "pending")
    controller.complete_call("turn", "pending", "success")
    assert controller.to_dict()["recent"][0]["result_signature"] is None
    assert (
        controller.prepare("default", "turn", 1) is None
    )  # stale observations
    assert controller.checkpoint_seq == 1


@pytest.mark.parametrize(
    "mutation",
    [
        {"version": 2},
        {"version": True},
        {"session_id": "other"},
        {"reply_id": "other"},
        {"execution_elapsed_seconds": float("nan")},
        {"execution_elapsed_seconds": float("inf")},
        {"execution_elapsed_seconds": -1},
        {"execution_elapsed_seconds": True},
        {"last_checkpoint_elapsed_seconds": 1},
        {"checkpoint_seq": -1},
        {"observation_version": True},
        {"recent": [None]},
        {"completed_call_ids": [None]},
        {"statuses": {"success": -1}},
        {"pending_calls": {"bad": {}}},
    ],
)
def test_invalid_checkpoint_does_not_break_session(controller, mutation):
    block = {**controller.to_dict(), **mutation}
    controller.load_state(block, session_id="default", reply_id="turn")
    assert controller.to_dict() is None


def test_restore_dedup_and_pending_without_replaying_output(controller, clock):
    record(controller)
    controller.remember_call("turn", "pending", "read_file", "{}")
    controller.append_text("turn", "pending", "private prefix")
    restored = ProgressReminderController(controller.config, clock=clock)
    restored.load_state(
        controller.to_dict(), session_id="default", reply_id="turn"
    )
    record(restored)
    restored.append_text("turn", "pending", "suffix")
    restored.complete_call("turn", "pending", "success")
    block = restored.to_dict()
    assert block["statuses"] == {"success": 2}
    assert block["recent"][-1]["result_signature"] is None


def test_disabled_checkpoint_does_not_activate(controller, clock):
    disabled = ProgressReminderController(
        ProgressReminderConfig(enabled=False), clock=clock
    )
    disabled.load_state(
        controller.to_dict(), session_id="default", reply_id="turn"
    )
    disabled.begin_segment("default", "turn")
    record(disabled)
    clock.now = 10000
    assert disabled.prepare("default", "turn", 0) is None
    assert disabled.to_dict() is None
