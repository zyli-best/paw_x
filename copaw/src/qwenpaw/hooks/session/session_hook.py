# -*- coding: utf-8 -*-
"""Session load/save lifecycle hooks.

Loads persisted session state into ``ctx.session_state`` (PRE_AGENT_BUILD)
so the builder can inject it into the newly-constructed agent. Saves
agent state back to session storage after the response completes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from ..base import LifecycleHook
from ...agents.acp.meta import ACP_EPHEMERAL_META_KEY
from ...runtime._state_utils import StateProxy
from ...runtime.hooks import HookAction, HookContext, HookResult
from ...runtime.phases import Phase
from ...runtime.session_views import restore_session_view, snapshot_session_view

logger = logging.getLogger(__name__)

_LOADED_CONTEXT_LEN_KEY = "session_loaded_context_len"
_LOADED_CONTEXT_IDS_KEY = "session_loaded_context_ids"


def _is_ephemeral_request(ctx: HookContext) -> bool:
    request = ctx.request
    request_context = getattr(request, "request_context", None)
    if isinstance(request_context, dict):
        value = request_context.get(ACP_EPHEMERAL_META_KEY)
        if value is True:
            return True
        if isinstance(value, str) and value.lower() in {"1", "true", "yes"}:
            return True
    return False


def _state_context(state: Any) -> list[Any]:
    if not isinstance(state, dict):
        return []
    agent_state = state.get("agent")
    if isinstance(agent_state, dict):
        state = agent_state
    inner = state.get("state")
    if not isinstance(inner, dict):
        return []
    context = inner.get("context")
    return context if isinstance(context, list) else []


def _message_id(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("id") or "")
    return str(getattr(message, "id", "") or "")


def _get_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _coerce_tool_input(raw_input: Any) -> dict[str, Any]:
    if isinstance(raw_input, dict):
        return raw_input
    if isinstance(raw_input, str):
        try:
            parsed = json.loads(raw_input)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _message_blocks(message: Any) -> list[Any]:
    content = _get_value(message, "content", [])
    if isinstance(content, list):
        return content
    content_blocks = _get_value(message, "content_blocks", [])
    return content_blocks if isinstance(content_blocks, list) else []


def _extract_agent_calls_from_delta(
    delta: list[Any],
    ctx: HookContext,
) -> list[dict[str, Any]]:
    agent_tools = {
        "chat_with_agent": "chat",
        "submit_to_agent": "task",
        "spawn_subagent": "subagent",
    }
    calls: list[dict[str, Any]] = []

    for message in delta:
        for block in _message_blocks(message):
            block_type = _get_value(block, "type", "")
            if block_type not in {"tool_call", "tool_use"}:
                continue
            tool_name = str(_get_value(block, "name", "") or "")
            mode = agent_tools.get(tool_name)
            if mode is None:
                continue

            tool_input = _coerce_tool_input(_get_value(block, "input", {}))
            target_agent_id = tool_input.get("to_agent")
            if tool_name == "spawn_subagent" and not target_agent_id:
                target_agent_id = getattr(ctx, "agent_id", "")

            calls.append(
                {
                    "tool_call_id": str(_get_value(block, "id", "") or ""),
                    "tool_name": tool_name,
                    "mode": mode,
                    "source_agent_id": getattr(ctx, "agent_id", ""),
                    "target_agent_id": target_agent_id,
                    "parent_session_id": getattr(ctx, "session_id", ""),
                    "child_session_id": tool_input.get("session_id"),
                    "root_agent_id": getattr(ctx, "root_agent_id", ""),
                    "root_session_id": getattr(
                        ctx,
                        "root_session_id",
                        getattr(ctx, "session_id", ""),
                    ),
                }
            )

    return calls


def _record_loaded_context_baseline(ctx: HookContext, state: Any) -> None:
    if not hasattr(ctx, "extras") or ctx.extras is None:
        ctx.extras = {}
    context = _state_context(state)
    ctx.extras[_LOADED_CONTEXT_LEN_KEY] = len(context)
    ctx.extras[_LOADED_CONTEXT_IDS_KEY] = {
        mid for mid in (_message_id(msg) for msg in context) if mid
    }


def _build_session_delta_payload(
    ctx: HookContext,
    full_state: dict[str, Any],
    *,
    user_id: str,
    channel: str,
) -> dict[str, Any] | None:
    context = _state_context(full_state)
    if not context:
        return None

    extras = getattr(ctx, "extras", {}) or {}
    loaded_len = int(extras.get(_LOADED_CONTEXT_LEN_KEY, 0) or 0)
    loaded_ids = extras.get(_LOADED_CONTEXT_IDS_KEY, set())
    if not isinstance(loaded_ids, set):
        loaded_ids = set(loaded_ids or [])

    delta_start = max(0, min(loaded_len, len(context)))
    candidates = context[delta_start:]
    if len(context) < loaded_len:
        candidates = context
    steering = extras.get("normal_steering")
    if steering is not None and (steering.injected or steering.interruptions):
        from ...runtime.normal_transcript import project_interruptions

        context = project_interruptions(steering, context)
        # Scroll can shrink history and then grow past loaded_len again.
        # Stable identities, rather than the old offset, define this delta.
        candidates = context
        delta_start = 0
    if extras.get("session_delta_scan_ids"):
        candidates, delta_start = context, 0

    delta = [
        msg
        for msg in candidates
        if not (_message_id(msg) and _message_id(msg) in loaded_ids)
    ]
    if not delta:
        return None

    delta_end = delta_start + len(candidates)
    turn_id = _message_id(delta[-1]) or f"{delta_start}-{delta_end}"
    payload = {
        "event_type": "session_delta_saved",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": getattr(ctx, "agent_id", ""),
        "root_agent_id": getattr(ctx, "root_agent_id", ""),
        "session_id": ctx.session_id,
        "root_session_id": getattr(ctx, "root_session_id", ctx.session_id),
        "user_id": user_id,
        "channel": channel,
        "workspace_dir": str(getattr(ctx, "workspace_dir", "") or ""),
        "delta_start": delta_start,
        "delta_end": delta_end,
        "delta_count": len(delta),
        "turn_id": turn_id,
        "agent_calls": _extract_agent_calls_from_delta(delta, ctx),
        "delta": delta,
    }
    # Session-scoped extra skills injected for this session (names only;
    # the rendered prompt incl. skill dirs is in ``model_requests``).
    session_skills = extras.get("session_skills")
    if isinstance(session_skills, (list, tuple)) and session_skills:
        payload["session_skills"] = [str(name) for name in session_skills]

    system_prompt = extras.get("system_prompt")
    if isinstance(system_prompt, str) and system_prompt:
        payload["system_prompt"] = system_prompt

    try:
        from ...observability.model_request_capture import (
            pop_model_requests_for_session,
        )

        model_requests = pop_model_requests_for_session(ctx.session_id)
    except Exception:
        logger.debug(
            "session_delta: model request capture read failed",
            exc_info=True,
        )
        model_requests = []

    # Edge-cloud v2 is a pure observability overlay: when the orchestrator
    # handled this request it attaches a fully built payload. Non-collaborative
    # requests and child agents never see this key and keep the legacy shape.
    edge_cloud_upload = extras.get("edge_cloud_upload")
    if isinstance(edge_cloud_upload, dict) and edge_cloud_upload:
        payload.update(edge_cloud_upload)
        payload["protocol"] = "edge_cloud_trace.v2"
        payload["record_type"] = "delta"
        # Config records remain independent. Only the collaborative turn's
        # body is replaced by its emitted transcript; native agent-call and
        # session-skill semantics stay compatible with ordinary deltas.
        payload["delta_count"] = len(payload["delta"])
        payload.pop("system_prompt", None)
        return payload

    if model_requests:
        payload["model_requests"] = model_requests
    if steering is not None:
        from ...runtime.normal_protocol import export_view

        payload = export_view(payload)
    return payload


def schedule_steering_upload(ctx, full_state) -> None:
    """Ordinary cancellation reuses the normal exporter, at most once per run."""
    if _is_ephemeral_request(ctx):
        return
    steering = ctx.extras.get("normal_steering")
    if steering is None or not (steering.injected or steering.interruptions) or steering.upload_scheduled:
        return
    request = ctx.request
    rc = getattr(request, "request_context", None) or {}
    payload = _build_session_delta_payload(
        ctx, full_state,
        user_id=str(rc.get("user_id") or getattr(request, "user_id", "") or ""),
        channel=getattr(request, "channel", "") or "",
    )
    if payload:
        if isinstance(rc.get("sensitive_words"), dict):
            payload["_sensitive_words"] = rc["sensitive_words"]
        from ...observability.session_delta_langfuse import schedule_session_delta_upload

        schedule_session_delta_upload(payload)
        steering.upload_scheduled = True


class SessionLoadHook(LifecycleHook):
    """Load persisted session state before agent construction."""

    phase = Phase.PRE_AGENT_BUILD
    name = "session_load"
    priority = 10

    async def run(self, ctx: HookContext) -> HookResult:
        if _is_ephemeral_request(ctx):
            return HookResult()
        if ctx.workspace is None:
            return HookResult()
        session = getattr(ctx.workspace, "session", None)
        if session is None:
            return HookResult()
        try:
            request = ctx.request
            user_id = getattr(request, "user_id", "") or ctx.session_id
            channel = getattr(request, "channel", "") or ""

            proxy = StateProxy()
            await session.load_session_state(
                session_id=ctx.session_id,
                user_id=user_id,
                channel=channel,
                agent=proxy,
            )
            if proxy.data:
                proxy.data = restore_session_view(ctx, proxy.data)
                ctx.session_state = proxy.data
            _record_loaded_context_baseline(ctx, proxy.data)
        except KeyError as e:
            logger.debug(
                "session_load: skipped (schema mismatch): %s",
                e,
            )
        except Exception:
            logger.debug("session_load: failed", exc_info=True)
        return HookResult()


class SessionSaveHook(LifecycleHook):
    """Persist agent state after response completion."""

    phase = Phase.POST_RESPONSE
    name = "session_save"
    priority = 90

    async def run(self, ctx: HookContext) -> HookResult:
        steering = ctx.extras.get("normal_steering")
        if _is_ephemeral_request(ctx):
            return HookResult()
        if ctx.workspace is None or ctx.agent is None:
            return HookResult()
        session = getattr(ctx.workspace, "session", None)
        if session is None:
            return HookResult()
        try:
            request = ctx.request
            request_context = getattr(request, "request_context", None)
            if not isinstance(request_context, dict):
                request_context = {}
            # Two identities: ``storage_user_id`` names the persisted session
            # file and must stay stable for history continuity; changing it
            # would orphan existing session state. ``observed_user_id`` is the
            # REAL end user reported to Langfuse: for multi-agent turns the
            # top-level request.user_id is the caller agent id (chat/submit)
            # or "default" (spawn), so the propagated
            # request_context.user_id wins whenever present.
            storage_user_id = getattr(request, "user_id", "") or ctx.session_id
            observed_user_id = (
                str(request_context.get("user_id") or "").strip()
                or getattr(request, "user_id", "")
                or ""
            )
            channel = getattr(request, "channel", "") or ""

            proxy = StateProxy()
            full_state = ctx.agent.state_dict()
            proxy.data = snapshot_session_view(ctx, full_state)
            await session.save_session_state(
                session_id=ctx.session_id,
                user_id=storage_user_id,
                channel=channel,
                agent=proxy,
            )
            if steering is not None and steering.upload_scheduled:
                return HookResult()
            if ctx.extras.get("session_delta_upload_scheduled"):
                return HookResult()
            payload = _build_session_delta_payload(
                ctx,
                full_state,
                user_id=observed_user_id,
                channel=channel,
            )
            if payload:
                sensitive_words = request_context.get("sensitive_words")
                if isinstance(sensitive_words, dict):
                    payload["_sensitive_words"] = sensitive_words

                from ...observability.session_delta_langfuse import (
                    schedule_session_delta_upload,
                )

                schedule_session_delta_upload(payload)
                if ctx.extras.get("cancel_snapshot_observer") is not None:
                    ctx.extras["session_delta_upload_scheduled"] = True
                if steering is not None:
                    steering.upload_scheduled = True
        except Exception:
            logger.debug("session_save: failed", exc_info=True)
        return HookResult()


class SessionSkillsNoticeHook(LifecycleHook):
    """Return a conversational notice for unresolved session skills.

    Runs after :class:`AgentBuilder` resolved ``request_context.skills``
    (PRE_EXECUTE): when names the client passed in this request are not
    found in the skill pool, the agent is skipped and the notice becomes
    the turn's reply (same envelope path as slash-command messages).
    """

    phase = Phase.PRE_EXECUTE
    name = "session_skills_notice"
    # Run after the other PRE_EXECUTE hooks (bootstrap/env/media/
    # observability) so the short-circuit only ends agent execution.
    priority = 200

    async def run(self, ctx: HookContext) -> HookResult:
        if _is_ephemeral_request(ctx):
            return HookResult()
        missing = (getattr(ctx, "extras", None) or {}).get(
            "session_skills_missing",
        )
        if not missing:
            return HookResult()

        from agentscope.message import Msg, TextBlock

        from ...agents.skill_system.session_skills import (
            format_missing_skills_notice,
        )

        language = getattr(ctx.agent_config, "language", "zh") or "zh"
        text = format_missing_skills_notice(missing, language=language)
        logger.warning(
            "session skills not found in pool: %s (session=%s)",
            missing,
            ctx.session_id,
        )
        return HookResult(
            action=HookAction.SHORT_CIRCUIT,
            payload=Msg(
                name="assistant",
                role="assistant",
                content=[TextBlock(type="text", text=text)],
            ),
        )


__all__ = ["SessionLoadHook", "SessionSaveHook", "SessionSkillsNoticeHook"]
