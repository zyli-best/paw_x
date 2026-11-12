# -*- coding: utf-8 -*-
"""Windows Subversion runtime discovery for AppContainer shell sandboxing."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


_SVN_TOOL_RE = re.compile(
    r"(?<![\w.-])(?:svn|svnadmin|svnlook|svnserve|svnversion)"
    r"(?:\.exe)?(?![\w.-])",
    re.IGNORECASE,
)
_SVN_METADATA_RE = re.compile(r"(?<![\w.-])\.svn(?:[\\/ ]|$)", re.IGNORECASE)


@dataclass(frozen=True)
class WindowsSvnDiscovery:
    requested: bool = False
    rx_roots: list[str] = field(default_factory=list)
    rw_roots: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def discover_windows_svn_runtime(
    workspace_dir: str,
    session_id: str | None = None,
    command: str = "",
    cwd: str | None = None,  # pylint: disable=unused-argument
) -> WindowsSvnDiscovery:
    """Discover SVN executable roots for Windows shell commands."""
    if not _command_needs_svn(command):
        return WindowsSvnDiscovery()

    workspace = Path(workspace_dir).expanduser().resolve()
    warnings: list[str] = []
    roots = _discover_svn_roots()

    tmp_root = workspace / "session_tmp" / _safe_session_id(session_id)
    svn_config_dir = tmp_root / "svn-config"
    svn_config_dir.mkdir(parents=True, exist_ok=True)

    env = {
        "SVN_CONFIG_DIR": str(svn_config_dir),
    }
    path_roots = _runtime_path_roots(roots)
    if path_roots:
        env[_path_env_key()] = _prepend_env_path(path_roots)
    else:
        warnings.append(
            "SVN runtime was not discovered. Add svn.exe to PATH "
            "or install a Subversion client.",
        )

    return WindowsSvnDiscovery(
        requested=True,
        rx_roots=_dedupe_existing_dirs(roots),
        rw_roots=[str(svn_config_dir)],
        env=env,
        warnings=warnings,
    )


def _command_needs_svn(command: str) -> bool:
    return bool(_SVN_TOOL_RE.search(command) or _SVN_METADATA_RE.search(command))


def _discover_svn_roots() -> list[str]:
    roots: list[str] = []
    svn_home = os.environ.get("SVN_HOME")
    if svn_home:
        roots.extend(_svn_layout_roots(svn_home))

    for exe in (
        "svn.exe",
        "svnadmin.exe",
        "svnlook.exe",
        "svnserve.exe",
        "svnversion.exe",
    ):
        for found in _which_all(exe):
            roots.append(str(Path(found).parent))

    for raw in _common_svn_roots():
        roots.append(raw)

    return _dedupe_existing_dirs(roots)


def _svn_layout_roots(raw_root: str) -> list[str]:
    root = Path(raw_root).expanduser()
    return [str(root), str(root / "bin")]


def _common_svn_roots() -> list[str]:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get(
        "ProgramFiles(x86)",
        r"C:\Program Files (x86)",
    )
    return [
        str(Path(program_files) / "TortoiseSVN" / "bin"),
        str(Path(program_files) / "SlikSvn" / "bin"),
        str(Path(program_files) / "VisualSVN" / "bin"),
        str(Path(program_files_x86) / "Subversion" / "bin"),
    ]


def _which_all(executable: str) -> list[str]:
    matches: list[str] = []
    seen: set[str] = set()
    path_exts = _path_extensions(executable)

    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir:
            continue
        directory = os.path.expandvars(os.path.expanduser(raw_dir.strip('"')))
        for suffix in path_exts:
            candidate = Path(directory) / f"{executable}{suffix}"
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if not resolved.is_file():
                continue
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            seen.add(key)
            matches.append(str(resolved))

    return matches


def _path_extensions(executable: str) -> list[str]:
    if Path(executable).suffix:
        return [""]
    pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    separators = (os.pathsep, ";")
    extensions = [pathext]
    for separator in separators:
        extensions = [
            part
            for value in extensions
            for part in value.split(separator)
        ]
    return ["", *(ext for ext in extensions if ext)]


def _dedupe_existing_dirs(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in paths:
        try:
            path = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if not path.is_dir():
            continue
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        result.append(str(path))
    return result


def _runtime_path_roots(roots: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in roots:
        root = Path(raw)
        if not any((root / name).is_file() for name in ("svn.exe", "svn")):
            continue
        key = os.path.normcase(str(root))
        if key in seen:
            continue
        seen.add(key)
        result.append(str(root))
    return result


def _path_env_key() -> str:
    for key in os.environ:
        if key.upper() == "PATH":
            return key
    return "PATH"


def _prepend_env_path(roots: list[str]) -> str:
    existing_path = os.environ.get(_path_env_key(), "")
    parts = [*roots]
    if existing_path:
        parts.append(existing_path)
    return os.pathsep.join(parts)


def _safe_session_id(session_id: str | None) -> str:
    if not session_id:
        return "shell"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id).strip("._")
    return safe or "shell"
