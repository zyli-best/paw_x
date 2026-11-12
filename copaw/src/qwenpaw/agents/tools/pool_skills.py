"""Tools for the shared skill pool: paginated listing and counting."""

# NOTE: no ``from __future__ import annotations`` here — FunctionTool builds
# the model-facing JSON schema from the wrapped function's runtime
# annotations, and stringified ones fail pydantic's resolution (same
# reasoning as recall_tool.py).

import logging
from typing import Optional
from xml.sax.saxutils import escape

from agentscope.message import TextBlock
from agentscope.message import ToolResultState
from agentscope.tool import ToolChunk

from ...runtime.tool_registry import tool_descriptor
from ..skill_system.store import get_skill_pool_dirs, read_skill_from_dir

logger = logging.getLogger(__name__)


def _text_response(text: str) -> ToolChunk:
    """Wrap text in a single-TextBlock ToolChunk."""
    return ToolChunk(
        is_last=True,
        state=ToolResultState.SUCCESS,
        content=[TextBlock(type="text", text=text)],
    )


@tool_descriptor(async_execution=True, non_coding_only=True)
async def list_skills(
    offset: Optional[int | str] = None,
    limit: Optional[int | str] = None,
) -> ToolChunk:
    """List skills in the local shared skill pool (Non-Coding mode only).

    Returns local pool skills with their name, description and directory, one
    page at a time. Pool skills are equivalent to the agent's own skills.
    To use one, call Skill(skill="<name>") with the returned name to load
    its instructions and current request parameters, then follow them.

    The ``<skill-pool>`` header carries ``total`` / ``offset`` / ``limit``
    / ``shown`` attributes (plus ``next_offset`` when more skills remain),
    and every page ends with a line stating which slice it covers and the
    ``offset`` to pass for the next page. Use ``get_skill_count`` when
    only the total is needed. If a page comes back truncated in context,
    call again with a smaller ``limit`` (e.g. 10).

    Args:
        offset (`int | str`, optional):
            Index of the first skill to return (0-based). Defaults to 0.
            Decimal strings are accepted for tool-call compatibility.
        limit (`int | str`, optional):
            Maximum number of skills to return, clamped to 1-100.
            Defaults to 20. Decimal strings are accepted for tool-call
            compatibility.
    """
    # Numeric strings are accepted for tool-call compatibility (same
    # pattern as read_file's start_line / end_line).
    if offset is not None:
        try:
            offset = int(offset)
        except (ValueError, TypeError):
            return _text_response(
                f"Error: offset must be an integer, got {offset!r}.",
            )
    else:
        offset = 0
    if limit is not None:
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            return _text_response(
                f"Error: limit must be an integer, got {limit!r}.",
            )
    else:
        limit = 20
    offset = max(0, offset)
    limit = min(100, max(1, limit))

    try:
        seen: set[str] = set()
        entries: list[tuple[str, str, str]] = []
        for pool_dir in get_skill_pool_dirs():
            if not pool_dir.is_dir():
                continue
            for child in sorted(pool_dir.iterdir()):
                if not child.is_dir() or child.name in seen:
                    continue
                info = read_skill_from_dir(child, "pool")
                if info is None:
                    continue
                seen.add(child.name)
                entries.append(
                    (info.name, info.description or "", str(child)),
                )
    except Exception as exc:  # tool output must never raise
        logger.error("list_skills failed: %s", exc)
        return _text_response(f"Error: failed to list skill pool: {exc}")

    if not entries:
        return _text_response("The skill pool is empty.")

    total = len(entries)
    page = entries[offset : offset + limit]
    shown = len(page)
    next_offset = offset + shown if offset + shown < total else None

    attrs = (
        f'total="{total}" offset="{offset}" limit="{limit}" '
        f'shown="{shown}"'
    )
    if next_offset is not None:
        attrs += f' next_offset="{next_offset}"'

    lines = [
        f"<skill-pool {attrs}>",
        "Skills from the shared skill pool. They are equivalent to your "
        'own skills. To use one, call Skill(skill="<name>") with the '
        "returned name to load its instructions and current request "
        "parameters, then follow them.",
    ]
    for name, description, directory in page:
        lines.append("<skill>")
        lines.append(f"<name>{escape(name)}</name>")
        lines.append(f"<description>{escape(description)}</description>")
        lines.append(f"<dir>{escape(directory)}</dir>")
        lines.append("</skill>")
    lines.append("</skill-pool>")

    if shown == 0:
        lines.append(
            f"No skills at offset={offset}: the pool has {total} "
            f"skills (valid offsets are 0-{max(0, total - 1)}).",
        )
    elif next_offset is not None:
        lines.append(
            f"Showing skills {offset + 1}-{offset + shown} of {total}. "
            f"Call list_skills(offset={next_offset}) for the next page.",
        )
    else:
        lines.append(
            f"Showing skills {offset + 1}-{offset + shown} of {total}. "
            "All skills shown.",
        )
    return _text_response("\n".join(lines))


@tool_descriptor(async_execution=True, non_coding_only=True)
async def get_skill_count() -> ToolChunk:
    """Return the total number of skills in the shared skill pool.

    Lightweight probe for large pools: query the total first, then page
    through ``list_skills`` with ``offset`` / ``limit``. The number
    matches the ``total`` attribute of ``list_skills``.

    Returns:
        `ToolChunk`: JSON text ``{"skills": N}``.
    """
    try:
        seen: set[str] = set()
        count = 0
        for pool_dir in get_skill_pool_dirs():
            if not pool_dir.is_dir():
                continue
            for child in sorted(pool_dir.iterdir()):
                if not child.is_dir() or child.name in seen:
                    continue
                info = read_skill_from_dir(child, "pool")
                if info is None:
                    continue
                seen.add(child.name)
                count += 1
    except Exception as exc:  # tool output must never raise
        logger.error("get_skill_count failed: %s", exc)
        return _text_response(f"Error: failed to count skill pool: {exc}")
    return _text_response('{"skills": %d}' % count)
