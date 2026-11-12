"""Local execution-time checkpoints. No model calls, timers, or stop decisions."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .progress_config import ProgressReminderConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProgressCheckpoint:
    key: tuple[str, str, int]
    checkpoint_seq: int
    observation_version: int
    elapsed: float
    since_checkpoint: float
    stats: str


@dataclass
class _Call:
    name: str
    operation_signature: str
    iteration: int | None
    digest: Any = field(default_factory=hashlib.sha256)
    non_text: bool = False
    offloaded: bool = False
    # A hashlib object cannot be resumed from its hex digest. After restoring
    # partial output, report its result signature as unknown, never hash a
    # suffix and present it as the signature of the full result.
    partial: bool = False


class ProgressReminderController:
    def __init__(
        self,
        config: ProgressReminderConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        self.session_id = ""
        self.reply_id = ""
        self.execution_elapsed_seconds = 0.0
        self.last_checkpoint_elapsed_seconds = 0.0
        self.checkpoint_seq = 0
        self.observation_version = 0
        self._started: float | None = None
        self._calls: dict[str, _Call] = {}
        self._completed: set[str] = set()
        self._statuses: Counter = Counter()
        self._recent: deque[dict] = deque(maxlen=16)
        self._candidate: ProgressCheckpoint | None = None
        self.last_outcome: str | None = None

    def begin_segment(self, session_id: str, reply_id: str) -> None:
        if not self.config.enabled or not reply_id:
            return
        if (session_id, reply_id) != (self.session_id, self.reply_id):
            self.reset()
            self.session_id, self.reply_id = session_id, reply_id
        if self._started is None:
            self._started = self._clock()

    def current_elapsed(self) -> float:
        return self.execution_elapsed_seconds + (
            max(0.0, self._clock() - self._started)
            if self._started is not None
            else 0.0
        )

    def pause(self) -> None:
        self.execution_elapsed_seconds = self.current_elapsed()
        self._started = None

    def close(self) -> None:
        # Destruction is not business completion; keep the resumable checkpoint.
        self.pause()
        self._candidate = None

    def _matches(self, reply_id: str) -> bool:
        return (
            self.config.enabled
            and bool(self.reply_id)
            and self.reply_id == reply_id
        )

    def remember_call(
        self,
        reply_id: str,
        call_id: str,
        name: str,
        arguments: Any,
        *,
        iteration: int | None = None,
    ) -> None:
        if (
            not self._matches(reply_id)
            or call_id in self._completed
            or call_id in self._calls
        ):
            return
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, TypeError):
                pass
        canonical = json.dumps(
            [name, arguments], sort_keys=True, ensure_ascii=False, default=str
        )
        self._calls[call_id] = _Call(
            name[:128],
            hashlib.sha256(canonical.encode()).hexdigest(),
            iteration,
        )
        self.observation_version += 1

    def append_text(self, reply_id: str, call_id: str, text: str) -> None:
        call = self._calls.get(call_id) if self._matches(reply_id) else None
        if call is not None:
            call.digest.update(text.encode("utf-8"))
            self.observation_version += 1

    def note_data(self, reply_id: str, call_id: str) -> None:
        call = self._calls.get(call_id) if self._matches(reply_id) else None
        if call is not None and not call.non_text:
            call.non_text = True
            self.observation_version += 1

    def complete_call(
        self,
        reply_id: str,
        call_id: str,
        status: str,
        metadata: dict | None = None,
    ) -> None:
        call = self._calls.get(call_id) if self._matches(reply_id) else None
        if call is None:
            return
        if (metadata or {}).get("offloaded"):
            if not call.offloaded:
                call.offloaded = True
                # The offload acknowledgement is not the business result.
                call.digest = hashlib.sha256()
                call.non_text = False
                self.observation_version += 1
            return
        status = str(getattr(status, "value", status))
        # Some SDK versions surface parked calls as a non-terminal state.
        if status in {"pending", "asking", "submitted", "running"}:
            return
        self._calls.pop(call_id)
        self._completed.add(call_id)
        self._statuses[status] += 1
        self._recent.append(
            {
                "call_id": call_id,
                "name": call.name,
                "iteration": call.iteration,
                "status": status,
                "operation_signature": call.operation_signature,
                "result_signature": (
                    None
                    if call.partial or call.non_text
                    else call.digest.hexdigest()
                ),
                "non_text": call.non_text,
            }
        )
        self.observation_version += 1

    def prepare(
        self, session_id: str, reply_id: str, iteration: int
    ) -> ProgressCheckpoint | None:
        if not self._matches(reply_id) or session_id != self.session_id:
            return None
        key = (session_id, reply_id, iteration)
        if self._candidate is not None and self._candidate.key == key:
            return (
                self._candidate
                if self.is_current(self._candidate, key)
                else None
            )
        if any(not call.offloaded for call in self._calls.values()):
            self.record_outcome("omitted", "pending_tools")
            return None
        elapsed = self.current_elapsed()
        since = elapsed - self.last_checkpoint_elapsed_seconds
        if since < self.config.interval_seconds:
            return None
        self.checkpoint_seq += 1
        self.last_checkpoint_elapsed_seconds = elapsed
        # No raw arguments/output/names in model-facing statistics.
        distribution = ", ".join(
            f"{key}={self._statuses[key]}"
            for key in ("success", "error", "cancelled", "denied")
            if self._statuses[key]
        )
        other = sum(
            count
            for key, count in self._statuses.items()
            if key not in {"success", "error", "cancelled", "denied"}
        )
        stats = (
            f"calls={len(self._completed) + len(self._calls)}, "
            f"completed={len(self._completed)}, pending={len(self._calls)}, "
            f"offloaded={sum(call.offloaded for call in self._calls.values())}"
        )
        if distribution:
            stats += ", " + distribution
        if other:
            stats += f", other_terminal={other}"
        pairs = Counter(
            (row["operation_signature"], row["result_signature"])
            for row in self._recent
            if row["result_signature"]
        )
        repeated = max(pairs.values(), default=0)
        if repeated > 1:
            stats += (
                f"; same operation/result observed {repeated} times "
                f"in last {len(self._recent)} records (observation only)"
            )
        self._candidate = ProgressCheckpoint(
            key,
            self.checkpoint_seq,
            self.observation_version,
            elapsed,
            since,
            stats,
        )
        self.record_outcome("checkpoint_due")
        self.record_outcome("prepared")
        return self._candidate

    def is_current(self, candidate: ProgressCheckpoint, key: tuple) -> bool:
        return (
            self.config.enabled
            and candidate is self._candidate
            and candidate.key == key
            and key[:2] == (self.session_id, self.reply_id)
            and candidate.observation_version == self.observation_version
            and not any(not call.offloaded for call in self._calls.values())
        )

    def record_outcome(
        self, outcome: str, reason: str = "", *, hint_tokens: int | None = None
    ) -> None:
        self.last_outcome = outcome
        logger.info(
            "ProgressReminder session=%s reply=%s iteration=%s "
            "checkpoint_seq=%d elapsed_seconds=%.1f since_checkpoint_seconds=%.1f "
            "completed=%d pending=%d outcome=%s reason=%s hint_tokens=%s",
            self.session_id,
            self.reply_id,
            self._candidate.key[2] if self._candidate else None,
            self.checkpoint_seq,
            self.current_elapsed(),
            (
                self._candidate.since_checkpoint
                if self._candidate
                else self.current_elapsed()
                - self.last_checkpoint_elapsed_seconds
            ),
            len(self._completed),
            len(self._calls),
            outcome,
            reason,
            hint_tokens,
        )

    def to_dict(self) -> dict | None:
        if not self.config.enabled or not self.reply_id:
            return None
        return {
            "version": 1,
            "session_id": self.session_id,
            "reply_id": self.reply_id,
            "execution_elapsed_seconds": self.current_elapsed(),
            "last_checkpoint_elapsed_seconds": self.last_checkpoint_elapsed_seconds,
            "checkpoint_seq": self.checkpoint_seq,
            "observation_version": self.observation_version,
            "completed_call_ids": sorted(self._completed),
            "statuses": dict(self._statuses),
            "recent": list(self._recent),
            "pending_calls": {
                key: {
                    "name": call.name,
                    "operation_signature": call.operation_signature,
                    "iteration": call.iteration,
                    "non_text": call.non_text,
                    "offloaded": call.offloaded,
                }
                for key, call in self._calls.items()
            },
        }

    def load_state(
        self, block: Any, *, session_id: str, reply_id: str
    ) -> None:
        self.reset()
        if not self.config.enabled or block is None:
            return
        try:
            if (
                not isinstance(block, dict)
                or type(block.get("version")) is not int
                or block["version"] != 1
                or not reply_id
                or block.get("session_id") != session_id
                or block.get("reply_id") != reply_id
            ):
                raise ValueError("version or scope")
            elapsed, checkpoint = (
                block[key]
                for key in (
                    "execution_elapsed_seconds",
                    "last_checkpoint_elapsed_seconds",
                )
            )
            for value in (elapsed, checkpoint):
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError("elapsed")
            if checkpoint > elapsed:
                raise ValueError("checkpoint")
            seq, observation = (
                block["checkpoint_seq"],
                block["observation_version"],
            )
            for value in (seq, observation):
                if type(value) is not int or value < 0:
                    raise ValueError("counter")
            completed = block.get("completed_call_ids", [])
            statuses = block.get("statuses", {})
            recent = block.get("recent", [])
            pending = block.get("pending_calls", {})
            if not isinstance(completed, list) or any(
                not isinstance(key, str) for key in completed
            ):
                raise ValueError("call IDs")
            if not isinstance(statuses, dict) or any(
                not isinstance(key, str) or type(value) is not int or value < 0
                for key, value in statuses.items()
            ):
                raise ValueError("statuses")
            if (
                not isinstance(recent, list)
                or len(recent) > 16
                or not isinstance(pending, dict)
            ):
                raise ValueError("records")
            calls = {}
            for key, row in pending.items():
                if (
                    not isinstance(key, str)
                    or key in completed
                    or not isinstance(row, dict)
                ):
                    raise ValueError("pending")
                if not isinstance(row["name"], str) or not isinstance(
                    row["operation_signature"], str
                ):
                    raise ValueError("signature")
                calls[key] = _Call(
                    row["name"][:128],
                    row["operation_signature"],
                    row.get("iteration"),
                    non_text=bool(row.get("non_text")),
                    offloaded=bool(row.get("offloaded")),
                    partial=True,
                )
            for row in recent:
                if (
                    not isinstance(row, dict)
                    or any(
                        not isinstance(row.get(key), str)
                        for key in (
                            "call_id",
                            "name",
                            "status",
                            "operation_signature",
                        )
                    )
                    or (
                        row.get("result_signature") is not None
                        and not isinstance(row["result_signature"], str)
                    )
                ):
                    raise ValueError("recent")
            self.session_id, self.reply_id = session_id, reply_id
            (
                self.execution_elapsed_seconds,
                self.last_checkpoint_elapsed_seconds,
            ) = float(elapsed), float(checkpoint)
            self.checkpoint_seq, self.observation_version = seq, observation
            self._completed, self._statuses = set(completed), Counter(statuses)
            self._recent, self._calls = deque(recent, maxlen=16), calls
            self.record_outcome("state_restored")
        except (ValueError, TypeError, KeyError, OverflowError):
            self.reset()
            logger.warning("Ignoring invalid progress reminder checkpoint")
