# -*- coding: utf-8 -*-
"""on_acting middleware delegating tool execution to ToolCoordinator."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable

from agentscope.middleware import MiddlewareBase

if TYPE_CHECKING:
    from agentscope.agent import Agent

    from ._coordinator import ToolCoordinator
    from ._result_limiter import ToolResultLimiter

logger = logging.getLogger(__name__)


class ToolCoordinatorMiddleware(MiddlewareBase):
    """Thin on_acting middleware delegating to ToolCoordinator.

    Uses agentscope 2.0's official extension point — no Toolkit subclass.
    Direct access to agent.request_context (no ContextVar indirection).
    ``_execute_tool_call`` side effects work automatically.
    """

    def __init__(
        self,
        coordinator: "ToolCoordinator",
        result_limiter: "ToolResultLimiter | None" = None,
    ) -> None:
        self._coordinator = coordinator
        self._result_limiter = result_limiter

    async def on_acting(
        self,
        agent: "Agent",
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        tool_call = input_kwargs["tool_call"]

        request_context = getattr(agent, "_request_context", None) or {}
        session_id = request_context.get("session_id", "")
        agent_id = request_context.get("agent_id", "")
        root_session_id = request_context.get("root_session_id", "")
        steering = getattr(agent, "_normal_steering", None)
        yield_on_steer = False
        if steering is not None:
            from ..runtime.tool_registry import get_builtin_tool_funcs

            yield_on_steer = any(
                fn._tool_descriptor.name == tool_call.name
                and fn._tool_descriptor.metadata.get("yield_on_steer") is True
                for fn in get_builtin_tool_funcs()
            )

        async for item in self._coordinator.execute(
            tool_call=tool_call,
            next_handler=next_handler,
            session_id=session_id,
            agent_id=agent_id,
            root_session_id=root_session_id,
            normal_steering=steering,
            yield_on_steer=yield_on_steer,
            result_finalizer=(
                self._result_limiter.limit_async
                if self._result_limiter is not None
                else None
            ),
        ):
            yield item
