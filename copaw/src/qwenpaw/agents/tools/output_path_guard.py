# -*- coding: utf-8 -*-
"""Shared guards for Non-Coding mode output file paths."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from ...config.context import (
    SESSION_TMP_SUBDIR,
    get_current_request_context,
    get_current_workspace_dir,
    get_default_relative_file_dir,
    get_request_sandbox_mounts,
    get_request_sandbox_rx_mounts,
    get_session_scoped_work_dir,
)
from ...constant import WORKING_DIR
from ...config.mount_policy import normalize_rx_mount_overlaps

_WIN_ABS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def is_non_coding_output_mode() -> bool:
    """Whether new output path restrictions should run for this request."""

    request_context = get_current_request_context()
    if not request_context:
        return False
    return not bool(request_context.get("is_coding_mode"))


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _is_absolute_path(path: str) -> bool:
    raw = (path or "").strip()
    if not raw:
        return False
    return (
        Path(raw).expanduser().is_absolute()
        or bool(_WIN_ABS_PATH_RE.match(raw))
        or raw.startswith("\\\\")
    )


def is_workspace_skill_absolute_path(
    file_path: str,
    *,
    allow_existing_skill_json: bool = False,
) -> bool:
    """Recognize explicit workspace skill targets, never relative inputs."""
    candidate = Path(file_path).expanduser()
    if not candidate.is_absolute():
        return False
    workspace = Path(
        get_current_workspace_dir() or WORKING_DIR,
    ).expanduser().resolve(strict=False)
    resolved = candidate.resolve(strict=False)
    if _path_is_under(resolved, workspace / "skills"):
        return True
    return (
        allow_existing_skill_json
        and resolved == workspace / "skill.json"
        and resolved.is_file()
    )


def allowed_new_absolute_roots() -> list[Path]:
    """Return allowed absolute roots for newly-created output files."""

    workspace_dir = Path(
        get_current_workspace_dir() or WORKING_DIR,
    ).expanduser()
    roots: list[Path] = []
    session_dir = get_session_scoped_work_dir(workspace_dir=workspace_dir)
    if session_dir != workspace_dir:
        roots.append(session_dir)
    roots.append(workspace_dir / "skills")
    roots.extend(get_request_sandbox_mounts())
    return roots


def _absolute_path_denial_message(file_path: str) -> str:
    workspace_dir = Path(
        get_current_workspace_dir() or WORKING_DIR,
    ).expanduser()
    roots = allowed_new_absolute_roots()
    roots_text = "\n".join(f"- {root}" for root in roots)
    default_dir = get_default_relative_file_dir()

    return (
        "Error: File creation was denied because the requested absolute path "
        "would create a new file or directory outside the allowed Non-Coding "
        "creation locations.\n\n"
        "For new ordinary files or directories, use a relative path so it "
        "resolves to the default output directory. If an external mounted "
        "directory is available, prioritize that mounted directory; otherwise "
        "use the session scratch directory.\n\n"
        "For new skill files, use materialize_skill first. If exceptional "
        "circumstances require generic file tools, create new skill files "
        "only under workspace_dir/skills.\n\n"
        f"Requested path:\n- {file_path}\n\n"
        f"Recommended default output directory:\n- {default_dir}\n\n"
        f"Allowed new absolute creation roots:\n{roots_text}\n\n"
        f"Workspace skills directory:\n- {workspace_dir / 'skills'}\n"
        "Workspace session scratch root:\n"
        f"- {workspace_dir / SESSION_TMP_SUBDIR}"
    )


def check_new_absolute_output_path(file_path: str) -> ToolChunk | None:
    """Reject new absolute output paths outside allowed Non-Coding roots."""

    if not is_non_coding_output_mode():
        return None

    if not _is_absolute_path(file_path):
        return None

    raw_path = Path(file_path).expanduser()
    resolved = raw_path.resolve(strict=False)
    if resolved.exists():
        return None

    if any(
        _path_is_under(resolved, root)
        for root in allowed_new_absolute_roots()
    ):
        return None

    return ToolChunk(
        is_last=True,
        state=ToolResultState.DENIED,
        content=[
            TextBlock(
                type="text",
                text=_absolute_path_denial_message(str(resolved)),
            ),
        ],
    )


def check_rx_mount_write_denial(path: str) -> ToolChunk | None:
    """Reject write targets inside read/execute-only request mounts."""

    if not is_non_coding_output_mode():
        return None

    expanded = os.path.expandvars((path or "").strip())
    if not expanded or not _is_absolute_path(expanded):
        return None

    rx_roots = get_request_sandbox_rx_mounts()
    if not rx_roots:
        return None

    resolved = Path(expanded).expanduser().resolve(strict=False)
    if not any(_path_is_under(resolved, root) for root in rx_roots):
        return None

    writable_roots = [
        Path(get_current_workspace_dir() or WORKING_DIR).expanduser(),
        *allowed_new_absolute_roots(),
    ]
    if any(_path_is_under(resolved, root) for root in writable_roots):
        return None  # writable roots win when RX is nested inside one

    return ToolChunk(
        is_last=True,
        state=ToolResultState.DENIED,
        content=[
            TextBlock(
                type="text",
                text=(
                    "Error: The target path is inside a read/execute-only "
                    "external directory (sandbox_rx_mounts) and cannot be "
                    "written, modified or deleted. Use a relative path "
                    "(resolves to the writable mount) or an absolute path "
                    "under the writable mount instead.\n\n"
                    f"Requested path:\n- {resolved}"
                ),
            ),
        ],
    )


def check_resolved_rx_mutation(
    path: str | Path, *, affects_subtree: bool = False,
) -> ToolChunk | None:
    """Check a resolved tool target, including destructive directory changes.

    Unlike the shell's raw-argument guard, this must never accept relative
    paths. On Windows explicit RW parents absorb RX children, while RW
    children are exceptions to retained RX parents. Implicit workspace
    overlaps still fail closed.
    """
    if not is_non_coding_output_mode():
        return None
    try:
        target = Path(path).expanduser()
        if not target.is_absolute():
            raise ValueError("RX mutation checks require an absolute path")
        target = target.resolve(strict=False)
        if sys.platform == "win32":
            from ...utils.runtime_storage import runtime_home

            home = runtime_home()
            for root in (home / "e", home / "bin/runtime"):
                if _path_is_under(target, root) or (affects_subtree and _path_is_under(root, target)):
                    raise ValueError(f"任务文件只能写到可写挂载或会话临时目录：{target}")
        if not get_request_sandbox_rx_mounts():
            return None
        writable = [p.resolve() for p in get_request_sandbox_mounts()]
        readonly = normalize_rx_mount_overlaps(
            writable, get_request_sandbox_rx_mounts(), is_coding_mode=False,
        )
        workspace = Path(get_current_workspace_dir() or WORKING_DIR).resolve()
        # Validate all roots before granting any target exception.
        for raw_root in readonly:
            root = Path(raw_root)
            if _path_is_under(root, workspace) or _path_is_under(workspace, root):
                raise ValueError(f"RX mount conflicts with a writable root: {root}")
        target_is_writable = any(_path_is_under(target, rw) for rw in writable)
        for raw_root in readonly:
            root = Path(raw_root)
            if (affects_subtree and _path_is_under(root, target)) or (
                _path_is_under(target, root) and not target_is_writable
            ):
                raise ValueError(
                    f"Cannot modify {target}: it is inside or contains "
                    f"a sandbox_rx_mounts read-only directory: {root}"
                )
    except (OSError, RuntimeError, ValueError) as exc:
        return ToolChunk(
            is_last=True, state=ToolResultState.DENIED,
            content=[TextBlock(type="text", text=f"Error: {exc}")],
        )
    return None


def _norm_rel_text(path: str | Path) -> str:
    return str(path).replace("\\", "/").strip("/")


def _known_relative_prefixes() -> set[str]:
    workspace = Path(
        get_current_workspace_dir() or WORKING_DIR,
    ).expanduser()
    roots = [
        get_default_relative_file_dir(),
        workspace,
        *get_request_sandbox_mounts(),
        *allowed_new_absolute_roots(),
    ]

    prefixes: set[str] = set()
    for root in roots:
        root = Path(root).expanduser()
        root_text = _norm_rel_text(root)
        root_parts = root_text.split("/")
        for idx, part in enumerate(root_parts):
            if part in {SESSION_TMP_SUBDIR, ".devco_paw"}:
                prefixes.add("/".join(root_parts[idx:]))
        try:
            rel = root.resolve(strict=False).relative_to(
                workspace.resolve(strict=False),
            )
            rel_text = _norm_rel_text(rel)
            if rel_text and rel_text != ".":
                prefixes.add(rel_text)
                parts = rel_text.split("/")
                for idx, part in enumerate(parts):
                    suffix = "/".join(parts[idx:])
                    if part in {SESSION_TMP_SUBDIR, ".devco_paw"}:
                        prefixes.add(suffix)
            if root.name:
                prefixes.add(root.name)
        except ValueError:
            if root.name:
                prefixes.add(root.name)

    return {prefix for prefix in prefixes if prefix}


def _relative_output_prefix_correction(file_path: str) -> str | None:
    normalized = _norm_rel_text(file_path)
    if not normalized:
        return None

    matches = [
        prefix
        for prefix in _known_relative_prefixes()
        if normalized == prefix or normalized.startswith(prefix + "/")
    ]
    if not matches:
        return None

    prefix = max(matches, key=len)
    corrected = normalized[len(prefix) :].lstrip("/")
    return corrected or Path(normalized).name


def check_relative_output_path_prefix(file_path: str) -> ToolChunk | None:
    """Ask the model to retry if a relative path includes an output prefix."""

    if not is_non_coding_output_mode():
        return None
    if _is_absolute_path(file_path):
        return None

    corrected = _relative_output_prefix_correction(file_path)
    if corrected is None:
        return None

    default_dir = get_default_relative_file_dir()
    return ToolChunk(
        is_last=True,
        state=ToolResultState.DENIED,
        content=[
            TextBlock(
                type="text",
                text=(
                    "Error: The provided relative path appears to include "
                    "an output directory prefix.\n\n"
                    "In Non-Coding mode, new files must be created using a "
                    "path relative to the default output directory only. Do "
                    "not include the workspace directory, session scratch "
                    "directory, external mounted directory, or any parent "
                    "output directory prefix.\n\n"
                    f"Provided path:\n- {file_path}\n\n"
                    f"Default output directory:\n- {default_dir}\n\n"
                    f"Use this relative path instead:\n- {corrected}"
                ),
            ),
        ],
    )


def check_new_output_path(file_path: str) -> ToolChunk | None:
    """Validate a caller-provided output path for new file creation."""

    denial = check_new_absolute_output_path(file_path)
    if denial is not None:
        return denial

    return check_relative_output_path_prefix(file_path)


def resolve_new_output_path(
    file_path: str,
    *,
    default_name: str = "",
) -> str | ToolChunk:
    """Validate and resolve a new output path through the default output dir."""

    path = (file_path or "").strip() or default_name
    if not path:
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[
                TextBlock(
                    type="text",
                    text="Error: No output path provided.",
                ),
            ],
        )

    candidate = Path(path).expanduser()
    if not _is_absolute_path(path):
        default_dir = get_default_relative_file_dir()
        candidate = default_dir / path
        if is_non_coding_output_mode() and not _path_is_under(candidate, default_dir):
            return ToolChunk(
                is_last=True, state=ToolResultState.DENIED,
                content=[TextBlock(type="text", text=f"相对输出路径必须位于当前任务目录：{default_dir}")],
            )
    denial = check_resolved_rx_mutation(candidate)
    if denial is not None:
        return denial
    denial = check_new_output_path(path)
    if denial is not None:
        return denial
    return str(candidate.resolve(strict=False))
