# -*- coding: utf-8 -*-
"""IterationGate — universal iteration limiter.

Tracks per-session iteration count.  Returns TERMINATE when
``max_iterations`` is reached.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from .base import StopAction, StopHandlerResult
from .loop_gate import LoopGate

logger = logging.getLogger(__name__)


@dataclass
class _IterState:
    """Per-session iteration state."""

    iteration: int = 0
    max_iterations: int = 20


class IterationGate(LoopGate):
    """Request-wide iteration cap, charged before other gates can stop a round."""

    def __init__(
        self,
        max_iterations: int = 20,
    ) -> None:
        super().__init__()
        self._default_max = max_iterations

    @property
    def name(self) -> str:
        return "iteration"

    @property
    def priority(self) -> int:
        return 0

    def activate(  # pylint: disable=arguments-renamed
        self,
        max_iterations: int | None = None,
    ) -> None:
        """Activate with optional custom limit."""
        limit = max_iterations or self._default_max
        super().activate(
            _IterState(max_iterations=limit),
        )

    async def check(
        self,
        ctx: Any,  # pylint: disable=unused-argument
    ) -> StopHandlerResult:
        """Check iteration limit."""
        state: Optional[_IterState] = self._state()
        if state is None:
            return StopHandlerResult(
                action=StopAction.BYPASS,
            )

        exhausted = self.exhausted_result()
        if exhausted is not None:
            return exhausted

        state.iteration += 1
        logger.debug(
            "IterationGate: %d/%d",
            state.iteration,
            state.max_iterations,
        )

        return self.exhausted_result() or StopHandlerResult(action=StopAction.BYPASS)

    def exhausted_result(self) -> StopHandlerResult | None:
        """Keep exhaustion visible across replies until the next request re-arms it."""
        state = self._state()
        if state is not None and state.iteration >= state.max_iterations:
            return StopHandlerResult(
                action=StopAction.TERMINATE,
                reason=f"Max iterations ({state.max_iterations}) reached",
            )
        return None

    def reset(self) -> None:
        """Reset iteration counter for current session."""
        state = self._state()
        if state is not None:
            state.iteration = 0


__all__ = ["IterationGate"]
