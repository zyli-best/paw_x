# -*- coding: utf-8 -*-
"""Resolve explicitly trusted Windows skill tool invocations.

Trust is configured outside skill-owned files.  A configured skill grants
host execution only to a simple command whose concrete EXE/script entrypoint
resolves inside that installed workspace skill directory.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from ..constant import WORKING_DIR

_CONFIG_NAME = "windows_trusted_skills.json"
_ENTRY_SUFFIXES = {".exe", ".com", ".bat", ".cmd", ".ps1", ".py"}
_TOKEN_RE = re.compile(r'''"(?:[^"\\]|\\.)*"|'[^']*'|\S+''')
_POWERSHELL_NAMES = {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}
_PYTHON_NAMES = {"python", "python.exe", "pythonw", "pythonw.exe"}


@dataclass(frozen=True)
class TrustedSkillExecution:
    """Verified association between a command entrypoint and a skill."""

    skill_name: str
    skill_root: Path
    entrypoint: Path | None
    match_mode: str = "resolved_entrypoint"


def trusted_skills_config_path() -> Path:
    """Return the standalone user-controlled trust configuration path."""
    configured_root = (
        os.environ.get("QWENPAW_WORKING_DIR")
        or os.environ.get("COPAW_WORKING_DIR")
    )
    root = Path(configured_root).expanduser() if configured_root else WORKING_DIR
    return Path(root) / "config" / _CONFIG_NAME


def _load_trusted_skill_names(config_path: Path) -> frozenset[str]:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return frozenset()

    if isinstance(payload, list):
        raw_names = payload
    elif isinstance(payload, dict):
        raw_names = payload.get("trusted_skills", [])
    else:
        return frozenset()
    if not isinstance(raw_names, list):
        return frozenset()

    names: set[str] = set()
    for value in raw_names:
        if not isinstance(value, str):
            continue
        name = value.strip()
        if name and name not in {".", ".."} and not any(
            char in name for char in ("/", "\\", ":")
        ):
            names.add(name.casefold())
    return frozenset(names)


def _split_simple_cd_chain(
    command: str,
    initial_cwd: Path,
) -> tuple[str, Path, bool] | None:
    """Accept a command, or one ``cd [/d] <dir> && command`` prefix.

    Other shell composition remains untrusted.  The returned cwd is resolved
    before the entrypoint is checked, so a relative executable cannot escape
    the trusted skill merely because the command text mentions its directory.
    """
    single = double = False
    separator_at: int | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if char == "'" and not double:
            single = not single
            index += 1
            continue
        if char == '"' and not single:
            double = not double
            index += 1
            continue
        if single or double:
            index += 1
            continue
        if char in ";|<>":
            return None
        if char == "&":
            if command[index : index + 2] == "&&":
                if separator_at is not None:
                    return None
                separator_at = index
                index += 2
                continue
            # One leading PowerShell call operator belongs to the actual
            # command.  Any other single ampersand composes/backgrounds work.
            active_start = separator_at + 2 if separator_at is not None else 0
            if command[active_start:index].strip():
                return None
        index += 1
    if single or double:
        return None
    if separator_at is None:
        return command.strip(), initial_cwd, False

    change_dir = command[:separator_at].strip()
    body = command[separator_at + 2 :].strip()
    tokens = [_unquote(token) for token in _TOKEN_RE.findall(change_dir)]
    if not tokens or tokens[0].casefold() not in {"cd", "chdir"}:
        return None
    if len(tokens) == 3 and tokens[1].casefold() == "/d":
        directory_token = tokens[2]
    elif len(tokens) == 2:
        directory_token = tokens[1]
    else:
        return None
    if not body or any(char in directory_token for char in ("$", "%", "`")):
        return None
    directory = Path(os.path.expanduser(directory_token))
    if not directory.is_absolute():
        directory = initial_cwd / directory
    try:
        directory = directory.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not directory.is_dir():
        return None
    return body, directory, True


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _entrypoint_token(command: str) -> str | None:
    if not command.strip():
        return None
    tokens = [_unquote(token) for token in _TOKEN_RE.findall(command.strip())]
    if tokens and tokens[0] == "&":
        tokens.pop(0)
    if not tokens:
        return None

    executable_name = Path(tokens[0].replace("\\", "/")).name.casefold()
    if executable_name in _POWERSHELL_NAMES:
        lowered = [token.casefold() for token in tokens]
        if "-command" in lowered or "-encodedcommand" in lowered:
            return None
        try:
            file_index = lowered.index("-file")
        except ValueError:
            return None
        return tokens[file_index + 1] if file_index + 1 < len(tokens) else None
    if executable_name in _PYTHON_NAMES:
        if len(tokens) < 2 or tokens[1].startswith("-"):
            return None
        return tokens[1]
    return tokens[0]


def _resolve_entrypoint(token: str, cwd: Path) -> Path | None:
    if not token or "$" in token or "%" in token or "`" in token:
        return None
    candidate = Path(os.path.expanduser(token))
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not candidate.is_file() or candidate.suffix.casefold() not in _ENTRY_SUFFIXES:
        return None
    return candidate


def _workspace_skill_roots(workspace_dir: Path) -> list[Path]:
    """Return skill directories owned by the current agent workspace."""
    container = workspace_dir / "skills"
    try:
        return [path for path in container.iterdir() if path.is_dir()]
    except OSError:
        return []


def _declared_skill_name(skill_root: Path) -> str | None:
    """Read the frontmatter name used by QwenPaw's skill registry."""
    skill_md = skill_root / "SKILL.md"
    try:
        with skill_md.open("r", encoding="utf-8-sig") as handle:
            if handle.readline().strip() != "---":
                return skill_root.name
            for line in handle:
                stripped = line.strip()
                if stripped == "---":
                    break
                match = re.match(r"^name\s*:\s*(.+?)\s*$", stripped, re.I)
                if match:
                    return match.group(1).strip(" \t\"'")
    except (OSError, UnicodeError):
        return None
    return skill_root.name


def resolve_trusted_skill_execution(
    command: str,
    cwd: str | Path,
    *,
    workspace_dir: str | Path,
    platform: str | None = None,
    config_path: str | Path | None = None,
) -> TrustedSkillExecution | None:
    """Return verified trust metadata, or ``None`` to keep sandboxing.

    This function is deliberately fail-closed.  It does not infer trust from
    the current prompt, active skill, cwd alone, or a skill name in command
    text.  The concrete entrypoint must exist under an installed configured
    skill root containing ``SKILL.md``.
    """
    if (platform or sys.platform) != "win32":
        return None
    path = Path(config_path) if config_path else trusted_skills_config_path()
    trusted_names = _load_trusted_skill_names(path)
    if not trusted_names:
        return None

    try:
        resolved_cwd = Path(cwd).resolve(strict=True)
        resolved_workspace = Path(workspace_dir).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved_workspace.is_dir():
        return None

    skill_roots: list[Path] = []
    for raw_root in _workspace_skill_roots(resolved_workspace):
        try:
            root = raw_root.resolve(strict=True)
            root.relative_to(resolved_workspace / "skills")
        except (OSError, RuntimeError, ValueError):
            continue
        if (root / "SKILL.md").is_file():
            skill_roots.append(root)

    # Explicit product trust model: a configured skill name appearing in the
    # command or cwd trusts the whole shell invocation.  This intentionally
    # runs before entrypoint parsing so PowerShell -Command, variables,
    # pipelines, and other shell forms used by a trusted skill are supported.
    normalized_command = command.replace("/", "\\").casefold()
    normalized_cwd = str(resolved_cwd).replace("/", "\\").casefold()
    for trusted_name in sorted(trusted_names, key=len, reverse=True):
        if (
            trusted_name not in normalized_command
            and trusted_name not in normalized_cwd
        ):
            continue
        matched_root: Path | None = None
        for root in skill_roots:
            declared_name = _declared_skill_name(root)
            if (
                trusted_name in root.name.casefold()
                or (
                    declared_name is not None
                    and trusted_name in declared_name.casefold()
                )
            ):
                matched_root = root
                break
        if matched_root is None:
            continue
        normalized_root = str(matched_root).replace("/", "\\").casefold()
        try:
            cwd_is_inside_skill = resolved_cwd.is_relative_to(matched_root)
        except (OSError, RuntimeError):
            cwd_is_inside_skill = False
        if normalized_root not in normalized_command and not cwd_is_inside_skill:
            continue
        return TrustedSkillExecution(
            skill_name=trusted_name,
            skill_root=matched_root,
            entrypoint=None,
            match_mode="substring",
        )

    parsed = _split_simple_cd_chain(command, resolved_cwd)
    if parsed is None:
        return None
    command_body, effective_cwd, has_cd_prefix = parsed
    token = _entrypoint_token(command_body)
    entrypoint = _resolve_entrypoint(token or "", effective_cwd)
    if entrypoint is None:
        return None

    for root in skill_roots:
        declared_name = _declared_skill_name(root)
        configured_match = root.name.casefold() in trusted_names or (
            declared_name is not None
            and declared_name.casefold() in trusted_names
        )
        if not configured_match:
            continue
        try:
            if has_cd_prefix or not Path(token or "").is_absolute():
                effective_cwd.relative_to(root)
            entrypoint.relative_to(root)
        except ValueError:
            continue
        return TrustedSkillExecution(
            skill_name=declared_name or root.name,
            skill_root=root,
            entrypoint=entrypoint,
            match_mode="resolved_entrypoint",
        )
    return None
