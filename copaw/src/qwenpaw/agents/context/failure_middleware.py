"""Bounded model advice for one inference, outside durable conversation state."""

from __future__ import annotations

import logging
from typing import Any, Callable

from agentscope.message import Msg, TextBlock
from agentscope.middleware import MiddlewareBase

from ...constant import (
    FAILURE_RECOVERY_MESSAGE_TAG,
    QWENPAW_MESSAGE_TAG_KEY,
)
from ...loop.gates.failure_loop import (
    FAILURE_HINT_FOOTER,
    FAILURE_HINT_HEADER,
    FailureLoopGate,
)
from .request_hints import hint_type

logger = logging.getLogger(__name__)


def is_failure_hint(msg: Any) -> bool:
    """Only our request-local messages; never match user text or other gates."""
    return hint_type(msg) == FAILURE_RECOVERY_MESSAGE_TAG


class FailureRecoveryMiddleware(MiddlewareBase):
    """Await review before reasoning; attach its advice before budget checks."""

    def __init__(self, gate: FailureLoopGate) -> None:
        self.gate = gate
        self._key: tuple | None = None
        self._turn = None
        self._entries: tuple[str, ...] = ()
        self._message: Msg | None = None
        # Stable message objects let budget compression recognize SDK retries.
        self._bounded: dict[tuple, Msg | None] = {}
        self._completed_count = 0

    def _decision_key(self, agent: Any) -> tuple:
        return (
            self.gate._session_id(),
            agent.state.reply_id,
            agent.state.cur_iter,
        )

    async def prepare(self, agent: Any) -> None:
        self._key = None
        self._turn = None
        self._entries = ()
        self._message = None
        self._bounded.clear()
        state = self.gate._turn(agent.state.reply_id)
        if state is None or state.calls:
            return
        decision_key = self._decision_key(agent)
        completed_count = state.inspector.count
        entries = await state.inspector.inspect(
            agent, check_interval=self.gate.config.check_interval
        )
        if (
            decision_key != self._decision_key(agent)
            or self.gate._turn(agent.state.reply_id) is not state
            or state.inspector.count != completed_count
            or state.calls
        ):
            return
        if not entries:
            return
        self._key = decision_key
        self._turn = state
        self._completed_count = completed_count
        self._entries = entries
        self._message = Msg(
            name="failure_recovery",
            role="user",
            content=[
                TextBlock(
                    type="text",
                    text=FAILURE_HINT_HEADER
                    + "\n".join(entries)
                    + FAILURE_HINT_FOOTER,
                )
            ],
            metadata={
                QWENPAW_MESSAGE_TAG_KEY: FAILURE_RECOVERY_MESSAGE_TAG,
                "source": self.gate.name,
                "reply_id": agent.state.reply_id,
                "iteration": agent.state.cur_iter,
            },
        )

    async def _fit(self, model: Any) -> Msg | None:
        if self._message is None:
            return None
        state = self.gate._turn(self._key[1]) if self._key else None
        if (
            state is None
            or state is not self._turn
            or state.inspector.count != self._completed_count
            or state.calls
        ):
            return None
        limit = self.gate.config.max_hint_tokens
        key = (id(model), limit)
        if key in self._bounded:
            return self._bounded[key]
        for size in range(len(self._entries), 0, -1):
            candidate = self._message.model_copy(
                update={
                    "content": [
                        TextBlock(
                            type="text",
                            text=FAILURE_HINT_HEADER
                            + "\n".join(self._entries[:size])
                            + FAILURE_HINT_FOOTER,
                        )
                    ],
                }
            )
            tokens = await model.count_tokens(messages=[candidate], tools=[])
            if tokens <= limit:
                self._bounded[key] = candidate
                return candidate
        self._bounded[key] = None
        logger.info(
            "FailureLoopGate hint omitted: no complete entry fits budget"
        )
        return None

    async def on_model_call(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable,
    ) -> Any:
        messages = [
            msg for msg in input_kwargs["messages"] if not is_failure_hint(msg)
        ]
        if self._key == self._decision_key(agent):
            try:
                hint = await self._fit(input_kwargs["current_model"])
            except Exception:
                # Advice must not disable the original model request.
                logger.warning("Failure hint sizing failed", exc_info=True)
                hint = None
            if hint is not None:
                messages.append(hint)
        input_kwargs["messages"] = messages
        return await next_handler(**input_kwargs)
