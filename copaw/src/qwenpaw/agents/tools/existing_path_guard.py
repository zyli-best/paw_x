# -*- coding: utf-8 -*-
"""Shared guards for resolving existing file paths."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from ...config.context import (
    get_current_workspace_dir,
    get_default_relative_file_dir,
    get_request_sandbox_mounts,
    get_session_scoped_work_dir,
)
from ...constant import WORKING_DIR
from .output_path_guard import is_non_coding_output_mode


_WIN_ABS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class SuspectedRelativePrefix:
    matched_prefix: str
    suggested_relative_path: str
    default_root: Path


def is_absolute_path(path: str) -> bool:
    """Recognize native absolute paths, Windows drive paths, and UNC paths."""

    raw = (path or "").strip()
    if not raw:
        return False
    return (
        Path(raw).expanduser().is_absolute()
        or bool(_WIN_ABS_PATH_RE.match(raw))
        or raw.startswith("\\\\")
    )


# Backward-compatible private alias used by existing tests and callers.
_is_absolute_path = is_absolute_path


def _error(text: str) -> ToolChunk:
    return ToolChunk(
        is_last=True,
        state=ToolResultState.ERROR,
        content=[TextBlock(type="text", text=text)],
    )


def _normalized_relative_parts(path: str | Path) -> list[str]:
    normalized = str(path).replace("\\", "/").strip("/")
    return [
        part
        for part in normalized.split("/")
        if part and part != "."
    ]


def _relative_prefix_candidates() -> list[str]:
    workspace = Path(
        get_current_workspace_dir() or WORKING_DIR,
    ).expanduser()
    default_root = Path(get_default_relative_file_dir()).expanduser()
    session_root = get_session_scoped_work_dir(workspace_dir=workspace)
    mounts = get_request_sandbox_mounts()

    candidates: set[str] = set()
    for root in [default_root, session_root, workspace, *mounts]:
        root = Path(root).expanduser()
        if root.name:
            candidates.add(root.name)

    try:
        session_relative = session_root.resolve(strict=False).relative_to(
            workspace.resolve(strict=False),
        )
        session_relative_text = str(session_relative).replace("\\", "/")
        if session_relative_text and session_relative_text != ".":
            candidates.add(session_relative_text)
    except ValueError:
        pass

    return sorted(
        candidates,
        key=lambda value: len(_normalized_relative_parts(value)),
        reverse=True,
    )


def detect_suspected_relative_root_prefix(
    requested_path: str,
) -> SuspectedRelativePrefix | None:
    """Detect a likely duplicated mount/workspace/session path prefix.

    This is diagnostic only. Callers must not silently rewrite the path.
    """

    if not requested_path or is_absolute_path(requested_path):
        return None

    requested_parts = _normalized_relative_parts(requested_path)
    if not requested_parts:
        return None
    comparable_requested = [part.casefold() for part in requested_parts]

    for prefix in _relative_prefix_candidates():
        prefix_parts = _normalized_relative_parts(prefix)
        if not prefix_parts:
            continue
        comparable_prefix = [part.casefold() for part in prefix_parts]
        prefix_length = len(comparable_prefix)
        if comparable_requested[:prefix_length] != comparable_prefix:
            continue

        remaining_parts = requested_parts[prefix_length:]
        if not remaining_parts:
            continue
        return SuspectedRelativePrefix(
            matched_prefix="/".join(requested_parts[:prefix_length]),
            suggested_relative_path="/".join(remaining_parts),
            default_root=Path(get_default_relative_file_dir()),
        )

    return None


def _missing_relative_file_error(
    *,
    requested_path: str,
    resolved_path: Path,
    tool_name: str,
) -> ToolChunk:
    return _error(
        "Error [FILE_NOT_FOUND]: The requested file does not exist.\n\n"
        "Requested relative path:\n"
        f"- {requested_path}\n\n"
        "Resolved Non-Coding path:\n"
        f"- {resolved_path}\n\n"
        "Resolution rule:\n"
        "- Relative paths use the first external mounted directory when "
        "one is available.\n"
        "- Otherwise, they use the current session scratch directory.\n\n"
        "Next action:\n"
        "- Carefully verify whether the requested relative path is correct.\n"
        "- Do not guess or invent another path.\n"
        "- If the file is located elsewhere, determine its exact location "
        "from the user's request or available context, then retry "
        f"`{tool_name}` with its exact absolute file path."
    )


def _missing_relative_file_with_prefix_error(
    *,
    requested_path: str,
    resolved_path: Path,
    tool_name: str,
    suspected_prefix: SuspectedRelativePrefix,
) -> ToolChunk:
    return _error(
        "Error [FILE_NOT_FOUND_POSSIBLE_DUPLICATED_PREFIX]: The requested "
        "file does not exist, and the relative path may contain a duplicated "
        "root-directory prefix.\n\n"
        "Requested relative path:\n"
        f"- {requested_path}\n\n"
        "Current default file root:\n"
        f"- {suspected_prefix.default_root}\n\n"
        "Resolved path:\n"
        f"- {resolved_path}\n\n"
        "Detected possible repeated prefix:\n"
        f"- {suspected_prefix.matched_prefix}\n\n"
        "Possible corrected relative path:\n"
        f"- {suspected_prefix.suggested_relative_path}\n\n"
        "Next action:\n"
        "1. Carefully confirm whether the detected prefix was accidentally "
        "included even though the tool already resolves relative paths from "
        "the current default file root.\n"
        "2. Do not automatically assume the suggested correction is valid.\n"
        f"3. If confirmed, retry `{tool_name}` with:\n"
        f"   - {suspected_prefix.suggested_relative_path}\n"
        "4. Otherwise, determine the exact file location and retry with its "
        "absolute file path."
    )


def _missing_absolute_or_coding_file_error(
    *,
    requested_path: str,
    resolved_path: Path,
) -> ToolChunk:
    return _error(
        "Error [FILE_NOT_FOUND]: The requested file does not exist.\n\n"
        "Requested path:\n"
        f"- {requested_path}\n\n"
        "Resolved path:\n"
        f"- {resolved_path}\n\n"
        "Next action:\n"
        "- Carefully verify that the provided file path is correct before "
        "retrying.\n"
        "- Do not guess or invent another path."
    )


def _missing_file_error(
    *,
    requested_path: str,
    resolved_path: Path,
    was_absolute: bool,
    tool_name: str,
    suspected_prefix: SuspectedRelativePrefix | None = None,
) -> ToolChunk:
    if is_non_coding_output_mode() and not was_absolute:
        if suspected_prefix is not None:
            return _missing_relative_file_with_prefix_error(
                requested_path=requested_path,
                resolved_path=resolved_path,
                tool_name=tool_name,
                suspected_prefix=suspected_prefix,
            )

        return _missing_relative_file_error(
            requested_path=requested_path,
            resolved_path=resolved_path,
            tool_name=tool_name,
        )

    return _missing_absolute_or_coding_file_error(
        requested_path=requested_path,
        resolved_path=resolved_path,
    )


def resolve_existing_file_path(
    file_path: str,
    *,
    tool_name: str = "read_file",
    path_argument: str = "file_path",
) -> str | ToolChunk:
    """Resolve and validate a path that must name an existing file.

    Absolute paths are preserved. Relative paths use the request's default
    file directory: the agent workspace in Coding Mode, or the first external
    mount (falling back to the session scratch directory) in Non-Coding mode.
    This helper never searches alternative roots or creates a missing file.
    """

    requested_path = (file_path or "").strip()
    if not requested_path:
        return _error(f"Error: No `{path_argument}` provided.")

    was_absolute = is_absolute_path(requested_path)
    candidate = Path(requested_path).expanduser()
    resolved_path = (
        candidate
        if was_absolute
        else Path(get_default_relative_file_dir()) / requested_path
    )

    try:
        exists = resolved_path.exists()
    except OSError as exc:
        return _error(
            "Error: The requested file path could not be accessed.\n\n"
            "Requested path:\n"
            f"- {requested_path}\n\n"
            "Resolved path:\n"
            f"- {resolved_path}\n\n"
            f"Reason:\n- {exc}"
        )

    if not exists:
        suspected_prefix = None
        if is_non_coding_output_mode() and not was_absolute:
            suspected_prefix = detect_suspected_relative_root_prefix(
                requested_path,
            )
        return _missing_file_error(
            requested_path=requested_path,
            resolved_path=resolved_path,
            was_absolute=was_absolute,
            tool_name=tool_name,
            suspected_prefix=suspected_prefix,
        )

    try:
        is_file = resolved_path.is_file()
    except OSError as exc:
        return _error(
            "Error: The resolved path could not be inspected.\n\n"
            "Resolved path:\n"
            f"- {resolved_path}\n\n"
            f"Reason:\n- {exc}"
        )

    if not is_file:
        return _error(
            "Error: The requested path exists but is not a file.\n\n"
            "Requested path:\n"
            f"- {requested_path}\n\n"
            "Resolved path:\n"
            f"- {resolved_path}"
        )

    return str(resolved_path)
