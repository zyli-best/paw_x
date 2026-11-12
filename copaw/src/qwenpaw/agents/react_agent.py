# -*- coding: utf-8 -*-
"""QwenPaw Agent - Main agent implementation.

This module provides the main QwenPawAgent class built on ReActAgent,
with integrated tools, skills, and memory management.

Agent construction is fully delegated to :class:`AgentBuilder` — the
agent accepts all dependencies (model, prompt, toolkit, middlewares)
as constructor parameters and does not build them internally.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import aclosing
from pathlib import Path
from typing import Any, Literal, Optional, TYPE_CHECKING

from agentscope.agent import Agent, ReActConfig
from agentscope.event import (
    ExternalExecutionResultEvent,
    ReplyStartEvent,
    RequireExternalExecutionEvent,
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolResultEndEvent,
    ToolResultDataDeltaEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
)
from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.state import AgentState
from agentscope.tool import Toolkit, ToolResponse

from .skill_system import get_workspace_skills_dir
from .skill_system.session_skills import SESSION_SKILLS_KEY
from .react_timing import (
    ActTiming,
    ReasoningTiming,
    ToolTiming,
    timing_context,
    timing_enabled,
)
from ..modes.coding import CodingModeMixin
from ..constant import (
    LOOP_CONTINUATION_MESSAGE_TAG,
    MEDIA_UNSUPPORTED_PLACEHOLDER,
    QWENPAW_MESSAGE_TAG_KEY,
    WORKING_DIR,
)
from ..loop.gates import StopAction, StopHandlerResult
from ..providers.model_capability_cache import get_capability_cache
from ..config.context import get_current_session_id

if TYPE_CHECKING:
    from ..agents.memory import BaseMemoryManager
    from ..config.config import AgentProfileConfig
    from ..loop.gates.failure_loop import FailureLoopGate
    from ..loop.progress_reminder import ProgressReminderController

logger = logging.getLogger(__name__)


class QwenPawAgent(CodingModeMixin, Agent):
    """QwenPaw Agent with integrated tools, skills, and memory management.

    This agent extends agentscope 2.0 ``Agent`` with:
    - Built-in tools (shell, file operations, browser, etc.)
    - Dynamic skill loading from working directory
    - Memory management with auto-compaction
    - Bootstrap guidance for first-time setup
    - Tool-guard security (via ``PolicyGuardedTool.check_permissions``)
    - Coding Mode features: Inline Diff (via CodingModeMixin)
    """

    def __init__(
        self,
        *,
        name: str,
        model: Any,
        system_prompt: str,
        toolkit: Toolkit,
        react_config: ReActConfig,
        middlewares: list,
        agent_config: "AgentProfileConfig",
        workspace_dir: Path | None = None,
        request_context: Optional[dict[str, str]] = None,
        memory_manager: "BaseMemoryManager | None" = None,
        offloader: Any = None,
        context_config: Any = None,
        context_manager: Any = None,
        effective_skills: Optional[list[str]] = None,
        session_skills: Optional[list[str]] = None,
        governor: Any = None,
        failure_loop_gate: "FailureLoopGate | None" = None,
        progress_reminder: "ProgressReminderController | None" = None,
    ):
        """Initialize QwenPawAgent.

        All construction dependencies (model, prompt, toolkit, middlewares)
        are provided externally by :class:`AgentBuilder`. The agent does
        not build any of these internally.
        """
        self._agent_config = agent_config
        self._request_context = dict(request_context or {})
        self._workspace_dir = workspace_dir
        self._language = agent_config.language
        # Optional context-management strategy. When None, the agent keeps its
        # native AgentScope compression (see compress_context /
        # _save_to_context).
        self._context_manager = context_manager

        # Session-scoped extra skills from ``request_context.skills``
        # (non-own names accumulated by the builder).
        self._session_skills: list[str] = list(session_skills or [])

        # Register skills metadata on toolkit
        self._register_skills(toolkit, effective_skills=effective_skills or [])

        self._governor = governor
        self._gate_pending_stop = None
        self._gate_pending_continue = None
        self._act_timing: ActTiming | None = None
        self._failure_loop_gate = failure_loop_gate
        self._failure_recovery_middleware = None
        self._progress_reminder = progress_reminder
        self._progress_reminder_middleware = None
        if failure_loop_gate is not None:
            from .context.failure_middleware import FailureRecoveryMiddleware
            from .context.budget_middleware import RequestBudgetMiddleware

            self._failure_recovery_middleware = FailureRecoveryMiddleware(
                failure_loop_gate,
            )
            middlewares = list(middlewares)
            budget_index = next((
                idx for idx, mw in enumerate(middlewares)
                if isinstance(mw, RequestBudgetMiddleware)
            ), len(middlewares))
            middlewares.insert(budget_index, self._failure_recovery_middleware)

        if progress_reminder is not None:
            from .context.progress_middleware import ProgressReminderMiddleware
            from .context.budget_middleware import RequestBudgetMiddleware

            self._progress_reminder_middleware = ProgressReminderMiddleware(
                progress_reminder,
            )
            middlewares = list(middlewares)
            budget_index = next((
                idx for idx, mw in enumerate(middlewares)
                if isinstance(mw, RequestBudgetMiddleware)
            ), len(middlewares))
            middlewares.insert(
                budget_index, self._progress_reminder_middleware,
            )

        self.memory_manager = memory_manager

        # Register memory tools into toolkit
        if self.memory_manager is not None:
            memory_tools = self.memory_manager.list_memory_tools()
            basic_group = toolkit.tool_groups[0]
            for tool_fn in memory_tools:
                from ..governance import PolicyGuardedTool

                basic_group.tools.append(
                    PolicyGuardedTool(
                        tool_fn,
                        governor=self._governor,
                        request_context=self._request_context,
                    ),
                )
            logger.debug(
                "Registered memory tools: %s",
                [fn.__name__ for fn in memory_tools],
            )

        init_kwargs: dict[str, Any] = {
            "name": name,
            "model": model,
            "system_prompt": system_prompt,
            "toolkit": toolkit,
            "react_config": react_config,
            "middlewares": middlewares,
            "offloader": offloader,
        }
        if context_config is not None:
            init_kwargs["context_config"] = context_config
        super().__init__(**init_kwargs)

        # Bypass agentscope's built-in permission engine — qwenpaw uses
        # its own PolicyGuardedTool.check_permissions for tool-guard.
        from agentscope.permission import PermissionMode

        self.state.permission_context.mode = PermissionMode.BYPASS

        # Tombstone for legacy ``getattr(agent, "memory", None)`` callers
        self.memory = None  # type: ignore[assignment]

        self._register_tool_call_hooks()

    async def compress_context(
        self,
        context_config: Any = None,
    ) -> None:
        """Delegate to the context manager, else native compression.

        With a ``context_manager`` injected (e.g. the scroll strategy), it owns
        compression. Otherwise fall back to AgentScope's native path, gated on
        ``context_compact_config.enabled``.
        """
        policy = getattr(self, "_context_input_policy", None)
        if policy is not None:
            await policy.prepare(self)
        steering = getattr(self, "_normal_steering", None)
        if steering is not None:
            await self._inject_pending_hints()
            await steering.prepare(self)
        if self._context_manager is not None:
            if context_config is None:
                try:
                    compact = (
                        self._agent_config.running.light_context_config
                        .context_compact_config
                    )
                    if not compact.enabled:
                        return
                except AttributeError:
                    pass
            await self._context_manager.compress(self, context_config)
            if policy is not None:
                policy.validate(self.state.context)
            if steering is not None:
                steering.assert_preserved(self.state.context)
            return
        try:
            lcc = self._agent_config.running.light_context_config
            if not lcc.context_compact_config.enabled:
                return
        except Exception:
            pass
        await super().compress_context(context_config)
        if policy is not None:
            policy.validate(self.state.context)
        if steering is not None:
            steering.assert_preserved(self.state.context)

    def _save_to_context(self, blocks: Any, usage: Any = None) -> None:
        """Append blocks, then let the context manager write them through."""
        steering = getattr(self, "_normal_steering", None)
        if steering is not None:
            for block in blocks if isinstance(blocks, list) else [blocks]:
                if getattr(block, "type", None) == "tool_result" and steering.stale_call(block.id):
                    block.metadata = {**(block.metadata or {}), "steer_superseded": True}
        previous_len = len(self.state.context)
        super()._save_to_context(blocks, usage)
        from ..runtime.output_attribution import current_frame, tag_output

        frame = current_frame()
        if (
            frame is not None and self.state.context
            and self.state.context[-1].role == "assistant"
            and self.state.context[-1].name == self.name
        ):
            tag_output(self.state.context[-1], frame.steer_message_id)
        policy = getattr(self, "_context_input_policy", None)
        if policy is not None:
            policy.on_save(self, previous_len)
        steering = getattr(self, "_normal_steering", None)
        if steering is not None and steering.injected and len(self.state.context) > previous_len:
            # The SDK reuses reply_id when a user message splits an assistant
            # reply. Give the new segment its own persistence id without
            # resetting the reply identity used by gates and streaming events.
            last = self.state.context[-1]
            if any(msg.id == last.id for msg in self.state.context[:-1]):
                last.id = uuid.uuid4().hex
                last.metadata = {**(last.metadata or {}), "steering_reply_id": self.state.reply_id}
        if self._context_manager is not None:
            self._context_manager.on_save(self, blocks)

    # Session persistence calls state_dict/load_state_dict on the agent;
    # these round-trip through self.state (AgentState pydantic model).
    def state_dict(self) -> dict:
        """Serialize the agent's 2.0 ``AgentState`` to a JSON-safe dict."""
        state = getattr(self, "state", None)
        if state is None:
            return {}
        out = {"state": state.model_dump(mode="json")}
        # Persist the scroll manager's dedup bookkeeping + eviction index so a
        # resumed session doesn't re-append its restored window to history.db.
        cm = getattr(self, "_context_manager", None)
        if cm is not None and hasattr(cm, "to_dict"):
            out["scroll"] = cm.to_dict()
        # ``getattr``: some tests drive this method through a thin shim that
        # only exposes ``state`` / ``_context_manager``.
        session_skills = getattr(self, "_session_skills", None)
        if session_skills:
            out[SESSION_SKILLS_KEY] = list(session_skills)
        progress = getattr(self, "_progress_reminder", None)
        if progress is not None:
            block = progress.to_dict()
            if block is not None:
                out["progress_reminder"] = block
        return out

    def load_state_dict(self, state_dict: dict, strict: bool = True) -> None:
        """Restore ``self.state`` from a dict produced by :meth:`state_dict`.

        Handles two formats:
        - **2.0**: ``{"state": {AgentState dump}}``
        - **1.x legacy**: ``{"memory": {"content": [[msg, marks], ...],
          "_compressed_summary": "..."}}`` — converted on-the-fly so
          existing sessions survive the upgrade.
        """
        if not isinstance(state_dict, dict):
            if strict:
                raise KeyError("state_dict is not a dict")
            return

        progress = getattr(self, "_progress_reminder", None)
        if progress is not None:
            progress.reset()

        # --- 2.0 format (preferred) ---
        raw = state_dict.get("state")
        if raw is not None:
            try:
                self.state = AgentState.model_validate(raw)
            except Exception as exc:
                raise KeyError(
                    f"Could not load AgentState from snapshot: {exc}",
                ) from exc
            # Rehydrate the scroll manager's bookkeeping so the restored window
            # is recognized as already durable (no re-append on resume).
            cm = getattr(self, "_context_manager", None)
            scroll = state_dict.get("scroll")
            if (
                cm is not None
                and scroll is not None
                and hasattr(cm, "load_state")
            ):
                cm.load_state(scroll)
            if progress is not None:
                progress.load_state(
                    state_dict.get("progress_reminder"),
                    session_id=get_current_session_id() or "default",
                    reply_id=self.state.reply_id,
                )
            return

        # --- 1.x legacy format: migrate ``memory`` → ``state`` ---
        memory_raw = state_dict.get("memory")
        if isinstance(memory_raw, dict):
            from qwenpaw.app.chats.utils import parse_legacy_memory_state

            msgs, summary = parse_legacy_memory_state(memory_raw)
            self.state = AgentState()
            self.state.context.extend(msgs)
            self.state.summary = summary
            logger.info(
                "Migrated 1.x session: %d messages + summary(%d chars)",
                len(msgs),
                len(self.state.summary),
            )
            return

        if strict:
            raise KeyError(
                "state_dict has neither 'state' nor 'memory' key",
            )

    async def close(self) -> None:
        """Shut down governor, release the history store, and clean up expired
        tool-result files."""
        gov = getattr(self, "_governor", None)
        progress = getattr(self, "_progress_reminder", None)
        if progress is not None:
            try:
                progress.close()
            except Exception:
                logger.debug("Progress reminder close failed", exc_info=True)
        failure_gate = getattr(self, "_failure_loop_gate", None)
        if failure_gate is not None:
            try:
                failure_gate.close()
            except Exception:
                logger.debug("Failure review close failed", exc_info=True)
        if gov is not None:
            try:
                gov.stop()
            except Exception:
                logger.debug("governor stop failed", exc_info=True)

        # Scroll history: apply the retention window (if any) while the
        # connection is still open, then release it (db + -wal + -shm fds —
        # otherwise they accumulate across requests on a long-lived server).
        cm = getattr(self, "_context_manager", None)
        if cm is not None:
            if hasattr(cm, "purge_old"):
                try:
                    lcc = self._agent_config.running.light_context_config
                    cm.purge_old(lcc.scroll_config.history_retention_days)
                except Exception:
                    logger.debug(
                        "history retention purge failed",
                        exc_info=True,
                    )
            if hasattr(cm, "close"):
                try:
                    cm.close()
                except Exception:
                    logger.debug(
                        "context manager close failed",
                        exc_info=True,
                    )

        offloader = getattr(self, "offloader", None)
        if offloader is not None and hasattr(
            offloader,
            "cleanup_expired",
        ):
            try:
                lcc = self._agent_config.running.light_context_config
                trc = lcc.tool_result_pruning_config
                offloader.cleanup_expired(
                    retention_days=trc.offload_retention_days,
                )
            except Exception:
                logger.debug("offloader cleanup failed", exc_info=True)

    def _register_skills(
        self,
        toolkit: Toolkit,
        effective_skills: list[str],
    ) -> None:
        """Load and register skills from workspace directory.

        Skills are stored in ``toolkit._qp_skills`` (a dict) for downstream
        consumption (e.g. ``/skill_name`` slash commands in the runner).
        """
        if not hasattr(toolkit, "_qp_skills"):
            toolkit._qp_skills = {}  # pylint: disable=protected-access
        workspace_dir = self._workspace_dir or WORKING_DIR
        working_skills_dir = get_workspace_skills_dir(Path(workspace_dir))

        for skill_name in effective_skills:
            skill_dir = working_skills_dir / skill_name
            if skill_dir.exists():
                try:
                    # pylint: disable=protected-access
                    toolkit._qp_skills[skill_name] = {
                        "dir": str(skill_dir),
                    }
                    logger.debug("Registered skill: %s", skill_name)
                except Exception as e:
                    logger.error(
                        "Failed to register skill '%s': %s",
                        skill_name,
                        e,
                    )

    # ------------------------------------------------------------------
    # Media-block fallback: strip unsupported media blocks (image, audio,
    # video, file) from memory and retry when the model rejects them.
    # Unlike ``model_factory._fixup_media_list`` (which converts file
    # blocks to text placeholders so the user-facing message history
    # stays readable), this fallback strips them entirely — its purpose
    # is to make a previously-rejected request retryable, so leaving
    # residue would defeat the point.
    # ------------------------------------------------------------------

    _MEDIA_BLOCK_TYPES = {"image", "audio", "video", "file"}
    _MEDIA_MIME_PREFIXES = ("image/", "audio/", "video/")

    def _get_model_key(self) -> str | None:
        """Return the capability-cache key for the active model."""
        model = getattr(self, "model", None)
        return getattr(model, "model_key", None)

    def _model_rejects_media(self) -> bool:
        """Check the capability cache for a learned ``rejects_media`` flag."""
        key = self._get_model_key()
        if key is None:
            return False
        return get_capability_cache().get(key, "rejects_media", False)

    def _proactive_strip_media_blocks(self) -> int:
        """Proactively strip media blocks from memory before model call.

        Only called when the active model does not support multimodal.
        Returns the number of blocks stripped.
        """
        return self._strip_media_blocks_from_memory()

    def _uses_request_time_media_normalization(self) -> bool:
        """Return True when request-time normalization can handle media."""
        return getattr(self, "formatter", None) is not None

    def _set_formatter_media_strip(self, enabled: bool) -> None:
        """Toggle request-time media stripping on the active formatter."""
        formatter = getattr(self, "formatter", None)
        if formatter is None:
            return
        setattr(formatter, "_qwenpaw_force_strip_media", enabled)

    async def _call_model(self, messages, tools, tool_choice=None):
        steering = getattr(self, "_normal_steering", None)
        if steering is None:
            return await self._call_model_timed(messages, tools, tool_choice)
        from ..runtime.normal_preemption import NormalModelCallMiddleware

        previous = self._model_call_middlewares
        self._model_call_middlewares = [*previous, NormalModelCallMiddleware(steering)]
        try:
            return await self._call_model_timed(messages, tools, tool_choice)
        finally:
            self._model_call_middlewares = previous

    async def _call_model_timed(self, messages, tools, tool_choice=None):
        """Time the main request separately from the failure-loop review."""
        from ..runtime.output_attribution import capture_model_input

        capture_model_input(messages)
        if not timing_enabled():
            return await super()._call_model(messages, tools, tool_choice)
        started = time.perf_counter()
        context = (get_current_session_id() or "default", self.state.reply_id, self.state.cur_iter)
        logger.info(
            "MainModel timing stage=request_start session=%s reply=%s iteration=%s model=%s messages=%d tools=%d",
            *context, getattr(self.model, "model", "unknown"), len(messages), len(tools),
        )
        try:
            response = await super()._call_model(messages, tools, tool_choice)
        except BaseException as exc:
            logger.info(
                "MainModel timing stage=request_error session=%s reply=%s iteration=%s error_type=%s elapsed_ms=%.1f",
                *context, type(exc).__name__, (time.perf_counter() - started) * 1000,
            )
            raise
        logger.info(
            "MainModel timing stage=request_return session=%s reply=%s iteration=%s response_type=%s elapsed_ms=%.1f",
            *context, type(response).__name__, (time.perf_counter() - started) * 1000,
        )
        return response

    async def _reasoning_impl(self, tool_choice=None):
        """Observe SDK events unchanged, without counting stream-open as output."""
        timing = ReasoningTiming(timing_context(self)) if timing_enabled() else None
        status = "interrupted"
        try:
            from ..runtime.output_attribution import OutputFrame, attributed_events

            source = attributed_events(
                super()._reasoning_impl(tool_choice=tool_choice),
                OutputFrame(getattr(self, "_output_response_id", None)),
            )
            async with aclosing(source):
                async for event in source:
                    if timing is not None:
                        timing.observe(event)
                    yield event
            status = "completed"
        except BaseException as exc:
            from ..runtime.normal_preemption import SteerPreempted, SamplingPreempted

            if isinstance(exc, SteerPreempted):
                status = "steered"
                anchor = self.state.context[-1].id if self.state.context else ""
                yield SamplingPreempted(anchor)
                return
            status = type(exc).__name__
            raise
        finally:
            if timing is not None:
                timing.finish(status)

    # pylint: disable=too-many-branches,too-many-statements
    async def _reasoning(
        self,
        tool_choice: Literal["auto", "none", "required"] | None = None,
    ):
        """Forward 2.0 ``_reasoning`` events with proactive media
        stripping, passive bad-request retry, and auto-continue on
        text-only responses."""

        # ── Pre-check: pending gate actions from previous iter ──
        from ..loop.gates.runner import check_pending_gates

        pending_stop = check_pending_gates(self)
        if pending_stop is not None:
            stop_text = pending_stop.reason or "Stopped by loop gate."
            block_id = uuid.uuid4().hex
            yield TextBlockStartEvent(
                reply_id=self.state.reply_id,
                block_id=block_id,
            )
            yield TextBlockDeltaEvent(
                reply_id=self.state.reply_id,
                block_id=block_id,
                delta=stop_text,
            )
            yield TextBlockEndEvent(
                reply_id=self.state.reply_id,
                block_id=block_id,
            )
            yield Msg(
                name=self.name,
                role="assistant",
                content=[
                    TextBlock(type="text", text=stop_text),
                ],
            )
            return

        steering = getattr(self, "_normal_steering", None)
        from ..runtime.normal_preemption import SamplingPreempted
        if steering is not None:
            await steering.prepare(self)
            steering.assert_preserved(self.state.context)
        await self._inject_failure_recovery()
        progress_middleware = self._progress_reminder_middleware
        if progress_middleware is not None:
            try:
                await progress_middleware.prepare(self)
            except Exception:
                logger.warning(
                    "Progress reminder preparation failed", exc_info=True,
                )

        # ── Proactive media stripping ──
        from .model_factory import _supports_multimodal_for_current_model

        should_strip = (
            not _supports_multimodal_for_current_model()
            or self._model_rejects_media()
        )
        if should_strip:
            if self._uses_request_time_media_normalization():
                self._set_formatter_media_strip(True)
            else:
                n = self._proactive_strip_media_blocks()
                if n > 0:
                    logger.warning(
                        "Proactively stripped %d media block(s) before "
                        "_reasoning (model lacks multimodal support).",
                        n,
                    )

        # ── Model call with passive retry on media error ──
        final_msg: Msg | None = None
        try:
            async for evt in super()._reasoning(tool_choice=tool_choice):
                if isinstance(evt, SamplingPreempted):
                    yield evt
                    return
                if isinstance(evt, Msg):
                    final_msg = evt
                else:
                    yield evt
        except Exception as e:
            if not self._is_bad_request_or_media_error(e):
                raise

            model_key = self._get_model_key()
            if model_key:
                get_capability_cache().learn(
                    model_key,
                    "rejects_media",
                    True,
                )
            logger.warning(
                "_reasoning failed with media error (%s); "
                "stripping media and retrying.",
                e,
            )
            if self._uses_request_time_media_normalization():
                self._set_formatter_media_strip(True)
            else:
                self._strip_media_blocks_from_memory()

            try:
                async for evt in super()._reasoning(
                    tool_choice=tool_choice,
                ):
                    if isinstance(evt, SamplingPreempted):
                        yield evt
                        return
                    if isinstance(evt, Msg):
                        final_msg = evt
                    else:
                        yield evt
            finally:
                if self._uses_request_time_media_normalization():
                    self._set_formatter_media_strip(False)
        else:
            if should_strip and self._uses_request_time_media_normalization():
                self._set_formatter_media_strip(False)

        # ── Stop Hook: run every iteration ──
        stop_result = await self._run_stop_handlers(final_msg)
        from ..runtime.final_files import observe_stop

        observe_stop(stop_result)

        if final_msg is None:
            from ..loop.gates.runner import apply_stop_result

            apply_stop_result(
                self,
                stop_result,
                is_tool_call=True,
            )
            return

        # Model produced text (wants to stop).
        if stop_result.action == StopAction.INTERRUPT_AND_CONTINUE:
            logger.info(
                "Stop handler BLOCKED exit: %s",
                stop_result.reason,
            )
            continuation = (
                stop_result.continuation_message
                or "Continue working on the task."
            )
            self.state.context.append(
                Msg(
                    name="user",
                    role="user",
                    content=[
                        TextBlock(
                            type="text",
                            text=continuation,
                        ),
                    ],
                    metadata={
                        QWENPAW_MESSAGE_TAG_KEY: LOOP_CONTINUATION_MESSAGE_TAG,
                    },
                ),
            )
            return  # outer loop continues

        if steering is not None:
            # An update arriving during the final inference still belongs to
            # this reply. Insert the actual user input to make the SDK loop
            # continue, without adding a synthetic continuation prompt.
            if (
                not stop_result.reason and steering.pending
                and self.state.cur_iter + 1 < self.react_config.max_iters
            ):
                if await steering.inject_pending(self):
                    return
            steering.close()
        yield final_msg

    @staticmethod
    def _is_content_safety_error(exc: Exception) -> bool:
        """Return True for provider-side content safety rejections."""
        error_str = str(exc).lower()
        safety_markers = (
            "new_sensitive",
            "image is sensitive",
            "content policy",
            "content_policy",
            "moderation",
            "content_safety",
            "safety_filter",
            "(1026)",
        )
        return any(marker in error_str for marker in safety_markers)

    @staticmethod
    def _is_bad_request_or_media_error(exc: Exception) -> bool:
        """Return True only for errors that genuinely look media-related.

        A bare 400 is no longer sufficient — provider gateways return
        400 for many unrelated reasons (request too large, malformed
        block fields, exceeded context length) and treating them all as
        "media rejected" poisons the capability cache, causing
        subsequent requests to silently drop user-uploaded images.
        """
        error_str = str(exc).lower()

        # Veto: content safety/moderation rejections are about a
        # particular input, not about whether the model supports media.
        if QwenPawAgent._is_content_safety_error(exc):
            return False

        # Veto: errors clearly about request size / context length are
        # never about media support — stripping media may incidentally
        # make the next request fit, but it's a coincidence, not a
        # learned capability.
        size_signals = (
            "too large",
            "toolarge",
            "max bytes",
            "request body",
            "context length",
            "context_length",
            "maximum context",
            "max_tokens",
        )
        if any(sig in error_str for sig in size_signals):
            return False

        # Match only when the error message itself names a media modality.
        media_keywords = (
            "image",
            "audio",
            "video",
            "vision",
            "multimodal",
            "image_url",
        )
        return any(kw in error_str for kw in media_keywords)

    def _is_media_block(self, block: Any) -> bool:
        """Return True if *block* carries image/audio/video data."""
        if isinstance(block, dict):
            return block.get("type") in self._MEDIA_BLOCK_TYPES
        btype = getattr(block, "type", None)
        if btype in self._MEDIA_BLOCK_TYPES:
            return True
        if btype == "data":
            source = getattr(block, "source", None)
            mt = getattr(source, "media_type", "") or ""
            return mt.startswith(self._MEDIA_MIME_PREFIXES)
        return False

    # ------------------------------------------------------------------
    # Tool call enhancement: hint injection + hook registration
    # ------------------------------------------------------------------

    def _get_tool_coordinator(self) -> Any:
        """Return the ToolCoordinator from request_context, or None."""
        return (self._request_context or {}).get("tool_coordinator")

    async def _inject_pending_hints(self) -> None:
        """Pop background-tool hints and append them to agent context."""
        mgr = self._get_tool_coordinator()
        if mgr is None:
            return
        session_id = (self._request_context or {}).get("session_id", "")
        if not session_id:
            return
        hints = await mgr.pop_pending_hints(session_id)
        for hint in hints:
            self.state.context.append(hint)

    async def _reply(self, **kwargs: Any) -> Any:
        """Forward events unchanged and observe current-turn tool results."""
        await self._inject_pending_hints()
        inputs = kwargs.get("inputs")
        self._failure_task_text = (
            inputs.get_text_content() or ""
            if isinstance(inputs, Msg) and inputs.role == "user" else ""
        )
        gate = self._failure_loop_gate
        if gate is not None and self.state.reply_id and isinstance(
            kwargs.get("inputs"),
            (UserConfirmResultEvent, ExternalExecutionResultEvent),
        ):
            # HITL/external-execution resumes may use a freshly built agent.
            # Remember call inputs only; never replay stored tool results.
            try:
                gate.begin_turn(self.state.reply_id)
                for msg in self.state.context:
                    if msg.id == self.state.reply_id and isinstance(
                        msg.content, list,
                    ):
                        for block in msg.content:
                            if isinstance(block, ToolCallBlock) and block.state in {
                                "asking", "submitted",
                            }:
                                gate.remember_call(
                                    self.state.reply_id, block.id,
                                    block.name, block.input,
                                    iteration=self.state.cur_iter,
                                )
            except Exception:
                logger.warning("Failure recovery resume failed", exc_info=True)
        progress = self._progress_reminder
        if progress is not None and self.state.reply_id and isinstance(
            inputs, (UserConfirmResultEvent, ExternalExecutionResultEvent),
        ):
            try:
                progress.begin_segment(
                    get_current_session_id() or "default", self.state.reply_id,
                )
                for msg in self.state.context:
                    if msg.id == self.state.reply_id and isinstance(
                        msg.content, list,
                    ):
                        for block in msg.content:
                            if isinstance(block, ToolCallBlock) and block.state in {
                                "asking", "submitted",
                            }:
                                progress.remember_call(
                                    self.state.reply_id, block.id,
                                    block.name, block.input,
                                    iteration=self.state.cur_iter,
                                )
            except Exception:
                logger.warning("Progress reminder resume failed", exc_info=True)
        try:
            async for evt in super()._reply(**kwargs):
                self._observe_failure_event(evt)
                self._observe_progress_event(evt)
                yield evt
        finally:
            timing = getattr(self, "_act_timing", None)
            if timing is not None:
                timing.finish()
                self._act_timing = None
            if progress is not None:
                try:
                    progress.pause()
                except Exception:
                    logger.warning(
                        "Progress reminder pause failed", exc_info=True,
                    )

    async def _batch_tool_calls(self):
        batches = await super()._batch_tool_calls()
        if timing_enabled():
            previous = getattr(self, "_act_timing", None)
            if previous is not None:
                previous.finish()
            calls = [call for batch in batches for call in batch.tool_calls]
            self._act_timing = (
                ActTiming(timing_context(self), calls) if calls else None
            )
        return batches

    async def _execute_tool_call(self, tool_call: ToolCallBlock):
        steering = getattr(self, "_normal_steering", None)
        if steering is not None and steering.stale_call(tool_call.id):
            from agentscope.message import ToolResultState

            async for evt in self._handle_error_tool_call(
                tool_call, "未执行：本次调用已被新的用户引导替代。",
                state=ToolResultState.DENIED,
            ):
                evt.metadata["steer_superseded"] = True
                yield evt
            return
        gate = self._failure_loop_gate
        if gate is not None:
            try:
                gate.remember_call(
                    self.state.reply_id, tool_call.id,
                    tool_call.name, tool_call.input,
                    iteration=self.state.cur_iter,
                )
            except Exception:
                logger.warning("Failure recovery input failed", exc_info=True)
        progress = self._progress_reminder
        if progress is not None:
            try:
                progress.remember_call(
                    self.state.reply_id, tool_call.id, tool_call.name,
                    tool_call.input, iteration=self.state.cur_iter,
                )
            except Exception:
                logger.warning("Progress reminder input failed", exc_info=True)
        timing = getattr(self, "_act_timing", None)
        if timing is None:
            async for evt in self._dispatch_tool_events(tool_call):
                yield evt
            return
        status = "interrupted"
        try:
            async with aclosing(self._dispatch_tool_events(tool_call)) as source:
                async for evt in source:
                    if isinstance(evt, RequireUserConfirmEvent):
                        status = "waiting_for_confirmation"
                        timing.statuses.append(status)
                    elif isinstance(evt, RequireExternalExecutionEvent):
                        status = "waiting_external_execution"
                        timing.statuses.append(status)
                    elif isinstance(evt, ToolResultEndEvent):
                        state = getattr(evt.state, "value", evt.state)
                        offloaded = (getattr(evt, "metadata", None) or {}).get("offloaded")
                        status = "offloaded" if offloaded or state == "running" else state
                    yield evt
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except GeneratorExit:
            raise
        except BaseException:
            status = "error"
            raise
        finally:
            timing.complete_call(tool_call.id, status)

    async def _dispatch_tool_events(self, tool_call):
        """Final admission also covers approval waits and external dispatch."""
        steering = getattr(self, "_normal_steering", None)
        if steering is None:
            async for event in super()._execute_tool_call(tool_call):
                yield event
            return
        async with aclosing(super()._execute_tool_call(tool_call)) as source:
            async for event in source:
                if steering is not None and isinstance(event, (RequireUserConfirmEvent, RequireExternalExecutionEvent)):
                    if steering.stale_call(tool_call.id):
                        from agentscope.message import ToolResultState

                        async for skipped in self._handle_error_tool_call(
                            tool_call, "未执行：本次调用已被新的用户引导替代。", state=ToolResultState.DENIED,
                        ):
                            skipped.metadata["steer_superseded"] = True
                            yield skipped
                        return
                    if isinstance(event, RequireExternalExecutionEvent):
                        steering.admit_call(tool_call.id)
                yield event

    async def _acting_impl(self, tool_call: ToolCallBlock):
        """Time actual toolkit execution, including work offloaded by middleware."""
        steering = getattr(self, "_normal_steering", None)
        if steering is not None and not steering.admit_call(tool_call.id):
            from ..runtime.normal_preemption import superseded_response

            yield superseded_response(tool_call.id)
            return
        if not timing_enabled():
            async for item in super()._acting_impl(tool_call):
                yield item
            return
        timing = ToolTiming(timing_context(self), tool_call)
        try:
            async with aclosing(super()._acting_impl(tool_call)) as source:
                async for item in source:
                    state = getattr(getattr(item, "state", None), "value", None)
                    state = state or getattr(item, "state", None)
                    if (
                        isinstance(item, ToolResponse)
                        or state in {"error", "interrupted"}
                    ):
                        # A coordinator may stop consuming at the terminal item.
                        timing.finish(state or "completed")
                    yield item
            timing.finish("completed")
        except asyncio.CancelledError:
            timing.finish("cancelled")
            raise
        except GeneratorExit:
            raise
        except BaseException:
            timing.finish("error")
            raise
        finally:
            timing.finish("interrupted")

    def _observe_progress_event(self, evt: Any) -> None:
        if (getattr(evt, "metadata", None) or {}).get("steer_superseded"):
            return
        progress = self._progress_reminder
        if progress is None:
            return
        try:
            # Context identity is authoritative; an old reply's late events
            # cannot switch the controller back to an earlier task.
            if isinstance(evt, ReplyStartEvent):
                if evt.reply_id == self.state.reply_id:
                    progress.begin_segment(
                        get_current_session_id() or "default", evt.reply_id,
                    )
            elif getattr(evt, "reply_id", None) == self.state.reply_id:
                if isinstance(evt, ToolResultTextDeltaEvent):
                    progress.append_text(
                        evt.reply_id, evt.tool_call_id, evt.delta,
                    )
                elif isinstance(evt, ToolResultDataDeltaEvent):
                    progress.note_data(evt.reply_id, evt.tool_call_id)
                elif isinstance(evt, ToolResultEndEvent):
                    progress.complete_call(
                        evt.reply_id, evt.tool_call_id, evt.state, evt.metadata,
                    )
        except Exception:
            logger.warning("Progress reminder observation failed", exc_info=True)

    def _observe_failure_event(self, evt: Any) -> None:
        if (getattr(evt, "metadata", None) or {}).get("steer_superseded"):
            return
        gate = self._failure_loop_gate
        if gate is None:
            return
        try:
            if isinstance(evt, ReplyStartEvent):
                gate.begin_turn(evt.reply_id, getattr(self, "_failure_task_text", ""))
            elif isinstance(evt, ToolResultTextDeltaEvent):
                gate.append_text(evt.reply_id, evt.tool_call_id, evt.delta)
            elif isinstance(evt, ToolResultDataDeltaEvent):
                gate.note_data(evt.reply_id, evt.tool_call_id, {
                    "media_type": evt.media_type, "url": evt.url, "data": evt.data,
                })
            elif isinstance(evt, ToolResultEndEvent):
                gate.complete_call(
                    evt.reply_id, evt.tool_call_id, evt.state, evt.metadata,
                )
        except Exception:
            # Advice must never interrupt tool execution or event delivery.
            logger.warning("Failure recovery observation failed", exc_info=True)

    async def _inject_failure_recovery(self) -> None:
        middleware = self._failure_recovery_middleware
        context = (get_current_session_id() or "default", self.state.reply_id, self.state.cur_iter)
        if middleware is None:
            logger.info(
                "FailureLoopGate skipped: session=%s reply=%s iteration=%s reason=not_active",
                *context,
            )
            return
        started = time.perf_counter()
        status = "interrupted"
        logger.info(
            "FailureLoopGate timing stage=check_start session=%s reply=%s iteration=%s",
            *context,
        )
        try:
            await middleware.prepare(self)
            status = "completed"
        except Exception:
            status = "error"
            logger.warning("Failure recovery hint failed", exc_info=True)
        finally:
            logger.info(
                "FailureLoopGate timing stage=check_end session=%s reply=%s iteration=%s status=%s elapsed_ms=%.1f",
                *context, status, (time.perf_counter() - started) * 1000,
            )

    def _register_tool_call_hooks(self) -> None:
        """Register per-tool default timeouts on the ToolCoordinator."""
        mgr = self._get_tool_coordinator()
        if mgr is None:
            return

        mgr.hooks.register(
            "execute_shell_command",
            default_timeout_secs=1200.0,
        )
        mgr.hooks.register("chat_with_agent", default_timeout_secs=300.0)
        mgr.hooks.register("check_agent_task", default_timeout_secs=30.0)
        mgr.hooks.register("grep_search", default_timeout_secs=30.0)
        mgr.hooks.register("glob_search", default_timeout_secs=15.0)
        mgr.hooks.register("ast_search", default_timeout_secs=35.0)
        mgr.hooks.register(
            "desktop_screenshot",
            default_timeout_secs=30.0,
        )
        for name in (
            "lsp_definition",
            "lsp_references",
            "lsp_rename",
            "lsp_hover",
            "lsp_diagnostics",
        ):
            mgr.hooks.register(name, default_timeout_secs=20.0)
        mgr.hooks.register(
            "browser_use",
            max_internal_timeout_secs=3600.0,
        )

        agent_id = (self._request_context or {}).get(
            "agent_id",
            self.name,
        )
        mgr.clear_agent_tool_timeouts(agent_id)
        builtin_tools = (
            getattr(
                getattr(self._agent_config, "tools", None),
                "builtin_tools",
                None,
            )
            or {}
        )
        for tool_name, cfg in builtin_tools.items():
            t = getattr(cfg, "timeout_seconds", None)
            if t is not None and t > 0:
                mgr.set_agent_tool_timeout(
                    agent_id,
                    tool_name,
                    float(t),
                )

    # ------------------------------------------------------------------
    # Stop Hook: loop continuation support
    # ------------------------------------------------------------------

    def _get_stop_handlers(self) -> list:
        """Retrieve stop handlers for this agent."""
        from ..app.agent_context import (
            get_current_agent_id,
        )
        from ..plugins.registry import PluginRegistry

        agent_id = get_current_agent_id()
        handlers = PluginRegistry.get_stop_handlers(
            agent_id=agent_id,
        )
        logger.debug(
            "stop_handlers: agent=%s count=%d",
            agent_id,
            len(handlers),
        )
        return handlers

    async def _run_stop_handlers(
        self,
        final_msg: Optional[Msg],
    ) -> StopHandlerResult:
        """Run registered stop handlers every iteration."""
        from ..loop.gates.runner import run_stop_handlers

        handlers = self._get_stop_handlers()
        return await run_stop_handlers(
            handlers,
            agent=self,
            final_msg=final_msg,
            iteration=self.state.cur_iter,
        )

    # pylint: disable=too-many-nested-blocks
    def _strip_media_blocks_from_memory(self) -> int:
        """Remove media blocks (image/audio/video/DataBlock) from all messages.

        Also strips media blocks nested inside ToolResultBlock outputs.
        Inserts placeholder text when stripping leaves content empty to
        avoid malformed API requests.

        Returns:
            Total number of media blocks removed.
        """
        total_stripped = 0

        for msg in self.state.context:
            if not isinstance(msg.content, list):
                continue

            new_content = []
            stripped_this_message = 0
            for block in msg.content:
                if self._is_media_block(block):
                    total_stripped += 1
                    stripped_this_message += 1
                    continue

                btype = (
                    block.get("type")
                    if isinstance(block, dict)
                    else getattr(block, "type", None)
                )
                if btype == "tool_result":
                    output = (
                        block.get("output")
                        if isinstance(block, dict)
                        else getattr(block, "output", None)
                    )
                    if isinstance(output, list):
                        filtered = [
                            item
                            for item in output
                            if not self._is_media_block(item)
                        ]
                        stripped_count = len(output) - len(filtered)
                        total_stripped += stripped_count
                        stripped_this_message += stripped_count
                        if stripped_count > 0:
                            if isinstance(block, dict):
                                block["output"] = (
                                    filtered or MEDIA_UNSUPPORTED_PLACEHOLDER
                                )
                            else:
                                block.output = (
                                    filtered or MEDIA_UNSUPPORTED_PLACEHOLDER
                                )

                new_content.append(block)

            if not new_content and stripped_this_message > 0:
                new_content.append(
                    TextBlock(type="text", text=MEDIA_UNSUPPORTED_PLACEHOLDER),
                )

            msg.content = new_content

        return total_stripped
