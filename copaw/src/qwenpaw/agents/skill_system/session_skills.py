# -*- coding: utf-8 -*-
"""Session-scoped extra skills passed by chat clients.

``request_context.skills`` carries skill *names* (skill directory names).
Names that are not the agent's own skills are accumulated per session and
registered as extra AgentScope skill directories, so the toolkit renders
them into the ``<agent-skills>`` system-prompt block on every turn until
the session ends.  Names missing from the skill pool are reported back to
the user as a conversational notice (see the notice hook).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

from ...exceptions import SkillsError
from .store import (
    get_workspace_skills_dir,
    normalize_skill_dir_name,
    resolve_pool_skill_dir,
)

logger = logging.getLogger(__name__)

SESSION_SKILLS_KEY = "session_skills"
MAX_REQUEST_SKILLS = 20
MAX_SESSION_SKILLS = 64


def normalize_skill_names(
    raw: Any,
    limit: int = MAX_REQUEST_SKILLS,
) -> list[str]:
    """Coerce ``request_context.skills`` into a validated, deduped list."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        logger.warning(
            "request_context.skills must be a list of names; got %s",
            type(raw).__name__,
        )
        return []

    names: list[str] = []
    for item in raw[:limit]:
        if not isinstance(item, str):
            logger.warning("Ignoring non-string skill entry: %r", item)
            continue
        try:
            name = normalize_skill_dir_name(item)
        except SkillsError as exc:
            logger.warning("Ignoring invalid skill name %r: %s", item, exc)
            continue
        if name not in names:
            names.append(name)
    return names


def is_own_skill(
    name: str,
    effective_skills: Iterable[str] | None,
    workspace_dir: str | Path | None,
) -> bool:
    """Whether ``name`` belongs to the agent (enabled or merely present)."""
    if name in {str(s) for s in (effective_skills or ())}:
        return True
    from ...constant import WORKING_DIR

    base = get_workspace_skills_dir(Path(workspace_dir or WORKING_DIR))
    return (base / name / "SKILL.md").exists()


def merge_session_skill_names(
    *,
    persisted: Any,
    incoming: Any,
    effective_skills: Iterable[str] | None,
    workspace_dir: str | Path | None,
) -> list[str]:
    """Accumulate non-own skill names for the session.

    ``persisted ∪ incoming``, own names filtered, order preserved (first
    occurrence wins), truncated to ``MAX_SESSION_SKILLS``.
    """
    merged: list[str] = []
    candidates = [
        *normalize_skill_names(persisted, limit=MAX_SESSION_SKILLS),
        *normalize_skill_names(incoming),
    ]
    for name in candidates:
        if name in merged:
            continue
        if is_own_skill(name, effective_skills, workspace_dir):
            continue
        merged.append(name)

    if len(merged) > MAX_SESSION_SKILLS:
        logger.warning(
            "session_skills truncated: %d name(s) dropped (cap=%d)",
            len(merged) - MAX_SESSION_SKILLS,
            MAX_SESSION_SKILLS,
        )
        merged = merged[:MAX_SESSION_SKILLS]
    return merged


def resolve_session_skill_dirs(
    names: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Split session skill names into pool directories and missing names."""
    dirs: list[str] = []
    missing: list[str] = []
    for name in names:
        skill_dir = resolve_pool_skill_dir(name)
        if skill_dir is None:
            missing.append(name)
            logger.debug(
                "session skill '%s' not found in skill pool; not injected",
                name,
            )
            continue
        dirs.append(str(skill_dir))
    return dirs, missing


def format_missing_skills_notice(
    names: Iterable[str],
    language: str = "zh",
) -> str:
    """Build the conversational notice for names missing from the pool."""
    listed = "\n".join(f"- `{name}`" for name in names)
    if (language or "").lower().startswith("zh"):
        return (
            "以下技能未在技能池中找到：\n"
            f"{listed}\n\n"
            "请检查技能名是否与技能池中的技能目录名一致，"
            "或先将技能安装到技能池；处理完成后重新发送本条消息即可。"
        )
    return (
        "Skill(s) not found in the skill pool:\n"
        f"{listed}\n\n"
        "Check that the skill name(s) match the skill-pool directory "
        "names, or install the skill(s) into the pool first, then resend "
        "your message."
    )
