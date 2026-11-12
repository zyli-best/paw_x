# -*- coding: utf-8 -*-
"""Path guards for file discovery and content-search tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from ...config.context import (
    get_current_workspace_dir,
    get_default_relative_file_dir,
    get_session_scoped_work_dir,
)
from ...constant import WORKING_DIR
from .existing_path_guard import (
    SuspectedRelativePrefix,
    detect_suspected_relative_root_prefix,
    is_absolute_path,
)
from .output_path_guard import is_non_coding_output_mode


@dataclass(frozen=True)
class ResolvedGlobSearch:
    root: Path
    pattern: str


def _error(text: str, *, denied: bool = False) -> ToolChunk:
    return ToolChunk(
        is_last=True,
        state=(
            ToolResultState.DENIED if denied else ToolResultState.ERROR
        ),
        content=[TextBlock(type="text", text=text)],
    )


def _missing_relative_search_path_error(
    *,
    requested_path: str,
    resolved_path: Path,
    tool_name: str,
) -> ToolChunk:
    return _error(
        "Error [SEARCH_PATH_NOT_FOUND]: The requested search path does not "
        "exist.\n\n"
        f"Requested relative path:\n- {requested_path or '(default)'}\n\n"
        f"Resolved Non-Coding search path:\n- {resolved_path}\n\n"
        "Resolution rule:\n"
        "- Relative search paths use the first external mounted directory "
        "when one is available.\n"
        "- Otherwise, they use the current session scratch directory.\n\n"
        "Next action:\n"
        "- Carefully verify whether the relative search path is correct.\n"
        "- Do not guess or invent another path.\n"
        "- If the search location is elsewhere, retry "
        f"`{tool_name}` with its exact absolute path in the `path` argument."
    )


def _missing_relative_search_path_with_prefix_error(
    *,
    requested_path: str,
    resolved_path: Path,
    tool_name: str,
    suspected_prefix: SuspectedRelativePrefix,
) -> ToolChunk:
    return _error(
        "Error [SEARCH_PATH_NOT_FOUND_POSSIBLE_DUPLICATED_PREFIX]: The "
        "requested search path does not exist, and the relative path may "
        "contain a duplicated root-directory prefix.\n\n"
        f"Requested relative path:\n- {requested_path}\n\n"
        f"Current default search root:\n- {suspected_prefix.default_root}\n\n"
        f"Resolved path:\n- {resolved_path}\n\n"
        "Detected possible repeated prefix:\n"
        f"- {suspected_prefix.matched_prefix}\n\n"
        "Possible corrected relative path:\n"
        f"- {suspected_prefix.suggested_relative_path}\n\n"
        "Next action:\n"
        "1. Confirm whether the detected prefix was accidentally included "
        "even though the tool already resolves relative paths from the "
        "current default search root.\n"
        "2. Do not automatically assume the suggested correction is valid.\n"
        f"3. If confirmed, retry `{tool_name}` with:\n"
        f"   - `path`: {suspected_prefix.suggested_relative_path}\n"
        "4. Otherwise, retry with the exact absolute path in the `path` "
        "argument."
    )


def _missing_absolute_or_coding_search_path_error(
    *,
    requested_path: str,
    resolved_path: Path,
) -> ToolChunk:
    return _error(
        "Error [SEARCH_PATH_NOT_FOUND]: The requested search path does not "
        "exist.\n\n"
        f"Requested path:\n- {requested_path or '(default)'}\n\n"
        f"Resolved path:\n- {resolved_path}\n\n"
        "Next action:\n"
        "- Carefully verify the search path before retrying.\n"
        "- Do not guess or invent another path."
    )


def _missing_search_path_error(
    *,
    requested_path: str,
    resolved_path: Path,
    was_absolute: bool,
    tool_name: str,
) -> ToolChunk:
    if is_non_coding_output_mode() and not was_absolute:
        suspected_prefix = None
        if requested_path:
            suspected_prefix = detect_suspected_relative_root_prefix(
                requested_path,
            )
        if suspected_prefix is not None:
            return _missing_relative_search_path_with_prefix_error(
                requested_path=requested_path,
                resolved_path=resolved_path,
                tool_name=tool_name,
                suspected_prefix=suspected_prefix,
            )
        return _missing_relative_search_path_error(
            requested_path=requested_path,
            resolved_path=resolved_path,
            tool_name=tool_name,
        )

    return _missing_absolute_or_coding_search_path_error(
        requested_path=requested_path,
        resolved_path=resolved_path,
    )


def resolve_existing_search_path(
    path: str | None,
    *,
    tool_name: str,
    require_directory: bool,
) -> Path | ToolChunk:
    """Resolve one existing file or directory used as a search root."""

    requested_path = (path or "").strip()
    used_default_root = not requested_path
    was_absolute = bool(requested_path) and is_absolute_path(requested_path)

    if used_default_root:
        resolved = Path(get_default_relative_file_dir())
    elif was_absolute:
        resolved = Path(requested_path).expanduser()
    else:
        resolved = Path(get_default_relative_file_dir()) / requested_path

    try:
        exists = resolved.exists()
    except OSError as exc:
        return _error(
            "Error: The requested search path could not be accessed.\n\n"
            f"Requested path:\n- {requested_path or '(default)'}\n\n"
            f"Resolved path:\n- {resolved}\n\n"
            f"Reason:\n- {exc}"
        )

    if not exists:
        return _missing_search_path_error(
            requested_path=requested_path,
            resolved_path=resolved,
            was_absolute=was_absolute,
            tool_name=tool_name,
        )

    try:
        if require_directory:
            valid_type = resolved.is_dir()
        else:
            valid_type = resolved.is_file() or resolved.is_dir()
    except OSError as exc:
        return _error(
            "Error: The resolved search path could not be inspected.\n\n"
            f"Resolved path:\n- {resolved}\n\n"
            f"Reason:\n- {exc}"
        )

    if not valid_type:
        expected = "a directory" if require_directory else "a file or directory"
        return _error(
            f"Error: The resolved search path is not {expected}.\n\n"
            f"Resolved path:\n- {resolved}"
        )

    return resolved


def _normalized_pattern_parts(pattern: str) -> list[str]:
    return pattern.replace("\\", "/").split("/")


def _current_session_pattern_prefix() -> tuple[str, Path] | None:
    """Return the current scratch path relative to workspace, if distinct."""

    workspace = Path(get_current_workspace_dir() or WORKING_DIR).expanduser()
    session = get_session_scoped_work_dir(workspace_dir=workspace)
    try:
        relative = session.resolve(strict=False).relative_to(
            workspace.resolve(strict=False),
        )
    except ValueError:
        return None
    prefix = str(relative).replace("\\", "/").strip("/")
    if not prefix or prefix == ".":
        return None
    return prefix, session


def _glob_root_prefix_error(pattern: str) -> ToolChunk | None:
    normalized = pattern.replace("\\", "/").lstrip("./")
    current = _current_session_pattern_prefix()
    if current is None:
        return None
    prefix, session_root = current
    if normalized != prefix and not normalized.startswith(prefix + "/"):
        return None

    remainder = normalized[len(prefix) :].lstrip("/") or "*"
    recursive_hint = (
        "\nTo include both files and directories recursively, use "
        "`pattern`: `**/*`."
        if remainder == "**"
        else ""
    )
    return _error(
        "Error [GLOB_PATTERN_POSSIBLE_DUPLICATED_ROOT_PREFIX]: The glob "
        "pattern appears to include the current session scratch root.\n\n"
        f"Provided pattern:\n- {pattern}\n\n"
        f"Detected root prefix:\n- {prefix}\n\n"
        f"Resolved search root:\n- {session_root}\n\n"
        f"Relative glob pattern:\n- {remainder}\n\n"
        "Next action:\nRetry `glob_search` with:\n"
        f"- `path`: {session_root}\n"
        f"- `pattern`: {remainder}"
        f"{recursive_hint}"
    )


def glob_no_match_prefix_error(
    *,
    pattern: str,
    path: str | None,
    search_root: Path,
) -> ToolChunk | None:
    """Diagnose a likely duplicated root prefix after a no-match result."""

    if path or not is_non_coding_output_mode():
        return None
    suspected = detect_suspected_relative_root_prefix(pattern)
    if suspected is None:
        return None

    return _error(
        "Error [GLOB_NO_MATCH_POSSIBLE_DUPLICATED_PREFIX]: No files matched, "
        "and the glob pattern may contain a duplicated search-root prefix."
        "\n\n"
        f"Provided pattern:\n- {pattern}\n\n"
        f"Current search root:\n- {search_root}\n\n"
        "Detected possible repeated prefix:\n"
        f"- {suspected.matched_prefix}\n\n"
        "Possible corrected glob pattern:\n"
        f"- {suspected.suggested_relative_path}\n\n"
        "Next action:\n"
        "1. Confirm whether the detected prefix was accidentally included "
        "even though `glob_search` already searches from the current root.\n"
        "2. Do not automatically assume the suggested correction is valid.\n"
        "3. If confirmed, retry `glob_search` with:\n"
        f"   - `pattern`: {suspected.suggested_relative_path}\n"
        "4. Otherwise, provide the exact absolute search directory in "
        "`path`."
    )


def resolve_glob_search(
    *,
    pattern: str,
    path: str | None,
) -> ResolvedGlobSearch | ToolChunk:
    """Validate a glob pattern and resolve its existing directory root."""

    normalized_pattern = (pattern or "").strip()
    if not normalized_pattern:
        return _error("Error: No glob `pattern` provided.")

    if ".." in _normalized_pattern_parts(normalized_pattern):
        return _error(
            "Error: Glob patterns must remain relative to the resolved "
            "search root. Parent-directory traversal (`..`) is not allowed. "
            "Use the exact absolute directory in the `path` argument "
            "instead.",
            denied=True,
        )

    if is_absolute_path(normalized_pattern):
        return _error(
            "Error: An absolute path was provided in the glob `pattern`.\n\n"
            "`glob_search` separates the search root from the glob pattern. "
            "Put the exact absolute directory in `path` and keep only the "
            "relative glob expression in `pattern`.\n\n"
            f"Provided pattern:\n- {pattern}"
        )

    prefix_error = _glob_root_prefix_error(normalized_pattern)
    if prefix_error is not None:
        return prefix_error

    root = resolve_existing_search_path(
        path,
        tool_name="glob_search",
        require_directory=True,
    )
    if isinstance(root, ToolChunk):
        return root

    return ResolvedGlobSearch(root=root, pattern=normalized_pattern)
