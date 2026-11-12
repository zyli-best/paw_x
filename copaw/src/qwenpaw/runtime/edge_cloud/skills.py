"""Reuse default Skill discovery/loading and tool groups for edge execution."""

from __future__ import annotations

import copy
from types import MethodType

from agentscope.message import TextBlock
from agentscope.tool import ToolBase, ToolChunk

from .safety import DEVICE_TOOLS

FORBIDDEN_EDGE_TOOLS = frozenset(
    {
        "chat_with_agent",
        "submit_to_agent",
        "spawn_subagent",
        "check_agent_task",
        "delegate_external_agent",
        "list_agents",
        "get_agent_count",
        "create_agent",
        "update_agent",
        "delete_agent",
        "run_skill",
        "execute_skill",
        "run_tool_batch",
        "recall",
        "recall_history",
        "recall_history_python",
        "get_historical_tool_result",
        "memory_search",
        "material_skill",
        "materialize_skill",
    }
)


async def skill_summaries(toolkit, activated_groups):
    """Use the same registered/session Skill loaders as the default toolkit.

    Pool discovery remains with the existing list_skills tool. No new index,
    dependency declaration, approval list or Skill format is introduced.
    """
    skills = await toolkit._get_available_skills(activated_groups)
    return [
        {
            "name": skill.name,
            "description": skill.description,
            "directory": skill.dir,
        }
        for skill in skills.values()
    ]


class RouteSkillReader(ToolBase):
    """Inspect real Skill instructions without running or activating the Skill."""

    name = "Skill"
    description = (
        "Read the full instructions of an existing Skill without executing it. "
        "Used by the router and verifier; does not run the workflow or activate "
        "process tools."
    )
    input_schema = {
        "type": "object",
        "properties": {"skill": {"type": "string"}},
        "required": ["skill"],
        "additionalProperties": False,
    }
    is_read_only = True
    is_concurrency_safe = True

    def __init__(self, toolkit, viewer, groups, allowed_skill: str = ""):
        super().__init__()
        self.toolkit, self.viewer, self.groups = toolkit, viewer, list(groups)
        self.allowed_skill = str(allowed_skill or "")
        self.inspected: set[str] = set()

    async def check_permissions(self, *args, **kwargs):
        return await self.viewer.check_permissions(*args, **kwargs)

    async def call(self, skill: str):
        if not isinstance(skill, str) or not skill.strip():
            raise ValueError("An exact existing Skill name is required")
        skill = skill.strip()
        if self.allowed_skill and skill != self.allowed_skill:
            raise ValueError(
                f"Only the selected Skill '{self.allowed_skill}' can be inspected"
            )
        resolver = getattr(self.viewer, "resolve_skill", None)
        if resolver is not None:
            target = await resolver(skill, self.groups)
        else:
            skills = await self.toolkit._get_available_skills(self.groups)
            target = skills.get(skill)
        if target is None:
            raise ValueError(f"Skill '{skill}' not found in the existing loaders")
        if not isinstance(target.markdown, str) or not target.markdown.strip():
            raise ValueError(f"Skill '{skill}' has no instructions to inspect")
        result = ToolChunk(content=[TextBlock(text=target.markdown)])
        self.inspected.add(skill)
        return result


def make_edge_toolkit(original, journal, *, selected_skill: str = ""):
    """Filter and guard the native resolver, including tools activated later.

    Keep the original Skill viewer (including process-tool bindings), MCP
    clients, groups and permission wrappers. A copied resolver view leaves the
    cloud toolkit untouched and blocks delegation even after reset_tools.

    ``selected_skill`` narrows the rendered ``<agent-skills>`` catalog to the
    one Skill Route selected. The viewer keeps resolving through the original
    toolkit, so loading that Skill (including a shared-pool Skill) is unchanged.
    """
    toolkit = copy.copy(original)
    journal.edge_mode = True
    wrappers = {}

    async def available(_self, groups):
        tools = await original._get_available_tools(groups)
        result = {}
        for name, entry in tools.items():
            if name in FORBIDDEN_EDGE_TOOLS or entry.tool.name in FORBIDDEN_EDGE_TOOLS:
                continue
            key = (name, id(entry.tool))
            wrapper = wrappers.get(key)
            if wrapper is None:
                wrapper = journal.wrap(entry.tool, lower=True)
                wrappers[key] = wrapper
            registered = copy.copy(entry)
            registered.tool = wrapper
            result[name] = registered
        return result

    async def available_skills(_self, groups):
        skills = await original._get_available_skills(groups)
        if not selected_skill:
            return skills
        selected = skills.get(selected_skill)
        return {selected_skill: selected} if selected is not None else {}

    toolkit._get_available_tools = MethodType(available, toolkit)
    toolkit._get_available_skills = MethodType(available_skills, toolkit)
    return toolkit
