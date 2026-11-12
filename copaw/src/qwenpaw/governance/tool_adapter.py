# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""PolicyGuardedTool — Governance policy-checked tool wrapper.

Replaces the GuardedFunctionTool. Each tool call goes through two layers:
1. check_permissions: pre-execution decision
2. __call__: actual execution — handles sandbox violation retry loop
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

from agentscope.message import TextBlock
from agentscope.tool import ToolChunk

from .policy import (
    GovernanceDecision,
    GovernanceAction,
    ToolCallSpec,
)
from .resource_governor import (
    ResourceGovernor,
    is_windows_shell_sandbox_platform,
    strict_shell_sandbox_enabled,
)
from .tool_registry import DEFAULT_REGISTRY
from ..config.context import (
    get_current_request_context,
    set_current_request_context,
)
from ..config.shell_policy import force_direct_subprocess_sync_enabled

logger = logging.getLogger(__name__)

_NO_RETRY_INSTRUCTION = (
    "\n\n\u26a0\ufe0f **System instruction**: this denial is final"
    " for the current request. Do not retry this tool with similar"
    " parameters. Reply to the user explaining why the action could"
    " not be completed and, if appropriate, ask them how they want"
    " to proceed."
)


def _is_execution_level_off() -> bool:
    """Check if execution_level is 'off' (dev mode pass-through).

    Reads directly from policy.yaml (without needing a governor) to
    support the case where governor initialization failed but the user
    explicitly configured execution_level=off for development.
    """
    try:
        from pathlib import Path

        import yaml

        from ..constant import WORKING_DIR

        policy_path = Path(WORKING_DIR) / ".qwenpaw" / "policy.yaml"
        if not policy_path.exists():
            return False
        with open(policy_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return isinstance(data, dict) and data.get("execution_level") == "off"
    except Exception:
        return False


def _resolve_effective_approval_level(
    request_context: dict[str, str] | None,
) -> Optional[Any]:
    """Resolve the effective approval_level for this tool call.

    Priority:
      1. ``request_context["approval_level"]`` — session-level override
         injected by the frontend (localStorage per chat, carried in each
         request). Zero I/O: already in memory.
      2. ``agent.json`` → ``AgentProfileConfig.approval_level`` — the
         agent-level default set via the Web UI 'Tool Execution Security' card.
      3. ``None`` — unresolvable (caller falls back to AUTO).

    Returns the :class:`ToolExecutionLevel` enum, or ``None``.
    """
    if not request_context:
        return None

    from ..security.tool_guard.execution_level import ToolExecutionLevel

    # Session-level override (injected by frontend per request)
    session_raw = request_context.get("approval_level")
    if session_raw:
        level = ToolExecutionLevel.from_config(session_raw)
        if level is not None:
            return level

    # Agent-level default from agent.json
    agent_id = request_context.get("agent_id", "")
    if not agent_id:
        return None
    try:
        from ..config.config import load_agent_config

        profile = load_agent_config(agent_id)
        raw = getattr(profile, "approval_level", None)
        return ToolExecutionLevel.from_config(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# PolicyGuardedTool
# ---------------------------------------------------------------------------


class PolicyGuardedTool:
    """Governance policy-checked tool wrapper.

    Dynamically inherits from FunctionTool, implementing:
    - check_permissions: calls assert_policy() + audit() for policy decision
    - __call__: overrides to handle sandbox execution + violation retry

    .. warning:: Known limitation — dynamic anonymous class

        ``__new__`` creates a fresh class via ``type(...)`` on every call,
        so ``isinstance(tool, PolicyGuardedTool)`` always returns False.
        This is the same pattern used by ``GuardedFunctionTool`` and
        ``DriverCapabilityTool``.  A unified refactor (metaclass or mixin)
        is tracked as a follow-up task.
    """

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        from agentscope.tool import FunctionTool

        if cls is PolicyGuardedTool:
            real_cls = type(
                "PolicyGuardedTool",
                (FunctionTool,),
                {
                    "__init__": _policy_tool_init,
                    "_build_tc_spec": _build_tc_spec,
                    "check_permissions": _policy_tool_check_permissions,
                    "__call__": _policy_tool_call,
                    "__doc__": cls.__doc__,
                },
            )
            return real_cls(*args, **kwargs)
        return object.__new__(cls)


def _policy_tool_init(
    self: Any,
    func: Any,
    *,
    governor: Optional[ResourceGovernor] = None,
    request_context: dict[str, str] | None = None,
    **kwargs: Any,
) -> None:
    from agentscope.tool import FunctionTool

    FunctionTool.__init__(self, func, **kwargs)
    from ..config.shell_policy import shell_tool_description

    self.description = shell_tool_description(
        self.name, self.description, request_context,
    )
    self._qp_governor = governor
    self._qp_request_context = request_context or {}
    self._qp_policy_decision = None  # Pre-evaluation result
    self._qp_sandbox_mode = False  # Whether to execute in sandbox
    self._qp_raw_params = {}  # Set per-call by check_permissions


def _build_tc_spec(self: Any) -> ToolCallSpec:
    """Build ToolCallSpec from instance fields + dynamic target."""
    governor = self._qp_governor
    params = getattr(self, "_qp_raw_params", {})
    tool_name = DEFAULT_REGISTRY.python_to_policy_name(
        getattr(self, "name", "Unknown"),
    )
    request_ctx = getattr(self, "_qp_request_context", {}) or {}
    return ToolCallSpec(
        tool_name=tool_name,
        target=DEFAULT_REGISTRY.extract_target(
            tool_name,
            params,
            workspace_dir=str(governor.workspace_dir) if governor else "",
        ),
        agent_id=request_ctx.get("agent_id", ""),
        session_id=request_ctx.get("session_id", ""),
        raw_params=params,
    )


def _is_forced_direct_shell_tool(tool: Any) -> bool:
    """Identify Bash calls needing host-execution policy evaluation."""
    return (
        DEFAULT_REGISTRY.python_to_policy_name(
            getattr(tool, "name", "Unknown"),
        ) == "Bash"
        and force_direct_subprocess_sync_enabled()
    )


def _prepare_off_mode_sandbox(tool: Any, governor: Any) -> None:
    """Compile+attach a ``sandbox_config`` for fail-closed tools in OFF mode.

    ``approval_level=OFF`` short-circuits the policy pipeline to ALLOW-all,
    which normally also skips the ``SANDBOX_FALLBACK`` branch that compiles a
    ``sandbox_config`` (see :func:`_policy_tool_check_permissions`). Sandbox
    provisioning and user approval are independent concerns: skipping "ask the
    user" must not skip "run it in a sandbox".

    The REPL and strict Windows Bash still receive isolation in normal mode.
    Forced-subprocess mode skips provisioning; the builder hides the REPL.
    Other fail-open Shell routes retain their existing OFF-mode behavior.

    A no-op (leaving ``sandbox_config=None``) when the platform has no sandbox:
    such a REPL is never registered in the first place, or it tolerates
    unsandboxed execution via ``allow_unsandboxed``.
    """
    if force_direct_subprocess_sync_enabled():
        return
    if governor is None or not getattr(governor, "sandbox_available", False):
        return
    policy_name = DEFAULT_REGISTRY.python_to_policy_name(
        getattr(tool, "name", "Unknown"),
    )
    if (
        policy_name == "Bash"
        and is_windows_shell_sandbox_platform()
        and strict_shell_sandbox_enabled()
    ):
        if hasattr(governor, "compile_shell_sandbox_config"):
            try:
                tc_spec = tool._build_tc_spec()
                tool._qp_sandbox_config = governor.compile_shell_sandbox_config(
                    tc_spec,
                )
                tool._qp_sandbox_mode = True
            except Exception:
                logger.exception(
                    "OFF-mode shell sandbox_config compilation failed for "
                    "'%s'.",
                    getattr(tool, "name", "Unknown"),
                )
        return

    if not DEFAULT_REGISTRY.requires_sandbox(policy_name):
        return
    try:
        tc_spec = tool._build_tc_spec()
        tool._qp_sandbox_config = governor.compile_sandbox_config(tc_spec)
        tool._qp_sandbox_mode = True
    except Exception:
        # Leave sandbox_config unset; the tool's own fail-closed guard still
        # protects us — better a clean denial than an unsandboxed run.
        logger.exception(
            "OFF-mode sandbox_config compilation failed for '%s'.",
            getattr(tool, "name", "Unknown"),
        )


# pylint: disable=too-many-return-statements
async def _policy_tool_check_permissions(
    self: Any,
    input_data: dict[str, Any] | None = None,
    context: Any = None,
    *_extra_args: Any,
    **_extra_kwargs: Any,
) -> Any:
    """Perform governance policy evaluation for a tool call.

    Flow:
        1. Construct ToolCallSpec(tool_name, target, agent_id, session_id)
        2. governor.assert_policy(tool_call) → GovernanceDecision
        3. governor.audit(tool_call, decision)
        4. Map to PermissionDecision
    """
    from agentscope.permission import PermissionBehavior, PermissionDecision

    if self.name == "send_final_files_to_user" and "file_paths" in (input_data or {}):
        from ..runtime.final_files import current, source_path

        delivery = current()
        if delivery is not None:
            # A rejected replacement must not silently release an older list.
            delivery.submitted = False
            delivery.files = []
            delivery.verified = False
        paths = input_data["file_paths"]
        if not isinstance(paths, list) or not paths or any(
            not isinstance(path, str) or not path.strip() for path in paths
        ):
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message="file_paths must be a nonempty array of file paths.",
            )
        # Each file gets the existing policy/approval pipeline. Never flatten
        # the array to a string or authorize only its first path.
        for path in paths:
            try:
                path = str(source_path(path))
            except (OSError, ValueError) as exc:
                return PermissionDecision(
                    behavior=PermissionBehavior.DENY,
                    message=f"Invalid final-file path: {exc}",
                )
            decision = await _policy_tool_check_permissions(
                self, {"file_path": path}, context,
            )
            if decision.behavior != PermissionBehavior.ALLOW:
                return decision
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="governance: all final-file paths allowed.",
        )

    del context

    governor = getattr(self, "_qp_governor", None)
    self._qp_raw_params = input_data or {}
    # Clear previous-call isolation state, including when the next call takes
    # the OFF or direct route before reaching the normal decision cache.
    self._qp_sandbox_mode = False
    self._qp_sandbox_config = None
    self._qp_policy_decision = None
    self._qp_tc_spec = None
    direct_shell = _is_forced_direct_shell_tool(self)

    # ── Effective approval_level check (session > agent) ──
    request_ctx = getattr(self, "_qp_request_context", None) or {}
    effective_level = _resolve_effective_approval_level(request_ctx)
    if effective_level is not None and effective_level.is_disabled():
        # OFF means "never ask the user" — it does NOT mean "skip the
        # sandbox". Sandbox isolation is an execution mechanism, not an
        # approval gate. Fail-closed tools (the REPL) return DENIED without
        # a sandbox_config, which the guard layer then misreads as a sandbox
        # violation and escalates to a recurring approval prompt OFF can
        # never resolve. So we still compile+attach the sandbox here; only
        # the "ask the user" step is skipped.
        _prepare_off_mode_sandbox(self, governor)
        policy_name = DEFAULT_REGISTRY.python_to_policy_name(
            getattr(self, "name", "Unknown"),
        )
        if (
            not direct_shell
            and policy_name == "Bash"
            and is_windows_shell_sandbox_platform()
            and strict_shell_sandbox_enabled()
            and not getattr(self, "_qp_sandbox_mode", False)
        ):
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=(
                    "governance: approval_level=off, but strict Windows "
                    "shell sandbox is enabled and no sandbox is available."
                ),
            )
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="governance: approval_level=off, all tools allowed.",
        )

    # Sync effective approval_level to the governor's policy
    # so the three-phase evaluation uses the correct threshold.
    if governor is not None and effective_level is not None:
        governor.policy.execution_level = effective_level.value

    if governor is None:
        # Check if execution_level is "off" (dev mode) — allow pass-through
        if _is_execution_level_off():
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="governance: execution_level=off (dev mode), "
                "governor unavailable — pass-through.",
            )
        # Fail-closed: if governance layer failed to initialize, deny all
        # tool calls rather than silently allowing unguarded execution.
        logger.error(
            "PolicyGuardedTool: governor is None for tool '%s' — "
            "governance layer not initialized; denying (fail-closed).",
            getattr(self, "name", "Unknown"),
        )
        return PermissionDecision(
            behavior=PermissionBehavior.DENY,
            message=(
                "Governance layer unavailable (governor not initialized). "
                "All tool calls are denied until governance is restored. "
                "Please check server logs for initialization errors."
            ),
        )

    tc_spec = self._build_tc_spec()

    if direct_shell:
        previous_context = get_current_request_context()
        set_current_request_context(_request_context_for_tool_call(self))
        try:
            decision = governor.assert_policy(tc_spec, allow_sandbox=False)
        finally:
            set_current_request_context(previous_context)
    else:
        decision = governor.assert_policy(tc_spec)
    governor.audit(tc_spec, decision)

    # Cache the decision + tc_spec for __call__ to use
    self._qp_policy_decision = decision
    self._qp_tc_spec = tc_spec
    self._qp_sandbox_mode = False

    if decision.action is GovernanceAction.ALLOW:
        if decision.sandbox_config is not None:
            self._qp_sandbox_mode = True
            self._qp_sandbox_config = decision.sandbox_config
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="governance: tool allowed.",
        )
    elif decision.action is GovernanceAction.DENY:
        logger.warning(
            "governance deny: tool=%s target=%r source=%s reason=%s",
            tc_spec.tool_name,
            tc_spec.target,
            decision.source,
            decision.reason,
        )
        return PermissionDecision(
            behavior=PermissionBehavior.DENY,
            message=_format_policy_deny_message(tc_spec, decision),
        )
    elif decision.action is GovernanceAction.SANDBOX_FALLBACK:
        # Bash tool with no rule match → allow execution in sandbox
        self._qp_sandbox_mode = True
        self._qp_sandbox_config = decision.sandbox_config
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="governance: sandbox fallback.",
        )
    elif decision.action is GovernanceAction.ASK:
        # Requires user confirmation
        self._qp_policy_decision = decision

        return await _ask_user_approval(
            governor=governor,
            tc_spec=tc_spec,
            request_context=getattr(self, "_qp_request_context", {}) or {},
            policy_findings=decision.findings,
            governance_reason=decision.reason,
            source=decision.source,
            execution_mode=decision.execution_mode,
        )
    else:
        # Unknown decision → deny as safe default
        return PermissionDecision(
            behavior=PermissionBehavior.DENY,
            message=f"Unknown policy decision: {decision.action}",
        )


def _format_policy_deny_message(
    tc_spec: ToolCallSpec,
    decision: GovernanceDecision,
) -> str:
    lines = [
        f"governance: '{tc_spec.tool_name}' is denied by policy.",
        f"Reason: {decision.reason or 'unspecified'}",
        f"Source: {decision.source or 'unknown'}",
    ]
    if tc_spec.target:
        lines.append(f"Target: {tc_spec.target}")
    if tc_spec.tool_name == "Bash":
        lines.append(
            "Shell commands may only access the agent workspace, explicit "
            "mounts, and allowed runtime paths when sandboxing is enabled.",
        )
    return "\n".join(lines)


def _request_context_for_tool_call(tool: Any) -> dict[str, Any]:
    """Merge current ContextVar data with the wrapper request context."""
    current = get_current_request_context()
    merged = dict(current) if isinstance(current, dict) else {}
    request_context = getattr(tool, "_qp_request_context", {}) or {}
    if isinstance(request_context, dict):
        merged.update(request_context)
    return merged


def _format_tool_debug_value(value: Any, *, max_len: int = 2000) -> str:
    """Return a bounded, console-friendly representation for tool inputs."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = repr(value)
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _print_tool_call_params(
    tool: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    """Print user/tool-call parameters to the backend console for debugging."""
    tool_name = getattr(tool, "name", "Unknown")
    visible_kwargs = {
        key: value for key, value in kwargs.items() if key != "sandbox_config"
    }
    print(
        "[ToolCallInput] "
        f"tool={tool_name} "
        f"args={_format_tool_debug_value(args)} "
        f"kwargs={_format_tool_debug_value(visible_kwargs)}",
        flush=True,
    )


async def _call_function_tool_with_request_context(
    tool: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """Run FunctionTool with request_context visible to tool helpers."""
    from agentscope.tool import FunctionTool

    previous_context = get_current_request_context()
    set_current_request_context(_request_context_for_tool_call(tool))
    try:
        _print_tool_call_params(tool, args, kwargs)
        return await FunctionTool.__call__(tool, *args, **kwargs)
    finally:
        set_current_request_context(previous_context)


async def _policy_tool_call(
    self: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Override FunctionTool.__call__ for sandbox execution + retry.

    If sandbox execution triggers a violation (ToolChunk state=DENIED),
    request user approval.
    If the user approves, retry without sandbox.
    """
    if force_direct_subprocess_sync_enabled():
        # No sandbox execution in this mode. Preserve every tool's own
        # denial instead of escalating/retrying it as a sandbox failure.
        kwargs.pop("sandbox_config", None)
        return await _call_function_tool_with_request_context(self, args, kwargs)

    sandbox_mode = getattr(self, "_qp_sandbox_mode", False)
    if sandbox_mode:
        sandbox_config = getattr(self, "_qp_sandbox_config", None)
        if sandbox_config is not None:
            kwargs["sandbox_config"] = sandbox_config

    # Call the original function
    from agentscope.message import ToolResultState

    try:
        result = await _call_function_tool_with_request_context(self, args, kwargs)
    except Exception as exc:
        try:
            from ..sandbox import WindowsSandboxAclElevationRequired
        except ImportError:
            WindowsSandboxAclElevationRequired = ()  # type: ignore
        if isinstance(exc, WindowsSandboxAclElevationRequired):
            return await _handle_acl_elevation_required(
                self,
                args,
                kwargs,
                exc,
            )
        raise

    # Check if sandbox violation was returned (state=DENIED)
    if not (
        isinstance(result, ToolChunk)
        and result.state == ToolResultState.DENIED
    ):
        return result

    # Extract violation message from metadata or content
    violation_msg = ""
    if hasattr(result, "metadata") and result.metadata:
        violation_msg = result.metadata.get("sandbox_violation", "")
    if not violation_msg:
        # Fallback: extract from content text
        for block in result.content or []:
            if hasattr(block, "text") and "Sandbox violation:" in block.text:
                violation_msg = (
                    block.text.split("Sandbox violation:", 1)[1]
                    .split("\n")[0]
                    .strip()
                )
                break

    logger.info(
        "PolicyGuardedTool: sandbox violation for '%s': %s",
        getattr(self, "name", "Unknown"),
        violation_msg,
    )

    governor = getattr(self, "_qp_governor", None)
    request_context = getattr(self, "_qp_request_context", {}) or {}

    if governor is None:
        # No governor, can't approve — return the violation as DENIED
        return ToolChunk(
            is_last=True,
            state=ToolResultState.DENIED,
            content=[
                TextBlock(
                    type="text",
                    text=f"Sandbox violation: {violation_msg}\n"
                    f"Command was blocked by sandbox security policy.",
                ),
            ],
        )

    # Trigger approval flow — reuse tc_spec from check_permissions
    tc_spec = getattr(self, "_qp_tc_spec", None)
    if tc_spec is None:
        # Fallback: reconstruct if check_permissions didn't run
        self._qp_raw_params = {}
        tc_spec = self._build_tc_spec()

    governance_reason = getattr(
        getattr(self, "_qp_policy_decision", None),
        "reason",
        None,
    )
    governance_source = getattr(
        getattr(self, "_qp_policy_decision", None),
        "source",
        "No rule hit",
    )

    # Record the ASK escalation (sandbox violation → ask user)
    governor.audit(
        tc_spec,
        GovernanceDecision(
            action=GovernanceAction.ASK,
            reason=(
                f"sandbox violation: {violation_msg}"
                if violation_msg
                else "sandbox violation, ask user"
            ),
        ),
    )

    from agentscope.permission import PermissionBehavior

    decision = await _ask_user_approval(
        governor=governor,
        tc_spec=tc_spec,
        request_context=request_context,
        violation_msg=violation_msg or None,
        governance_reason=governance_reason,
        source=governance_source,
    )

    if decision.behavior == PermissionBehavior.ALLOW:
        if _is_strict_shell_sandbox_tool(self, governor):
            try:
                sandbox_config = governor.compile_shell_sandbox_config(tc_spec)
            except Exception:
                logger.exception(
                    "PolicyGuardedTool: failed to recompile strict shell "
                    "sandbox for '%s'",
                    getattr(self, "name", "Unknown"),
                )
                sandbox_config = None
            if sandbox_config is None:
                return ToolChunk(
                    is_last=True,
                    state=ToolResultState.DENIED,
                    content=[
                        TextBlock(
                            type="text",
                            text=(
                                "Sandbox violation: "
                                f"{violation_msg}\n"
                                "Command cannot be re-run outside the "
                                "strict shell sandbox."
                            ),
                        ),
                    ],
                )
            kwargs["sandbox_config"] = sandbox_config
            self._qp_sandbox_mode = True
            self._qp_sandbox_config = sandbox_config
            logger.info(
                "PolicyGuardedTool: user approved sandbox violation, "
                "retrying inside strict shell sandbox for '%s'",
                getattr(self, "name", "Unknown"),
            )
            return await _call_function_tool_with_request_context(self, args, kwargs)

        # User approved: retry without sandbox
        logger.info(
            "PolicyGuardedTool: user approved sandbox violation, "
            "retrying without sandbox for '%s'",
            getattr(self, "name", "Unknown"),
        )
        kwargs.pop("sandbox_config", None)
        self._qp_sandbox_mode = False
        return await _call_function_tool_with_request_context(self, args, kwargs)
    else:
        # User denied: return the violation as DENIED
        return ToolChunk(
            is_last=True,
            state=ToolResultState.DENIED,
            content=[
                TextBlock(
                    type="text",
                    text=f"Sandbox violation: {violation_msg}\n"
                    f"Command was blocked and user denied approval.\n\n"
                    f"{_NO_RETRY_INSTRUCTION}",
                ),
            ],
        )


def _is_strict_shell_sandbox_tool(tool: Any, governor: Any) -> bool:
    if governor is None:
        return False
    policy_name = DEFAULT_REGISTRY.python_to_policy_name(
        getattr(tool, "name", "Unknown"),
    )
    return (
        policy_name == "Bash"
        and is_windows_shell_sandbox_platform()
        and strict_shell_sandbox_enabled()
        and hasattr(governor, "compile_shell_sandbox_config")
    )


async def _handle_acl_elevation_required(
    tool: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    exc: Any,
) -> ToolChunk:
    from agentscope.message import ToolResultState
    from agentscope.permission import PermissionBehavior

    governor = getattr(tool, "_qp_governor", None)
    request_context = getattr(tool, "_qp_request_context", {}) or {}
    tc_spec = getattr(tool, "_qp_tc_spec", None)
    if tc_spec is None:
        tool._qp_raw_params = {}
        tc_spec = tool._build_tc_spec()
    approval_context = _resolve_acl_approval_context(
        tool,
        tc_spec,
        request_context,
    )

    if governor is None:
        return _acl_elevation_denied_chunk(
            "Sandbox setup failed: administrator ACL grant is required, "
            "but governance is unavailable.",
        )

    governor.audit(
        tc_spec,
        GovernanceDecision(
            action=GovernanceAction.ASK,
            reason=_acl_elevation_reason(exc),
        ),
    )
    decision = await _ask_acl_elevation_approval(
        governor=governor,
        tc_spec=tc_spec,
        request_context=approval_context,
        elevation=exc,
    )
    if decision.behavior != PermissionBehavior.ALLOW:
        return _acl_elevation_denied_chunk(
            _acl_elevation_denied_message(exc),
        )

    ok, detail = await _run_windows_acl_helper_elevated(exc)
    if not ok:
        return _acl_elevation_denied_chunk(
            "Sandbox setup failed: administrator ACL helper did not complete "
            f"successfully.\n{detail}",
        )

    logger.info(
        "PolicyGuardedTool: elevated ACL grant succeeded, retrying '%s'",
        getattr(tool, "name", "Unknown"),
    )
    return await _call_function_tool_with_request_context(tool, args, kwargs)


def _acl_elevation_denied_chunk(message: str) -> ToolChunk:
    from agentscope.message import ToolResultState

    return ToolChunk(
        is_last=True,
        state=ToolResultState.DENIED,
        content=[
            TextBlock(
                type="text",
                text=message + _NO_RETRY_INSTRUCTION,
            ),
        ],
        metadata={"sandbox_acl_elevation": "denied"},
    )


def _resolve_acl_approval_context(
    tool: Any,
    tc_spec: ToolCallSpec,
    request_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a frontend-routable context for sandbox ACL approvals.

    ACL elevation is raised while the tool is already executing, so relying
    only on the tool wrapper's construction-time context can produce a pending
    approval whose root session does not match the active console session.
    Keep this fix scoped to the ACL exception path and preserve the existing
    approval frontend contract: root_session_id must identify the current root
    session.
    """
    ctx: dict[str, Any] = {}
    tool_context = getattr(tool, "_qp_request_context", None)
    if isinstance(tool_context, dict):
        ctx.update(tool_context)
    if isinstance(request_context, dict):
        ctx.update(request_context)
    current_context = get_current_request_context()
    if isinstance(current_context, dict):
        ctx.update(current_context)

    session_id = str(ctx.get("session_id") or tc_spec.session_id or "")
    root_session_id = str(ctx.get("root_session_id") or session_id)
    agent_id = str(ctx.get("agent_id") or tc_spec.agent_id or "unknown")
    root_agent_id = str(ctx.get("root_agent_id") or agent_id)

    ctx["session_id"] = session_id
    ctx["root_session_id"] = root_session_id
    ctx["agent_id"] = agent_id
    ctx["root_agent_id"] = root_agent_id
    ctx["channel"] = str(ctx.get("channel") or "console")
    return ctx


def _acl_elevation_permission(elevation: Any) -> str:
    return str(getattr(elevation, "permission", "RX") or "RX").upper()


def _acl_elevation_reason(elevation: Any) -> str:
    return (
        "Windows sandbox runtime directory ACL grant requires administrator "
        "approval"
    )


def _acl_elevation_title(elevation: Any) -> str:
    return "Administrator ACL Grant Required"


def _acl_elevation_description(elevation: Any) -> str:
    return (
        "The Windows sandbox needs read/execute access to runtime or "
        "toolchain directories. Approving runs a restricted elevated helper "
        "for ACL setup only; the command remains sandboxed."
    )


def _acl_elevation_remediation(elevation: Any) -> str:
    return (
        "Approve to run the elevated ACL helper, or deny to block the command."
    )


def _acl_elevation_denied_message(elevation: Any) -> str:
    return (
        "Sandbox setup failed: runtime or toolchain directories require "
        "administrator ACL grant. The request was denied or timed out, so "
        "the command was not executed."
    )


async def _ask_acl_elevation_approval(
    governor: ResourceGovernor,
    tc_spec: ToolCallSpec,
    request_context: dict[str, str],
    *,
    elevation: Any,
) -> Any:
    from agentscope.permission import PermissionBehavior, PermissionDecision

    from ..app.approvals import get_approval_service
    from ..constant import TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS
    from ..security.tool_guard.approval import ApprovalDecision
    from ..security.tool_guard.models import (
        GuardFinding,
        GuardSeverity,
        GuardThreatCategory,
        ToolGuardResult,
    )

    ctx = request_context or {}
    session_id = str(ctx.get("session_id") or tc_spec.session_id or "")
    permission = _acl_elevation_permission(elevation)
    paths = list(getattr(elevation, "paths", []) or [])
    path_lines = "\n".join(f"- {path}" for path in paths[:8])
    if len(paths) > 8:
        path_lines += f"\n- ... and {len(paths) - 8} more path(s)"

    guard_result = ToolGuardResult(
        tool_name=tc_spec.tool_name,
        params=tc_spec.raw_params,
        findings=[
            GuardFinding(
                id=uuid.uuid4().hex[:8],
                rule_id="windows_sandbox_acl_elevation",
                category=GuardThreatCategory.PRIVILEGE_ESCALATION,
                severity=GuardSeverity.HIGH,
                title=_acl_elevation_title(elevation),
                description=_acl_elevation_description(elevation),
                tool_name=tc_spec.tool_name,
                remediation=_acl_elevation_remediation(elevation),
                guardian="windows_sandbox",
                metadata={
                    "kind": "sandbox_acl_elevation",
                    "container_name": getattr(elevation, "container_name", ""),
                    "sid": getattr(elevation, "sid", ""),
                    "permission": permission,
                    "paths": paths,
                },
            ),
        ],
        guardians_used=["windows_sandbox"],
    )

    svc = get_approval_service()
    tool_call_id = str(ctx.get("tool_call_id") or "")
    if session_id and tool_call_id:
        await svc.cancel_stale_pending_for_tool_call(session_id, tool_call_id)

    pending = await svc.create_pending(
        session_id=session_id,
        root_session_id=str(ctx.get("root_session_id") or session_id),
        owner_agent_id=str(
            ctx.get("root_agent_id") or ctx.get("agent_id") or "unknown",
        ),
        user_id=str(ctx.get("user_id") or ""),
        channel=str(ctx.get("channel") or ""),
        agent_id=tc_spec.agent_id or "unknown",
        tool_name=tc_spec.tool_name,
        result=guard_result,
        timeout_seconds=TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS,
        extra={
            "tool_call": {
                "id": tool_call_id,
                "name": tc_spec.tool_name,
                "input": dict(tc_spec.raw_params or {}),
            },
            "display": {
                "tool_name": tc_spec.tool_name,
                "tool_source": "windows_sandbox_acl",
                "exact_target": tc_spec.target,
                "similar_target": tc_spec.target,
                "is_generalized": False,
            },
            "sandbox_acl_elevation": {
                "container_name": getattr(elevation, "container_name", ""),
                "permission": permission,
                "paths": paths,
                "summary": path_lines,
            },
            "channel_meta": ctx.get("channel_meta"),
            "_channel_instance": ctx.get("_channel_instance"),
        },
    )
    try:
        approval = await svc.wait_for_approval(
            pending.request_id,
            TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("Windows sandbox ACL approval wait failed.")
        approval = ApprovalDecision.DENIED

    approved = approval == ApprovalDecision.APPROVED
    governor.audit(
        tc_spec,
        GovernanceDecision(
            action=GovernanceAction.ALLOW if approved else GovernanceAction.DENY,
            reason=(
                "User approved Windows sandbox ACL elevation"
                if approved
                else f"Windows sandbox ACL elevation {approval.value}"
            ),
        ),
    )
    if approved:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Approved Windows sandbox ACL elevation.",
        )
    return PermissionDecision(
        behavior=PermissionBehavior.DENY,
        message="Windows sandbox ACL elevation was denied or timed out.",
    )


async def _run_windows_acl_helper_elevated(elevation: Any) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Windows ACL helper can only run on Windows."
    state_dir = (
        Path(os.environ.get("USERPROFILE", os.path.expanduser("~")))
        / ".qwenpaw"
    )
    request_id = uuid.uuid4().hex
    helper_dir = state_dir / "acl-elevation"
    helper_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = helper_dir / f"{request_id}.json"
    result_path = helper_dir / f"{request_id}.result.json"
    manifest = {
        "request_id": request_id,
        "container_name": getattr(elevation, "container_name", ""),
        "sid": getattr(elevation, "sid", ""),
        "permission": getattr(elevation, "permission", "RX"),
        "paths": list(getattr(elevation, "paths", []) or []),
        "result_path": str(result_path),
    }
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    helper_args = (
        "-m qwenpaw.sandbox.windows_acl_helper "
        f"--manifest {str(manifest_path)}"
    )
    command = (
        "Start-Process -Verb RunAs -Wait "
        f"-FilePath {_ps_single_quote(sys.executable)} "
        "-ArgumentList "
        f"{_ps_single_quote(helper_args)}"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except Exception as exc:
        return False, str(exc)

    if not result_path.is_file():
        detail = "\n".join(
            part.decode("utf-8", errors="replace").strip()
            for part in (stdout, stderr)
            if part
        )
        return False, detail or "ACL helper did not produce a result file."
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"Failed to read ACL helper result: {exc}"
    if result.get("ok") is True:
        return True, "ACL helper completed successfully."
    return False, json.dumps(result, ensure_ascii=False)


def _ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# ASK path: reuse ApprovalService
# ---------------------------------------------------------------------------


async def _ask_user_approval(
    governor: ResourceGovernor,
    tc_spec: ToolCallSpec,
    request_context: dict[str, str],
    *,
    violation_msg: str | None = None,
    governance_reason: str | None = None,
    policy_findings: list[Any] | None = None,
    source: str = "No rule hit",
    execution_mode: str | None = None,
) -> Any:
    """Request user approval, blocking until a reply is received."""
    from agentscope.permission import PermissionBehavior, PermissionDecision

    from ..app.approvals import get_approval_service
    from ..constant import TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS
    from ..security.tool_guard.approval import (
        ApprovalDecision,
        ApprovalScope,
        format_findings_summary,
    )
    from ..security.tool_guard.models import (
        GuardFinding,
        GuardSeverity,
        GuardThreatCategory,
        ToolGuardResult,
    )

    tool_name = tc_spec.tool_name
    target = tc_spec.target
    agent_id = tc_spec.agent_id
    session_id = tc_spec.session_id
    params = tc_spec.raw_params

    ctx = request_context or {}
    user_id = str(ctx.get("user_id") or "")
    channel = str(ctx.get("channel") or "")
    root_session_id = str(ctx.get("root_session_id") or session_id)
    root_agent_id = str(ctx.get("root_agent_id") or agent_id or "unknown")

    from .generalize import generalize_target_for_approval

    generalized_target = await generalize_target_for_approval(
        tool_name,
        target,
        source,
        agent_id=agent_id,
    )
    display_target = generalized_target or target

    # Construct a ToolGuardResult for ApprovalService.
    # If deep-scan findings were attached by policy.evaluate(),
    # convert them into GuardFindings for the approval card.
    if policy_findings:
        converted_findings = []
        for pf in policy_findings:
            # pf is a governance.detectors.GuardFinding (dataclass)
            converted_findings.append(
                GuardFinding(
                    id=getattr(pf, "id", uuid.uuid4().hex[:8]),
                    rule_id=getattr(pf, "rule_id", "policy_deep_scan"),
                    category=GuardThreatCategory(
                        getattr(pf, "category", "resource_abuse"),
                    ),
                    severity=GuardSeverity(
                        getattr(pf, "severity", "INFO"),
                    ),
                    title=getattr(pf, "title", "Policy Approval Required"),
                    description=getattr(pf, "description", ""),
                    tool_name=tool_name,
                    param_name=getattr(pf, "param_name", None),
                    matched_value=getattr(pf, "matched_value", None),
                    matched_pattern=getattr(pf, "matched_pattern", None),
                    snippet=getattr(pf, "snippet", None),
                    remediation=getattr(pf, "remediation", None),
                    guardian=getattr(pf, "detector", "governance_policy"),
                    metadata=getattr(pf, "metadata", {}),
                ),
            )
        guard_result = ToolGuardResult(
            tool_name=tool_name,
            params=params,
            findings=converted_findings,
            guardians_used=["governance_policy"],
        )
    else:
        guard_result = ToolGuardResult(
            tool_name=tool_name,
            params=params,
            findings=[
                GuardFinding(
                    id=uuid.uuid4().hex[:8],
                    rule_id="policy_ask",
                    category=GuardThreatCategory.RESOURCE_ABUSE,
                    severity=(
                        GuardSeverity.HIGH
                        if violation_msg
                        else GuardSeverity.INFO
                    ),
                    title=(
                        "Sandbox Violation — Approve Unsandboxed Execution?"
                        if violation_msg
                        else "Policy Approval Required"
                    ),
                    description=(
                        (
                            f"Governance reason: {governance_reason}"
                            if governance_reason
                            else ""
                        )
                        + (
                            f"\n\n\u26a0\ufe0f Sandbox violation: "
                            f"{violation_msg}"
                            f"\n\n**If you approve, this command will be "
                            f"re-executed WITHOUT sandbox isolation (full "
                            f"host access).** The kernel-level filesystem "
                            f"restrictions that blocked it will no longer "
                            f"apply."
                            if violation_msg
                            else ""
                        )
                    ),
                    tool_name=tool_name,
                    remediation=(
                        "Approve to re-run without sandbox (full host "
                        "access), or deny to block the command."
                        if violation_msg
                        else "Approve or deny this tool call"
                    ),
                    guardian="governance_policy",
                    metadata={
                        "target": target,
                        **(
                            {
                                "sandbox_violation": violation_msg,
                                "escalation": "sandbox_to_host",
                            }
                            if violation_msg
                            else {}
                        ),
                    },
                ),
            ],
            guardians_used=["governance_policy"],
        )

    svc = get_approval_service()
    tool_call_id = str(ctx.get("tool_call_id") or "")
    if session_id and tool_call_id:
        await svc.cancel_stale_pending_for_tool_call(session_id, tool_call_id)

    pending = await svc.create_pending(
        session_id=session_id,
        root_session_id=root_session_id,
        owner_agent_id=root_agent_id,
        user_id=user_id,
        channel=channel,
        agent_id=agent_id or "unknown",
        tool_name=tool_name,
        result=guard_result,
        timeout_seconds=TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS,
        extra={
            "tool_call": {
                "id": tool_call_id,
                "name": tool_name,
                "input": dict(params or {}),
            },
            "display": {
                "tool_name": tool_name,
                "tool_source": source,
                "exact_target": target,
                "similar_target": display_target,
                "is_generalized": display_target != target,
            },
            "channel_meta": ctx.get("channel_meta"),
            "_channel_instance": ctx.get("_channel_instance"),
        },
    )

    logger.info(
        "PolicyGuardedTool: awaiting approval for tool=%s session=%s "
        "request_id=%s target=%s",
        tool_name,
        session_id[:8] if session_id else "",
        pending.request_id[:8],
        target,
    )

    try:
        decision = await svc.wait_for_approval(
            pending.request_id,
            TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.error(
            "PolicyGuardedTool: wait_for_approval crashed (%s); denying",
            exc,
            exc_info=True,
        )
        decision = ApprovalDecision.DENIED

    # Record user approve/deny result to audit log
    approved = decision == ApprovalDecision.APPROVED
    # The scope the user chose (set by resolve_request on the same pending
    # object). None = no choice offered → EXACT.
    scope = getattr(pending, "scope", None)
    scope_label = scope.value if scope else "exact"
    approval_decision = GovernanceDecision(
        action=GovernanceAction.ALLOW if approved else GovernanceAction.DENY,
        reason=(f"User Approve ({scope_label})" if approved else "User Deny"),
        execution_mode=execution_mode,
    )
    governor.audit(tc_spec, approval_decision)

    summary = format_findings_summary(guard_result)
    if decision == ApprovalDecision.APPROVED:
        # ── Record approved rule (skip for builtin ask) ──
        # SIMILAR → the generalized pattern; EXACT (default) → the literal
        # target the user actually approved. Widening is opt-in.
        rule_target = (
            generalized_target if scope == ApprovalScope.SIMILAR else target
        )
        await governor.add_approved_rule(
            tc_spec,
            generalized_target=rule_target,
        )
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=f"Approved by user ({scope_label}).\n{summary}",
        )

    denial_msg = (
        f"User denied the request to run '{tool_name}'.\n{summary}"
        if decision == ApprovalDecision.DENIED
        else (
            f"Approval for '{tool_name}' timed out after "
            f"{int(TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS)}s.\n{summary}"
        )
    )
    return PermissionDecision(
        behavior=PermissionBehavior.DENY,
        message=denial_msg + _NO_RETRY_INSTRUCTION,
    )
