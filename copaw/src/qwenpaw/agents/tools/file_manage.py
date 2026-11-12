# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long
"""Structured file/directory copy, move & create tools (Non-Coding only).

These tools provide a guarded, cross-platform alternative to shell
``cp``/``mv``/``mkdir`` for Non-Coding mode, where shell access may be
restricted.

Path resolution follows the existing file-tool conventions:

* **source_path** — resolved like ``read_file``
  (:func:`resolve_existing_source_path`): absolute paths are used as-is;
  relative paths resolve from the first external mount (or the session
  scratch directory) in Non-Coding mode. The source must exist and may be
  either a file or a directory.
* **destination_path** / **directory_path** — resolved like ``write_file``
  (:func:`_resolve_destination_path`): subject to the Non-Coding
  first-mount restriction and the new-output-path guards. Parent
  directories are created automatically.

Visibility: all tools here declare ``non_coding_only=True`` so their schemas
are hidden from the model in Coding Mode; a runtime guard additionally
refuses execution outside Non-Coding mode as a second line of defence.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from .existing_path_guard import (
    SuspectedRelativePrefix,
    detect_suspected_relative_root_prefix,
    is_absolute_path,
)
from .output_path_guard import (
    check_resolved_rx_mutation,
    is_non_coding_output_mode,
    is_workspace_skill_absolute_path,
    resolve_new_output_path,
)
from ...config.context import (
    get_default_relative_file_dir,
    get_request_sandbox_mounts,
    get_request_sandbox_rx_mounts,
)
from ...runtime.tool_registry import tool_descriptor


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _error(text: str) -> ToolChunk:
    return ToolChunk(
        is_last=True,
        state=ToolResultState.ERROR,
        content=[TextBlock(type="text", text=text)],
    )


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _same_resolved(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return str(left) == str(right)


def _format_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _dir_stats(path: Path) -> tuple[int, int, int]:
    """Return (file_count, dir_count, total_bytes) under *path*."""
    n_files = 0
    n_dirs = 0
    total = 0
    for root, dirs, files in os.walk(path):
        n_dirs += len(dirs)
        for name in files:
            n_files += 1
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return n_files, n_dirs, total


def _ensure_parent_dirs(dst_path: Path) -> None:
    parent = dst_path.parent
    if str(parent) and not parent.exists():
        os.makedirs(parent, exist_ok=True)


def _is_link_or_junction(path: Path) -> bool:
    """Recognize junctions on Python 3.11 as well as ordinary symlinks."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) == (
        stat.IO_REPARSE_TAG_MOUNT_POINT
    )


def _remove_destination(dst_path: Path) -> None:
    if dst_path.is_dir() and not dst_path.is_symlink():
        shutil.rmtree(dst_path)
    else:
        os.remove(dst_path)


# ---------------------------------------------------------------------------
# Non-Coding mode guard (runtime second line of defence)
# ---------------------------------------------------------------------------


# Shell alternatives suggested when a tool is refused in Coding Mode.
_COPY_MOVE_SHELL_HINT = (
    "In Coding Mode, use `execute_shell_command` with the "
    "appropriate system command (e.g. `cp`, `mv`, `copy`, "
    "`move`) for file copy/move operations."
)

_MKDIR_SHELL_HINT = (
    "In Coding Mode, use `execute_shell_command` with the "
    "appropriate system command (e.g. `mkdir`, `md`, "
    "`New-Item -ItemType Directory`) to create directories."
)


def _check_non_coding_mode(
    tool_name: str,
    shell_hint: str = _COPY_MOVE_SHELL_HINT,
) -> ToolChunk | None:
    """Reject tool usage outside Non-Coding mode.

    ``shell_hint`` carries the Coding Mode alternative shown to the model;
    it defaults to the copy/move wording.
    """
    if not is_non_coding_output_mode():
        return ToolChunk(
            is_last=True,
            state=ToolResultState.DENIED,
            content=[
                TextBlock(
                    type="text",
                    text=(
                        f"Error: `{tool_name}` is only available in "
                        "Non-Coding mode.\n\n"
                        f"{shell_hint}"
                    ),
                ),
            ],
        )
    return None


# ---------------------------------------------------------------------------
# Source path resolution (read_file-style, file OR directory)
# ---------------------------------------------------------------------------


def _missing_relative_source_error(
    *,
    requested_path: str,
    resolved_path: Path,
    tool_name: str,
) -> ToolChunk:
    default_dir = get_default_relative_file_dir()
    return _error(
        "Error [SOURCE_NOT_FOUND]: The source path does not exist.\n\n"
        "Requested relative path:\n"
        f"- {requested_path}\n\n"
        "Resolved path:\n"
        f"- {resolved_path}\n\n"
        "Current default directory for relative paths:\n"
        f"- {default_dir}\n\n"
        "Resolution rule:\n"
        "- Relative paths resolve from the first external mounted directory "
        "when one is available; otherwise from the session scratch "
        "directory.\n\n"
        "Next action:\n"
        "- Carefully verify whether the source path is correct.\n"
        "- If the file is located outside the default directory, determine "
        "its exact absolute path from the user's request or available "
        f"context, then retry `{tool_name}` with the absolute path in "
        "`source_path`.\n"
        "- Do not guess or invent another path."
    )


def _missing_relative_source_with_prefix_error(
    *,
    requested_path: str,
    resolved_path: Path,
    tool_name: str,
    suspected_prefix: SuspectedRelativePrefix,
) -> ToolChunk:
    return _error(
        "Error [SOURCE_NOT_FOUND_POSSIBLE_DUPLICATED_PREFIX]: The source path "
        "does not exist, and the relative path may contain a duplicated "
        "root-directory prefix.\n\n"
        "Requested relative path:\n"
        f"- {requested_path}\n\n"
        "Current default directory:\n"
        f"- {suspected_prefix.default_root}\n\n"
        "Resolved path:\n"
        f"- {resolved_path}\n\n"
        "Detected possible repeated prefix:\n"
        f"- {suspected_prefix.matched_prefix}\n\n"
        "Possible corrected relative path:\n"
        f"- {suspected_prefix.suggested_relative_path}\n\n"
        "Next action:\n"
        "1. Confirm whether the detected prefix was accidentally included.\n"
        f"2. If confirmed, retry `{tool_name}` with:\n"
        f"   - source_path: {suspected_prefix.suggested_relative_path}\n"
        "3. Otherwise, determine the exact file location and retry with its "
        "absolute path."
    )


def _missing_absolute_source_error(
    *,
    requested_path: str,
    resolved_path: Path,
) -> ToolChunk:
    return _error(
        "Error [SOURCE_NOT_FOUND]: The source path does not exist.\n\n"
        "Requested path:\n"
        f"- {requested_path}\n\n"
        "Resolved path:\n"
        f"- {resolved_path}\n\n"
        "Next action:\n"
        "- Carefully verify that the provided path is correct before "
        "retrying.\n"
        "- Do not guess or invent another path."
    )


def _missing_source_error(
    *,
    requested_path: str,
    resolved_path: Path,
    was_absolute: bool,
    tool_name: str,
    suspected_prefix: SuspectedRelativePrefix | None = None,
) -> ToolChunk:
    if is_non_coding_output_mode() and not was_absolute:
        if suspected_prefix is not None:
            return _missing_relative_source_with_prefix_error(
                requested_path=requested_path,
                resolved_path=resolved_path,
                tool_name=tool_name,
                suspected_prefix=suspected_prefix,
            )
        return _missing_relative_source_error(
            requested_path=requested_path,
            resolved_path=resolved_path,
            tool_name=tool_name,
        )
    return _missing_absolute_source_error(
        requested_path=requested_path,
        resolved_path=resolved_path,
    )


def resolve_existing_source_path(
    path: str,
    *,
    tool_name: str = "copy_file_or_dir",
) -> str | ToolChunk:
    """Resolve and validate a source path (file OR directory) that must exist.

    Resolution rules (Non-Coding mode):

    * Absolute path -> used as-is, existence verified.
    * Relative path -> resolved from the default directory: the first
      external mount when available, otherwise the session scratch dir.

    On failure returns a detailed error carrying the requested path, the
    resolved path, the current default directory and next-action guidance.
    """

    requested_path = (path or "").strip()
    if not requested_path:
        return _error("Error: No `source_path` provided.")

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
            "Error: The source path could not be accessed.\n\n"
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
        return _missing_source_error(
            requested_path=requested_path,
            resolved_path=resolved_path,
            was_absolute=was_absolute,
            tool_name=tool_name,
            suspected_prefix=suspected_prefix,
        )

    try:
        is_file_or_dir = resolved_path.is_file() or resolved_path.is_dir()
    except OSError as exc:
        return _error(
            "Error: The resolved source path could not be inspected.\n\n"
            "Resolved path:\n"
            f"- {resolved_path}\n\n"
            f"Reason:\n- {exc}"
        )

    if not is_file_or_dir:
        return _error(
            "Error: The requested source path exists but is neither a file "
            "nor a directory.\n\n"
            "Requested path:\n"
            f"- {requested_path}\n\n"
            "Resolved path:\n"
            f"- {resolved_path}"
        )

    return str(resolved_path)


# ---------------------------------------------------------------------------
# Destination path resolution (write_file-style)
# ---------------------------------------------------------------------------


def _destination_absolute_path_denial(
    resolved: Path,
    allowed_root: Path,
    subject: str = "Destination path",
    allow_workspace_skills_absolute: bool = False,
) -> ToolChunk:
    skills_hint = (
        " Absolute paths to the agent workspace's skills directory or its "
        "subdirectories are also allowed."
        if allow_workspace_skills_absolute
        else ""
    )
    return ToolChunk(
        is_last=True,
        state=ToolResultState.DENIED,
        content=[
            TextBlock(
                type="text",
                text=(
                    f"Error: {subject} was denied because it is outside "
                    "the first mounted directory.\n\n"
                    f"Requested path:\n- {resolved}\n\n"
                    f"Allowed write root (first mount):\n- {allowed_root}\n\n"
                    "Use a relative path to write under the mounted "
                    "directory, or provide an absolute path under the allowed "
                    f"root.{skills_hint}"
                ),
            ),
        ],
    )


def _resolve_destination_path(
    destination_path: str,
    tool_name: str,  # noqa: ARG001 - kept for symmetry / future messages
    param_label: str = "destination_path",
    *,
    allow_workspace_skills_absolute: bool = False,
) -> str | ToolChunk:
    """Resolve a destination path following the write_file logic.

    * Relative paths -> resolved from the default output directory.
    * Absolute paths in Non-Coding mode with mounts -> must stay under the
      first mount (first-mount restriction).
    * Absolute paths without mounts -> validated against
      ``allowed_new_absolute_roots()`` via ``resolve_new_output_path``.
    * create_directory and copy_file_or_dir opt into the explicit workspace
      skills path exception; move retains the first-mount restriction.

    ``param_label`` only names the caller-facing parameter in error text
    (``destination_path`` for copy/move, ``directory_path`` for
    ``create_directory``).
    """

    requested = (destination_path or "").strip()
    if not requested:
        return _error(f"Error: No `{param_label}` provided.")

    candidate = Path(requested).expanduser()
    if not candidate.is_absolute():
        candidate = get_default_relative_file_dir() / candidate
    if is_non_coding_output_mode():
        mounts = get_request_sandbox_mounts()
        if mounts and not (
            allow_workspace_skills_absolute
            and is_workspace_skill_absolute_path(requested)
        ):
            resolved = candidate.resolve(strict=False)
            first_mount = mounts[0].resolve(strict=False)
            if not _path_is_under(resolved, first_mount):
                subject = (
                    "Directory path"
                    if param_label == "directory_path"
                    else "Destination path"
                )
                return _destination_absolute_path_denial(
                    resolved,
                    first_mount,
                    subject,
                    allow_workspace_skills_absolute,
                )

    denial = check_resolved_rx_mutation(candidate)
    if denial is not None:
        return denial
    return resolve_new_output_path(requested)


# ---------------------------------------------------------------------------
# Destination-exists / safety errors
# ---------------------------------------------------------------------------


def _destination_exists_error(
    *,
    destination_path: str,
    tool_name: str,
) -> ToolChunk:
    dst = Path(destination_path)
    if dst.is_dir():
        n_files, n_dirs, _ = _dir_stats(dst)
        type_text = f"directory ({n_files} files, {n_dirs} dirs)"
    else:
        try:
            size = dst.stat().st_size
        except OSError:
            size = 0
        type_text = f"file ({_format_size(size)})"

    return _error(
        "Error: Destination already exists.\n\n"
        "Destination:\n"
        f"- {destination_path}\n\n"
        f"Type: {type_text}\n\n"
        f"To replace it, retry `{tool_name}` with overwrite=True.\n"
        "To keep the existing file, provide a different destination_path."
    )


def _existing_path_not_directory_error(resolved: str) -> ToolChunk:
    target = Path(resolved)
    if target.is_symlink():
        kind = "symlink"
    else:
        try:
            size = target.stat().st_size
        except OSError:
            size = 0
        kind = f"file ({_format_size(size)})"

    return _error(
        "Error: Path exists but is not a directory.\n\n"
        "Directory path:\n"
        f"- {resolved}\n\n"
        f"Existing type: {kind}\n\n"
        "`create_directory` never replaces or removes existing files or "
        "symlinks. Choose a different directory_path."
    )


def _same_path_error(path: str) -> ToolChunk:
    return _error(
        "Error: Source and destination paths are identical.\n\n"
        "Path:\n"
        f"- {path}\n\n"
        "Provide a different destination_path."
    )


def _move_into_subdir_error(source: str, destination: str) -> ToolChunk:
    return _error(
        "Error: Cannot move a directory into its own subdirectory.\n\n"
        "Source:\n"
        f"- {source}\n\n"
        "Destination:\n"
        f"- {destination}\n\n"
        "This would create an infinite recursion. Choose a destination "
        "outside the source directory tree."
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool_descriptor(
    requires_sandbox=("file_read", "file_write"),
    async_execution=True,
    non_coding_only=True,
    # Alternatives mirror the shell.py forced-direct guard copy set.
    shell_redirect=(
        "copying files/directories",
        ("copy", "xcopy", "robocopy", "Copy-Item"),
    ),
)
async def copy_file_or_dir(
    source_path: str,
    destination_path: str,
    overwrite: bool = False,
) -> ToolChunk:
    """Copy a file or directory to a new location (Non-Coding mode only).

    source_path: Path to the existing file or directory to copy.
      - If relative, resolves from the mounted directory (when available)
        or the session scratch directory.
      - If the source is not found, the error will show the current default
        directory — verify whether you need an absolute path instead.

    destination_path: Where to place the copy.
      - If relative, resolves from the default output directory (same rule).
      - Absolute paths under the current agent workspace's skills directory
        are allowed regardless of mounts. Other absolute paths retain the
        usual output restrictions, including the first-mount restriction.
      - This is the final target path; the source name is not appended.
      - Parent directories are created automatically.
      - If the destination already exists and overwrite=False, the tool
        returns an error; set overwrite=True only when you are sure the
        target can be replaced.

    Args:
        source_path (`str`):
            Path to the source file or directory. Must exist.
            Relative paths resolve from the mounted directory (if
            available) or session scratch directory.
        destination_path (`str`):
            Path to the destination. If the source is a directory,
            this will be the new directory path. Parent directories
            are created automatically.
            Relative paths resolve from the default output directory.
        overwrite (`bool`, optional):
            Whether to overwrite if destination already exists.
            Default: False. When False and destination exists,
            returns an error asking to confirm overwrite intent.
    """

    guard = _check_non_coding_mode("copy_file_or_dir")
    if guard is not None:
        return guard

    src = resolve_existing_source_path(
        source_path,
        tool_name="copy_file_or_dir",
    )
    if isinstance(src, ToolChunk):
        return src

    dst = _resolve_destination_path(
        destination_path,
        "copy_file_or_dir",
        allow_workspace_skills_absolute=True,
    )
    if isinstance(dst, ToolChunk):
        return dst

    src_path = Path(src)
    dst_path = Path(dst)

    if get_request_sandbox_rx_mounts() and any(
        _is_link_or_junction(p)
        for p in (src_path, dst_path)
    ):
        return _error("Error: copy/move of links is unsupported with RX mounts.")

    if _same_resolved(src_path, dst_path):
        return _same_path_error(src)

    if dst_path.exists() or dst_path.is_symlink():
        if not overwrite:
            return _destination_exists_error(
                destination_path=dst,
                tool_name="copy_file_or_dir",
            )
        denial = check_resolved_rx_mutation(
            dst_path, affects_subtree=dst_path.is_dir(),
        )
        if denial is not None:
            return denial
        _remove_destination(dst_path)

    _ensure_parent_dirs(dst_path)

    try:
        if src_path.is_dir():
            shutil.copytree(src_path, dst_path, symlinks=True)
            n_files, n_dirs, total = _dir_stats(dst_path)
            message = (
                f"Copied directory: {src} → {dst} "
                f"({n_files} files, {n_dirs} dirs, "
                f"{_format_size(total)} total)"
            )
        else:
            shutil.copy2(src_path, dst_path, follow_symlinks=False)
            message = (
                f"Copied file: {src} → {dst} "
                f"({_format_size(dst_path.stat().st_size)})"
            )
    except OSError as exc:
        return _error(
            "Error: Copy failed.\n\n"
            f"Source:\n- {src}\n\n"
            f"Destination:\n- {dst}\n\n"
            f"Reason:\n- {exc}"
        )

    return ToolChunk(
        is_last=True,
        state=ToolResultState.SUCCESS,
        content=[TextBlock(type="text", text=message)],
    )


@tool_descriptor(
    requires_sandbox=("file_read", "file_write"),
    async_execution=True,
    non_coding_only=True,
    # Alternatives mirror the shell.py forced-direct guard move set.
    shell_redirect=(
        "moving/renaming files/directories",
        ("move", "ren", "Move-Item", "Rename-Item"),
    ),
)
async def move_file_or_dir(
    source_path: str,
    destination_path: str,
    overwrite: bool = False,
) -> ToolChunk:
    """Move or rename a file or directory (Non-Coding mode only).

    The source will no longer exist after a successful move.

    source_path: Path to the existing file or directory to move.
      - If relative, resolves from the mounted directory (when available)
        or the session scratch directory.
      - If the source is not found, the error will show the current default
        directory — verify whether you need an absolute path instead.

    destination_path: New location or name for the file/directory.
      - If relative, resolves from the default output directory (same rule).
      - Parent directories are created automatically.
      - If the destination already exists and overwrite=False, the tool
        returns an error; set overwrite=True only when you are sure the
        target can be replaced.

    Args:
        source_path (`str`):
            Path to the source file or directory. Must exist.
            Relative paths resolve from the mounted directory (if
            available) or session scratch directory.
        destination_path (`str`):
            Path to the destination. The source will no longer exist
            after a successful move. Parent directories are created
            automatically.
            Relative paths resolve from the default output directory.
        overwrite (`bool`, optional):
            Whether to overwrite if destination already exists.
            Default: False. When False and destination exists,
            returns an error asking to confirm overwrite intent.
    """

    guard = _check_non_coding_mode("move_file_or_dir")
    if guard is not None:
        return guard

    src = resolve_existing_source_path(
        source_path,
        tool_name="move_file_or_dir",
    )
    if isinstance(src, ToolChunk):
        return src

    denial = check_resolved_rx_mutation(src, affects_subtree=Path(src).is_dir())
    if denial is not None:
        return denial

    dst = _resolve_destination_path(destination_path, "move_file_or_dir")
    if isinstance(dst, ToolChunk):
        return dst

    src_path = Path(src)
    dst_path = Path(dst)

    if get_request_sandbox_rx_mounts() and any(
        _is_link_or_junction(p)
        for p in (src_path, dst_path)
    ):
        return _error("Error: copy/move of links is unsupported with RX mounts.")

    if _same_resolved(src_path, dst_path):
        return _same_path_error(src)

    if src_path.is_dir() and _path_is_under(dst_path, src_path):
        return _move_into_subdir_error(src, dst)

    if dst_path.exists() or dst_path.is_symlink():
        if not overwrite:
            return _destination_exists_error(
                destination_path=dst,
                tool_name="move_file_or_dir",
            )
        denial = check_resolved_rx_mutation(
            dst_path, affects_subtree=dst_path.is_dir(),
        )
        if denial is not None:
            return denial
        _remove_destination(dst_path)

    _ensure_parent_dirs(dst_path)

    try:
        was_dir = src_path.is_dir()
        shutil.move(src, dst)
        if was_dir:
            n_files, n_dirs, _ = _dir_stats(dst_path)
            message = (
                f"Moved directory: {src} → {dst} "
                f"({n_files} files, {n_dirs} dirs)"
            )
        else:
            message = (
                f"Moved file: {src} → {dst} "
                f"({_format_size(dst_path.stat().st_size)})"
            )
    except OSError as exc:
        return _error(
            "Error: Move failed.\n\n"
            f"Source:\n- {src}\n\n"
            f"Destination:\n- {dst}\n\n"
            f"Reason:\n- {exc}"
        )

    return ToolChunk(
        is_last=True,
        state=ToolResultState.SUCCESS,
        content=[TextBlock(type="text", text=message)],
    )


@tool_descriptor(
    requires_sandbox=("file_write",),
    async_execution=True,
    non_coding_only=True,
    # Alternatives mirror the shell.py forced-direct guard mkdir set.
    shell_redirect=(
        "creating directories",
        ("mkdir", "md", "New-Item -ItemType Directory"),
    ),
)
async def create_directory(directory_path: str) -> ToolChunk:
    """Create a directory and any missing parents (Non-Coding mode only).

    Creation is non-destructive and idempotent: missing intermediate
    directories are created, and calling the tool again on an existing
    directory succeeds without changing anything. An existing file (or
    symlink) at `directory_path` is never replaced or removed - it returns
    an error instead.

    directory_path: Where the new directory should live.
      - If relative, resolves from the default output directory: the first
        external mounted directory when available, otherwise the session
        scratch directory.
      - In Non-Coding mode a relative path must not embed an output
        directory prefix (workspace / scratch / mount), and an absolute path
        must stay inside the allowed output roots - and, when request-scoped
        mounts exist, under the first mount. Explicit absolute paths to the
        agent workspace's skills directory or its subdirectories are also
        allowed regardless of mounts.

    This tool creates directories only - use `write_file` to create files;
    `write_file` also creates missing parent directories automatically.

    Args:
        directory_path (`str`):
            Path of the directory to create. Relative paths resolve from
            the default output directory; absolute paths must stay inside
            the allowed output roots.
    """

    guard = _check_non_coding_mode(
        "create_directory",
        shell_hint=_MKDIR_SHELL_HINT,
    )
    if guard is not None:
        return guard

    dst = _resolve_destination_path(
        directory_path,
        "create_directory",
        param_label="directory_path",
        allow_workspace_skills_absolute=True,
    )
    if isinstance(dst, ToolChunk):
        return dst

    target = Path(dst)

    if target.is_symlink() or target.exists():
        if not target.is_symlink() and target.is_dir():
            return ToolChunk(
                is_last=True,
                state=ToolResultState.SUCCESS,
                content=[
                    TextBlock(
                        type="text",
                        text=(
                            "Directory already exists (nothing was "
                            f"changed): {dst}"
                        ),
                    ),
                ],
            )
        return _existing_path_not_directory_error(dst)

    try:
        target.mkdir(parents=True)
    except OSError as exc:
        return _error(
            "Error: Directory creation failed.\n\n"
            "Requested path:\n"
            f"- {(directory_path or '').strip()}\n\n"
            "Resolved path:\n"
            f"- {dst}\n\n"
            f"Reason:\n- {exc}"
        )

    return ToolChunk(
        is_last=True,
        state=ToolResultState.SUCCESS,
        content=[
            TextBlock(type="text", text=f"Created directory: {dst}"),
        ],
    )
