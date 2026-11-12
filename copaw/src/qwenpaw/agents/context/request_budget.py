"""Configured per-request context/output limits; no extra safety reserve."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...exceptions import ModelContextLengthExceededException


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value)
        return number if number > 0 else None
    return None


@dataclass(frozen=True)
class RequestBudget:
    context_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.context_tokens, int)
            or not isinstance(self.output_tokens, int)
            or isinstance(self.context_tokens, bool)
            or isinstance(self.output_tokens, bool)
            or self.context_tokens <= 0
            or self.output_tokens <= 0
        ):
            raise ValueError(
                "Context and output limits must be positive integers"
            )

    @property
    def input_limit(self) -> int:
        return self.context_tokens - self.output_tokens

    def exceeded(self, input_tokens: int) -> bool:
        return input_tokens + self.output_tokens > self.context_tokens

    def error(self, model: Any, input_tokens: int, reason: str) -> Exception:
        name = getattr(model, "model", "unknown")
        error = ModelContextLengthExceededException(
            str(name),
            details={
                "source": "react_request_budget",
                "input_tokens": input_tokens,
                "max_output_tokens": self.output_tokens,
                "context_tokens": self.context_tokens,
                "reason": reason,
            },
        )
        error.message = (
            f"本次模型输入 {input_tokens} token + 配置的最大输出 "
            f"{self.output_tokens} token 超过配置的上下文长度 "
            f"{self.context_tokens} token。{reason}"
        )
        error.args = (error.message,)
        return error


def budget_for_model(model: Any) -> RequestBudget | None:
    """Use factory-captured configuration, or a supplied model's own limits."""
    visited: set[int] = set()
    current = model
    context_tokens = None
    output_tokens = None
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        attributes = getattr(current, "__dict__", {})
        configured = attributes.get("_qwenpaw_request_budget")
        if isinstance(configured, RequestBudget):
            return configured
        context_tokens = context_tokens or _positive_int(
            attributes.get("context_size"),
        )
        # Explicit call aliases in compatibility adapters override a default
        # max_tokens parameter. Never add aliases or invent an output value.
        extra = attributes.get("_extra_generate_kwargs") or {}
        if isinstance(extra, dict):
            for key in (
                "max_output_tokens",
                "max_completion_tokens",
                "max_tokens",
            ):
                explicit_output = _positive_int(extra.get(key))
                if explicit_output is not None:
                    output_tokens = explicit_output
                    break
        parameters = attributes.get("parameters")
        output_tokens = output_tokens or _positive_int(
            getattr(parameters, "max_tokens", None),
        )
        # Redaction guards proxy attributes through __getattr__, but keep
        # their wrapped model under _original_model rather than _inner/_model.
        current = (
            attributes.get("_inner")
            or attributes.get("_model")
            or attributes.get("_original_model")
        )
    if context_tokens is not None and output_tokens is not None:
        return RequestBudget(context_tokens, output_tokens)
    return None


def configured_model_budget(
    provider: Any, model_name: str, model: Any
) -> RequestBudget | None:
    """Capture the same selected model configuration used to build the model."""
    info = provider.get_model_info(model_name)
    configured_fields = getattr(info, "model_fields_set", set())
    context_tokens = (
        _positive_int(info.max_input_length)
        if "max_input_length" in configured_fields
        else None
    ) or _positive_int(getattr(model, "context_size", None))
    kwargs = provider.get_effective_generate_kwargs(model_name)
    output_tokens = next(
        (
            number
            for key in (
                "max_output_tokens",
                "max_completion_tokens",
                "max_tokens",
            )
            if (number := _positive_int(kwargs.get(key))) is not None
        ),
        None,
    )
    if output_tokens is None:
        inferred = budget_for_model(model)
        output_tokens = (
            inferred.output_tokens if inferred is not None else None
        )
    if context_tokens is not None and output_tokens is not None:
        return RequestBudget(context_tokens, output_tokens)
    return None


@dataclass(frozen=True)
class RequestSnapshot:
    """Preserve the actual system/summary and per-call additions on rebuilding.

    Scroll changes only state.context. Refuse ambiguous/interleaved rewrites
    rather than dropping or reordering a middleware's extra messages.
    """

    prefix: tuple[Any, ...]
    suffix: tuple[Any, ...]
    tools: Any

    @classmethod
    def capture(
        cls, agent: Any, messages: list, tools: Any
    ) -> "RequestSnapshot":
        context = list(agent.state.context)
        if not context:
            raise ValueError("没有可以安全压缩的对话内容。")
        starts = [
            index
            for index, message in enumerate(messages)
            if getattr(message, "id", None) == context[0].id
        ]
        matches = [
            index
            for index in starts
            if messages[index : index + len(context)] == context
        ]
        if len(matches) != 1:
            raise ValueError(
                "本次请求的消息经过额外改写，无法安全重建压缩后的输入。"
            )
        start = matches[0]
        return cls(
            tuple(messages[:start]),
            tuple(messages[start + len(context) :]),
            tools,
        )

    async def rebuild(self, agent: Any) -> dict[str, Any]:
        return {
            "messages": [
                *self.prefix,
                *agent.state.context,
                *self.suffix,
            ],
            "tools": self.tools,
        }
