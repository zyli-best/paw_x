"""Check the complete ReAct request after memory/plugin additions."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable

from agentscope.middleware import MiddlewareBase

from ...exceptions import ConfigurationException
from .request_budget import RequestSnapshot, budget_for_model
from .request_hints import HINT_OMISSION_ORDER, hint_type, is_optional_hint

logger = logging.getLogger(__name__)


class RequestBudgetMiddleware(MiddlewareBase):
    """Registered last, and only when Scroll is actually wired for the request."""

    def __init__(
        self, manager: Any, *, compression_enabled: bool = True
    ) -> None:
        self._manager = manager
        self._compression_enabled = compression_enabled
        # One compacted request, retained only for retries of the same ReAct
        # inference. SDK retries can otherwise pass the old pre-memory list.
        self._retry_request: dict[str, Any] | None = None
        self._rejected_request: tuple[str, int, str] | None = None

    def _restore_retry_request(self, agent: Any, input_kwargs: dict) -> None:
        cached = self._retry_request
        if cached is None:
            return
        state = agent.state
        key = (
            getattr(state, "reply_id", None),
            getattr(state, "cur_iter", None),
        )
        messages = input_kwargs["messages"]
        incoming_hints = [msg for msg in messages if is_optional_hint(msg, agent)]
        if (
            key == cached["key"]
            and tuple(id(msg) for msg in state.context)
            == cached["context_ids"]
            and input_kwargs.get("tools") == cached["tools"]
            and messages
            and all(
                id(msg) in cached["source_ids"]
                for msg in messages
                if not is_optional_hint(msg, agent)
            )
        ):
            restored = list(cached["messages"])
            if (
                id(input_kwargs["current_model"]) != cached["model"]
                or {id(msg) for msg in incoming_hints}
                != cached["source_hint_ids"]
            ):
                restored = [
                    msg for msg in restored if not is_optional_hint(msg, agent)
                ]
                restored.extend(incoming_hints)
            input_kwargs["messages"] = restored
        else:
            self._retry_request = None

    async def on_model_call(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable,
    ) -> Any:
        model = input_kwargs["current_model"]
        if getattr(model, "_qwenpaw_use_model_defaults", False):
            # Transient device/edge models use the serving model's defaults;
            # no configured context/output pair is required. Do not reuse a
            # configured model's compacted retry request across this switch.
            self._retry_request = None
            self._rejected_request = None
            return await self._dispatch(agent, input_kwargs, next_handler)
        self._restore_retry_request(agent, input_kwargs)
        budget = budget_for_model(model)
        if budget is None:
            raise ConfigurationException(
                "无法读取当前模型的上下文长度或最大输出，请检查这两个配置。",
                config_key="model.context_budget",
            )
        messages = input_kwargs["messages"]
        source_ids = {id(msg) for msg in messages}
        if self._retry_request is not None:
            source_ids.update(self._retry_request["source_ids"])
        tools = input_kwargs.get("tools")
        source_hint_ids = {id(msg) for msg in messages if is_optional_hint(msg, agent)}
        before = await model.count_tokens(messages=messages, tools=tools)
        if not budget.exceeded(before):
            self._rejected_request = None
            return await self._dispatch(agent, input_kwargs, next_handler)
        identity = hashlib.sha256(
            json.dumps(
                {
                    "reply_id": getattr(agent.state, "reply_id", None),
                    "iteration": getattr(agent.state, "cur_iter", None),
                    "model": id(model),
                    "budget": (budget.context_tokens, budget.output_tokens),
                    "messages": [msg.model_dump() for msg in messages],
                    "context": [
                        msg.model_dump() for msg in agent.state.context
                    ],
                    "tools": tools,
                },
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            ).encode()
        ).hexdigest()
        if (
            self._rejected_request is not None
            and self._rejected_request[0] == identity
        ):
            raise budget.error(
                model, self._rejected_request[1], self._rejected_request[2]
            )

        def reject(tokens, reason):
            error = budget.error(model, tokens, reason)
            self._rejected_request = (identity, tokens, reason)
            return error

        if budget.input_limit <= 0:
            raise reject(before, "配置没有留下可用的输入空间。")
        if not self._compression_enabled:
            messages, before = await self._omit_optional_hint(
                agent,
                model,
                messages,
                tools,
                budget,
                before,
            )
            if budget.exceeded(before):
                raise reject(before, "自动压缩已关闭，本次请求未发送。")
            input_kwargs["messages"] = messages
            return await self._dispatch(agent, input_kwargs, next_handler)
        try:
            snapshot = RequestSnapshot.capture(agent, messages, tools)
        except ValueError as exc:
            kept, tokens = await self._omit_optional_hint(
                agent,
                model,
                messages,
                tools,
                budget,
                before,
            )
            if not budget.exceeded(tokens):
                input_kwargs["messages"] = kept
                return await self._dispatch(agent, input_kwargs, next_handler)
            raise reject(before, str(exc)) from exc

        async def prepare_input():
            return await snapshot.rebuild(agent)

        logger.info(
            "ReAct budget compression: input=%d output=%d context=%d",
            before,
            budget.output_tokens,
            budget.context_tokens,
        )
        await self._manager.compress_to_budget(
            agent,
            budget=budget,
            prepare_input=prepare_input,
            count_model=model,
        )
        rebuilt = await prepare_input()
        after = await model.count_tokens(**rebuilt)
        rebuilt["messages"], after = await self._omit_optional_hint(
            agent,
            model,
            rebuilt["messages"],
            tools,
            budget,
            after,
        )
        logger.info("ReAct budget after compression: input=%d", after)
        if budget.exceeded(after):
            raise reject(
                after,
                "Scroll 整理后仍无法满足预算；当前请求或受保护内容过大，"
                "或历史未能安全保存。本次请求未发送，请缩小输入或调整配置。",
            )
        # Update the same list when possible so SDK-level retries also see
        # the shortened request. The existing middleware chain is not rerun.
        messages[:] = rebuilt["messages"]
        reply_id = getattr(agent.state, "reply_id", None)
        if reply_id:
            self._retry_request = {
                "key": (reply_id, getattr(agent.state, "cur_iter", None)),
                "context_ids": tuple(id(msg) for msg in agent.state.context),
                "source_ids": source_ids | {id(msg) for msg in messages},
                "messages": tuple(messages),
                "tools": tools,
                "model": id(model),
                "source_hint_ids": source_hint_ids,
            }
        return await self._dispatch(agent, input_kwargs, next_handler)

    @staticmethod
    async def _dispatch(agent, input_kwargs, next_handler):
        from ...runtime.output_attribution import capture_model_input

        capture_model_input(input_kwargs["messages"])
        policy = getattr(agent, "_context_input_policy", None)
        if policy is not None:
            policy.validate(input_kwargs["messages"])
        steering = getattr(agent, "_normal_steering", None)
        if steering is not None:
            steering.assert_preserved(input_kwargs["messages"])
        progress = getattr(agent, "_progress_reminder_middleware", None)
        if progress is not None:
            try:
                progress.record_request(input_kwargs["messages"])
            except Exception:
                logger.warning("Progress request observation failed", exc_info=True)
        return await next_handler(**input_kwargs)

    @staticmethod
    async def _omit_optional_hint(
        agent, model, messages, tools, budget, tokens
    ):
        """An optional reminder must not block an otherwise fitting request."""
        if not budget.exceeded(tokens):
            return messages, tokens
        kept = messages
        for tag in HINT_OMISSION_ORDER:
            reduced = [msg for msg in kept if hint_type(msg, agent) != tag]
            if len(reduced) == len(kept):
                continue
            kept = reduced
            tokens_without_hint = await model.count_tokens(messages=kept, tools=tools)
            if not budget.exceeded(tokens_without_hint):
                logger.info("Request hint omitted: type=%s request budget is full", tag)
                return kept, tokens_without_hint
        return messages, tokens
