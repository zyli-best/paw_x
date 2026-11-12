"""Flow-owned guidance, consumed only at existing inference boundaries."""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections import deque
from datetime import datetime, timezone

from agentscope.message import Msg, TextBlock

from ...app.task_tracker import RunControlError

logger = logging.getLogger(__name__)


class _ViewPolicy:
    def __init__(self, owner, anchors):
        self.owner, self.anchors = owner, set(anchors)

    def active_turn_start(self, context):
        if not self.owner.injected:
            return None
        return next(
            (i for i, msg in enumerate(context) if msg.id in self.anchors), None
        )

    def validate(self, messages):
        self.owner.validate_guidance(messages)
        if self.owner.injected and not self.anchors.issubset({m.id for m in messages}):
            raise RuntimeError("Compression removed the collaborative task input")


class EdgeCloudSteeringController:
    """Independent of ordinary steering; one controller per tracked flow.

    Admissions are serialized by TaskTracker. The flow owner alone prepares
    messages. Receipts remain until the tracked producer is removed, no TTL.
    """

    def __init__(self, ctx, response_id, run_id, chat_id):
        self.ctx, self.response_id, self.run_id, self.chat_id = (
            ctx,
            response_id,
            run_id,
            chat_id,
        )
        self.agent_id = ctx.agent_id
        self.phase, self.flow_id = "starting", ""
        self.records, self.pending, self.injected = {}, deque(), []
        self.originals = []
        self.redaction = None
        self.observers = []
        self._cycle = None

    def start(self, redaction, flow_id):
        if self.phase == "closing":
            return
        self.redaction, self.flow_id = redaction, flow_id
        self.originals = list(self.ctx.input_msgs)
        self.phase = "running"

    def enqueue(self, message_id, text):
        digest = hashlib.sha256(text.encode()).hexdigest()
        previous = self.records.get(message_id)
        if previous is not None:
            if previous["digest"] != digest:
                raise RunControlError(
                    "MESSAGE_ID_CONFLICT", "message_id already has different text"
                )
            return self.receipt(previous), True
        if self.phase != "running":
            raise RunControlError(
                "RUN_NOT_ACCEPTING", "Execution is starting or closing"
            )
        if not text.strip():
            raise RunControlError("EMPTY_STEER", "Steering text must not be empty", 422)
        if len(self.records) >= 128:
            raise RunControlError(
                "STEER_LIMIT_REACHED", "Execution has reached its steering limit", 429
            )
        record = dict(
            message_id=message_id,
            text=text,
            digest=digest,
            sequence=len(self.records) + 1,
            status="queued",
            received_at=datetime.now(timezone.utc).isoformat(),
        )
        self.records[message_id] = record
        self.pending.append(record)
        return self.receipt(record), False

    def receipt(self, record):
        return {key: record[key] for key in ("message_id", "sequence", "status")} | {
            "run_id": self.run_id
        }

    async def _inject(self):
        batch = list(self.pending)
        self.pending.clear()
        if not batch:
            return
        messages = []
        for record in batch:
            record["status"] = "preparing"
            messages.append(
                Msg(
                    id=uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"edge-cloud:{self.response_id}:{record['message_id']}",
                    ).hex,
                    name="user",
                    role="user",
                    content=[TextBlock(text=record["text"])],
                    metadata={
                        "qwenpaw_message_kind": "edge_cloud_steering",
                        "agent_id": self.agent_id,
                        "chat_id": self.chat_id,
                        "run_id": self.run_id,
                        "response_id": self.response_id,
                        "flow_id": self.flow_id,
                        "message_id": record["message_id"],
                        "sequence": record["sequence"],
                        "received_at": record["received_at"],
                    },
                )
            )
        try:
            prepared = await self.redaction.prepare(messages)
            if self.phase != "running":
                return
            if len(prepared) != len(batch):
                raise RuntimeError("Steering input preparation changed the batch size")
            for message in prepared:
                message.metadata["injected_at"] = datetime.now(timezone.utc).isoformat()
            self.redaction.mark_prepared(prepared)
            context = self.ctx.agent.state.context
            known = {message.id for message in context}
            # A Router input is temporary. Preserve the real root first, and
            # filter already committed inputs when execution later begins.
            for original in self.originals:
                if original.id not in known:
                    context.append(original.model_copy(deep=True))
                    known.add(original.id)
            for record, message in zip(batch, prepared):
                context.append(message)
                self.injected.append(message.model_copy(deep=True))
                record["status"] = "injected"
                for observer in self.observers:
                    try:
                        observer(message)
                    except Exception:
                        logger.warning(
                            "Steering transcript observation failed", exc_info=True
                        )
            self.ctx.extras["session_delta_scan_ids"] = True
        finally:
            for record in batch:
                if record["status"] == "preparing":
                    record["status"] = "not_applied"

    async def prepare(self, agent):
        cycle = (agent.state.reply_id, agent.state.cur_iter)
        if self._cycle != cycle:
            self._cycle = cycle
            await self._inject()
        # Model-only history compaction may have projected away stable ids.
        if self.injected:
            self.merge(agent.state.context, [*self.originals, *self.injected])

    async def prepare_view(self, messages):
        await self._inject()
        messages = list(messages)
        self.merge(messages, self.injected)
        return messages

    @staticmethod
    def merge(messages, additions):
        known = {m.id for m in messages}
        for message in additions:
            if message.id not in known:
                messages.append(message.model_copy(deep=True))
                known.add(message.id)

    def view_policy(self, anchors):
        return _ViewPolicy(self, anchors)

    def active_turn_start(self, context):
        return self.view_policy({m.id for m in self.originals}).active_turn_start(
            context
        )

    def validate_guidance(self, messages):
        actual = {m.id: m for m in messages}
        for message in self.injected:
            found = actual.get(message.id)
            if found is None or found.content != message.content:
                raise RuntimeError(
                    "Collaborative input compression/projection removed steering"
                )

    def validate(self, messages):
        self.view_policy({m.id for m in self.originals}).validate(messages)

    def on_save(self, agent, previous_len):
        if self.injected and len(agent.state.context) > previous_len:
            last = agent.state.context[-1]
            if any(m.id == last.id for m in agent.state.context[:-1]):
                last.id = uuid.uuid4().hex
                last.metadata = {
                    **(last.metadata or {}),
                    "context_reply_id": agent.state.reply_id,
                }

    def restore_context(self, original, current, compact_ids):
        if not self.injected:
            return None
        restored = list(original)
        self.merge(restored, [m for m in current if m.id not in compact_ids])
        self.validate(restored)
        return restored

    def close(self):
        self.phase = "closing"
        self.pending.clear()
        for record in self.records.values():
            if record["status"] in {"queued", "preparing"}:
                record["status"] = "not_applied"

    def outcomes(self):
        # No queued plaintext in metadata or upload side channels.
        return [self.receipt(record) for record in self.records.values()]

    def schedule_cancel_upload(self, ctx, full_state):
        from ...hooks.session.session_hook import (
            _build_session_delta_payload,
            _is_ephemeral_request,
        )
        from ...observability.session_delta_langfuse import (
            schedule_session_delta_upload,
        )

        if (
            not self.injected
            or ctx.extras.get("session_delta_upload_scheduled")
            or _is_ephemeral_request(ctx)
        ):
            return
        request = ctx.request
        rc = getattr(request, "request_context", None) or {}
        payload = _build_session_delta_payload(
            ctx,
            full_state,
            user_id=str(rc.get("user_id") or getattr(request, "user_id", "") or ""),
            channel=getattr(request, "channel", "") or "",
        )
        if payload:
            if isinstance(rc.get("sensitive_words"), dict):
                payload["_sensitive_words"] = rc["sensitive_words"]
            schedule_session_delta_upload(payload)
            ctx.extras["session_delta_upload_scheduled"] = True
