# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long
import os
from pathlib import Path
from typing import Optional

import aiofiles
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from .utils import (
    truncate_text_output,
    read_file_safe,
    DEFAULT_MAX_BYTES,
)
from .output_path_guard import (
    allowed_new_absolute_roots,
    check_new_absolute_output_path,
    check_rx_mount_write_denial,
    check_resolved_rx_mutation,
    is_non_coding_output_mode,
    is_workspace_skill_absolute_path,
    resolve_new_output_path,
)
from .existing_path_guard import resolve_existing_file_path
from ...config.context import (
    get_current_workspace_dir,
    get_current_recent_max_bytes,
    get_default_relative_file_dir,
    get_request_sandbox_mounts,
)
from ...constant import TRUNCATION_NOTICE_MARKER
from ...runtime.tool_registry import tool_descriptor


def _path_to_file_url(path: str) -> str:
    """Convert a local file path to a ``file://`` URL.

    Does NOT percent-encode non-ASCII characters because agentscope's
    DashScope formatter extracts the local path from the URL without
    ``unquote()``, causing ``FileNotFoundError`` for files with
    non-ASCII names (e.g. Chinese characters).
    """
    abs_path = os.path.abspath(path)
    if os.name == "nt":
        abs_path = abs_path.replace("\\", "/")

    if os.name == "nt":
        if abs_path.startswith("//"):
            return f"file:{abs_path}"
        return f"file:///{abs_path}"
    return f"file://{abs_path}"


def _resolve_file_path(file_path: str) -> str:
    """Resolve file path: use absolute path as-is,
    resolve relative path from current workspace or WORKING_DIR.

    Args:
        file_path: The input file path (absolute or relative).

    Returns:
        The resolved absolute file path as string.
    """
    path = Path(file_path).expanduser()
    if path.is_absolute():
        return str(path)
    else:
        # Use current workspace_dir from context, fallback to WORKING_DIR
        workspace_dir = get_default_relative_file_dir(
            workspace_dir=get_current_workspace_dir(),
        )
        return str(workspace_dir / file_path)


def _get_encoding_for_file(file_path: str) -> str:
    """Determine the appropriate encoding for a file based on its type.

    For cross-platform compatibility, especially with Windows Excel/Notepad:
    - CSV/TSV/TXT files: Use UTF-8-BOM (Windows Excel needs BOM to detect UTF-8)
    - All other files: Use UTF-8 (safer default, no BOM)

    Args:
        file_path: Path to the file

    Returns:
        Encoding string: "utf-8-sig" or "utf-8"
    """
    suffix = Path(file_path).suffix.lower()

    # Files that need BOM for Windows compatibility
    if suffix in {".csv", ".tsv", ".tab", ".txt", ".log"}:
        return "utf-8-sig"

    # Default: UTF-8 without BOM (safe for all other files)
    # This includes: .sh, .yaml, .json, .py, .js, .md, etc.
    return "utf-8"


def _is_windows_non_coding_mode() -> bool:
    return is_non_coding_output_mode()


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _allowed_new_absolute_roots() -> list[Path]:
    return allowed_new_absolute_roots()


def _new_absolute_path_denial_message(file_path: str) -> str:
    denial = check_new_absolute_output_path(file_path)
    if denial is not None and denial.content:
        return getattr(denial.content[0], "text", "")
    return ""


def _check_new_absolute_file_creation(file_path: str) -> ToolChunk | None:
    """Reject new absolute file creation outside Non-Coding output roots.

    Existing files/directories and relative paths are explicitly exempt.
    Relative paths continue to resolve through get_default_relative_file_dir().
    """
    return check_new_absolute_output_path(file_path)


def _write_file_absolute_path_denial(
    file_path: Path,
    allowed_root: Path,
) -> ToolChunk:
    """Build a denial response for write_file's first-mount policy."""

    return ToolChunk(
        is_last=True,
        state=ToolResultState.DENIED,
        content=[
            TextBlock(
                type="text",
                text=(
                    "Error: write_file is restricted to the first external "
                    "mounted directory in Non-Coding mode.\n\n"
                    f"Requested path:\n- {file_path}\n\n"
                    f"Allowed write root:\n- {allowed_root}\n\n"
                    "Use a relative path to write under the first mounted "
                    "directory. Absolute paths under the agent workspace's "
                    "skills directory and its existing skill.json file are "
                    "also allowed."
                ),
            ),
        ],
    )


def _resolve_write_file_path(file_path: str) -> str | ToolChunk:
    """Apply the first-mount restriction only to the write_file tool.

    Relative paths already resolve to the first external mount in Non-Coding
    mode. Absolute targets, including existing files, must also remain under
    that first mount whenever request-scoped mounts are present, except for
    explicit workspace skills paths and the existing workspace skill.json.
    """

    candidate = Path(file_path).expanduser()
    if not candidate.is_absolute():
        candidate = get_default_relative_file_dir() / candidate
    if is_non_coding_output_mode():
        mounts = get_request_sandbox_mounts()
        if mounts and not is_workspace_skill_absolute_path(
            file_path,
            allow_existing_skill_json=True,
        ):
            resolved = candidate.resolve(strict=False)
            first_mount = mounts[0].resolve(strict=False)
            if not _path_is_under(resolved, first_mount):
                return _write_file_absolute_path_denial(
                    resolved,
                    first_mount,
                )

    denial = check_rx_mount_write_denial(file_path)
    if denial is not None:
        return denial

    return resolve_new_output_path(file_path)


async def _write_content(file_path: str, content: str) -> ToolChunk:
    """Write to a resolved path without applying a tool-specific policy."""

    encoding = _get_encoding_for_file(file_path)
    try:
        async with aiofiles.open(file_path, "w", encoding=encoding) as file:
            await file.write(content)
        return ToolChunk(
            is_last=True,
            state=ToolResultState.SUCCESS,
            content=[
                TextBlock(
                    type="text",
                    text=f"Wrote {len(content)} bytes to {file_path}.",
                ),
            ],
        )
    except Exception as e:
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: Write file failed due to \n{e}",
                ),
            ],
        )


@tool_descriptor(
    requires_sandbox=("file_read",),
    async_execution=True,
    shell_redirect=(
        "viewing file contents",
        ("type", "cat", "Get-Content"),
    ),
)
# Keep numeric strings in the public type: AgentScope validates the generated
# schema before entering this function, where they are normalized to integers.
async def read_file(  # pylint: disable=too-many-return-statements
    file_path: str,
    start_line: Optional[int | str] = None,
    end_line: Optional[int | str] = None,
) -> ToolChunk:
    """Read a text file and return its contents.

    ``file_path`` may be absolute (``~`` is expanded) or relative.  Relative
    paths resolve from the request's default file directory: the agent
    workspace in Coding Mode, or the first external mount — falling back to
    the current session scratch directory — in Non-Coding mode.  The path
    must name an existing file; it is never created and no alternative roots
    are searched.

    Content is decoded as UTF-8 (a leading BOM is stripped); bytes that
    cannot be decoded are silently ignored, so binary files come back as
    lossy text.  At most 200 MB is read into memory.

    Use ``start_line`` / ``end_line`` to read a specific 1-based, inclusive
    line range; omit both to read the whole file.  The returned text is the
    raw file content for that range — it is **not** prefixed with line
    numbers.  ``start_line`` defaults to 1 and ``end_line`` to the last
    line; a non-integer value, a ``start_line`` past end-of-file, or
    ``start_line > end_line`` returns an error.

    Output is capped at a ~50 KB byte budget and cut at a line boundary.
    When the content is truncated — or when the requested range stops before
    end-of-file — a continuation notice is appended stating the total line
    count and the ``start_line`` to pass to the next ``read_file`` call.

    Args:
        file_path (`str`):
            Path to an existing file.  Absolute paths are used as-is
            (``~`` expanded); relative paths resolve from the request's
            default file directory.
        start_line (`int | str`, optional):
            First line to read (1-based, inclusive).  Decimal strings are
            accepted for tool-call compatibility.  Defaults to 1.
        end_line (`int | str`, optional):
            Last line to read (1-based, inclusive).  Decimal strings are
            accepted for tool-call compatibility.  Defaults to the last
            line of the file.
    """

    # Convert start_line/end_line to int if they are strings
    if start_line is not None:
        try:
            start_line = int(start_line)
        except (ValueError, TypeError):
            return ToolChunk(
                is_last=True,
                state=ToolResultState.ERROR,
                content=[
                    TextBlock(
                        type="text",
                        text=f"Error: start_line must be an integer, got {start_line!r}.",
                    ),
                ],
            )

    if end_line is not None:
        try:
            end_line = int(end_line)
        except (ValueError, TypeError):
            return ToolChunk(
                is_last=True,
                state=ToolResultState.ERROR,
                content=[
                    TextBlock(
                        type="text",
                        text=f"Error: end_line must be an integer, got {end_line!r}.",
                    ),
                ],
            )

    resolved = resolve_existing_file_path(file_path)
    if isinstance(resolved, ToolChunk):
        return resolved
    file_path = resolved

    try:
        content = await read_file_safe(file_path)
        all_lines = content.split("\n")
        total = len(all_lines)

        # Determine read range
        s = max(1, start_line if start_line is not None else 1)
        e = min(total, end_line if end_line is not None else total)

        if s > total:
            return ToolChunk(
                is_last=True,
                state=ToolResultState.ERROR,
                content=[
                    TextBlock(
                        type="text",
                        text=f"Error: start_line {s} exceeds file length ({total} lines).",
                    ),
                ],
            )

        if s > e:
            return ToolChunk(
                is_last=True,
                state=ToolResultState.ERROR,
                content=[
                    TextBlock(
                        type="text",
                        text=f"Error: start_line ({s}) > end_line ({e}).",
                    ),
                ],
            )

        # Extract selected lines
        selected_content = "\n".join(all_lines[s - 1 : e])

        # Apply smart truncation (consistent with shell output format)
        max_bytes = get_current_recent_max_bytes() or DEFAULT_MAX_BYTES
        text = truncate_text_output(
            selected_content,
            start_line=s,
            total_lines=total,
            file_path=file_path,
            max_bytes=max_bytes,
        )

        # Add continuation hint if partial read without truncation.
        # Use TRUNCATION_NOTICE_MARKER format so ToolResultCompactor can
        # re-truncate with the correct start_line when compacting old messages.
        if text == selected_content and e < total:
            content_bytes = len(text.encode("utf-8"))
            notice = (
                TRUNCATION_NOTICE_MARKER + f"\nThe output above was truncated."
                f"\nThe full content is saved to the file "
                f"and contains {total} lines in total."
                f"\nThis excerpt starts at line {s} and "
                f"covers the next {content_bytes} bytes."
                "\nIf the current content is not enough, "
                f"call `read_file` with file_path={file_path} "
                f"start_line={e + 1} to read more."
            )
            text = text + notice

        return ToolChunk(
            is_last=True,
            state=ToolResultState.SUCCESS,
            content=[TextBlock(type="text", text=text)],
        )

    except Exception as e:
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: Read file failed due to \n{e}",
                ),
            ],
        )


@tool_descriptor(
    requires_sandbox=("file_write",),
    async_execution=True,
    shell_redirect=(
        "creating or overwriting files",
        ("echo > redirection", "Set-Content", "Out-File"),
    ),
)
async def write_file(
    file_path: str,
    content: str,
) -> ToolChunk:
    """Create a new file or overwrite an existing one.

    Writing is destructive: an existing file at ``file_path`` is truncated
    and replaced by ``content``. Missing parent directories are created
    recursively after path validation succeeds.

    ``file_path`` may be absolute (``~`` is expanded) or relative.  Relative
    paths resolve from the request's default file directory: the agent
    workspace in Coding Mode, or the first external mount — falling back to
    the current session scratch directory — in Non-Coding mode.  In
    Non-Coding mode extra guards apply: a relative path must not embed an
    output-directory prefix (workspace / scratch / mount), and an absolute
    path must stay within the allowed output roots (and, when request-scoped
    mounts exist, under the first mount). Explicit absolute paths under the
    agent workspace's skills directory or to its existing skill.json file
    are exceptions to the first-mount restriction. Relative paths retain
    the default directory behavior. Violations are denied with a path hint.

    ``content`` is written as text, with the encoding chosen by extension:
    ``.csv`` / ``.tsv`` / ``.tab`` / ``.txt`` / ``.log`` are written as
    UTF-8 with a BOM (for Windows Excel/Notepad), everything else as plain
    UTF-8.

    Args:
        file_path (`str`):
            Destination path.  Absolute paths are used as-is (``~``
            expanded); relative paths resolve from the request's default
            file directory. Missing parent directories are created recursively.
        content (`str`):
            Text to write.  Overwrites any existing file contents.
    """

    if not file_path:
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[
                TextBlock(
                    type="text",
                    text="Error: No `file_path` provided.",
                ),
            ],
        )

    resolved = _resolve_write_file_path(file_path)
    if isinstance(resolved, ToolChunk):
        return resolved
    file_path = resolved
    try:
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: Could not create parent directories for {file_path}: {exc}",
                ),
            ],
        )
    return await _write_content(file_path, content)


# pylint: disable=too-many-return-statements
@tool_descriptor(
    requires_sandbox=("file_write",),
    async_execution=True,
    shell_redirect=(
        "partial in-place edits",
        ("(Get-Content ...) -replace ... | Set-Content",),
    ),
)
async def edit_file(
    file_path: str,
    old_text: str,
    new_text: str,
) -> ToolChunk:
    """Find-and-replace text in an existing file.

    ``old_text`` is matched as a literal substring (not a regex) and must
    appear exactly — whitespace, indentation and newlines are significant.
    **Every** occurrence of ``old_text`` is replaced with ``new_text``; if
    ``old_text`` is not found, the call fails with an error and the file is
    left unchanged.

    The file must already exist (it is never created).  The whole file is
    read, replaced in memory, and rewritten, preserving the extension-based
    encoding (``.csv`` / ``.tsv`` / ``.tab`` / ``.txt`` / ``.log`` are
    written back as UTF-8 with a BOM, everything else as plain UTF-8).

    ``file_path`` may be absolute (``~`` is expanded) or relative.  Relative
    paths resolve from the request's default file directory: the agent
    workspace in Coding Mode, or the first external mount — falling back to
    the current session scratch directory — in Non-Coding mode.

    Args:
        file_path (`str`):
            Path to an existing file.  Absolute paths are used as-is
            (``~`` expanded); relative paths resolve from the request's
            default file directory.
        old_text (`str`):
            Exact literal text to find.  Must match verbatim (including
            whitespace/indentation); all occurrences are replaced.
        new_text (`str`):
            Replacement text.  May be empty to delete ``old_text``.
    """

    resolved = resolve_existing_file_path(
        file_path,
        tool_name="edit_file",
        path_argument="file_path",
    )
    if isinstance(resolved, ToolChunk):
        return resolved
    resolved_path = resolved

    denial = check_resolved_rx_mutation(resolved_path)
    if denial is not None:
        return denial

    try:
        content = await read_file_safe(resolved_path)
    except Exception as e:
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: Read file failed due to \n{e}",
                ),
            ],
        )

    if old_text not in content:
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: The text to replace was not found in {file_path}.",
                ),
            ],
        )

    new_content = content.replace(old_text, new_text)
    write_response = await _write_content(resolved_path, new_content)

    if write_response.content and len(write_response.content) > 0:
        write_text = getattr(write_response.content[0], "text", "")
        if write_text.startswith("Error:"):
            return write_response

    return ToolChunk(
        is_last=True,
        state=ToolResultState.SUCCESS,
        content=[
            TextBlock(
                type="text",
                text=f"Successfully replaced text in {file_path}.",
            ),
        ],
    )


@tool_descriptor(
    requires_sandbox=("file_write",),
    async_execution=True,
    enabled_by_default=False,
    shell_redirect=(
        "appending to files",
        ("echo >> redirection", "Add-Content"),
    ),
)
async def append_file(
    file_path: str,
    content: str,
) -> ToolChunk:
    """Append content to the end of a file.

    Existing content is preserved and ``content`` is written after it; if the
    file does not exist it is created.  The parent directory must already
    exist — missing intermediate directories are not created and cause an
    error.

    ``file_path`` may be absolute (``~`` is expanded) or relative.  Relative
    paths resolve from the request's default file directory: the agent
    workspace in Coding Mode, or the first external mount — falling back to
    the current session scratch directory — in Non-Coding mode.  In
    Non-Coding mode extra guards apply: a relative path must not embed an
    output-directory prefix (workspace / scratch / mount), and a new absolute
    path must stay within the allowed output roots; violations are denied
    with a corrected-path hint.

    The encoding is chosen by extension: ``.csv`` / ``.tsv`` / ``.tab`` /
    ``.txt`` / ``.log`` use UTF-8 with a BOM, everything else plain UTF-8.
    Appending to an existing file does not insert a BOM mid-file.

    Args:
        file_path (`str`):
            Destination path.  Absolute paths are used as-is (``~``
            expanded); relative paths resolve from the request's default
            file directory.  The parent directory must exist.
        content (`str`):
            Text to append after the file's existing content.
    """

    if not file_path:
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[
                TextBlock(
                    type="text",
                    text="Error: No `file_path` provided.",
                ),
            ],
        )

    denial = check_rx_mount_write_denial(file_path)
    if denial is not None:
        return denial

    resolved = resolve_new_output_path(file_path)
    if isinstance(resolved, ToolChunk):
        return resolved
    file_path = resolved
    encoding = _get_encoding_for_file(file_path)

    try:
        with open(file_path, "a", encoding=encoding) as file:
            file.write(content)
        return ToolChunk(
            is_last=True,
            state=ToolResultState.SUCCESS,
            content=[
                TextBlock(
                    type="text",
                    text=f"Appended {len(content)} bytes to {file_path}.",
                ),
            ],
        )
    except Exception as e:
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: Append file failed due to \n{e}",
                ),
            ],
        )
