# -*- coding: utf-8 -*-
"""Request-context bindings declared by a skill's process-tool.json.

This module intentionally stays independent from AgentScope internals.  It
parses the small JSON contract next to a skill, builds the optional context
hint injected when deterministic request values are present, and exposes a
pure pre-execution binding helper for the eventual structured scripts tool.
"""

from __future__ import annotations

import contextvars
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROCESS_TOOL_FILENAME = "process-tool.json"
_ACTIVE_PROCESS_TOOL_SPECS: contextvars.ContextVar[
    tuple["ProcessToolSpec", ...]
] = contextvars.ContextVar("active_process_tool_specs", default=())


@dataclass(frozen=True)
class ProcessToolBinding:
    """Map one scripts argument to one request-context key."""

    script_arg: str
    source: str
    request_key: str


@dataclass(frozen=True)
class ProcessToolSpec:
    """Parsed process-tool.json contract for one skill."""

    skill_dir: Path
    input_schema: dict[str, Any]
    bindings: tuple[ProcessToolBinding, ...]


def load_process_tool_spec(skill_dir: Path) -> ProcessToolSpec | None:
    """Load ``process-tool.json`` from *skill_dir*, if it is valid enough."""

    path = Path(skill_dir) / PROCESS_TOOL_FILENAME
    if not path.exists() or not path.is_file():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    input_schema = data.get("inputSchema")
    bindings = data.get("bindings")
    if not isinstance(input_schema, dict) or not isinstance(bindings, dict):
        return None

    parsed: list[ProcessToolBinding] = []
    for script_arg, raw_binding in bindings.items():
        if not isinstance(script_arg, str) or not script_arg.strip():
            continue
        if not isinstance(raw_binding, dict):
            continue

        source = raw_binding.get("source")
        request_key = raw_binding.get("key")
        if source not in {"request_input", "request_context"}:
            continue
        if not isinstance(request_key, str) or not request_key.strip():
            continue

        parsed.append(
            ProcessToolBinding(
                script_arg=script_arg,
                source=source,
                request_key=request_key,
            ),
        )

    if not parsed:
        return None

    return ProcessToolSpec(
        skill_dir=Path(skill_dir),
        input_schema=input_schema,
        bindings=tuple(parsed),
    )


def _required_keys(input_schema: dict[str, Any]) -> set[str]:
    required = input_schema.get("required") or []
    if not isinstance(required, list):
        return set()
    return {item for item in required if isinstance(item, str)}


def is_required(input_schema: dict[str, Any], key: str) -> bool:
    """Return whether *key* is required by the process input schema."""

    return key in _required_keys(input_schema)


def validate_request_value(
    input_schema: dict[str, Any],
    key: str,
    value: Any,
) -> str | None:
    """Validate one request-context value against a small JSON-schema subset.

    The current product contract only needs object properties with primitive
    ``type`` checks and array item constraints.  Unknown schema constructs are
    ignored here instead of failing closed.
    """

    properties = input_schema.get("properties") or {}
    if not isinstance(properties, dict):
        return None
    prop = properties.get(key) or {}
    if not isinstance(prop, dict):
        return None

    expected_type = prop.get("type")

    if expected_type == "array":
        if not isinstance(value, list):
            return f"request_context.{key} must be an array"

        min_items = prop.get("minItems")
        max_items = prop.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            return (
                f"request_context.{key} must contain at least "
                f"{min_items} item(s)"
            )
        if isinstance(max_items, int) and len(value) > max_items:
            return (
                f"request_context.{key} must contain at most "
                f"{max_items} item(s)"
            )

        items = prop.get("items") or {}
        if isinstance(items, dict) and items.get("type") == "string":
            if not all(isinstance(item, str) for item in value):
                return f"request_context.{key} items must be strings"
        return None

    if expected_type == "string" and not isinstance(value, str):
        return f"request_context.{key} must be a string"
    if expected_type == "number" and not isinstance(value, (int, float)):
        return f"request_context.{key} must be a number"
    if expected_type == "integer" and not isinstance(value, int):
        return f"request_context.{key} must be an integer"
    if expected_type == "boolean" and not isinstance(value, bool):
        return f"request_context.{key} must be a boolean"
    if expected_type == "object" and not isinstance(value, dict):
        return f"request_context.{key} must be an object"

    return None


def bound_context_values(
    spec: ProcessToolSpec | None,
    request_context: dict[str, Any],
) -> dict[str, Any]:
    """Return scripts-argument values available from request context."""

    if spec is None or not isinstance(request_context, dict):
        return {}

    values: dict[str, Any] = {}
    for binding in spec.bindings:
        value = request_context.get(binding.request_key)
        if value is None:
            continue
        if validate_request_value(spec.input_schema, binding.request_key, value):
            continue
        values[binding.script_arg] = value
    return values


def build_process_tool_context_hint(
    spec: ProcessToolSpec | None,
    request_context: dict[str, Any],
) -> str:
    """Build the optional model-facing hint for available bindings.

    Missing required values intentionally produce no hint.  The missing-value
    message is emitted only by the scripts execution guard.
    """

    values = bound_context_values(spec, request_context)
    if not values:
        return ""

    lines = []
    for script_arg, value in values.items():
        value_text = json.dumps(value, ensure_ascii=False, indent=2)
        indented_value = "\n".join(f"  {line}" for line in value_text.splitlines())
        lines.append(
            f"- scripts 参数 {script_arg} 的值为：\n"
            f"{indented_value}",
        )

    return (
        "\n\n当前 skill 的确定性入参已经由 request_context 提供：\n"
        + "\n".join(lines)
        + "\n\n调用该 skill 的 scripts 工具时，请使用上述值作为对应入参。"
        "\n系统也会在脚本执行前用该值覆盖/补齐对应入参。"
    )


def remember_active_process_tool_spec(spec: ProcessToolSpec | None) -> None:
    """Remember one viewed skill spec for later structured tool calls."""

    if spec is None:
        return

    active = list(_ACTIVE_PROCESS_TOOL_SPECS.get())
    active = [item for item in active if item.skill_dir != spec.skill_dir]
    active.append(spec)
    _ACTIVE_PROCESS_TOOL_SPECS.set(tuple(active[-8:]))


def active_process_tool_specs() -> tuple[ProcessToolSpec, ...]:
    """Return process-tool specs viewed in this request context."""

    return _ACTIVE_PROCESS_TOOL_SPECS.get()


def clear_active_process_tool_specs() -> None:
    """Clear viewed process-tool specs for a new top-level request."""

    _ACTIVE_PROCESS_TOOL_SPECS.set(())


async def _load_pool_skill(skill: str) -> Any:
    """Load only the requested pool directory, using AgentScope's parser.

    list_skills exposes directory names, as does request_context.skills.
    Resolve through the same pool roots and path validation as session skills.
    """
    from agentscope.skill import LocalSkillLoader

    from .store import resolve_pool_skill_dir

    skill_dir = resolve_pool_skill_dir(skill)
    if skill_dir is None:
        return None
    skills = await LocalSkillLoader(directory=str(skill_dir)).list_skills()
    return skills[0] if skills else None


def install_process_tool_skill_viewer(
    toolkit: Any,
    request_context: dict[str, Any] | None,
    *,
    allow_pool_skills: bool = True,
) -> None:
    """Extend Skill lookup to the pool and append process-tool hints.

    Registered skills take precedence. Pool skills are loaded on demand,
    without registering them or changing session state. No hint is appended
    without ``process-tool.json`` and a valid request-context binding.
    """

    if not isinstance(request_context, dict):
        return

    registered = getattr(toolkit, "builtin_skill_viewer", None)
    original = getattr(registered, "tool", None)
    get_skills_method = getattr(original, "_get_skills_method", None)
    if original is None or get_skills_method is None:
        return

    try:
        from agentscope.message import TextBlock, ToolResultState
        from agentscope.state import AgentState
        from agentscope.tool import ToolChunk
        from agentscope.tool._base import ToolBase
    except Exception:
        return

    class ProcessToolSkillViewer(ToolBase):
        name = getattr(original, "name", "Skill")
        description = getattr(original, "description", "")
        input_schema = getattr(original, "input_schema", {})
        is_concurrency_safe = getattr(original, "is_concurrency_safe", True)
        is_external_tool = getattr(original, "is_external_tool", False)
        is_state_injected = getattr(original, "is_state_injected", True)
        is_read_only = getattr(original, "is_read_only", True)
        is_mcp = getattr(original, "is_mcp", False)
        mcp_name = getattr(original, "mcp_name", None)

        async def check_permissions(self, *args: Any, **kwargs: Any) -> Any:
            return await original.check_permissions(*args, **kwargs)

        async def resolve_skill(self, skill: str, groups: list[str]) -> Any:
            """Read Skill information without activating process-tool bindings."""
            skills = await get_skills_method(groups)
            target = skills.get(skill)
            if target is None and allow_pool_skills:
                target = await _load_pool_skill(skill)
            return target

        async def call(
            self,
            skill: str,
            _agent_state: AgentState,
        ) -> ToolChunk:
            skills = await get_skills_method(
                _agent_state.tool_context.activated_groups,
            )
            target_skill = skills.get(skill)
            if target_skill is None and allow_pool_skills:
                target_skill = await _load_pool_skill(skill)
                if target_skill is not None:
                    chunk = ToolChunk(
                        content=[TextBlock(text=target_skill.markdown)],
                    )
                else:
                    return await original.call(
                        skill=skill, _agent_state=_agent_state,
                    )
            else:
                chunk = await original.call(
                    skill=skill, _agent_state=_agent_state,
                )
            if chunk.state == ToolResultState.ERROR:
                return chunk

            skill_dir = getattr(target_skill, "dir", None)
            if not skill_dir:
                return chunk

            spec = load_process_tool_spec(Path(skill_dir))
            if spec is None:
                return chunk

            remember_active_process_tool_spec(spec)
            hint = build_process_tool_context_hint(spec, request_context)
            if not hint:
                return chunk

            content = list(chunk.content)
            if content and isinstance(content[-1], TextBlock):
                content[-1] = TextBlock(text=content[-1].text.rstrip() + hint)
            else:
                content.append(TextBlock(text=hint.lstrip()))

            return chunk.model_copy(update={"content": content})

    registered.tool = ProcessToolSkillViewer()


def apply_process_tool_bindings_or_error(
    spec: ProcessToolSpec | None,
    args: dict[str, Any],
    request_context: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Apply bindings to scripts args or return a model-facing error text."""

    if spec is None:
        return dict(args), None

    next_args = dict(args)
    for binding in spec.bindings:
        value = request_context.get(binding.request_key)

        if value is None:
            if is_required(spec.input_schema, binding.request_key):
                return None, (
                    f"当前 skill 的 scripts 参数 {binding.script_arg} 必须由 "
                    f"request_context.{binding.request_key} 提供，但本次请求"
                    f"没有携带 {binding.request_key}。脚本未执行。请告知用户"
                    "缺少必要参数。"
                )
            continue

        error = validate_request_value(
            spec.input_schema,
            binding.request_key,
            value,
        )
        if error:
            return None, (
                f"当前 skill 的确定性入参 "
                f"request_context.{binding.request_key} 不符合 "
                f"process-tool.json 的 inputSchema：{error}。脚本未执行。"
            )

        next_args[binding.script_arg] = value

    return next_args, None


__all__ = [
    "ProcessToolBinding",
    "ProcessToolSpec",
    "active_process_tool_specs",
    "apply_process_tool_bindings_or_error",
    "bound_context_values",
    "build_process_tool_context_hint",
    "clear_active_process_tool_specs",
    "install_process_tool_skill_viewer",
    "is_required",
    "load_process_tool_spec",
    "remember_active_process_tool_spec",
    "validate_request_value",
]
