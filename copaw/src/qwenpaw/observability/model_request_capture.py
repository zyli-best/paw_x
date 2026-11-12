# -*- coding: utf-8 -*-
"""Capture model request snapshots for session-delta observability."""

from __future__ import annotations

import copy
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

_requests_by_session: dict[str, list[dict[str, Any]]] = {}
_edge_cloud_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "qwenpaw_edge_cloud_model_request_context",
    default=None,
)
_edge_cloud_usage_sink: ContextVar[Callable[..., None] | None] = ContextVar(
    "qwenpaw_edge_cloud_usage_sink",
    default=None,
)
_session_system_prompt: ContextVar[str | None] = ContextVar(
    "qwenpaw_execution_session_system_prompt",
    default=None,
)
_stage_sink: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "qwenpaw_edge_cloud_stage_sink",
    default=None,
)
_agent_input_sink: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "qwenpaw_edge_cloud_agent_input_sink",
    default=None,
)
_EXTRA_GENERATE_ATTRS = (
    "_extra_generate_kwargs",
    "_qp_extra_generate_kwargs",
    "_qp_extra_config_kwargs",
)


def _json_safe(value: Any) -> Any:
    """Return a JSON-friendly copy without mutating the source object."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe(model_dump(mode="json"))
        except Exception:
            pass

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _json_safe(to_dict())
        except Exception:
            pass

    try:
        return copy.deepcopy(value)
    except Exception:
        return str(value)


def _model_parameters(model: Any) -> dict[str, Any]:
    raw = getattr(model, "parameters", None)
    safe = _json_safe(raw)
    return safe if isinstance(safe, dict) else {}


def _extra_generate_kwargs(model: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for attr in _EXTRA_GENERATE_ATTRS:
        raw = getattr(model, attr, None)
        if isinstance(raw, dict):
            safe = _json_safe(raw)
            if isinstance(safe, dict):
                merged.update(safe)
    return merged


def record_model_request(
    *,
    provider_id: str,
    model: Any,
    messages: list[dict],
    tools: list[dict] | None,
    tool_choice: Any,
    params: dict[str, Any],
) -> None:
    """Record one best-effort model request snapshot for the current session."""

    try:
        from ..app.agent_context import get_current_session_id

        session_id = get_current_session_id()
        input_sink = _agent_input_sink.get()
        if not session_id and not callable(input_sink):
            return
        edge_cloud_context = _edge_cloud_context.get()
        snapshot = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "provider_id": provider_id,
            "model": str(getattr(model, "model", "") or ""),
            "stream": bool(getattr(model, "stream", False)),
            "context_size": int(getattr(model, "context_size", 0) or 0),
            "messages": _json_safe(messages),
            "tools": _json_safe(tools or []),
            "tool_choice": _json_safe(tool_choice),
            "params": _json_safe(params),
            "model_parameters": _model_parameters(model),
            "generate_kwargs": _extra_generate_kwargs(model),
        }
        from ..runtime.normal_preemption import current_normal

        ordinary = current_normal()
        if ordinary is not None and ordinary.attempt is not None:
            snapshot["status"] = "in_progress"
            snapshot["metadata"] = {
                "run_id": ordinary.run_id, "response_id": ordinary.response_id,
                "steer_message_id": ordinary.attempt.steer_message_id,
            }
            ordinary.attempt.requests.append(snapshot)
        # ReAct supplies tools even for text-only agents ([]). Scroll's
        # auxiliary headline requests omit them and are not agent inputs.
        # Log the actual role prompt before the legacy prompt replacement.
        if callable(input_sink) and tools is not None:
            try:
                input_sink(copy.deepcopy(snapshot))
            except Exception:
                logger.debug("edge-cloud agent input sink failed", exc_info=True)
        if not session_id:
            return
        # Side-channel for the v2 edge-cloud trace: capture the raw request
        # before the default prompt replacement below, and keep the legacy
        # session-delta path completely unchanged.
        sink = _stage_sink.get()
        if callable(sink):
            try:
                sink(copy.deepcopy(snapshot))
            except Exception:
                logger.debug("edge-cloud stage sink failed", exc_info=True)
        if (edge_cloud_context or {}).get("stage") in {"router", "verifier"}:
            return

        if edge_cloud_context:
            # All tiers persist the default prompt, never temporary role or
            # handoff instructions. Control dialogues returned above.
            prompt = _session_system_prompt.get()
            if prompt is not None:
                snapshot["messages"] = [
                    {"role": "system", "content": prompt},
                    *[
                        m
                        for m in snapshot["messages"]
                        if m.get("role") not in {"system", "developer"}
                    ],
                ]
        _requests_by_session.setdefault(session_id, []).append(snapshot)
    except Exception:
        logger.debug("model request capture failed", exc_info=True)


@contextmanager
def edge_cloud_model_request_context(
    *,
    stage: str,
    base_agent_id: str,
    flow_id: str,
    usage_sink: Callable[..., None] | None = None,
    session_system_prompt: str | None = None,
    stage_sink: Callable[[dict[str, Any]], None] | None = None,
    agent_input_sink: Callable[[dict[str, Any]], None] | None = None,
) -> Iterator[None]:
    """Attach edge-cloud stage metadata to captured model requests."""

    context_token = _edge_cloud_context.set(
        {
            "enabled": True,
            "stage": stage,
            "base_agent_id": base_agent_id,
            "flow_id": flow_id,
        },
    )
    usage_token = _edge_cloud_usage_sink.set(usage_sink)
    prompt_token = _session_system_prompt.set(session_system_prompt)
    sink_token = _stage_sink.set(stage_sink)
    input_token = _agent_input_sink.set(agent_input_sink)
    try:
        yield
    finally:
        _agent_input_sink.reset(input_token)
        _stage_sink.reset(sink_token)
        _session_system_prompt.reset(prompt_token)
        _edge_cloud_usage_sink.reset(usage_token)
        _edge_cloud_context.reset(context_token)


def record_edge_cloud_model_usage(
    *,
    provider_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """Mirror already-counted cloud usage into the active edge-cloud trace."""

    try:
        context = _edge_cloud_context.get()
        sink = _edge_cloud_usage_sink.get()
        if not isinstance(context, dict) or not callable(sink):
            return
        sink(
            stage=str(context.get("stage") or ""),
            provider_id=provider_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    except Exception:
        logger.debug("edge-cloud usage capture failed", exc_info=True)


def is_edge_cloud_cloud_stage() -> bool:
    """Return whether usage belongs to a billable edge-cloud cloud stage."""

    context = _edge_cloud_context.get()
    if not isinstance(context, dict):
        return False
    return context.get("stage") in {"router", "verifier", "cloud"}


def pop_model_requests_for_session(session_id: str) -> list[dict[str, Any]]:
    """Return and clear captured model requests for *session_id*."""

    return _requests_by_session.pop(session_id, [])


def clear_model_request_captures() -> None:
    """Clear all captures; intended for tests."""

    _requests_by_session.clear()


__all__ = [
    "clear_model_request_captures",
    "edge_cloud_model_request_context",
    "is_edge_cloud_cloud_stage",
    "pop_model_requests_for_session",
    "record_edge_cloud_model_usage",
    "record_model_request",
]
