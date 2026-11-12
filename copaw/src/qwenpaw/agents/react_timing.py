"""Opt-in wall-clock timing; never log prompts, thoughts or tool arguments."""

from __future__ import annotations

import logging
import os
from time import perf_counter
from typing import Any

logger = logging.getLogger(__name__)

TIMING_ENV = "QWENPAW_REACT_TIMING_ENABLED"


def timing_enabled() -> bool:
    return os.environ.get(TIMING_ENV, "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def timing_context(agent: Any) -> tuple[str, str, int]:
    from ..config.context import get_current_session_id

    return (
        get_current_session_id() or "default",
        agent.state.reply_id,
        agent.state.cur_iter,
    )


class ReasoningTiming:
    """Measure observable stream phases, not the server's hidden computation.

    Overlapping blocks of the same phase count once. Different phases may
    overlap, so their durations are not an additive breakdown of total time.
    """

    _STARTS = {
        "THINKING_BLOCK_START": "thinking",
        "TEXT_BLOCK_START": "text",
        "TOOL_CALL_START": "tool_args",
        "DATA_BLOCK_START": "data",
    }
    _ENDS = {
        "THINKING_BLOCK_END": "thinking",
        "TEXT_BLOCK_END": "text",
        "TOOL_CALL_END": "tool_args",
        "DATA_BLOCK_END": "data",
    }

    def __init__(self, context: tuple[str, str, int]) -> None:
        self.context = context
        self.started = perf_counter()
        self.first_output_ms: float | None = None
        self._open: dict[tuple[str, str], float] = {}
        self._phase_started: dict[str, float] = {}
        self._totals = dict.fromkeys(self._STARTS.values(), 0.0)
        self._thinking_blocks = 0
        logger.info(
            "MainModel timing stage=round_start session=%s reply=%s iteration=%s",
            *context,
        )

    def observe(self, event: Any) -> None:
        kind = getattr(event, "type", None)
        phase = self._STARTS.get(kind)
        if phase is not None:
            now = perf_counter()
            if self.first_output_ms is None:
                self.first_output_ms = (now - self.started) * 1000
                logger.info(
                    "MainModel timing stage=first_output session=%s reply=%s iteration=%s event_type=%s elapsed_ms=%.1f",
                    *self.context, type(event).__name__, self.first_output_ms,
                )
            identity = getattr(event, "tool_call_id", None)
            if identity is None:
                identity = event.block_id
            key = (phase, identity)
            if key not in self._open:
                self._open[key] = now
                self._phase_started.setdefault(phase, now)
                if phase == "thinking":
                    self._thinking_blocks += 1
                    logger.info(
                        "MainModel timing stage=thinking_start session=%s reply=%s iteration=%s block_id=%s",
                        *self.context, identity,
                    )
        else:
            phase = self._ENDS.get(kind)
            if phase is not None:
                identity = getattr(event, "tool_call_id", None)
                if identity is None:
                    identity = event.block_id
                self._close(phase, identity, perf_counter(), "completed")

    def _close(self, phase: str, identity: str, now: float, status: str) -> None:
        started = self._open.pop((phase, identity), None)
        if started is None:
            return
        if phase == "thinking":
            logger.info(
                "MainModel timing stage=thinking_end session=%s reply=%s iteration=%s block_id=%s status=%s elapsed_ms=%.1f",
                *self.context, identity, status, (now - started) * 1000,
            )
        if not any(key[0] == phase for key in self._open):
            self._totals[phase] += now - self._phase_started.pop(phase)

    def finish(self, status: str) -> None:
        now = perf_counter()
        for phase, identity in list(self._open):
            self._close(phase, identity, now, status)
        logger.info(
            "MainModel timing stage=round_end session=%s reply=%s iteration=%s status=%s first_output_ms=%s elapsed_ms=%.1f thinking_stream_ms=%s thinking_blocks=%d text_stream_ms=%.1f tool_args_stream_ms=%.1f data_stream_ms=%.1f",
            *self.context, status,
            "none" if self.first_output_ms is None else f"{self.first_output_ms:.1f}",
            (now - self.started) * 1000,
            f"{self._totals['thinking'] * 1000:.1f}" if self._thinking_blocks else "unavailable",
            self._thinking_blocks, self._totals["text"] * 1000,
            self._totals["tool_args"] * 1000, self._totals["data"] * 1000,
        )


class ActTiming:
    """Wall time until the last tool call finishes, including dispatch/results.

    One timer spans every sequential/concurrent batch in a ReAct iteration.
    A paused/offloaded Act ends when control returns to the agent; the raw
    tool timer can continue in the background until actual execution ends.
    """

    def __init__(self, context, calls) -> None:
        self.context = context
        self.started = perf_counter()
        self.pending = {call.id for call in calls}
        self.tool_count = len(calls)
        self.statuses: list[str] = []
        self.finished = False
        logger.info(
            "ReAct timing stage=act_start session=%s reply=%s iteration=%s tool_count=%d",
            *context, self.tool_count,
        )

    def complete_call(self, call_id: str, status: str) -> None:
        if self.finished or call_id not in self.pending:
            return
        self.pending.remove(call_id)
        self.statuses.append(status)
        if not self.pending:
            self.finish()

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        status = "completed"
        for candidate in (
            "error", "cancelled", "denied", "waiting_for_confirmation",
            "waiting_external_execution", "offloaded", "interrupted",
        ):
            if candidate in self.statuses:
                status = candidate
                break
        else:
            if self.pending:
                status = "interrupted"
        logger.info(
            "ReAct timing stage=act_end session=%s reply=%s iteration=%s status=%s tool_count=%d finished_calls=%d elapsed_ms=%.1f",
            *self.context, status, self.tool_count,
            self.tool_count - len(self.pending), (perf_counter() - self.started) * 1000,
        )


class ToolTiming:
    """Measure toolkit execution, excluding agent-side preparation/persistence."""

    def __init__(self, context, tool_call) -> None:
        self.context = (*context, tool_call.name, tool_call.id)
        self.started = perf_counter()
        self.finished = False
        logger.info(
            "ReAct timing stage=tool_start session=%s reply=%s iteration=%s tool=%s tool_call_id=%s",
            *self.context,
        )

    def finish(self, status: str) -> None:
        if self.finished:
            return
        self.finished = True
        logger.info(
            "ReAct timing stage=tool_end session=%s reply=%s iteration=%s tool=%s tool_call_id=%s status=%s elapsed_ms=%.1f",
            *self.context, status, (perf_counter() - self.started) * 1000,
        )
