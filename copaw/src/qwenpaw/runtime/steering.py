"""Inputs for one ordinary Console execution; owned by its event-loop task."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from agentscope.message import Msg, TextBlock


class SteeringError(ValueError):
    def __init__(self, code: str, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.code = code
        self.status_code = status_code


class SteeringState:
    """No TTL: receipts live exactly as long as the tracked execution.

    All synchronous mutations run on the owning event loop. HTTP admission is
    additionally guarded by TaskTracker's lock. Only the agent consumes input.
    """

    def __init__(self, response_id: str, agent_id: str, run_id: str, chat_id: str):
        self.response_id = response_id
        self.agent_id = agent_id
        self.run_id = run_id
        self.chat_id = chat_id
        self.phase = "starting"
        self.records: dict[str, dict[str, Any]] = {}
        self.pending: deque[str] = deque()
        self.original_ids: set[str] = set()
        self.injected: list[Msg] = []
        self.prepare_inputs = None
        self._cycle = None
        self.upload_scheduled = False
        self.prepare_model_call = None
        self._changed = asyncio.Event()
        self.attempt = None
        self.tool_origins: dict[str, dict] = {}
        self.started_calls: set[str] = set()
        self.interruptions: list[dict] = []

    @property
    def latest_sequence(self) -> int:
        return len(self.records)

    async def wait_after(self, sequence: int) -> None:
        while self.phase != "closing" and self.latest_sequence <= sequence:
            changed = self._changed
            await changed.wait()

    def begin_sampling(self, messages):
        from .normal_preemption import SamplingAttempt

        guides = [
            m.metadata
            for m in messages
            if (getattr(m, "metadata", None) or {}).get("qwenpaw_message_kind")
            == "user_steering"
            and m.metadata.get("response_id") == self.response_id
        ]
        latest = max(guides, key=lambda m: m["sequence"], default={})
        self.attempt = SamplingAttempt(
            latest.get("sequence", 0), latest.get("message_id"),
        )
        return self.attempt

    def complete_sampling(self, attempt, response):
        attempt.complete = True
        for block in getattr(response, "content", ()):
            if getattr(block, "type", None) == "tool_call":
                self.tool_origins[block.id] = {
                    "sequence": attempt.sequence,
                    "steer_message_id": attempt.steer_message_id,
                    "agent_id": self.agent_id,
                    "chat_id": self.chat_id,
                    "run_id": self.run_id,
                    "response_id": self.response_id,
                }

    def stale_call(self, call_id):
        origin = self.tool_origins.get(call_id)
        return bool(
            origin is not None
            and call_id not in self.started_calls
            and self.latest_sequence > origin["sequence"]
        )

    def admit_call(self, call_id):
        if self.phase == "closing" or self.stale_call(call_id):
            return False
        self.started_calls.add(call_id)
        return True

    def enqueue(self, message_id: str, text: str) -> tuple[dict, bool]:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        previous = self.records.get(message_id)
        if previous is not None:
            if previous["digest"] != digest:
                raise SteeringError("MESSAGE_ID_CONFLICT", "message_id already has different text")
            return self.receipt(previous), True
        if self.phase != "running":
            raise SteeringError("RUN_NOT_ACCEPTING", "Execution is starting or closing")
        if not text.strip():
            raise SteeringError("EMPTY_STEER", "Steering text must not be empty", 422)
        if len(self.records) >= 128:
            raise SteeringError("STEER_LIMIT_REACHED", "Execution has reached its steering limit", 429)
        record = {
            "message_id": message_id,
            "digest": digest,
            "text": text,
            "sequence": len(self.records) + 1,
            "status": "queued",
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        self.records[message_id] = record
        self.pending.append(message_id)
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()
        return self.receipt(record), False

    def receipt(self, record: dict) -> dict:
        return {
            "message_id": record["message_id"],
            "run_id": self.run_id,
            "sequence": record["sequence"],
            "status": record["status"],
        }

    async def prepare(self, agent) -> None:
        cycle = (agent.state.reply_id, agent.state.cur_iter)
        if cycle == self._cycle:
            return
        self._cycle = cycle
        await self.inject_pending(agent)

    async def inject_pending(self, agent) -> bool:
        batch = []
        while self.pending:
            record = self.records[self.pending.popleft()]
            record["status"] = "preparing"
            batch.append(record)
        if not batch:
            return False
        messages = [
            Msg(
                id=uuid.uuid5(uuid.NAMESPACE_URL, f"{self.response_id}:{r['message_id']}").hex,
                name="user",
                role="user",
                content=[TextBlock(text=r["text"])],
                metadata={
                    "qwenpaw_message_kind": "user_steering",
                    "agent_id": self.agent_id,
                    "chat_id": self.chat_id,
                    "response_id": self.response_id,
                    "run_id": self.run_id,
                    "message_id": r["message_id"],
                    "sequence": r["sequence"],
                    "received_at": r["received_at"],
                },
            )
            for r in batch
        ]
        try:
            if self.prepare_inputs is not None:
                messages = await self.prepare_inputs(messages)
            if self.phase != "running":
                return False
            # No await between committing context and receipts: cancellation
            # cannot leave an appended message marked as merely pending.
            for record, message in zip(batch, messages):
                message.metadata["injected_at"] = datetime.now(timezone.utc).isoformat()
                agent.state.context.append(message)
                self.injected.append(message)
                record["status"] = "injected"
            return True
        finally:
            for record in batch:
                if record["status"] == "preparing":
                    record["status"] = "not_applied"

    def assert_preserved(self, messages) -> None:
        if not self.injected:
            return
        ids = {getattr(message, "id", None) for message in messages}
        required = self.original_ids | {message.id for message in self.injected}
        if not required.issubset(ids):
            raise RuntimeError("Context compression removed the active task or steering input")

    def close(self) -> None:
        self.phase = "closing"
        self._changed.set()
        self.pending.clear()
        for record in self.records.values():
            if record["status"] in {"queued", "preparing"}:
                record["status"] = "not_applied"
