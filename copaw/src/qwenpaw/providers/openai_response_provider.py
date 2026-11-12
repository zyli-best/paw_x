# -*- coding: utf-8 -*-
"""An OpenAI Responses API provider implementation."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
from typing import Any

from agentscope.model import ChatModelBase, OpenAIResponseModel
from pydantic import TypeAdapter, ValidationError

from .capping_formatter import _CappingOpenAIResponseFormatter
from .openai_provider import OpenAIProvider

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _response_input_adapter() -> TypeAdapter:
    """Use the installed SDK's item types without maintaining another schema."""
    from openai.types.responses import ResponseInputItemParam

    return TypeAdapter(ResponseInputItemParam)


def _normalize_response_input(items: Any) -> Any:
    """Repair legacy formatter messages without changing stored history.

    Gateways may require the optional message discriminator explicitly. Plain
    text also avoids mixing EasyInputMessage with incomplete output messages.
    Preserve annotated output, media sources, reasoning and tool identities.
    """
    if isinstance(items, str):
        return items
    if not isinstance(items, (list, tuple)):
        raise ValueError("Responses input must be a string or a list of items")

    normalized = deepcopy(list(items))
    types_added = texts_flattened = image_defaults = 0
    for index, item in enumerate(normalized):
        prefix = f"Responses input[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{prefix} must be an object")
        if item.get("type") in (None, ""):
            role = item.get("role")
            if not isinstance(role, str) or role not in {
                "system", "developer", "user", "assistant",
            } or "content" not in item:
                raise ValueError(f"{prefix} cannot infer a message type")
            item["type"] = "message"
            types_added += 1

        content = item.get("content")
        media_content = (
            item.get("output")
            if item["type"] == "function_call_output"
            else content if item["type"] == "message" else None
        )
        if isinstance(media_content, list):
            for part_index, part in enumerate(media_content):
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "input_image":
                    # AgentScope omits detail; the SDK schema requires it and
                    # documents auto as the default. Keep explicit choices.
                    if part.get("detail") in (None, ""):
                        part["detail"] = "auto"
                        image_defaults += 1
                    if not (part.get("image_url") or part.get("file_id")):
                        raise ValueError(
                            f"{prefix} media block[{part_index}] has no image source",
                        )
                elif part.get("type") == "input_file" and not any(
                    part.get(key) for key in ("file_id", "file_url", "file_data")
                ):
                    raise ValueError(
                        f"{prefix} media block[{part_index}] has no file source",
                    )
        if item["type"] == "message" and isinstance(content, list):
            # Never flatten citation/cache metadata or genuine output items.
            plain_text = bool(content) and all(
                isinstance(part, dict)
                and part.get("type") in ("input_text", "output_text")
                and isinstance(part.get("text"), str)
                and part.keys() <= {"type", "text"}
                for part in content
            )
            if plain_text and not {"id", "status"}.intersection(item):
                item["content"] = "".join(part["text"] for part in content)
                texts_flattened += 1
        try:
            # Validate, but keep the original dictionaries: validation results
            # may omit provider extensions or turn Iterables into iterators.
            validated = [_response_input_adapter().validate_python(item)]
            while validated:
                value = validated.pop()
                if isinstance(value, dict):
                    validated.extend(value.values())
                elif isinstance(value, Iterable) and not isinstance(
                    value, (str, bytes),
                ):
                    # Force validation of SDK Iterable fields (tool outputs,
                    # reasoning summaries, citations), not only their container.
                    validated.extend(value)
        except ValidationError:
            # Do not include Pydantic's input_value, which can contain prompts.
            raise ValueError(
                f"{prefix} does not match an OpenAI SDK input item schema",
            ) from None

        logger.debug(
            "Responses input[%d]: type=%s role=%s content_types=%s",
            index,
            item["type"],
            item.get("role"),
            [
                part.get("type") if isinstance(part, dict) else type(part).__name__
                for part in item["content"]
            ]
            if isinstance(item.get("content"), list)
            else "text" if isinstance(item.get("content"), str) else None,
        )
    logger.info(
        "Responses input normalized: items=%d message_types_added=%d "
        "text_messages_flattened=%d image_details_defaulted=%d",
        len(normalized), types_added, texts_flattened, image_defaults,
    )
    return normalized


def _normalize_reasoning_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Translate Chat effort aliases in one configuration/call layer."""
    result = deepcopy(kwargs)
    body = result.get("extra_body")
    sources = [result, body] if isinstance(body, dict) else [result]
    reasoning: dict[str, Any] = {}
    for source in sources:
        native = source.pop("reasoning", None)
        legacy = source.pop("reasoning_effort", None)
        if native is not None:
            if not isinstance(native, dict):
                raise ValueError("reasoning must be an object")
            native = {k: v for k, v in native.items() if v is not None}
            if (
                "effort" in native
                and "effort" in reasoning
                and native["effort"] != reasoning["effort"]
            ):
                raise ValueError("Conflicting reasoning effort settings")
            reasoning.update(native)
        if legacy is not None:
            if "effort" in reasoning and reasoning["effort"] != legacy:
                raise ValueError("Conflicting reasoning effort settings")
            reasoning["effort"] = legacy
    if reasoning:
        result["reasoning"] = reasoning
    if isinstance(body, dict) and not body:
        result.pop("extra_body", None)
    return result


class OpenAIResponseModelCompat(OpenAIResponseModel):
    """OpenAIResponseModel with extra-kwargs injection and tool schema
    sanitization.

    * ``extra_generate_kwargs`` — merged into every ``_call_api`` call
      (provider-level kwargs like ``extra_body``).
    * ``_format_tools`` — sanitizes boolean JSON Schema values that strict
      providers reject (same fix as ``OpenAIChatModelCompat``).
    * ``input`` — normalizes the final formatter output for strict gateways.
    """

    def __init__(
        self,
        *,
        extra_generate_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._extra_generate_kwargs = extra_generate_kwargs or {}
        super().__init__(**kwargs)

    async def _call_api(
        self,
        model_name: str,
        messages: Any,
        tools: list[dict] | None = None,
        tool_choice: Any | None = None,
        **generate_kwargs: Any,
    ) -> Any:
        import openai

        # Normalize before merging so a call's native effort overrides an
        # inherited Chat alias (and vice versa), including Shell's ``none``.
        merged = OpenAIProvider._deep_merge(
            _normalize_reasoning_kwargs(self._extra_generate_kwargs),
            _normalize_reasoning_kwargs(generate_kwargs),
        )
        merged.pop("disable_thinking", None)

        # AgentScope 2.0.4 has no pre-send hook. Keep its request assembly here
        # so normalization runs AFTER the complete (possibly wrapped) formatter,
        # exactly once, without replacing shared formatter state.
        api_kwargs: dict[str, Any] = {
            "model": model_name,
            "input": await self.formatter.format(messages),
            "stream": self.stream,
        }
        if self.parameters.max_tokens is not None:
            api_kwargs["max_output_tokens"] = self.parameters.max_tokens
        if self.parameters.temperature is not None:
            api_kwargs["temperature"] = self.parameters.temperature
        if self.parameters.thinking_enable and self.parameters.reasoning_effort:
            api_kwargs["reasoning"] = {
                "effort": self.parameters.reasoning_effort,
            }
        # Match AgentScope's filtering of Chat-only audio output options.
        api_kwargs.update({
            key: value for key, value in merged.items()
            if key not in {"modalities", "audio"}
        })
        fmt_tools, fmt_choice = self._format_tools(tools, tool_choice)
        if fmt_tools is not None:
            api_kwargs["tools"] = fmt_tools
        if fmt_choice is not None:
            api_kwargs["tool_choice"] = fmt_choice
        from ..runtime.normal_preemption import current_normal

        if current_normal() is not None:
            include = list(api_kwargs.get("include") or [])
            if "reasoning.encrypted_content" not in include:
                include.append("reasoning.encrypted_content")
            api_kwargs["include"] = include

        # The SDK merges extra_body over named arguments; normalize the actual
        # input even if a caller supplied it through that escape hatch.
        body = api_kwargs.get("extra_body")
        if isinstance(body, dict) and "input" in body:
            api_kwargs["input"] = body.pop("input")
        api_kwargs["input"] = _normalize_response_input(api_kwargs["input"])
        logger.debug(
            "Responses request: model=%s stream=%s",
            model_name, api_kwargs["stream"],
        )
        client = openai.AsyncClient(**{
            "api_key": self.credential.api_key.get_secret_value(),
            "organization": self.credential.organization,
            "base_url": self.credential.base_url,
            **self.client_kwargs,
        })
        started = datetime.now()
        try:
            response = await client.responses.create(**api_kwargs)
        except openai.APIStatusError as exc:
            logger.warning(
                "Responses request failed: status=%s request_id=%s",
                exc.status_code, exc.request_id,
            )
            raise
        if api_kwargs["stream"]:
            return self._parse_stream_response(started, response)
        return self._parse_completion_response(started, response)

    def _format_tools(
        self,
        tools: list[dict] | None,
        tool_choice: Any | None,
    ) -> tuple[list[dict] | None, Any]:
        from .openai_chat_model_compat import _sanitize_tool_schemas

        if tools:
            tools = _sanitize_tool_schemas(tools)
        return super()._format_tools(tools, tool_choice)

    @staticmethod
    def _retain_reasoning(chunk, response):
        from agentscope.message import ThinkingBlock, TextBlock, ToolCallBlock
        from ..runtime.normal_protocol import REPLAY_KEY

        if not any(item.type == "reasoning" for item in response.output):
            return
        # The SDK streaming accumulator uses one thinking block for the whole
        # response. Rebuild only the completed response, in native item order,
        # including empty reasoning summaries and reasoning between calls.
        blocks = []
        for item in response.output:
            if item.type == "reasoning":
                thinking = ThinkingBlock(
                    id=item.id, reasoning_item_id=item.id,
                    thinking=" ".join(s.text for s in item.summary if getattr(s, "text", "")),
                )
                setattr(thinking, REPLAY_KEY, [item.model_dump(mode="json", exclude_none=True)])
                blocks.append(thinking)
            elif item.type == "function_call":
                blocks.append(ToolCallBlock(id=item.call_id, name=item.name, input=item.arguments or "{}"))
            elif item.type == "message":
                blocks.extend(TextBlock(text=part.text) for part in item.content if part.type == "output_text")
        chunk.content = blocks

    async def _parse_stream_response(self, started, response):
        from ..runtime.normal_preemption import current_normal

        if current_normal() is None:
            async for chunk in super()._parse_stream_response(started, response):
                yield chunk
            return
        completed = None
        from ..runtime.normal_preemption import retain_stream

        close = retain_stream(response)

        async def events():
            nonlocal completed
            async for event in response:
                if event.type == "response.completed":
                    completed = event.response
                yield event

        from contextlib import aclosing

        try:
            async with aclosing(super()._parse_stream_response(started, events())) as parsed:
                async for chunk in parsed:
                    if completed is not None:
                        yield self._parse_completion_response(started, completed)
                        return
                    yield chunk
            if completed is not None:
                yield self._parse_completion_response(started, completed)
        finally:
            await close()

    def _parse_completion_response(self, started, response):
        chunk = super()._parse_completion_response(started, response)
        from ..runtime.normal_preemption import current_normal

        if current_normal() is not None:
            self._retain_reasoning(chunk, response)
        return chunk


class OpenAIResponseProvider(OpenAIProvider):
    """Provider that uses the OpenAI Responses API instead of Chat Completions.

    Inherits connection/discovery logic from ``OpenAIProvider`` but
    creates ``OpenAIResponseModel`` instances via ``get_chat_model_instance``.
    The ``check_model_connection`` method uses the Responses API endpoint.

    Multimodal probing (``_probe_image_support`` / ``_probe_video_support``)
    is inherited from ``OpenAIProvider`` and uses the Chat Completions
    endpoint.  This works for OpenAI (which supports both APIs) and fails
    gracefully (returns "probe inconclusive") for third-party providers
    that only expose the Response API.
    """

    def get_effective_generate_kwargs(self, model_id: str) -> dict[str, Any]:
        effective = _normalize_reasoning_kwargs(self.generate_kwargs)
        info = self.get_model_info(model_id)
        if info is not None:
            effective = self._deep_merge(
                effective, _normalize_reasoning_kwargs(info.generate_kwargs),
            )
            effective.setdefault("max_tokens", info.max_tokens)
        return effective

    async def check_model_connection(
        self,
        model_id: str,
        timeout: float = 5,
    ) -> tuple[bool, str]:
        """Check if a model is reachable via the Responses API."""
        from openai import APIError

        model_id = (model_id or "").strip()
        if not model_id:
            return False, "Empty model ID"

        try:
            client = self._client(timeout=timeout)
            res = await client.responses.create(
                model=model_id,
                input="ping",
                timeout=timeout,
                max_output_tokens=20,
                stream=True,
            )
            async for _ in res:
                break
            return True, ""
        except APIError:
            return False, f"API error when connecting to model '{model_id}'"
        except Exception:
            return (
                False,
                f"Unknown exception when connecting to model '{model_id}'",
            )

    def get_chat_model_instance(self, model_id: str) -> ChatModelBase:
        from agentscope.credential import OpenAICredential

        credential = OpenAICredential(
            id=f"qwenpaw-{self.id}",
            api_key=self.api_key,
            base_url=self.base_url,
        )

        merged_headers = self._build_default_headers()
        gen_kwargs = self.get_effective_generate_kwargs(model_id)
        parameters = OpenAIResponseModel.Parameters(
            max_tokens=gen_kwargs.pop("max_tokens", None),
            temperature=gen_kwargs.pop("temperature", None),
        )

        client_kwargs: dict[str, Any] = {}
        if merged_headers:
            client_kwargs["default_headers"] = merged_headers

        return OpenAIResponseModelCompat(
            credential=credential,
            model=model_id,
            parameters=parameters,
            stream=True,
            context_size=self._get_context_size(model_id),
            client_kwargs=client_kwargs,
            extra_generate_kwargs=gen_kwargs or None,
            formatter=_CappingOpenAIResponseFormatter(
                max_bytes=self.max_inline_media_bytes,
            ),
        )
