# -*- coding: utf-8 -*-
"""Per-request agent assembly.

:class:`AgentBuilder` fully constructs a :class:`QwenPawAgent` for each
request.  It obtains tools from the per-workspace
:class:`QwenPawLocalWorkspace` (via ``list_tools``), the system prompt
from :class:`PromptManager`, and the model from the factory, then
injects all dependencies into the agent constructor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..agents.acp.meta import ACP_CODING_PROJECT_META_KEY
from ..config.context import set_current_request_context
from ..config.mount_policy import normalize_rx_mount_overlaps
from ..config.shell_policy import force_direct_subprocess_sync_enabled

_logger = logging.getLogger(__name__)


@dataclass
class AgentBuildOverride:
    """Request-scoped build override for virtual runtime roles."""

    model: Any | None = None
    formatter: Any | None = None
    system_prompt: str | None = None
    disable_skills: bool = False
    disabled_tool_names: set[str] = field(default_factory=set)
    max_iters: int | None = None
    virtual_role: str = ""


class AgentBuilder:
    """Compose an agent for each request.

    Tools are obtained from ``ctx.workspace.local_workspace.list_tools()``.
    ``app_services`` provides cross-workspace shared services.
    """

    def __init__(
        self,
        app_services: Any | None = None,
    ) -> None:
        self._app_services = app_services

    # ------------------------------------------------------------------ public
    async def build_toolkit(
        self,
        agent_config: Any,
        *,
        agent_id: str | None = None,
        request_context: dict[str, str] | None = None,
        active_modes: Iterable[str] | None = None,
        effective_skills: Iterable[str] | None = None,
        enabled_features: Iterable[str] | None = None,
        extra_tools: Iterable[Any] | None = None,
        memory_tools: Iterable[Any] | None = None,
        governor: Any = None,
        ctx: Any = None,
        workspace_dir: str | None = None,
        extra_skill_dirs: Iterable[str] | None = None,
        allow_pool_skills: bool = True,
    ) -> Any:
        """Build a populated ``Toolkit`` for one agent invocation.

        Tools are obtained from the per-workspace
        :class:`QwenPawLocalWorkspace` via ``list_tools()``.
        ``extra_tools`` and ``memory_tools`` are appended after the
        workspace tools.

        """
        from agentscope.tool import Toolkit
        from .final_files import FEATURE, current

        features = set(enabled_features or ())
        if current() is not None:
            features.add(FEATURE)

        local_ws = self._get_local_workspace(ctx) if ctx else None
        if local_ws is not None:
            tools: list[Any] = await local_ws.list_tools(
                agent_config=agent_config,
                agent_id=agent_id,
                request_context=request_context,
                active_modes=active_modes or (),
                active_skills=effective_skills or (),
                enabled_features=features,
            )
        else:
            tools = []

        if extra_tools:
            tools.extend(extra_tools)

        if memory_tools:
            from ..governance import PolicyGuardedTool

            for fn in memory_tools:
                tools.append(
                    PolicyGuardedTool(
                        fn,
                        governor=governor,
                        request_context=request_context,
                    ),
                )

        skill_dirs = self._resolve_skill_loader_dirs(
            effective_skills,
            workspace_dir,
        )
        # Session-scoped extra skills (request_context.skills): plain
        # directories, same rendering path as the agent's own skills.
        if extra_skill_dirs:
            known = set(skill_dirs)
            for skill_dir in extra_skill_dirs:
                if skill_dir and skill_dir not in known:
                    known.add(skill_dir)
                    skill_dirs.append(skill_dir)

        toolkit = Toolkit(tools=tools, skills_or_loaders=skill_dirs)
        from ..agents.skill_system.process_tool import (
            install_process_tool_skill_viewer,
        )

        install_process_tool_skill_viewer(
            toolkit, request_context, allow_pool_skills=allow_pool_skills,
        )
        # AgentScope hides Skill when no skill loaders produce a skill.
        # Keep the same viewer callable for on-demand pool lookup, without
        # adding pool skills to the agent prompt or session state.
        if allow_pool_skills and not await toolkit._get_available_skills():
            toolkit.tool_groups[0].tools.append(
                toolkit.builtin_skill_viewer.tool,
            )
        return toolkit

    @staticmethod
    def _resolve_skill_loader_dirs(
        effective_skills: Iterable[str] | None,
        workspace_dir: str | None,
    ) -> list[str]:
        """Map effective skill names to their SKILL.md-bearing directories."""
        names = list(effective_skills or ())
        if not names:
            return []

        from ..agents.skill_system import get_workspace_skills_dir
        from ..constant import WORKING_DIR

        base = get_workspace_skills_dir(Path(workspace_dir or WORKING_DIR))
        dirs: list[str] = []
        for name in names:
            skill_dir = base / name
            if (skill_dir / "SKILL.md").exists():
                dirs.append(str(skill_dir))
            else:
                _logger.debug(
                    "skill '%s' has no SKILL.md at %s; not injected",
                    name,
                    skill_dir,
                )
        return dirs

    # ----------------------------------------------------------------- build

    async def build(  # pylint: disable=too-many-statements
        self,
        ctx: Any,
        override: AgentBuildOverride | None = None,
    ) -> Any:
        """Construct a fully-wired :class:`QwenPawAgent` for one request.

        Integrates all per-workspace registries: QwenPawLocalWorkspace
        (toolkit), PromptManager (system prompt), model factory, and
        middlewares.  The agent receives all dependencies externally —
        it does not build any of them internally.
        """
        from agentscope.agent import ReActConfig

        from ..agents.react_agent import QwenPawAgent
        from ..agents.skill_system import (
            ensure_skills_initialized,
            resolve_effective_skills,
        )
        from ..config.config import load_agent_config
        from ..constant import WORKING_DIR
        from ..providers.provider_manager import ProviderManager
        from ..utils.bundled_runtime import current_runtimes

        current_runtimes.set(None)

        agent_id = getattr(ctx, "agent_id", None) or "default"
        agent_config = load_agent_config(agent_id)
        request_context = self._build_request_context(ctx)
        set_current_request_context(request_context)
        agent_config = self._apply_request_coding_project(
            agent_config,
            request_context,
        )
        ctx.agent_config = agent_config

        # Validate model availability.
        active = agent_config.active_model
        if not (active and active.provider_id and active.model):
            active = ProviderManager.get_instance().get_active_model()
        if active is None or not active.provider_id or not active.model:
            raise RuntimeError(
                "No active model configured; pick one in the UI",
            )

        workspace_dir = getattr(ctx, "workspace_dir", None)

        # Resolve skills.
        ensure_skills_initialized(workspace_dir or WORKING_DIR)
        channel_name = request_context.get("channel", "console")
        try:
            effective_skills = resolve_effective_skills(
                workspace_dir or WORKING_DIR,
                channel_name,
            )
        except Exception:
            effective_skills = []
        if override is not None and override.disable_skills:
            effective_skills = []

        # Session-scoped extra skills: chat clients pass skill names in
        # ``request_context.skills``; non-own names accumulate per session
        # and persist through the session state file.
        from ..agents.skill_system.session_skills import (
            SESSION_SKILLS_KEY,
            merge_session_skill_names,
            normalize_skill_names,
            resolve_session_skill_dirs,
        )

        incoming_skill_names = normalize_skill_names(
            request_context.get("skills"),
        )
        session_skill_names = merge_session_skill_names(
            persisted=(getattr(ctx, "session_state", None) or {}).get(
                SESSION_SKILLS_KEY,
            ),
            incoming=incoming_skill_names,
            effective_skills=effective_skills,
            workspace_dir=workspace_dir,
        )
        session_skill_dirs: list[str] = []
        missing_session_skills: list[str] = []
        if not (override is not None and override.disable_skills):
            session_skill_dirs, missing_session_skills = (
                resolve_session_skill_dirs(session_skill_names)
            )
        # Only names passed in THIS request trigger the user notice.
        missing_this_turn = [
            name
            for name in incoming_skill_names
            if name in set(missing_session_skills)
        ]

        # Compute active modes.
        active_modes: set[str] = set()
        workspace = getattr(ctx, "workspace", None)
        if workspace is not None:
            plugins = getattr(workspace, "plugins", None)
            if plugins is not None:
                active_modes = plugins.active_mode_names(ctx)

        # Governor (governance policy layer).
        _cm = getattr(agent_config, "coding_mode", None)
        _coding_enabled = bool(_cm and getattr(_cm, "enabled", False))
        _project_dir = (
            _cm.project_dir
            if _cm
            and _coding_enabled
            and getattr(_cm, "project_dir", None)
            else None
        )
        request_context["is_coding_mode"] = bool(_project_dir)
        if "sandbox_rx_mounts" in request_context:
            request_context["sandbox_rx_mounts"] = normalize_rx_mount_overlaps(
                request_context.get("sandbox_mounts") or [],
                request_context.get("sandbox_rx_mounts") or [],
                is_coding_mode=bool(_project_dir),
            )
        if not _project_dir and request_context.get("sandbox_rx_mounts"):
            # Session scratch and skills live below this implicit writable
            # root. Reject conflicts outside _init_governor's fallback handler.
            writable_workspace = Path(workspace_dir or WORKING_DIR).resolve()
            for raw_rx in request_context["sandbox_rx_mounts"]:
                rx = Path(raw_rx).resolve()
                if rx.is_relative_to(writable_workspace) or (
                    writable_workspace.is_relative_to(rx)
                ):
                    raise ValueError(
                        "sandbox_rx_mounts conflicts with writable workspace: "
                        f"{rx} / {writable_workspace}",
                    )
        set_current_request_context(request_context)
        governor = self._init_governor(
            workspace_dir,
            _project_dir,
            request_context,
            allow_request_mounts=True,
        )

        # Inject governor into local_workspace so list_tools() can
        # wrap tools with PolicyGuardedTool.
        local_ws = self._get_local_workspace(ctx) if ctx else None
        if local_ws is not None:
            local_ws.set_governor(governor)

        # Toolkit.
        extra_tools = self._collect_coding_mode_tools(
            agent_config,
            workspace_dir,
            agent_id,
            request_context,
            governor,
        )
        (
            driver_tools,
            driver_prompt_hints,
        ) = await self._collect_driver_tools_and_prompts(
            ctx,
            request_context,
        )
        extra_tools.extend(driver_tools)
        if not hasattr(ctx, "extras") or ctx.extras is None:
            ctx.extras = {}
        ctx.extras["driver_prompt_hints"] = driver_prompt_hints
        ctx.extras["session_skills"] = session_skill_names
        ctx.extras["session_skills_missing"] = missing_this_turn

        # Model + formatter (built before the toolkit so the scroll context
        # strategy, which needs the model for token counting, can wire in).
        if override is not None and override.model is not None:
            model, _formatter = override.model, override.formatter
            self._install_formatter(model, _formatter)
        else:
            model, _formatter = self.build_model(agent_config)

        # Built once and shared: the agent's native offloader, and (when
        # ``offload_dialog`` is on) scroll's optional dialog archive.
        offloader = self._build_offloader(ctx, agent_config)

        # Optional scroll context strategy (None unless strategy="scroll").
        scroll = self._build_scroll_components(
            ctx,
            agent_config,
            model,
            offloader=offloader,
        )
        # Eviction and recall must live or die together. The structured
        # recall_history tool reads history in-process (no sandbox needed),
        # but it is still guard-wrapped — with no governor the guard layer
        # itself is degraded. Keep the conservative gate: if the governor
        # never came up and the operator hasn't opted into unsandboxed
        # recall, degrade to native so the full history stays in-context.
        if scroll is not None and not self._scroll_recall_runnable(
            agent_config,
            governor,
        ):
            _logger.warning(
                "scroll: recall tools cannot run (governor unavailable and "
                "allow_unsandboxed is off) — falling back to native context "
                "management so evicted history stays accessible",
            )
            scroll = None
        if scroll is not None:
            self._append_scroll_recall_tools(
                extra_tools,
                scroll,
                agent_config,
                agent_id,
                request_context,
                governor,
            )

        toolkit = await self.build_toolkit(
            agent_config,
            agent_id=agent_id,
            request_context=request_context,
            active_modes=active_modes,
            effective_skills=effective_skills,
            extra_tools=extra_tools,
            governor=governor,
            ctx=ctx,
            workspace_dir=workspace_dir,
            extra_skill_dirs=session_skill_dirs,
            allow_pool_skills=not (
                override is not None and override.disable_skills
            ),
        )
        if override is not None and override.disabled_tool_names:
            self._filter_toolkit_tools(
                toolkit,
                override.disabled_tool_names,
            )

        # Final tool surface for this request; consumed by the
        # env-context shell-redirect hint so it only mentions tools
        # the model actually receives.
        ctx.extras["active_tool_names"] = {
            getattr(tool, "name", None)
            for group in getattr(toolkit, "tool_groups", []) or []
            for tool in (getattr(group, "tools", None) or [])
            if getattr(tool, "name", None)
        }

        from ..loop.progress_config import ProgressReminderConfig

        progress_config = ProgressReminderConfig()
        ctx.extras["progress_reminder_config"] = progress_config

        # Preparation happens before prompt construction, including overrides.
        # One immutable selection is shared by the prompt and child processes.
        from ..utils.bundled_runtime import render_runtime_instructions

        ctx.extras["bundled_runtimes"] = None
        prepared = await self._prepare_bundled_runtimes(ctx, request_context)
        ctx.extras["runtime_environments"] = {}
        # System prompt.
        if override is not None and override.system_prompt is not None:
            sys_prompt = override.system_prompt
            if prepared is not None:
                import os

                ctx.extras["runtime_environments"] = prepared.info
                shell = (
                    getattr(agent_config.running, "shell_command_executable", "")
                    or os.environ.get("SHELL") or "cmd.exe"
                )
                sys_prompt += "\n" + render_runtime_instructions(prepared.info, shell)
        else:
            import asyncio
            from ..utils.terminal_runtime import detect_runtime_environments

            try:
                kinds = tuple(
                    kind for kind in ("python", "nodejs")
                    if prepared is None or kind not in prepared.info
                )
                if kinds == ("python", "nodejs"):
                    detected = await asyncio.to_thread(detect_runtime_environments)
                elif kinds:
                    detected = await asyncio.to_thread(
                        detect_runtime_environments, kinds=kinds,
                    )
                else:
                    detected = {}
                ctx.extras["runtime_environments"] = detected
            except Exception:
                _logger.warning(
                    "[RuntimeDetect] detection failed; runtime paths omitted",
                    exc_info=True,
                )
            if prepared is not None:
                ctx.extras["runtime_environments"].update(prepared.info)
            sys_prompt = self.build_prompt(ctx, agent_config)
        from .final_files import current
        from .final_files.prompts import append_prompt

        sys_prompt = append_prompt(
            sys_prompt, current(), ctx.extras["active_tool_names"],
        )
        ctx.extras["system_prompt"] = sys_prompt

        middlewares = self._build_middlewares(ctx, agent_config)
        if scroll is not None:
            middlewares.append(scroll.cap_middleware)
            from ..agents.context.budget_middleware import (
                RequestBudgetMiddleware,
            )

            # Innermost model-call middleware: memory and plugin additions
            # must be included in the final ReAct request budget.
            middlewares.append(
                RequestBudgetMiddleware(
                    scroll.context_manager,
                    compression_enabled=(
                        agent_config.running.light_context_config
                        .context_compact_config.enabled
                    ),
                ),
            )

        running_config = agent_config.running

        from ..loop.react_gates import (
            resolve_max_iterations,
        )

        effective_max = (
            override.max_iters
            if override is not None and override.max_iters is not None
            else resolve_max_iterations(running_config)
        )

        from ..loop.progress_reminder import ProgressReminderController

        progress_reminder = ProgressReminderController(progress_config)
        _logger.info(
            "ReminderConfig progress_active=True progress_interval_seconds=%d",
            progress_config.interval_seconds,
        )

        agent = QwenPawAgent(
            name=agent_config.name or "QwenPaw",
            model=model,
            system_prompt=sys_prompt,
            toolkit=toolkit,
            react_config=ReActConfig(max_iters=effective_max),
            middlewares=middlewares,
            agent_config=agent_config,
            workspace_dir=workspace_dir,
            request_context=request_context,
            memory_manager=self._get_memory_manager(ctx),
            offloader=offloader,
            context_config=self._build_context_config(agent_config),
            context_manager=(
                scroll.context_manager if scroll is not None else None
            ),
            effective_skills=effective_skills,
            session_skills=session_skill_names,
            governor=governor,
            progress_reminder=progress_reminder,
        )

        # Register default ReAct gates (StopHandler).
        if workspace is not None:
            from ..loop.react_gates import (
                register_react_gates,
            )

            register_react_gates(workspace, running_config)

        # Load session state if SessionLoadHook populated it.
        if ctx.session_state:
            agent.load_state_dict(ctx.session_state)

        _logger.info(
            "builder: built agent for session=%s agent=%s role=%s"
            " model=%s/%s tools=%d",
            getattr(ctx, "session_id", ""),
            agent_id,
            getattr(override, "virtual_role", "") if override else "",
            active.provider_id,
            active.model,
            len(agent.toolkit.tool_groups[0].tools),
        )
        return agent

    @staticmethod
    async def _prepare_bundled_runtimes(ctx: Any, request_context: dict):
        import asyncio
        from ..config import load_config
        from ..constant import WORKING_DIR
        from ..utils.bundled_runtime import (
            current_runtimes, prepare_request_runtimes, supported_request,
        )

        if not supported_request(request_context):
            return None
        config = load_config().bundled_runtimes.model_copy(deep=True)
        if not config.enabled or not any((config.python, config.nodejs, config.git)):
            return None
        prepared = await asyncio.to_thread(
            prepare_request_runtimes, request_context,
            Path(getattr(ctx, "workspace_dir", None) or WORKING_DIR),
            request_context.get("session_id") or getattr(ctx, "session_id", None),
            config,
        )
        current_runtimes.set(prepared)
        ctx.extras["bundled_runtimes"] = prepared
        return prepared

    def build_prompt(self, ctx: Any, agent_config: Any = None) -> str:
        """Build the system prompt via the per-workspace
        :class:`PromptManager`.
        """
        from types import SimpleNamespace
        from ..constant import WORKING_DIR

        if agent_config is None:
            from ..config.config import load_agent_config

            agent_config = load_agent_config(
                getattr(ctx, "agent_id", "default"),
            )

        workspace_dir = getattr(ctx, "workspace_dir", None) or WORKING_DIR

        heartbeat_enabled = False
        hb = getattr(agent_config, "heartbeat", None)
        if hb is not None:
            heartbeat_enabled = getattr(hb, "enabled", False)

        prompt_ctx = SimpleNamespace(
            workspace_dir=workspace_dir,
            agent_id=getattr(ctx, "agent_id", None),
            extras={
                "language": agent_config.language,
                "heartbeat_enabled": heartbeat_enabled,
                "env_context": self._build_env_context(ctx, agent_config),
                "agent_config": agent_config,
                "driver_prompt_hints": self._get_driver_prompt_hints(ctx),
                "progress_reminder_config": (
                    getattr(ctx, "extras", {}) or {}
                ).get("progress_reminder_config"),
            },
        )

        workspace = getattr(ctx, "workspace", None)
        if workspace is not None:
            plugins = getattr(workspace, "plugins", None)
            pm = getattr(plugins, "prompt_manager", None) if plugins else None
            if pm is not None and len(pm) > 0:
                return pm.build_sync(prompt_ctx)

        from .prompt_contributors import build_default_prompt_manager

        return build_default_prompt_manager().build_sync(prompt_ctx)

    def build_model(self, agent_config: Any) -> tuple[Any, Any]:
        """Create model and formatter using the factory method."""
        from ..agents.model_factory import create_model_and_formatter

        model, formatter = create_model_and_formatter(
            agent_id=agent_config.id,
        )
        self._install_formatter(model, formatter)
        return model, formatter

    @staticmethod
    def _install_formatter(model: Any, formatter: Any) -> None:
        """Install formatter on the innermost wrapped model."""
        if formatter is not None:
            innermost = model
            # pylint: disable=protected-access
            while hasattr(innermost, "_inner"):
                innermost = innermost._inner
            while hasattr(innermost, "_model"):
                innermost = innermost._model
            # pylint: enable=protected-access
            if hasattr(innermost, "formatter"):
                innermost.formatter = formatter

    @staticmethod
    def _filter_toolkit_tools(
        toolkit: Any,
        disabled_tool_names: set[str],
    ) -> None:
        """Remove disabled tools after normal governance wrapping."""
        disabled = {str(name) for name in disabled_tool_names if name}
        if not disabled:
            return
        for group in getattr(toolkit, "tool_groups", []) or []:
            tools = getattr(group, "tools", None)
            if not isinstance(tools, list):
                continue
            group.tools = [
                tool
                for tool in tools
                if (
                    getattr(tool, "name", None)
                    or getattr(tool, "__name__", None)
                    or getattr(getattr(tool, "fn", None), "__name__", None)
                    or getattr(getattr(tool, "_fn", None), "__name__", None)
                )
                not in disabled
            ]

    @staticmethod
    def _init_governor(
        workspace_dir: Any,
        coding_project_dir: Any = None,
        request_context: dict[str, Any] | None = None,
        allow_request_mounts: bool = True,
    ) -> Any:
        """Initialize ResourceGovernor if governance is available.

        Returns the started governor, or ``None`` when governance cannot
        be initialised (missing dependencies, unsupported platform, etc.).
        """
        if not workspace_dir:
            return None
        try:
            from ..governance import ResourceGovernor

            extra_mounts = []
            extra_rx_mounts = []
            extra_executable_roots = []
            if allow_request_mounts:
                extra_mounts = (
                    (request_context or {}).get("sandbox_mounts") or []
                )
                extra_rx_mounts = (
                    (request_context or {}).get("sandbox_rx_mounts") or []
                )
                extra_executable_roots = (
                    (request_context or {}).get(
                        "windows_sandbox_executable_roots",
                    )
                    or []
                )
            governor = ResourceGovernor(
                str(workspace_dir),
                coding_project_dir=(
                    str(coding_project_dir) if coding_project_dir else None
                ),
                extra_mounts=extra_mounts,
                extra_executable_roots=extra_executable_roots,
                extra_rx_mounts=extra_rx_mounts,
            )
            governor.start()
            _logger.info("Governance started: dir=%s", workspace_dir)
            return governor
        except Exception:
            _logger.error(
                "Failed to start governance; tool calls will be "
                "fail-closed (governance layer DISABLED)",
                exc_info=True,
            )
            return None

    @staticmethod
    def _get_local_workspace(ctx: Any) -> Any:
        workspace = getattr(ctx, "workspace", None)
        if workspace is not None:
            return getattr(workspace, "local_workspace", None)
        return None

    @staticmethod
    def _build_request_context(ctx: Any) -> dict[str, Any]:
        request = getattr(ctx, "request", None)
        rc: dict[str, Any] = {
            "session_id": getattr(ctx, "session_id", "") or "",
            "agent_id": getattr(ctx, "agent_id", "") or "",
            "channel": (
                (getattr(request, "channel", None) or "") if request else ""
            ),
            "user_id": (
                (getattr(request, "user_id", None) or "") if request else ""
            ),
            "root_session_id": getattr(ctx, "root_session_id", "") or "",
            "root_agent_id": getattr(ctx, "root_agent_id", "") or "",
        }
        app_services = getattr(ctx, "app_services", None)
        if app_services is not None:
            rc["approval_coordinator"] = getattr(
                app_services,
                "approval_coordinator",
                None,
            )
            rc["tool_coordinator"] = getattr(
                app_services,
                "tool_coordinator",
                None,
            )
        _channel_meta = (
            getattr(request, "channel_meta", None) if request else None
        )
        if isinstance(_channel_meta, dict):
            user_name = _channel_meta.get("user_name")
            if user_name:
                rc["user_name"] = user_name
            rc["channel_meta"] = _channel_meta
        rc["_channel_instance"] = getattr(
            request,
            "channel_instance",
            None,
        )
        _payload_ctx = (
            getattr(request, "request_context", None) if request else None
        )
        if isinstance(_payload_ctx, dict):
            rc.update(_payload_ctx)
        from ..agents.skill_system.process_tool import (
            clear_active_process_tool_specs,
        )

        clear_active_process_tool_specs()
        return rc

    @staticmethod
    def _apply_request_coding_project(
        agent_config: Any,
        request_context: dict[str, Any],
    ) -> Any:
        """Enable Coding Mode for this request when ACP supplies a project."""
        raw_project_dir = request_context.get(ACP_CODING_PROJECT_META_KEY)
        if not isinstance(raw_project_dir, str) or not raw_project_dir.strip():
            return agent_config

        project_dir = Path(raw_project_dir).expanduser().resolve()
        if not project_dir.is_dir():
            _logger.warning(
                "Ignoring non-directory Coding Mode project: %s",
                raw_project_dir,
            )
            return agent_config

        if not hasattr(agent_config, "model_copy"):
            _logger.warning(
                "Ignoring request Coding Mode project for unsupported config "
                "type: %s",
                type(agent_config).__name__,
            )
            return agent_config

        agent_config = agent_config.model_copy(deep=True)
        cm = getattr(agent_config, "coding_mode", None)
        if cm is None:
            from ..config.config import CodingModeConfig

            cm = CodingModeConfig()
            agent_config.coding_mode = cm
        cm.enabled = True
        cm.project_dir = str(project_dir)
        return agent_config

    @staticmethod
    def _build_env_context(ctx: Any, agent_config: Any) -> str:
        import os
        import sys
        from ..config.context import get_current_request_context
        from ..app.chats.utils import build_env_context
        from ..constant import WORKING_DIR

        workspace_dir = getattr(ctx, "workspace_dir", None)
        ws = str(workspace_dir) if workspace_dir else str(WORKING_DIR)

        _cm = getattr(agent_config, "coding_mode", None)
        _project_dir = (
            _cm.project_dir
            if _cm
            and getattr(_cm, "enabled", False)
            and getattr(_cm, "project_dir", None)
            else None
        )
        _configured_shell = getattr(
            getattr(agent_config, "running", None),
            "shell_command_executable",
            None,
        )
        _default_shell = (
            _configured_shell
            or os.environ.get("SHELL")
            or ("cmd.exe" if sys.platform == "win32" else "/bin/sh")
        )
        request_context = get_current_request_context()
        active_names = (getattr(ctx, "extras", None) or {}).get(
            "active_tool_names",
        )
        # Include only the request-scoped values needed for file placement
        # guidance.
        return build_env_context(
            session_id=request_context.get("session_id"),
            working_dir=ws,
            default_shell=_default_shell,
            project_dir=_project_dir,
            sandbox_mounts=request_context.get("sandbox_mounts") or [],
            sandbox_rx_mounts=(
                request_context.get("sandbox_rx_mounts") or []
            ),
            is_coding_mode=bool(request_context.get("is_coding_mode")),
            shell_redirect_entries=(
                AgentBuilder._resolve_shell_redirect_entries(
                    ctx,
                    active_names,
                )
            ),
            shell_command_visible=(
                AgentBuilder._shell_command_tool_visible(active_names)
            ),
            active_tool_names=active_names,
            runtime_environments=(getattr(ctx, "extras", None) or {}).get(
                "runtime_environments",
            ),
        )

    @staticmethod
    def _shell_command_tool_visible(active_names: Any) -> bool:
        """Return whether ``execute_shell_command`` is in the final toolset.

        Gates the Non-Coding atomic-command env-context hint so it is only
        shown when the model can actually see the shell tool. Platform and
        mode gating are applied by the shared shell policy in the renderer.
        """
        names = {str(n) for n in (active_names or ()) if n}
        return "execute_shell_command" in names

    @staticmethod
    def _resolve_shell_redirect_entries(
        ctx: Any,
        active_names: Any,
    ) -> list[tuple[str, str, tuple[str, ...]]]:
        """Resolve Windows shell-redirect hint entries from the toolset.

        Returns ``[]`` unless the platform is Windows and the final
        tool surface includes ``execute_shell_command`` — without a
        shell tool there is nothing to steer the model away from.
        """
        import platform as _platform

        if _platform.system().lower() != "windows":
            return []
        names = {str(n) for n in (active_names or ()) if n}
        if "execute_shell_command" not in names:
            return []
        local_ws = AgentBuilder._get_local_workspace(ctx) if ctx else None
        registry = getattr(local_ws, "tool_registry", None)
        if registry is None:
            return []
        entries: list[tuple[str, str, tuple[str, ...]]] = []
        for name in sorted(names):
            desc = registry.get(name)
            meta = getattr(desc, "metadata", None) or {}
            redirect = meta.get("shell_redirect")
            if not redirect or len(redirect) != 2:
                continue
            purpose, alts = redirect
            alts = tuple(str(a) for a in (alts or ()) if a)
            if not purpose or not alts:
                continue
            entries.append((name, str(purpose), alts))
        return entries

    @staticmethod
    def _collect_coding_mode_tools(
        agent_config: Any,
        workspace_dir: Any,
        agent_id: str,
        request_context: dict[str, Any],
        governor: Any = None,
    ) -> list[Any]:
        from ..modes.coding import collect_coding_tools

        return collect_coding_tools(
            agent_config,
            workspace_dir,
            agent_id=agent_id,
            request_context=request_context,
            governor=governor,
        )

    @staticmethod
    def _get_driver_prompt_hints(ctx: Any) -> list[str]:
        extras = getattr(ctx, "extras", {}) or {}
        hints = extras.get("driver_prompt_hints") or []
        return [str(hint) for hint in hints if hint]

    @staticmethod
    async def _collect_driver_tools_and_prompts(
        ctx: Any,
        request_context: dict[str, Any],
    ) -> tuple[list[Any], list[str]]:
        """Build request-time Driver tools and prompt hints.

        MCP is exposed through Driver capabilities only, so a server cannot
        be exposed twice through separate runtime paths.
        """
        workspace = getattr(ctx, "workspace", None)
        driver_manager = (
            getattr(workspace, "driver_manager", None)
            if workspace is not None
            else None
        )
        from ..drivers.adapters.agentscope_tool import build_driver_agent_tools

        return await build_driver_agent_tools(
            driver_manager,
            request_context,
        )

    @staticmethod
    def _get_memory_manager(ctx: Any) -> Any:
        workspace = getattr(ctx, "workspace", None)
        if workspace is not None:
            return getattr(workspace, "memory_manager", None)
        return None

    @staticmethod
    def _build_context_config(agent_config: Any) -> Any:
        """Map QwenPaw's ``ContextCompactConfig`` to AS ``ContextConfig``."""
        from agentscope.agent import ContextConfig

        try:
            lcc = agent_config.running.light_context_config
            ccc = lcc.context_compact_config
            return ContextConfig(
                trigger_ratio=ccc.compact_threshold_ratio,
                reserve_ratio=ccc.reserve_threshold_ratio,
            )
        except Exception:
            return ContextConfig()

    @staticmethod
    def _build_scroll_components(
        ctx: Any,
        agent_config: Any,
        model: Any,
        offloader: Any = None,
    ) -> Any:
        """Build the scroll context strategy, or None when not selected.

        Returns ``None`` for the native strategy (the default) so nothing
        changes unless ``light_context_config.strategy == "scroll"``. The
        shared ``offloader`` is forwarded so scroll can archive evicted turns
        to ``dialog/*.jsonl`` (``offload_dialog``, on by default).
        """
        workspace = getattr(ctx, "workspace", None)
        workspace_dir = (
            str(getattr(workspace, "workspace_dir", ""))
            if workspace is not None
            else ""
        )
        session_id = getattr(ctx, "session_id", None) or "local"
        agent_id = (
            getattr(agent_config, "id", None)
            or getattr(ctx, "agent_id", None)
            or "default"
        )

        from ..agents.context import build_scroll_components

        # history.db is shared across sessions in this workspace; rows are
        # keyed by session_id (the conversation) and agent_id (which agent
        # wrote them).
        return build_scroll_components(
            agent_config=agent_config,
            workspace_dir=workspace_dir,
            model=model,
            session_id=session_id,
            agent_id=agent_id,
            offloader=offloader,
        )

    @staticmethod
    def _scroll_recall_runnable(agent_config: Any, governor: Any) -> bool:
        """Whether scroll's recall tools can actually execute in this build.

        Two recall paths exist: the structured ``recall_history`` tool
        (in-process bound queries — needs no sandbox, only a working guard
        layer) and the sandboxed ``recall_history_python`` REPL, which fails
        closed unless a ``sandbox_config`` is supplied. That config is
        injected only by the governor (via ``PolicyGuardedTool``); the
        ``GuardedFunctionTool`` fallback used when the governor is absent
        never supplies one. A missing governor means the guard layer itself is
        degraded, so we stay conservative: recall is runnable iff the governor
        is present, or the deployment has opted into unsandboxed recall —
        which requires BOTH the ``QWENPAW_ALLOW_UNSANDBOXED_RECALL`` env var
        and ``scroll_config.allow_unsandboxed`` (see
        ``scroll_unsandboxed_allowed`` — agent.json alone can never bypass the
        sandbox). When neither holds, wiring scroll would evict history that
        nothing can read back, so the caller degrades to native context
        management.
        """
        if governor is not None:
            return True
        try:
            from ..agents.context import scroll_unsandboxed_allowed

            sc = agent_config.running.light_context_config.scroll_config
            return scroll_unsandboxed_allowed(sc)
        except Exception:
            return False

    @staticmethod
    def _scroll_repl_runnable(agent_config: Any, governor: Any) -> bool:
        """Whether the sandboxed ``recall_history_python`` REPL should be
        offered to the model in this build.

        The REPL runs model-authored Python and so needs a sandbox. It is
        worth registering only when one is actually available — meaning the
        governor is present AND its platform probe found a sandbox — or when
        the operator explicitly opted into unsandboxed recall (both the
        ``QWENPAW_ALLOW_UNSANDBOXED_RECALL`` env var and
        ``scroll_config.allow_unsandboxed``, via
        ``scroll_unsandboxed_allowed``).

        When neither holds (e.g. Windows without WSL2), every call would fail
        closed, and the guard layer misreads that ``DENIED`` as a sandbox
        violation and escalates to a recurring approval prompt. So we omit the
        REPL and let the model recall through the structured ``recall_history``
        tool, which needs no sandbox. This is narrower than
        :meth:`_scroll_recall_runnable`, which gates whether scroll is wired at
        all; here scroll is already wired and structured recall is present.
        """
        # Keep structured recall, but hide model-authored Python in host mode.
        if force_direct_subprocess_sync_enabled():
            return False
        if governor is not None and getattr(
            governor,
            "sandbox_available",
            False,
        ):
            return True
        try:
            from ..agents.context import scroll_unsandboxed_allowed

            sc = agent_config.running.light_context_config.scroll_config
            return scroll_unsandboxed_allowed(sc)
        except Exception:
            return False

    def _append_scroll_recall_tools(
        self,
        extra_tools: list,
        scroll: Any,
        agent_config: Any,
        agent_id: str,
        request_context: dict[str, Any],
        governor: Any,
    ) -> None:
        """Register scroll's recall tools onto ``extra_tools``.

        The structured ``recall_history`` tool is ALWAYS registered: its
        expand/search/recall_tool ops are bound read-only queries (internal
        governance type) — no sandbox, no approval, working on every platform.

        The sandboxed ``recall_history_python`` REPL is registered ONLY when
        it can actually run in a sandbox (or unsandboxed recall is explicitly
        opted in). Where no sandbox exists — e.g. Windows without WSL2, or an
        OFF-mode path that skips sandbox compilation — every call would fail
        closed, and the guard layer misreads that ``DENIED`` as a sandbox
        violation and turns it into a recurring approval prompt. Omitting it
        removes that dead-end: the model recalls through the structured tool.
        """
        extra_tools.append(
            self._wrap_tool(
                scroll.recall_tool,
                agent_id,
                request_context,
                governor,
            ),
        )
        if self._scroll_repl_runnable(agent_config, governor):
            extra_tools.append(
                self._wrap_tool(
                    scroll.repl_tool,
                    agent_id,
                    request_context,
                    governor,
                ),
            )
        else:
            _logger.info(
                "scroll: no sandbox available for recall_history_python — "
                "registering only the structured recall_history tool "
                "(no approval prompt, works without a sandbox)",
            )

    @staticmethod
    def _wrap_tool(
        fn: Any,
        agent_id: str,
        request_context: dict[str, Any],
        governor: Any,
    ) -> Any:
        """Wrap a raw tool fn in the repo's standard guard (policy or tool)."""
        if governor is not None:
            from ..governance import PolicyGuardedTool

            return PolicyGuardedTool(
                fn,
                governor=governor,
                request_context=request_context,
            )
        from .tool_guard import GuardedFunctionTool

        return GuardedFunctionTool(
            fn,
            agent_id=agent_id,
            request_context=request_context,
        )

    @staticmethod
    def _build_offloader(ctx: Any, agent_config: Any) -> Any:
        """Build the offloader for context and tool-result persistence."""
        workspace = getattr(ctx, "workspace", None)
        workspace_dir = (
            str(getattr(workspace, "workspace_dir", ""))
            if workspace is not None
            else ""
        )
        if not workspace_dir:
            return None

        import os

        from ..agents.offloader import QwenPawOffloader

        lcc = agent_config.running.light_context_config
        dialog_path = os.path.join(workspace_dir, lcc.dialog_path)
        trc = lcc.tool_result_pruning_config
        tool_results_dir = os.path.join(workspace_dir, trc.tool_results_cache)
        return QwenPawOffloader(
            dialog_path=dialog_path,
            tool_results_dir=tool_results_dir,
        )

    @staticmethod
    def _build_middlewares(  # pylint: disable=too-many-statements
        ctx: Any,
        agent_config: Any,
    ) -> list[Any]:
        """Build middleware list.

        Order (onion model, outermost first):
        1. ToolCoordinatorMiddleware — tool call lifecycle management
        2. ToolResultPruningMiddleware — tiered tool result pruning
        3. Plugin-registered middlewares (sorted by priority)
        """
        mws: list[Any] = []

        app_services = getattr(ctx, "app_services", None)
        if app_services is not None:
            tool_coordinator = getattr(
                app_services,
                "tool_coordinator",
                None,
            )
            if tool_coordinator is not None:
                from ..tool_calls import (
                    ToolCoordinatorMiddleware,
                    ToolResultLimiter,
                )

                result_limiter = None
                try:
                    import os

                    lcc = agent_config.running.light_context_config
                    trc = lcc.tool_result_pruning_config
                    workspace = getattr(ctx, "workspace", None)
                    workspace_dir = (
                        str(getattr(workspace, "workspace_dir", ""))
                        if workspace is not None
                        else ""
                    )
                    tool_results_dir = (
                        os.path.join(workspace_dir, trc.tool_results_cache)
                        if workspace_dir
                        else None
                    )
                    result_limiter = ToolResultLimiter(
                        enabled=trc.enabled,
                        max_text_bytes=trc.execution_layer_max_bytes,
                        cache_dir=tool_results_dir,
                    )
                except Exception:
                    _logger.debug(
                        "ToolResultLimiter not created",
                        exc_info=True,
                    )

                mws.append(
                    ToolCoordinatorMiddleware(
                        coordinator=tool_coordinator,
                        result_limiter=result_limiter,
                    ),
                )

        memory_manager = AgentBuilder._get_memory_manager(ctx)
        if memory_manager is not None:
            try:
                build_middlewares = getattr(
                    memory_manager,
                    "build_middlewares",
                    None,
                )
                if callable(build_middlewares):
                    mws.extend(build_middlewares())
            except Exception:
                _logger.debug("Memory middlewares not created", exc_info=True)

        # Tiered tool-result pruning (ported from LightContextManager)
        try:
            import os

            from ..agents.middlewares import ToolResultPruningMiddleware

            lcc = agent_config.running.light_context_config
            trc = lcc.tool_result_pruning_config

            workspace = getattr(ctx, "workspace", None)
            workspace_dir = (
                str(getattr(workspace, "workspace_dir", ""))
                if workspace is not None
                else ""
            )
            tool_results_dir = (
                os.path.join(workspace_dir, trc.tool_results_cache)
                if workspace_dir
                else ""
            )

            mws.append(
                ToolResultPruningMiddleware(
                    enabled=trc.enabled,
                    recent_n=trc.pruning_recent_n,
                    old_max_bytes=trc.pruning_old_msg_max_bytes,
                    recent_max_bytes=trc.pruning_recent_msg_max_bytes,
                    exempt_file_extensions={
                        e.lower() for e in trc.exempt_file_extensions
                    },
                    exempt_tool_names={
                        n.lower() for n in trc.exempt_tool_names
                    },
                    tool_results_dir=tool_results_dir,
                    agent_id=getattr(agent_config, "id", "default"),
                ),
            )
        except Exception:
            _logger.debug(
                "ToolResultPruningMiddleware not created",
                exc_info=True,
            )

        # Langfuse tool observability
        try:
            from ..observability.langfuse import is_langfuse_enabled

            if is_langfuse_enabled():
                from ..agents.middlewares import LangfuseToolSpanMiddleware

                mws.append(LangfuseToolSpanMiddleware())
        except Exception:
            _logger.debug(
                "LangfuseToolSpanMiddleware not created",
                exc_info=True,
            )

        # Plugin-registered middlewares
        from ..plugins.registry import PluginRegistry

        registry = PluginRegistry()
        for reg in registry.get_middleware_factories():
            try:
                mw = reg.factory(ctx, agent_config)
                if mw is not None:
                    mws.append(mw)
            except Exception:
                _logger.warning(
                    "plugin %s middleware factory failed",
                    reg.plugin_id,
                    exc_info=True,
                )

        return mws


__all__ = ["AgentBuildOverride", "AgentBuilder"]
