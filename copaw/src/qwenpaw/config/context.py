# -*- coding: utf-8 -*-
"""Context variable for agent workspace directory.

This module provides a context variable to pass the agent's workspace
directory to tool functions, allowing them to resolve relative paths
correctly in a multi-agent environment.

Relative paths for file tools resolve under a per-session directory
(``{workspace}/session_tmp/<session_id>/``) when a session id is active.
"""
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SESSION_TMP_SUBDIR = "session_tmp"

# Context variable to store the current agent's workspace directory
current_workspace_dir: ContextVar[Path | None] = ContextVar(
    "current_workspace_dir",
    default=None,
)


def get_current_workspace_dir() -> Path | None:
    """Get the current agent's workspace directory from context.

    Returns:
        Path to the current agent's workspace directory, or None if not set.
    """
    return current_workspace_dir.get()


def set_current_workspace_dir(workspace_dir: Path | None) -> None:
    """Set the current agent's workspace directory in context.

    Args:
        workspace_dir: Path to the agent's workspace directory.
    """
    current_workspace_dir.set(workspace_dir)


current_session_id: ContextVar[str | None] = ContextVar(
    "current_session_id",
    default=None,
)


def get_current_session_id() -> str | None:
    """Get the current session id from context."""
    return current_session_id.get()


def set_current_session_id(session_id: str | None) -> None:
    """Set the current session id for config/tool context."""
    current_session_id.set(session_id)


current_request_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "current_request_context",
    default=None,
)


def get_current_request_context() -> dict[str, Any]:
    """Return request-scoped context for the current agent invocation."""
    return current_request_context.get() or {}


def set_current_request_context(context: dict[str, Any] | None) -> None:
    """Set request-scoped context for tools spawned by this invocation."""
    current_request_context.set(context or {})


@dataclass(frozen=True)
class DefaultRelativeFileDir:
    """Resolved default base directory and why it was selected."""

    path: Path
    source: str


def get_session_scoped_work_dir(
    workspace_dir: Path | None = None,
    session_id: str | None = None,
    create: bool = True,
) -> Path:
    """Return the base directory for relative file-tool paths in this session.

    When *session_id* is set (explicitly or from request context), returns
    ``{workspace}/session_tmp/<sanitized_session_id>/`` (created if missing).
    Otherwise returns the agent workspace root.

    Args:
        workspace_dir: Agent workspace; uses context or global default if None.
        session_id: Session id; uses :func:`get_current_session_id` if None.
        create: Whether to create the resolved session directory.

    Returns:
        Directory used as the root for unresolved relative paths.
    """
    from ..constant import WORKING_DIR

    workspace = Path(
        workspace_dir or get_current_workspace_dir() or WORKING_DIR,
    ).expanduser()

    sid = session_id
    if sid is None:
        sid = get_current_session_id()
    if sid is None:
        request_context = get_current_request_context()
        raw_sid = request_context.get("session_id")
        if isinstance(raw_sid, str):
            sid = raw_sid
    if sid is None:
        try:
            from ..app.agent_context import (
                get_current_session_id as _get_app_session_id,
            )

            sid = _get_app_session_id()
        except ImportError:
            sid = None

    if not sid or not str(sid).strip():
        return workspace

    try:
        from ..app.chats.session import sanitize_filename
    except ImportError:
        from ..app.runner.session import sanitize_filename

    safe_sid = sanitize_filename(str(sid).strip()) or "default"
    session_dir = workspace / SESSION_TMP_SUBDIR / safe_sid
    if create:
        session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def get_request_sandbox_mounts() -> list[Path]:
    """Return request-scoped sandbox mounts from the current context."""

    request_context = get_current_request_context()
    raw_mounts = request_context.get("sandbox_mounts") or []
    if not isinstance(raw_mounts, list):
        return []

    mounts: list[Path] = []
    for raw in raw_mounts:
        if not isinstance(raw, str) or not raw.strip():
            continue
        mounts.append(Path(raw).expanduser())
    return mounts


def get_request_sandbox_rx_mounts() -> list[Path]:
    """Return request-scoped read/execute-only mounts from the context."""

    request_context = get_current_request_context()
    raw_mounts = request_context.get("sandbox_rx_mounts") or []
    if not isinstance(raw_mounts, list):
        return []

    mounts: list[Path] = []
    for raw in raw_mounts:
        if not isinstance(raw, str) or not raw.strip():
            continue
        mounts.append(Path(raw).expanduser())
    return mounts


def request_is_coding_mode() -> bool:
    """Whether the current request is operating in Coding Mode."""

    request_context = get_current_request_context()
    return bool(request_context.get("is_coding_mode"))


def describe_default_relative_file_dir(
    workspace_dir: Path | None = None,
    session_id: str | None = None,
    is_coding_mode: bool | None = None,
    sandbox_mounts: list[str | Path] | None = None,
    create_session_dir: bool = False,
) -> DefaultRelativeFileDir:
    """Resolve the request-scoped default base dir and its source.

    This is the shared source of truth for prompt text and tool defaults.
    Set *create_session_dir* only for callers that are about to use the
    directory for file or shell execution.
    """

    from ..constant import WORKING_DIR

    workspace = Path(
        workspace_dir or get_current_workspace_dir() or WORKING_DIR,
    ).expanduser()

    coding = (
        request_is_coding_mode()
        if is_coding_mode is None
        else is_coding_mode
    )
    if coding:
        return DefaultRelativeFileDir(
            path=workspace,
            source="agent workspace",
        )

    if sandbox_mounts is None:
        mounts = get_request_sandbox_mounts()
    else:
        mounts = [
            Path(raw).expanduser()
            for raw in sandbox_mounts
            if str(raw).strip()
        ]
    if mounts:
        return DefaultRelativeFileDir(
            path=mounts[0],
            source="first external mounted directory",
        )

    session_dir = get_session_scoped_work_dir(
        workspace_dir=workspace,
        session_id=session_id,
        create=create_session_dir,
    )
    source = (
        "session scratch directory"
        if session_dir != workspace
        else "agent workspace"
    )
    return DefaultRelativeFileDir(path=session_dir, source=source)


def get_default_relative_file_dir(
    workspace_dir: Path | None = None,
    session_id: str | None = None,
) -> Path:
    """Return the base dir for relative paths in file and shell tools.

    Coding Mode keeps the historical workspace-relative behavior. Non-Coding
    Mode routes relative paths to an explicit external mount when present,
    otherwise to the per-session scratch directory under the workspace.
    """

    return describe_default_relative_file_dir(
        workspace_dir=workspace_dir,
        session_id=session_id,
        create_session_dir=True,
    ).path


# Context variable to store the recent_max_bytes limit
current_recent_max_bytes: ContextVar[int | None] = ContextVar(
    "current_recent_max_bytes",
    default=None,
)


def get_current_recent_max_bytes() -> int | None:
    """Get the current agent's recent_max_bytes limit from context.

    Returns:
        Byte limit for recent tool output truncation, or None if not set.
    """
    return current_recent_max_bytes.get()


def set_current_recent_max_bytes(max_bytes: int | None) -> None:
    """Set the current agent's recent_max_bytes limit in context.

    Args:
        max_bytes: Byte limit for recent tool output truncation.
    """
    current_recent_max_bytes.set(max_bytes)


# Context variable to store the configured shell command timeout
current_shell_command_timeout: ContextVar[float | None] = ContextVar(
    "current_shell_command_timeout",
    default=None,
)


def get_current_shell_command_timeout() -> float | None:
    """Get the configured default timeout for execute_shell_command.

    Returns:
        Timeout in seconds, or None if not configured.
    """
    return current_shell_command_timeout.get()


def set_current_shell_command_timeout(timeout: float | None) -> None:
    """Set the configured default timeout for execute_shell_command.

    Args:
        timeout: Timeout in seconds.
    """
    current_shell_command_timeout.set(timeout)


current_shell_command_executable: ContextVar[str | None] = ContextVar(
    "current_shell_command_executable",
    default=None,
)


def get_current_shell_command_executable() -> str | None:
    """Get the configured shell executable for execute_shell_command.

    Returns:
        Path to the shell executable, or None if not configured.
    """
    return current_shell_command_executable.get()


def set_current_shell_command_executable(executable: str | None) -> None:
    """Set the configured shell executable for execute_shell_command.

    Args:
        executable: Path to the shell executable (e.g. "/bin/bash").
    """
    current_shell_command_executable.set(executable)


current_toolkit: ContextVar[Any | None] = ContextVar(
    "current_toolkit",
    default=None,
)


def get_current_toolkit() -> Any | None:
    """Get the current agent Toolkit from context."""
    return current_toolkit.get()


def set_current_toolkit(toolkit: Any | None) -> None:
    """Set the current agent Toolkit for tool helpers."""
    current_toolkit.set(toolkit)
