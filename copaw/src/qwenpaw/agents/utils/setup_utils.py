# -*- coding: utf-8 -*-
"""Setup and initialization utilities for agent configuration.

This module handles copying markdown configuration files to
the working directory.
"""
import logging
from pathlib import Path

from ...constant import SUPPORTED_AGENT_LANGUAGES
from ...prompt_resources import templates

logger = logging.getLogger(__name__)

_TEMPLATE_OVERRIDE_FILENAMES = {
    "AGENTS.md",
    "BOOTSTRAP.md",
    "PROFILE.md",
    "SOUL.md",
}


def normalize_agent_language(language: str) -> str:
    """Map *language* to a supported agent language"""
    if language in SUPPORTED_AGENT_LANGUAGES:
        return language
    return "en"


def copy_md_files(
    language: str,
    skip_existing: bool = False,
    workspace_dir: Path | None = None,
    exclude_filenames: set[str] | None = None,
) -> list[str]:
    """Copy built-in Markdown templates to the working directory.

    Args:
        language: Supported agent language code.
        skip_existing: If True, skip files that already exist in working dir.
        workspace_dir: Target workspace directory. If None, uses WORKING_DIR.
        exclude_filenames: File names to skip while copying.

    Returns:
        List of copied file names.
    """
    from ...constant import WORKING_DIR

    # Use provided workspace_dir or default to WORKING_DIR
    target_dir = workspace_dir if workspace_dir is not None else WORKING_DIR

    files = templates(language)
    if not files:
        logger.warning(
            "MD templates not found for %s, falling back to 'en'",
            language,
        )
        files = templates("en")
        if not files:
            logger.error("Default 'en' md files not found either")
            return []

    # Ensure target directory exists
    target_dir.mkdir(parents=True, exist_ok=True)

    # Copy all .md files to target directory
    copied_files: list[str] = []
    for filename, content in files.items():
        if exclude_filenames and filename in exclude_filenames:
            continue
        target_file = target_dir / filename
        if skip_existing and target_file.exists():
            logger.debug("Skipped existing md file: %s", filename)
            continue
        try:
            target_file.write_bytes(content)
            logger.debug("Copied md file: %s", filename)
            copied_files.append(filename)
        except Exception as e:
            logger.error(
                "Failed to copy md file '%s': %s",
                filename,
                e,
            )

    if copied_files:
        logger.debug(
            "Copied %d md file(s) [%s] to %s",
            len(copied_files),
            language,
            target_dir,
        )

    return copied_files


def _template_fallback_language_order(language: str) -> list[str]:
    ordered: list[str] = []
    for lang_opt in (language, "en", "zh", "ru"):
        if lang_opt not in ordered:
            ordered.append(lang_opt)
    return ordered


def _remove_bootstrap_from_workspace(workspace_dir: Path) -> None:
    bootstrap = workspace_dir / "BOOTSTRAP.md"
    if not bootstrap.exists():
        return
    try:
        bootstrap.unlink()
        logger.info(
            "Removed BOOTSTRAP.md from builtin QA workspace %s",
            workspace_dir,
        )
    except OSError as e:
        logger.warning("Could not remove BOOTSTRAP.md: %s", e)


def copy_template_md_files(
    template_id: str,
    language: str,
    workspace_dir: Path | str,
    *,
    only_if_missing: bool = True,
) -> list[str]:
    """Copy template-specific markdown files into an agent workspace.

    Each file falls back from the requested language to the built-in
    fallback languages.

    Args:
        template_id: Built-in template group name.
        language: Supported agent language code.
        workspace_dir: Agent workspace root.
        only_if_missing: If True, skip targets that already exist.

    Returns:
        List of copied or overwritten file names.
    """
    workspace_dir = Path(workspace_dir).expanduser()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    files: dict[str, bytes] = {}
    for lang in _template_fallback_language_order(language):
        for filename, content in templates(lang, template_id).items():
            files.setdefault(filename, content)
    if not files:
        logger.warning(
            "Workspace templates not found: %s",
            template_id,
        )
        return []

    copied_files: list[str] = []
    for filename, content in files.items():
        target = workspace_dir / filename
        if only_if_missing and target.exists():
            continue
        try:
            target.write_bytes(content)
            copied_files.append(filename)
        except OSError as exc:
            logger.warning("Failed to copy workspace template %s: %s", filename, exc)
    _remove_bootstrap_from_workspace(workspace_dir)
    return copied_files


def copy_workspace_md_files(
    language: str,
    workspace_dir: Path | str,
    *,
    md_template_id: str | None = None,
    only_if_missing: bool = True,
) -> list[str]:
    """Copy common workspace md files plus optional template overrides."""
    workspace_dir = Path(workspace_dir).expanduser()

    copied_files = copy_md_files(
        language,
        skip_existing=only_if_missing,
        workspace_dir=workspace_dir,
        exclude_filenames=(
            _TEMPLATE_OVERRIDE_FILENAMES if md_template_id else None
        ),
    )

    if not md_template_id:
        return copied_files

    copied_files.extend(
        copy_template_md_files(
            md_template_id,
            language,
            workspace_dir,
            only_if_missing=only_if_missing,
        ),
    )
    return copied_files


def copy_builtin_qa_md_files(
    language: str,
    workspace_dir: Path | str,
    *,
    only_if_missing: bool = True,
) -> list[str]:
    """Backward-compatible wrapper for builtin QA workspace templates."""
    return copy_workspace_md_files(
        language,
        workspace_dir,
        md_template_id="qa",
        only_if_missing=only_if_missing,
    )
