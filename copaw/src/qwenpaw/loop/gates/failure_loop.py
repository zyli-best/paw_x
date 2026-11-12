"""Collect this task's tool results for advisory review before reasoning."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..failure_config import FailureLoopConfig
from .base import StopAction, StopHandlerResult
from .loop_gate import LoopGate

if TYPE_CHECKING:
    from ..model_loop import ModelLoopInspector

logger = logging.getLogger(__name__)

FAILURE_HINT_HEADER = (
    "<system-reminder>Failure recovery based on completed tool results. "
    "Existing task instructions, permissions, stop limits and parallel "
    "rules still apply. A corrected call is allowed only under those "
    "existing rules.\n"
)
FAILURE_HINT_FOOTER = "\n</system-reminder>"


@dataclass
class _Call:
    name: str
    arguments: Any
    text_parts: list[str] = field(default_factory=list)
    data: list[dict] = field(default_factory=list)
    iteration: int | None = None


@dataclass
class _Turn:
    reply_id: str
    inspector: ModelLoopInspector
    calls: dict[str, _Call] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    suspended: bool = False


class FailureLoopGate(LoopGate):
    """Keep tool evidence isolated by session and reply, without local rules."""

    def __init__(
        self,
        config: FailureLoopConfig,
        *,
        history_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.history_path = history_path

    @property
    def name(self) -> str:
        return "failure_loop"

    def begin_turn(self, reply_id: str, task_text: str = "") -> None:
        if not self.config.enabled:
            return
        state = self._state()
        if state is None or state.reply_id != reply_id:
            from ..model_loop import ModelLoopInspector

            if state is not None:
                state.inspector.close()
            self.activate(
                _Turn(
                    reply_id,
                    ModelLoopInspector(
                        self._session_id(),
                        reply_id,
                        history_path=self.history_path,
                        task_text=task_text,
                    ),
                )
            )

    def reset(self) -> None:
        state = self._state()
        if state is not None:
            state.inspector.close()
        self.deactivate()

    def close(self) -> None:
        for state in self._sessions.values():
            state.inspector.close()
        self._sessions.clear()

    def _turn(self, reply_id: str) -> _Turn | None:
        state = self._state()
        if (
            not self.config.enabled
            or state is None
            or state.reply_id != reply_id
            or state.suspended
        ):
            return None
        return state

    def remember_call(
        self,
        reply_id: str,
        call_id: str,
        name: str,
        arguments: Any,
        *,
        iteration: int | None = None,
    ) -> None:
        state = self._turn(reply_id)
        if (
            state is None
            or call_id in state.completed
            or call_id in state.calls
        ):
            return
        state.calls[call_id] = _Call(name, arguments, iteration=iteration)

    def append_text(self, reply_id: str, call_id: str, text: str) -> None:
        state = self._turn(reply_id)
        call = state.calls.get(call_id) if state is not None else None
        if call is not None:
            call.text_parts.append(text)

    def note_data(
        self, reply_id: str, call_id: str, data: dict | None = None
    ) -> None:
        state = self._turn(reply_id)
        call = state.calls.get(call_id) if state is not None else None
        if call is not None and data is not None:
            call.data.append(data)

    def complete_call(
        self,
        reply_id: str,
        call_id: str,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        state = self._turn(reply_id)
        if state is None or call_id in state.completed:
            return
        call = state.calls.pop(call_id, None)
        if call is None:
            return
        try:
            state.inspector.record(
                call_id,
                name=call.name,
                arguments=call.arguments,
                state=getattr(status, "value", status),
                text="".join(call.text_parts),
                metadata=metadata or {},
                data=call.data,
                iteration=call.iteration,
            )
        except Exception:
            # Incomplete evidence must not produce advice. The event observer
            # catches this error and continues forwarding the original events.
            state.suspended = True
            raise
        state.completed.add(call_id)

    async def check(self, ctx: Any) -> StopHandlerResult:
        # Advisory review is awaited before reasoning, never a stop decision.
        return StopHandlerResult(action=StopAction.BYPASS)


def build_failure_loop_gate(
    *,
    workspace_dir: Path | None,
    request_context: dict[str, Any],
    config: FailureLoopConfig | None = None,
) -> FailureLoopGate | None:
    """No gate or extra prompt outside enabled Windows non-Coding requests."""
    config = config if config is not None else FailureLoopConfig()
    reason = (
        "disabled_by_config" if not config.enabled else
        "unsupported_platform" if sys.platform != "win32" else
        "coding_mode" if request_context.get("is_coding_mode") else "enabled"
    )
    logger.info(
        "FailureLoopGate config: session=%s enabled=%s reason=%s interval=%d",
        FailureLoopGate._session_id(), reason == "enabled", reason,
        config.check_interval,
    )
    if reason != "enabled":
        return None
    from ...constant import WORKING_DIR

    return FailureLoopGate(
        config,
        history_path=Path(workspace_dir or WORKING_DIR)
        / "failure_loop"
        / "history.db",
    )
