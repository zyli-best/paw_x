# -*- coding: utf-8 -*-
import json
import logging
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Union
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agentscope.message import Msg
from qwenpaw.agents.context.scroll.serialize import strip_headline
from qwenpaw.schemas import (
    Message,
    TextContent,
    ImageContent,
    AudioContent,
    VideoContent,
    FileContent,
    DataContent,
    FunctionCall,
    FunctionCallOutput,
    MessageType,
)
from qwenpaw.exceptions import (
    AgentRuntimeErrorException,
)

from ...config.context import describe_default_relative_file_dir
from ...config import load_config
from ...runtime.session_views import display_message_metadata
from ...config.shell_policy import (
    WINDOWS_NON_CODING_SHELL_GUIDANCE,
    WINDOWS_NON_CODING_TOOL_DISPATCH_GUIDANCE,
    requires_atomic_shell,
)

logger = logging.getLogger(__name__)


def parse_legacy_memory_state(
    memory_raw: dict,
) -> tuple[List[Msg], str]:
    """Parse a 1.x ``InMemoryMemory.state_dict()`` payload.

    1.x stored ``{"content": [[msg_dict, marks], ...],
    "_compressed_summary": str}``.  2.0 keeps messages on
    ``AgentState.context`` instead, so this helper exists only for
    sessions on disk that pre-date the migration.  ``marks`` are dropped
    (only ``HINT`` / ``COMPRESSED`` were used and neither is reachable
    from the new state schema).

    Returns ``(messages, summary)``; either may be empty.
    """
    messages: List[Msg] = []
    for item in memory_raw.get("content", []) or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            payload = item[0]
        else:
            payload = item
        if isinstance(payload, dict):
            messages.append(Msg.from_dict(payload))
        elif isinstance(payload, Msg):
            messages.append(payload)
    summary = memory_raw.get("_compressed_summary") or ""
    return messages, summary


def format_shell_redirect_hint(entries: list) -> str:
    """Render toolset-driven Windows shell-redirect guidance as item 4.

    Returns ``""`` when there is no Windows tool guidance to render.
    """
    if not entries:
        return ""
    clauses = "; ".join(
        f"use {name} for {purpose} instead of {'/'.join(alts)}"
        for name, purpose, alts in entries
    )
    return (
        "  4. On Windows, prefer built-in file tools over shell "
        f"commands: {clauses}.\n"
    )


def format_shell_atomic_hint() -> str:
    """Render item 5; the caller gates it on Windows Non-Coding visibility."""
    return (
        "  5. execute_shell_command: "
        f"{WINDOWS_NON_CODING_SHELL_GUIDANCE}\n"
        "     Use the displayed Default Shell and existing path permissions. "
        "Correct the reported cause before retrying; keep the user's "
        "confirmed target, never switch locations just to succeed.\n"
        "     OK: pytest tests/unit with cwd=D:/repo. "
        "BAD: cd repo && pytest; dir | findstr .py > out.txt; "
        "interpreter code hiding independent operations.\n"
    )


_TOOL_SELECTION_HINTS = {
    "glob_search": "Find files or directories by filename/path glob pattern",
    "grep_search": "Search text inside files",
    "read_file": "Read the contents of a known text file",
    "edit_file": "Replace specific text within an existing file",
    "append_file": "Append content to the end of a file",
    "write_file": "Create a file or replace its entire contents",
    "copy_file_or_dir": "Copy a file or directory",
    "move_file_or_dir": "Move or rename a file or directory",
    "create_directory": "Create a directory",
    "execute_shell_command": (
        "Run a program or an operation not covered by an available dedicated tool"
    ),
    "ast_search": "Find source code by syntax-tree pattern",
    "web_search": "Search the web for information and relevant URLs",
    "web_fetch": "Read text from a known public webpage URL",
    "browser_use": "Interact with webpages or inspect JavaScript-rendered pages",
    "desktop_screenshot": "Capture a screenshot of the desktop",
    "view_image": "Inspect an image from a local path or URL",
    "view_video": "Inspect a video from a local path or URL",
    "send_file_to_user": "Deliver an existing file to the user",
    "list_agents": "Discover configured agents and their capabilities",
    "get_agent_count": "Get only the total number of configured agents",
    "chat_with_agent": "Consult another configured agent and wait for its reply",
    "submit_to_agent": "Submit work to another configured agent in the background",
    "check_agent_task": "Check a background agent task by its task ID",
    "spawn_subagent": "Delegate a one-shot subtask under the current agent identity",
    "delegate_external_agent": "Manage or communicate with an external ACP agent",
    "list_skills": "Discover skills in the shared skill pool",
    "get_skill_count": "Get only the total number of shared pool skills",
    "materialize_skill": "Install and enable a confirmed new skill proposal",
    "get_current_time": "Read the current date and time",
    "set_user_timezone": "Change the configured user timezone",
    "get_token_usage": "Query token usage statistics",
    "get_historical_tool_result": (
        "Retrieve a compacted past tool result from this session by tool call ID"
    ),
    "get_background_tool_result": (
        "Check an offloaded tool call or collect its pending result by tool call ID"
    ),
}

_TOOL_SELECTION_GROUPS = {
    "Files and code": (
        "glob_search", "grep_search", "ast_search", "read_file", "edit_file",
        "append_file", "write_file", "copy_file_or_dir", "move_file_or_dir",
        "create_directory", "execute_shell_command",
    ),
    "Web and browser": ("web_search", "web_fetch", "browser_use"),
    "Media and file delivery": (
        "desktop_screenshot", "view_image", "view_video", "send_file_to_user",
    ),
    "Agent collaboration": (
        "list_agents", "get_agent_count", "chat_with_agent", "submit_to_agent",
        "check_agent_task", "spawn_subagent", "delegate_external_agent",
    ),
    "Skills": ("list_skills", "get_skill_count", "materialize_skill"),
    "Time and usage": (
        "get_current_time", "set_user_timezone", "get_token_usage",
    ),
    "Tool results": ("get_historical_tool_result", "get_background_tool_result"),
}


def format_tool_selection_hint(
    active_tool_names: Optional[Iterable[str]] = None,
) -> str:
    """Render selection hints only for tools in the final model toolset."""
    names = set(active_tool_names or ())
    sections = []
    for title, tools in _TOOL_SELECTION_GROUPS.items():
        lines = [
            f"- {_TOOL_SELECTION_HINTS[name]} → {name}"
            for name in tools
            if name in names
        ]
        if lines:
            sections.append(f"### {title}\n" + "\n".join(lines))
    if not sections:
        return ""
    return "## Tool Selection\n" + "\n\n".join(sections) + "\n\n"


def build_tool_usage_guidance(
    *,
    is_coding_mode: bool = False,
    shell_redirect_entries: Optional[list] = None,
    shell_command_visible: bool = False,
    active_tool_names: Optional[Iterable[str]] = None,
) -> str:
    """Build a separate tool-guidance section for all env-context callers."""
    atomic_hint = (
        format_shell_atomic_hint()
        if shell_command_visible and requires_atomic_shell(
            is_coding_mode=is_coding_mode,
        )
        else ""
    )
    dispatch_hint = (
        "## Windows Non-Coding Dispatch\n"
        + WINDOWS_NON_CODING_TOOL_DISPATCH_GUIDANCE
        + "\n\n"
        if requires_atomic_shell(is_coding_mode=is_coding_mode)
        else ""
    )
    return (
        "# Tool Usage Guidelines\n"
        f"{format_tool_selection_hint(active_tool_names)}"
        f"{dispatch_hint}"
        "## Usage Constraints\n"
        "  1. Prefer applicable skills for tool workflows (e.g. cron for "
        "scheduling); conversation/direct answers need no skill discovery.\n"
        "  2. To avoid overwriting existing content with write_file, first "
        "inspect with read_file, then use edit_file for partial updates "
        "or appending.\n"
        "  3. When tools are needed, execute through actual calls, not "
        "narration; direct answers need no tool calls. Complete execution "
        "tasks only after required work and validation; report waiting for user "
        "input/background results, without claiming completion or making "
        "unrelated calls.\n"
        f"{format_shell_redirect_hint(shell_redirect_entries or [])}"
        f"{atomic_hint}"
        "\n## Skill Workflow Execution\n"
        "When using a skill, follow applicable user/workspace requirements "
        "for selecting a creation skill.\n"
        "1. Before execution, use Skill to load complete instructions and "
        "current-turn parameters; names/descriptions alone do not suffice. "
        "Direct SKILL.md reading is allowed only under explicit workspace "
        "loading-fallback conditions and cannot recover missing dynamic "
        "parameters.\n"
        "2. Preserve required step order and branch conditions. Skip optional "
        "steps or parallelize only as the workflow permits; do not silently "
        "substitute methods, tools, validation, or deliverables.\n"
        "3. Fulfill prerequisites before dependent steps; required tool "
        "actions need actual results, not narration, as completion evidence.\n"
        "4. For missing inputs/tools, failures, or conflicts, use applicable "
        "skill recovery; otherwise report the specific blocker, without "
        "skipping required work or repeating unchanged failures.\n"
        "5. Verify all deliverables and acceptance criteria before completion; "
        "identify anything unfinished.\n"
        "6. Higher-priority instructions and explicit user requests prevail. "
        "Explain conflicts; skills do not expand user authorization to publish, "
        "send messages, or perform other actions.\n"
    )


def build_file_access_guidance(
    *,
    has_readonly_mounts: bool = False,
    materialize_skill_visible: bool = False,
    include_skill_creation: bool = True,
    append_file_visible: bool = True,
) -> str:
    """Render file rules; the caller preserves their original scope."""
    parts = []
    parts.append(
        "## New File and Directory Output Rules (Mandatory)\n\n"
        "  1. Scope: Mandatory in Non-Coding Mode for NEW files/directories, "
        "including "
        + (
            "write_file/append_file"
            if append_file_visible
            else "write_file"
        )
        + " creation, browser downloads/saved "
        "artifacts, and desktop_screenshot captures. Existing-file reads "
        "and edits are EXEMPT from creation/output restrictions.\n"
        "  2. Ordinary new output MUST use paths relative to the default "
        "output directory, without workspace, scratch, mount, or parent "
        "output-root prefixes. Example: pass report.html, not "
        "<output_root>/report.html.\n"
        "  3. Output priority: MUST prioritize external mounted output "
        "over session scratch; without an external mount, MUST use scratch.\n"
    )
    parts.append(
        "## Existing File and Directory Resolution Rules (Mandatory)\n\n"
        "  Applies in Non-Coding Mode to existing-file reads/edits and "
        "directory search, listing, inspection, or traversal.\n"
        "  1. Prefer the user's specified file/directory within permissions. "
        "An explicit absolute path skips discovery, NOT permission checks.\n"
        "  2. For unspecified/ambiguous locations, use the same discovery "
        "order for files and directories; search authorized locations only. "
        "Read-only locations remain valid read/search/list/inspection sources.\n"
        "  3. Search writable sandbox_mounts in configured order. "
        + (
            "Then read/execute-only sandbox_rx_mounts in configured order. "
            if has_readonly_mounts
            else ""
        )
        + "Then session scratch. Tools do not search all roots automatically: "
        "pass each selected root explicitly as an absolute `path`.\n"
        "  4. Relative paths use ONLY the tool's default root, normally "
        "the first writable external mount, else scratch. Finding a match "
        "elsewhere does NOT change this root. Use relative paths only under "
        "it; other authorized targets require absolute paths. Example: "
        "default D:/mounts -> reports/a.txt; other root -> E:/reference/a.txt.\n"
        "  5. Broaden to other allowed workspace/mount/scratch locations "
        "ONLY after no match.\n"
        "  6. Filename equality alone does not identify the intended file. "
        "For multiple plausible matches, ask which exact file before "
        "editing; do not edit, overwrite, delete, or otherwise modify "
        "multiple candidates.\n"
        + (
            "  7. sandbox_rx_mounts permissions always apply, including "
            "explicit absolute paths: read, search, list, inspect, traverse, "
            "and execute only. Use absolute search roots and read-file paths. "
            "Finding/reading grants no edit permission. No create/write/"
            "modify/delete except in explicitly listed sandbox_mounts "
            "writable subdirectories. Relative output uses the writable "
            "output root; '..' or links grant no writes to the remaining "
            "read-only area.\n"
            if has_readonly_mounts
            else ""
        )
    )
    if include_skill_creation:
        parts.append(
            "## Agent-Owned Skill Creation\n\n"
            "- Ownership: Create AND enable for the current agent's own use "
            "unless the user specifies another owner, destination, or purpose "
            "(including another agent/shared pool); follow that explicit target. "
            "Preserve user/workspace "
            "requirements for selecting/loading a creation skill.\n"
            "- Workspace: Use the executing agent's workspace, also for "
            "subagents; project, scratch, and mounts do not replace it.\n"
            "- Path exception: For agent-owned skills, use absolute "
            "<workspace_dir>/skills/<skill_name>/ paths for SKILL.md/support "
            "files and <workspace_dir>/skill.json "
            "for registration, NOT a manifest inside skills/. These targets "
            "are exceptions to ordinary relative-output rules.\n"
            + (
                "- Creation options: Use materialize_skill OR available file "
                "tools to create/register/enable; file tools need no "
                "materialize_skill attempt first.\n"
                if materialize_skill_visible
                else "- Creation method: Use available file tools to create, "
                "register, and enable the skill.\n"
            )
            + "- Direct creation: First inspect the target skill directory and "
            "read workspace skill.json; preserve other entries, configuration, "
            "and fields. For new skills, add entries under skills by directory "
            "name with "
            'enabled=true, channels=["all"], source="agent"; follow the existing '
            "schema and derive metadata from actual skill files. Overwrite existing "
            "skills ONLY for a user-requested update.\n"
            "- Missing skill.json: Do not assume creation permission. Use an "
            "available method supporting manifest initialization under actual "
            "tool permissions; if none exists, report incomplete registration.\n"
            "- Completion: Verify SKILL.md/support files exist and the matching "
            "manifest entry has enabled=true; SKILL.md alone is incomplete.\n"
        )
    return "# File Access and Output Rules\n\n" + "\n\n".join(parts)


def build_env_context(
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    channel: Optional[str] = None,
    working_dir: Optional[str] = None,
    add_hint: bool = True,
    default_shell: Optional[str] = None,
    project_dir: Optional[str] = None,
    active_model_name: Optional[str] = None,
    sandbox_mounts: Optional[list[str]] = None,
    sandbox_rx_mounts: Optional[list[str]] = None,
    is_coding_mode: bool = False,
    shell_redirect_entries: Optional[list] = None,
    shell_command_visible: bool = False,
    active_tool_names: Optional[Iterable[str]] = None,
    runtime_environments: Optional[dict[str, dict[str, str]]] = None,
    include_skill_creation: bool = True,
    append_file_visible: bool = True,
) -> str:
    """
    Build environment context with current request context prepended.

    Args:
        session_id: Current session ID
        user_id: Current user ID
        user_name: Optional human-readable sender name (e.g. IM nickname).
            Only rendered when provided by the channel via channel_meta.
        channel: Current channel name
        working_dir: Working directory path
        add_hint: Whether to append the separate tool usage guidance section.
        default_shell: Shell executable used by execute_shell_command.
            When provided, included in the context so the LLM can
            generate syntax appropriate for that shell.
        project_dir: When set (Coding Mode), the agent's "Working
            directory" line is replaced with an explicit
            "Project directory" + "Agent workspace (internal)" pair
            so the LLM stops treating the workspace as home.
        active_model_name: Current active model name for runtime
            identity (e.g. "qwen-max", "gpt-4o").
        sandbox_mounts: Request-scoped external writable directories.
        sandbox_rx_mounts: Request-scoped external read/execute-only
            directories (readable and executable, NOT writable).
        is_coding_mode: Whether this request is operating in Coding Mode.
        shell_redirect_entries: Toolset-driven Windows shell-redirect
            entries ``(name, purpose, alternatives)`` rendered as the
            fourth tool usage guideline; empty/None renders nothing.
        shell_command_visible: Whether ``execute_shell_command`` is in the
            request's final tool surface (i.e. the model can actually see
            it). Only when this is true AND the request is NOT in Coding
            Mode is the atomic-command constraint rendered as the fifth
            tool usage guideline.
        active_tool_names: Final tools exposed to this model. Missing or empty
            means no tool selection hints are rendered.
        runtime_environments: Confirmed external Python and Node.js runtimes,
            keyed by ``python``/``nodejs`` with executable and directory paths.
            Missing or empty means no runtime paths are rendered.
        include_skill_creation: Whether to render the Agent-Owned Skill
            Creation rules. Edge execution disables them because it may not
            create or register Skills.
        append_file_visible: Whether ``append_file`` is exposed to this
            model. Controls whether the file-creation rule names it.

    Returns:
        Formatted environment context string
    """
    parts = []

    # Runtime identity
    powered = f", powered by {active_model_name}" if active_model_name else ""
    parts.append(
        f"- About: You are a personal AI assistant{powered}. "
        f"You operate in Devco, an open-source agent "
        f"framework built by aipc team from ilab.",
    )
    # Mode indicator — explicit mode context for the LLM
    if is_coding_mode:
        parts.append("- Operating Mode: Coding Mode")
    else:
        parts.append("- Operating Mode: Non-Coding Mode")
    user_tz = load_config().user_timezone or "UTC"
    try:
        now = datetime.now(ZoneInfo(user_tz))
    except (ZoneInfoNotFoundError, KeyError):
        logger.warning("Invalid timezone %r, falling back to UTC", user_tz)
        now = datetime.now(timezone.utc)
        user_tz = "UTC"

    if session_id is not None:
        parts.append(f"- Session ID: {session_id}")
    if user_id is not None:
        parts.append(f"- User ID: {user_id}")
    if user_name:
        parts.append(f"- User Name: {user_name}")
    if channel is not None:
        parts.append(f"- Channel: {channel}")

    parts.append(
        f"- OS: {platform.system()} {platform.release()} "
        f"({platform.machine()})",
    )

    if default_shell:
        parts.append(f"- Default Shell: {default_shell}")

    for kind, label in (
        ("python", "Python（非虚拟环境）"),
        ("nodejs", "Node.js"),
    ):
        runtime_info = (runtime_environments or {}).get(kind)
        if not runtime_info:
            continue
        if runtime_info.get("source") in {"bundled", "bundled_venv"}:
            continue
        if runtime_info.get("executable"):
            parts.append(f"- {label} 可执行文件：{runtime_info['executable']}")
        if runtime_info.get("directory"):
            parts.append(f"- {label} 所在目录：{runtime_info['directory']}")

    from ...utils.bundled_runtime import render_runtime_instructions

    runtime_instructions = render_runtime_instructions(
        runtime_environments or {}, default_shell,
    )
    if runtime_instructions:
        parts.append(runtime_instructions)

    if project_dir:
        parts.append(
            f"- Project directory (Coding Mode — operate here): "
            f"{project_dir}",
        )
        if working_dir is not None and str(working_dir) != str(project_dir):
            parts.append(
                f"- Agent workspace (internal — do NOT touch unless "
                f"the user explicitly asks): {working_dir}",
            )
    elif working_dir is not None:
        parts.append(f"- Working directory: {working_dir}")
    if working_dir is not None:
        try:
            default_cwd = describe_default_relative_file_dir(
                workspace_dir=Path(working_dir),
                session_id=session_id,
                is_coding_mode=is_coding_mode,
                sandbox_mounts=sandbox_mounts,
            )
            parts.append(
                "- execute_shell_command default cwd when cwd is omitted: "
                f"{default_cwd.path}",
            )
            parts.append(
                "- execute_shell_command default cwd source: "
                f"{default_cwd.source}",
            )
            if project_dir:
                parts.append(
                    "- execute_shell_command cwd guidance: pass cwd equal "
                    "to the active project directory for commands that "
                    "touch project files.",
                )
        except Exception:
            logger.debug(
                "Failed to derive execute_shell_command default cwd",
                exc_info=True,
            )
    if not is_coding_mode and working_dir is not None:
        session_scratch_dir = None
        if session_id:
            try:
                from .session import sanitize_filename

                safe_sid = (
                    sanitize_filename(str(session_id).strip()) or "default"
                )
                session_scratch_dir = str(
                    Path(working_dir) / "session_tmp" / safe_sid,
                )
                Path(session_scratch_dir).mkdir(
                    parents=True,
                    exist_ok=True,
                )
            except Exception:
                logger.debug(
                    "Failed to derive session scratch dir",
                    exc_info=True,
                )
        if session_scratch_dir:
            parts.append(
                f"- Session scratch directory: {session_scratch_dir}",
            )
        mounts = [
            mount
            for mount in (sandbox_mounts or [])
            if isinstance(mount, str) and mount.strip()
        ]
        if mounts:
            parts.append(
                "- External mounted directories: " + "; ".join(mounts),
            )
        else:
            parts.append("- External mounted directories: none")
        rx_mounts = [
            mount
            for mount in (sandbox_rx_mounts or [])
            if isinstance(mount, str) and mount.strip()
        ]
        if rx_mounts:
            parts.append(
                "- Read/execute-only external directories "
                "(readable and executable, NOT writable except for "
                "explicit sandbox_mounts writable subdirectories): "
                + "; ".join(rx_mounts),
            )
    parts.append(
        f"- Current date: {now.strftime('%Y-%m-%d')} "
        f"{user_tz} ({now.strftime('%A')})",
    )

    env_context = (
        "====================\n" + "\n".join(parts) + "\n===================="
    )
    if not is_coding_mode and working_dir is not None:
        env_context += "\n\n" + build_file_access_guidance(
            has_readonly_mounts=bool(rx_mounts),
            materialize_skill_visible=(
                "materialize_skill" in (active_tool_names or ())
            ),
            include_skill_creation=include_skill_creation,
            append_file_visible=append_file_visible,
        )
    if not add_hint:
        return env_context
    return env_context + "\n\n" + build_tool_usage_guidance(
        is_coding_mode=is_coding_mode,
        shell_redirect_entries=shell_redirect_entries,
        shell_command_visible=shell_command_visible,
        active_tool_names=active_tool_names,
    )


def _is_local_file_url(url: str) -> bool:
    """True if url is a local file reference (file:// or absolute path)."""
    if not url or not isinstance(url, str):
        return False
    s = url.strip()
    if not s:
        return False
    lower = s.lower()

    # Check for remote URLs
    if lower.startswith(("http://", "https://", "data:")):
        return False

    # Check for local file patterns: file://, Unix paths, or Windows drives
    return (
        lower.startswith("file:")
        or (s.startswith("/") and not s.startswith("//"))
        or (len(s) >= 2 and s[1] == ":" and s[0].isalpha())
    )


def _abspath_from_url(url: str) -> str:
    """Extract absolute path from file:// URL.

    Percent-decodes the path so non-ASCII filenames resolve correctly.
    """
    s = url.strip()
    if s.lower().startswith("file:"):
        s = s[5:]
    s = "/" + s.lstrip("/")
    return unquote(s)


def _resolve_content_url(url: str) -> str:
    """If url is local, return filename only; frontend builds URL."""
    if not isinstance(url, str):
        return url
    if not _is_local_file_url(url):
        return url
    return _abspath_from_url(url)


# pylint: disable=too-many-branches,too-many-statements, too-many-nested-blocks
def _build_media_message_from_block(
    block: dict,
    role: str,
    metadata: dict,
) -> Message:
    output = block.get("output")
    media_message = None
    if isinstance(output, list):

        def _resolve_media_type(item):
            if not isinstance(item, dict):
                return None
            t = item.get("type")
            if t in ("image", "audio", "video", "file"):
                return t
            if t == "data":
                src = item.get("source") or {}
                mt = src.get("media_type", "") if isinstance(src, dict) else ""
                for prefix in ("image", "audio", "video"):
                    if mt.startswith(f"{prefix}/"):
                        return prefix
                return "file"
            return None

        media_items = [
            item for item in output if _resolve_media_type(item) is not None
        ]
        if media_items:
            media_message = Message(
                type=MessageType.MESSAGE,
                role=role,
            )
            media_message.metadata = metadata

            for item in media_items:
                itype = _resolve_media_type(item)

                if itype == "image":
                    kwargs = {}
                    source = item.get("source")
                    if (
                        isinstance(source, dict)
                        and source.get("type") == "url"
                    ):
                        kwargs["image_url"] = _resolve_content_url(
                            source.get("url", ""),
                        )
                    elif (
                        isinstance(source, dict)
                        and source.get("type") == "base64"
                    ):
                        media_type = source.get(
                            "media_type",
                            "image/jpeg",
                        )
                        base64_data = source.get("data", "")
                        kwargs[
                            "image_url"
                        ] = f"data:{media_type};base64,{base64_data}"
                    media_message.add_content(
                        new_content=ImageContent(
                            delta=False,
                            index=None,
                            **kwargs,
                        ),
                    )

                elif itype == "audio":
                    kwargs = {}
                    source = item.get("source")
                    if (
                        isinstance(source, dict)
                        and source.get("type") == "url"
                    ):
                        url = _resolve_content_url(
                            source.get("url", ""),
                        )
                        kwargs["data"] = url
                        try:
                            kwargs["format"] = urlparse(
                                url,
                            ).path.split(
                                ".",
                            )[-1]
                        except (
                            AttributeError,
                            IndexError,
                            ValueError,
                        ):
                            kwargs["format"] = None
                    elif (
                        isinstance(source, dict)
                        and source.get("type") == "base64"
                    ):
                        media_type = source.get("media_type")
                        base64_data = source.get("data", "")
                        kwargs[
                            "data"
                        ] = f"data:{media_type};base64,{base64_data}"
                        kwargs["format"] = media_type
                    media_message.add_content(
                        new_content=AudioContent(
                            delta=False,
                            index=None,
                            **kwargs,
                        ),
                    )

                elif itype == "video":
                    kwargs = {}
                    source = item.get("source")
                    if (
                        isinstance(source, dict)
                        and source.get("type") == "url"
                    ):
                        kwargs["video_url"] = _resolve_content_url(
                            source.get("url", ""),
                        )
                    elif (
                        isinstance(source, dict)
                        and source.get("type") == "base64"
                    ):
                        media_type = source.get(
                            "media_type",
                            "video/mp4",
                        )
                        base64_data = source.get("data", "")
                        kwargs[
                            "video_url"
                        ] = f"data:{media_type};base64,{base64_data}"
                    media_message.add_content(
                        new_content=VideoContent(
                            delta=False,
                            index=None,
                            **kwargs,
                        ),
                    )

                elif itype == "file":
                    kwargs = {"filename": item.get("filename", "")}
                    source = item.get("source")
                    if (
                        isinstance(source, dict)
                        and source.get("type") == "url"
                    ):
                        kwargs["file_url"] = _resolve_content_url(
                            source.get("url", ""),
                        )
                    elif (
                        isinstance(source, dict)
                        and source.get("type") == "base64"
                    ):
                        media_type = source.get(
                            "media_type",
                            "application/octet-stream",
                        )
                        base64_data = source.get("data", "")
                        kwargs[
                            "file_url"
                        ] = f"data:{media_type};base64,{base64_data}"
                    elif isinstance(source, str):
                        kwargs["file_url"] = _resolve_content_url(
                            source,
                        )
                    media_message.add_content(
                        new_content=FileContent(
                            delta=False,
                            index=None,
                            **kwargs,
                        ),
                    )
    return media_message


# Matches the trailing <skill> block appended to a user message by
# slash-command skill expansion (runner._maybe_inject_skill).
_INJECTED_SKILL_BLOCK_RE = re.compile(
    r"\s*<skill\b[^>]*>.*</skill>\s*$",
    re.DOTALL,
)


def strip_injected_skill_block(text: str, role: str) -> str:
    """Hide the system-injected <skill> block from display.

    Slash-command skill expansion keeps the user's typed text at the
    head of the message and appends the skill body in a trailing
    <skill> block. The block is model-facing context; transcripts
    should show only what the user typed.
    """
    if role != "user" or "<skill" not in text:
        return text
    return _INJECTED_SKILL_BLOCK_RE.sub("", text)


def clean_display_text(text: str, role: str) -> str:
    """Hide model-facing artifacts from the transcript: the ``⟦ … ⟧``
    headline fence and the injected ``<skill>`` block. The SSE stream already
    strips the headline; the HTTP history path didn't, so it reappeared on
    reload — do both here so every display path matches. Headline first: the
    ``<skill>`` regex is ``$``-anchored, so a trailing headline would leave it
    un-anchored.
    """
    return strip_injected_skill_block(strip_headline(text) or "", role)


# pylint: disable=too-many-branches,too-many-statements, too-many-nested-blocks
def agentscope_msg_to_message(
    messages: Union[Msg, List[Msg]],
) -> List[Message]:
    """
    Convert AgentScope Msg(s) into one or more runtime Message objects.

    Args:
        messages: AgentScope message(s) from streaming.

    Returns:
        List[Message]: One or more constructed runtime Message objects.
    """
    if isinstance(messages, Msg):
        msgs = [messages]
    elif isinstance(messages, list):
        msgs = messages
    else:
        raise AgentRuntimeErrorException(
            code="INVALID_MESSAGE_TYPE",
            message=(
                f"Expected Msg or list[Msg], got {type(messages).__name__}"
            ),
        )

    results: List[Message] = []

    user_tz_name = load_config().user_timezone or "UTC"
    try:
        user_tz = ZoneInfo(user_tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        user_tz = timezone.utc

    for msg in msgs:
        role = msg.role or "assistant"

        ts_value = msg.timestamp
        if ts_value:
            try:
                dt_obj = datetime.strptime(ts_value, "%Y-%m-%d %H:%M:%S.%f")
                ts_value = dt_obj.replace(tzinfo=user_tz).isoformat()
            except ValueError:
                pass

        metadata = {
            "original_id": msg.id,
            "original_name": msg.name,
            "metadata": display_message_metadata(msg.metadata),
            "timestamp": ts_value,
        }

        if isinstance(msg.content, str):
            message = Message(type=MessageType.MESSAGE, role=role)
            message.metadata = metadata
            text_content = TextContent(
                delta=False,
                index=None,
                text=clean_display_text(msg.content, role),
            )
            message.add_content(new_content=text_content)
            results.append(message)
            continue

        current_message = None
        current_type = None

        for block in msg.content:
            # Normalize pydantic block models to dict so the rest of
            # this conversion (which uses .get) handles both shapes.
            if hasattr(block, "model_dump"):
                block = block.model_dump()
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "text")

            # DataBlock (2.0): map type="data" to concrete media type
            if btype == "data":
                source = block.get("source") or {}
                mt = (
                    source.get("media_type", "")
                    if isinstance(source, dict)
                    else ""
                )
                if mt.startswith("image/"):
                    btype = "image"
                elif mt.startswith("audio/"):
                    btype = "audio"
                elif mt.startswith("video/"):
                    btype = "video"
                else:
                    btype = "file"

            if btype == "text":
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(current_message.completed())
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                text_content = TextContent(
                    delta=False,
                    index=None,
                    text=clean_display_text(
                        block.get("text", ""),
                        role,
                    ),
                )
                current_message.add_content(new_content=text_content)

            elif btype == "thinking":
                if current_type != MessageType.REASONING:
                    if current_message:
                        results.append(current_message.completed())
                    current_message = Message(
                        type=MessageType.REASONING,
                        role=role,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.REASONING

                text_content = TextContent(
                    delta=False,
                    index=None,
                    text=block.get("thinking", ""),
                )
                current_message.add_content(new_content=text_content)

            elif btype in ("tool_use", "tool_call"):
                if current_message:
                    results.append(current_message.completed())

                current_message = Message(
                    type=MessageType.PLUGIN_CALL,
                    role=role,
                )
                current_message.metadata = metadata
                current_type = MessageType.PLUGIN_CALL

                if isinstance(block.get("input"), (dict, list)):
                    arguments = json.dumps(
                        block.get("input"),
                        ensure_ascii=False,
                    )
                else:
                    arguments = block.get("input")

                call_data = FunctionCall(
                    call_id=block.get("id"),
                    name=block.get("name"),
                    arguments=arguments,
                ).model_dump()

                data_content = DataContent(
                    delta=False,
                    index=None,
                    data=call_data,
                )
                current_message.add_content(new_content=data_content)

            elif btype == "tool_result":
                if current_message:
                    results.append(current_message.completed())

                current_message = Message(
                    type=MessageType.PLUGIN_CALL_OUTPUT,
                    role=role,
                )
                current_message.metadata = metadata
                current_type = MessageType.PLUGIN_CALL_OUTPUT

                if isinstance(block.get("output"), (dict, list)):
                    output = json.dumps(
                        block.get("output"),
                        ensure_ascii=False,
                    )
                else:
                    output = block.get("output")

                output_data = FunctionCallOutput(
                    call_id=block.get("id"),
                    name=block.get("name"),
                    output=output,
                ).model_dump(exclude_none=True)

                tool_state = block.get("state")
                if hasattr(tool_state, "value"):
                    tool_state = tool_state.value
                if tool_state is not None:
                    output_data["state"] = tool_state

                data_content = DataContent(
                    delta=False,
                    index=None,
                    data=output_data,
                )
                current_message.add_content(new_content=data_content)

            elif btype == "image":
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(current_message.completed())
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                kwargs = {}
                if (
                    isinstance(block.get("source"), dict)
                    and block.get("source", {}).get("type") == "url"
                ):
                    url = block.get("source", {}).get("url")
                    url = _resolve_content_url(url)
                    kwargs["image_url"] = url

                elif (
                    isinstance(block.get("source"), dict)
                    and block.get("source").get("type") == "base64"
                ):
                    media_type = block.get("source", {}).get(
                        "media_type",
                        "image/jpeg",
                    )
                    base64_data = block.get("source", {}).get("data", "")
                    url = f"data:{media_type};base64,{base64_data}"
                    kwargs["image_url"] = url

                image_content = ImageContent(
                    delta=False,
                    index=None,
                    **kwargs,
                )
                current_message.add_content(new_content=image_content)

            elif btype == "audio":
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(current_message.completed())
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                kwargs = {}
                if (
                    isinstance(block.get("source"), dict)
                    and block.get("source", {}).get("type") == "url"
                ):
                    url = block.get("source", {}).get("url")
                    url = _resolve_content_url(url)
                    kwargs["data"] = url
                    try:
                        kwargs["format"] = urlparse(url).path.split(".")[-1]
                    except (AttributeError, IndexError, ValueError):
                        kwargs["format"] = None

                elif (
                    isinstance(block.get("source"), dict)
                    and block.get("source").get("type") == "base64"
                ):
                    media_type = block.get("source", {}).get("media_type")
                    base64_data = block.get("source", {}).get("data", "")
                    url = f"data:{media_type};base64,{base64_data}"
                    kwargs["data"] = url
                    kwargs["format"] = media_type

                audio_content = AudioContent(
                    delta=False,
                    index=None,
                    **kwargs,
                )
                current_message.add_content(new_content=audio_content)

            elif btype == "video":
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(current_message.completed())
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                kwargs = {}
                if (
                    isinstance(block.get("source"), dict)
                    and block.get("source", {}).get("type") == "url"
                ):
                    url = block.get("source", {}).get("url")
                    url = _resolve_content_url(url)
                    kwargs["video_url"] = url

                elif (
                    isinstance(block.get("source"), dict)
                    and block.get("source").get("type") == "base64"
                ):
                    media_type = block.get("source", {}).get(
                        "media_type",
                        "video/mp4",
                    )
                    base64_data = block.get("source", {}).get("data", "")
                    url = f"data:{media_type};base64,{base64_data}"
                    kwargs["video_url"] = url

                video_content = VideoContent(
                    delta=False,
                    index=None,
                    **kwargs,
                )
                current_message.add_content(new_content=video_content)

            elif btype == "file":
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(current_message.completed())
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                kwargs = {
                    "filename": block.get("filename"),
                }
                if (msg.metadata or {}).get("final_files_v1"):
                    kwargs["filename"] = block.get("name") or kwargs["filename"]
                if (
                    isinstance(block.get("source"), dict)
                    and block.get("source", {}).get("type") == "url"
                ):
                    url = block.get("source", {}).get("url")
                    url = _resolve_content_url(url)
                    kwargs["file_url"] = url

                elif (
                    isinstance(block.get("source"), dict)
                    and block.get("source").get("type") == "base64"
                ):
                    media_type = block.get("source", {}).get(
                        "media_type",
                        "application/octet-stream",
                    )
                    base64_data = block.get("source", {}).get("data", "")
                    url = f"data:{media_type};base64,{base64_data}"
                    kwargs["file_url"] = url
                elif isinstance(block.get("source"), str):
                    url = _resolve_content_url(block.get("source", ""))
                    kwargs["file_url"] = url

                file_content = FileContent(
                    delta=False,
                    index=None,
                    **kwargs,
                )
                current_message.add_content(new_content=file_content)

            else:
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(current_message.completed())
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                text_content = TextContent(
                    delta=False,
                    index=None,
                    text=str(block),
                )
                current_message.add_content(new_content=text_content)

        if current_message:
            results.append(current_message.completed())

    return results
