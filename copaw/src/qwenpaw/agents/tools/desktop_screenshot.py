# -*- coding: utf-8 -*-
"""Desktop/screen screenshot tool."""

import json
import os
import platform
import time
import mimetypes
from pathlib import Path

from agentscope.message import DataBlock, TextBlock, URLSource
from agentscope.tool import ToolChunk
from agentscope.message import ToolResultState

from ...config.context import get_request_sandbox_mounts
from ...runtime.tool_registry import tool_descriptor
from .output_path_guard import (
    check_rx_mount_write_denial,
    check_resolved_rx_mutation,
    is_non_coding_output_mode,
    resolve_new_output_path,
)
from .file_io import (
    _path_to_file_url,
)


def _tool_error(msg: str) -> ToolChunk:
    return ToolChunk(
        is_last=True,
        state=ToolResultState.SUCCESS,
        content=[
            TextBlock(
                type="text",
                text=json.dumps(
                    {"ok": False, "error": msg},
                    ensure_ascii=False,
                    indent=2,
                ),
            ),
        ],
    )


def _tool_ok(path: str, message: str) -> ToolChunk:
    file_url = _path_to_file_url(path)
    mime_type, _ = mimetypes.guess_type(path)
    if mime_type is None:
        mime_type = "image/*"
    return ToolChunk(
        is_last=True,
        state=ToolResultState.SUCCESS,
        content=[
            DataBlock(
                source=URLSource(
                    url=file_url,
                    media_type=mime_type,
                ),
                name=os.path.basename(path),
            ),
            TextBlock(
                type="text",
                text=json.dumps(
                    {
                        "ok": True,
                        "path": os.path.abspath(path),
                        "message": message,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            ),
        ],
    )


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _desktop_screenshot_path_denial(
    path: Path,
    allowed_root: Path,
) -> ToolChunk:
    """Build a denial response for the screenshot first-mount policy."""

    return ToolChunk(
        is_last=True,
        state=ToolResultState.DENIED,
        content=[
            TextBlock(
                type="text",
                text=json.dumps(
                    {
                        "ok": False,
                        "error_code": (
                            "DESKTOP_SCREENSHOT_OUTSIDE_FIRST_MOUNT"
                        ),
                        "error": (
                            "desktop_screenshot absolute paths are "
                            "restricted to the first external mounted "
                            "directory in Non-Coding mode."
                        ),
                        "requested_path": str(path),
                        "allowed_root": str(allowed_root),
                        "hint": (
                            "Use a relative path to save the screenshot "
                            "under the first mounted directory."
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            ),
        ],
    )


def _resolve_desktop_screenshot_path(path: str) -> str | ToolChunk:
    """Resolve screenshot output path consistently with write_file.

    Relative paths resolve through the default output directory.  The
    first-mount restriction applies only to caller-provided absolute
    paths, mirroring ``_resolve_write_file_path`` in ``file_io``; the
    check runs before resolution so Coding mode is untouched.
    """
    path = (path or "").strip()

    candidate = Path(path).expanduser()
    if path and is_non_coding_output_mode() and candidate.is_absolute():
        mounts = get_request_sandbox_mounts()
        if mounts:
            resolved_abs = candidate.resolve(strict=False)
            first_mount = mounts[0].resolve(strict=False)
            if not _path_is_under(resolved_abs, first_mount):
                return _desktop_screenshot_path_denial(
                    resolved_abs,
                    first_mount,
                )

    denial = check_rx_mount_write_denial(path)
    if denial is not None:
        return denial

    resolved = resolve_new_output_path(
        path,
        default_name=f"desktop_screenshot_{int(time.time())}.png",
    )
    if isinstance(resolved, ToolChunk):
        return resolved
    if not resolved.lower().endswith(".png"):
        resolved = resolved.rstrip("/\\") + ".png"

    denial = check_resolved_rx_mutation(resolved)
    if denial is not None:
        return denial
    if is_non_coding_output_mode():
        mounts = get_request_sandbox_mounts()
        if mounts and not _path_is_under(Path(resolved), mounts[0]):
            return _desktop_screenshot_path_denial(Path(resolved), mounts[0])
    return resolved


def _capture_mss(path: str) -> ToolChunk:
    """Full-screen capture using mss (Windows, Linux, macOS)."""
    try:
        import mss
    except ImportError:
        return _tool_error(
            "desktop_screenshot requires the 'mss' package. "
            "Install with: pip install mss",
        )
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with mss.mss() as sct:
            # mon=0: all monitors combined
            sct.shot(mon=0, output=path)
        if not os.path.isfile(path):
            return _tool_error("mss reported success but file was not created")
        return _tool_ok(path, f"Desktop screenshot saved to {path}")
    except Exception as e:
        return _tool_error(f"desktop_screenshot (mss) failed: {e!s}")


async def _capture_macos_screencapture(
    path: str,
    capture_window: bool,
) -> ToolChunk:
    """macOS: screencapture (supports window selection with -w)."""
    import asyncio

    from ...tool_calls import cancellable_wait

    cmd = ["screencapture", "-x", path]
    if capture_window:
        cmd.insert(-1, "-w")
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await cancellable_wait(
            proc.communicate(),
            fallback_secs=30,
        )
        if proc.returncode != 0:
            stderr_str = (stderr or b"").decode().strip() or "Unknown error"
            return _tool_error(f"screencapture failed: {stderr_str}")
        if not os.path.isfile(path):
            return _tool_error(
                "screencapture reported success but file was not created",
            )
        return _tool_ok(path, f"Desktop screenshot saved to {path}")
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return _tool_error(
            "screencapture timed out (e.g. window selection cancelled)",
        )
    except Exception as e:
        return _tool_error(f"desktop_screenshot failed: {e!s}")


@tool_descriptor(requires_sandbox=("file_write",), async_execution=True)
async def desktop_screenshot(
    path: str = "",
    capture_window: bool = False,
) -> ToolChunk:
    """Capture a screenshot of the whole desktop, or one window on macOS.

    The full-screen capture combines all monitors into a single PNG and
    works on Windows, Linux and macOS (requires the ``mss`` package; a
    missing install is reported in the error message).

    ``capture_window=True`` is macOS-only: the system prompts the user to
    click a window and captures just that window.  The call blocks for up
    to 30s waiting for the click, and fails if the user cancels or does
    not respond — only request it when window-level capture is truly
    needed.  On Windows/Linux the flag is ignored and the full screen is
    captured.

    ``path`` may be absolute (``~`` is expanded) or relative; relative
    paths resolve from the request's default file directory — the agent
    workspace in Coding Mode, or the first external mount (falling back
    to the session scratch directory) in Non-Coding mode.  An empty
    ``path`` saves as ``desktop_screenshot_<unix-seconds>.png`` in that
    directory.  Output is always PNG: a missing ``.png`` suffix is
    appended (``shot.jpg`` → ``shot.jpg.png``), and missing parent
    directories are created automatically.  In Non-Coding mode the same
    guards as ``write_file`` apply (no output-directory prefix embedded
    in a relative path; a new absolute path must stay within the allowed
    output roots), and an absolute ``path`` must additionally stay under
    the first external mounted directory when mounts exist; otherwise the
    call is denied with a hint.

    Args:
        path (`str`, optional):
            Where to save the PNG.  Empty → auto-named file in the
            request's default file directory.  Defaults to "".
        capture_window (`bool`, optional):
            macOS only: let the user click a window to capture (blocks
            up to 30s).  Ignored on Windows/Linux.  Defaults to False.

    Returns:
        On success, the screenshot is attached as an image block (visible
        to multimodal models) plus a JSON text block ``{"ok": true,
        "path": "<absolute path>", "message": "..."}``.  On failure, a
        JSON block ``{"ok": false, "error": "..."}``.  Denials return an
        ``error_code`` of ``DESKTOP_SCREENSHOT_OUTSIDE_FIRST_MOUNT``.
    """
    resolved = _resolve_desktop_screenshot_path(path)
    if isinstance(resolved, ToolChunk):
        return resolved
    path = resolved

    system = platform.system()

    # macOS: optional window selection via screencapture -w
    if system == "Darwin" and capture_window:
        return await _capture_macos_screencapture(path, capture_window=True)

    # Full-screen on all platforms (macOS, Linux, Windows) via mss
    return _capture_mss(path)
