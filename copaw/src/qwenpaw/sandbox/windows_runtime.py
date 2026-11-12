# -*- coding: utf-8 -*-
"""Windows shell runtime discovery for strict shell sandboxing.

This module is intentionally configuration-free. The whitelist is a small
code-level baseline for Windows script hosts and common installed runtimes;
callers decide whether to use auto-discovery via environment flags.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


_PYTHON_COMMANDS = (
    "py",
    "python",
    "python3",
    "pip",
    "pip3",
    "pytest",
    "tox",
    "nox",
    "poetry",
    "pdm",
    "uv",
    "hatch",
    "pipenv",
    "conda",
    "mamba",
)
_NODE_COMMANDS = (
    "node",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "corepack",
    "tsx",
    "ts-node",
    "vite",
    "webpack",
    "rollup",
    "esbuild",
    "turbo",
    "nx",
    "lerna",
    "jest",
    "vitest",
    "playwright",
    "node-gyp",
    "prebuild",
    "prebuildify",
    "cmake-js",
)


@dataclass(frozen=True)
class WindowsRuntimeDiscovery:
    rx_roots: list[str] = field(default_factory=list)
    python_rx_roots: list[str] = field(default_factory=list)
    python_requested: bool = False
    node_rx_roots: list[str] = field(default_factory=list)
    node_requested: bool = False
    rw_roots: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def discover_windows_shell_runtime(
    workspace_dir: str,
    session_id: str | None = None,
    command: str = "",
) -> WindowsRuntimeDiscovery:
    """Discover readonly/executable runtime roots for Windows shell tools."""
    workspace = Path(workspace_dir).expanduser().resolve()
    roots: list[str] = []
    python_roots: list[str] = []
    node_roots: list[str] = []
    warnings: list[str] = []

    python_requested = _command_needs_python(command)
    if python_requested:
        python_roots = _discover_python_roots(workspace, warnings)
        roots.extend(python_roots)
    node_requested = _command_needs_node(command)
    if node_requested:
        node_roots = _discover_node_roots(warnings)
        roots.extend(node_roots)

    tmp_root = workspace / "session_tmp" / _safe_session_id(session_id)
    rw_roots = [
        tmp_root,
        tmp_root / "tmp",
        tmp_root / "pip-cache",
        tmp_root / "npm-cache",
        tmp_root / "pycache",
    ]
    for path in rw_roots:
        path.mkdir(parents=True, exist_ok=True)

    env = {
        "TEMP": str(tmp_root / "tmp"),
        "TMP": str(tmp_root / "tmp"),
        "PIP_CACHE_DIR": str(tmp_root / "pip-cache"),
        "npm_config_cache": str(tmp_root / "npm-cache"),
        "PYTHONPYCACHEPREFIX": str(tmp_root / "pycache"),
    }

    rx_roots = _dedupe_existing_dirs(roots)
    python_rx_roots = _dedupe_existing_dirs(python_roots)
    node_rx_roots = _dedupe_existing_dirs(node_roots)
    path_roots = _runtime_path_roots(python_rx_roots, node_rx_roots)
    if path_roots:
        env[_path_env_key()] = _prepend_env_path(path_roots)
    return WindowsRuntimeDiscovery(
        rx_roots=rx_roots,
        python_rx_roots=python_rx_roots,
        python_requested=python_requested,
        node_rx_roots=node_rx_roots,
        node_requested=node_requested,
        rw_roots=[str(path) for path in rw_roots],
        env=env,
        warnings=warnings,
    )


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


def _runtime_path_roots(
    python_roots: list[str],
    node_roots: list[str],
) -> list[str]:
    """Return discovered runtime directories that contain executables."""
    candidates: list[tuple[str, tuple[str, ...]]] = [
        *((root, ("python.exe", "python3.exe", "python", "python3")) for root in python_roots),
        *(
            (
                root,
                ("node.exe", "node", "npm.cmd", "npm", "npx.cmd", "npx"),
            )
            for root in node_roots
        ),
    ]
    result: list[str] = []
    seen: set[str] = set()
    for raw, executable_names in candidates:
        root = Path(raw)
        if not any((root / name).is_file() for name in executable_names):
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


def _discover_python_roots(workspace: Path, warnings: list[str]) -> list[str]:
    user_roots: list[str] = []
    fallback_roots: list[str] = []

    app_python = _resolve_path(sys.executable)
    app_python_dir = app_python.parent if app_python else None
    app_venv = _resolve_path(os.environ.get("VIRTUAL_ENV", ""))
    workspace_venv = _resolve_path(workspace / ".venv")

    for exe in ("python.exe", "python3.exe"):
        for found in _which_all(exe):
            found_path = _resolve_path(found)
            roots = _python_roots_from_executable(found)
            if _is_app_python(
                found_path,
                app_python,
                app_python_dir,
                app_venv,
                workspace_venv,
            ):
                fallback_roots.extend(roots)
            else:
                user_roots.extend(roots)

    if user_roots:
        return user_roots

    for raw in (
        os.environ.get("VIRTUAL_ENV", ""),
        os.environ.get("CONDA_PREFIX", ""),
        os.environ.get("PYTHONHOME", ""),
    ):
        fallback_roots.extend(_python_layout_roots(raw))

    if app_python:
        fallback_roots.extend(_python_roots_from_executable(app_python))

    if not fallback_roots:
        warnings.append(
            "Python runtime was not discovered. Add python.exe to PATH "
            "or run Python from a workspace/mount path.",
        )
    return fallback_roots


def _resolve_path(raw: object) -> Path | None:
    if not raw:
        return None
    try:
        return Path(os.fspath(raw)).expanduser().resolve()
    except (TypeError, OSError):
        return None


def _is_app_python(
    found: Path | None,
    app_python: Path | None,
    app_python_dir: Path | None,
    app_venv: Path | None,
    workspace_venv: Path | None,
) -> bool:
    if found is None:
        return False

    if app_python is not None and found == app_python:
        return True

    for root in (app_python_dir, app_venv, workspace_venv):
        if root is not None and _is_relative_to(found, root):
            return True

    return False


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _which_all(executable: str) -> list[str]:
    """Return all PATH matches for an executable, preserving PATH order."""
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


def _python_roots_from_executable(raw: object) -> list[str]:
    try:
        executable = Path(os.fspath(raw)).expanduser().resolve()
    except (TypeError, OSError):
        return []

    executable_dir = executable.parent
    if executable_dir.name.lower() == "scripts":
        venv_root = executable_dir.parent
        if (venv_root / "pyvenv.cfg").is_file():
            return _python_layout_roots(venv_root)

    return _python_layout_roots(executable_dir)


def _python_layout_roots(raw: object) -> list[str]:
    if not raw:
        return []
    try:
        root = Path(os.fspath(raw)).expanduser().resolve()
    except (TypeError, OSError):
        return []
    return [
        str(root),
        str(root / "Scripts"),
        str(root / "Lib"),
        str(root / "Lib" / "site-packages"),
    ]


def _discover_node_roots(warnings: list[str]) -> list[str]:
    roots: list[str] = []

    for exe in ("node.exe", "node", "npm.cmd", "npm", "npx.cmd", "npx"):
        for found in _which_all(exe):
            roots.extend(_node_layout_roots(Path(found).parent))

    for raw in (os.environ.get("NODE_HOME", ""), os.environ.get("NVM_SYMLINK", "")):
        roots.extend(_node_layout_roots(raw))

    if not roots:
        warnings.append(
            "Node.js runtime was not discovered. Add node.exe to PATH "
            "or run Node.js from a workspace/mount path.",
        )
    return roots


def _node_layout_roots(raw: object) -> list[str]:
    if not raw:
        return []
    try:
        root = Path(os.fspath(raw)).expanduser().resolve()
    except (TypeError, OSError):
        return []
    return [str(root), str(root / "node_modules")]


def _command_needs_python(command: str) -> bool:
    lower = f" {command.lower()} "
    command_pattern = "|".join(re.escape(cmd) for cmd in _PYTHON_COMMANDS)
    if re.search(rf"(?<![\w.-])(?:{command_pattern})(?:\.exe)?(?![\w.-])", lower):
        return True
    return bool(
        re.search(r"(?<![\w.-])[\w./\\:-]+\.py(?:\s|$)", lower)
        or re.search(
            r"(?<![\w.-])(?:pyproject\.toml|setup\.py|setup\.cfg|"
            r"requirements(?:-[\w.-]+)?\.txt|pipfile|poetry\.lock|"
            r"pdm\.lock|uv\.lock|tox\.ini|noxfile\.py|pytest\.ini)"
            r"(?![\w.-])",
            lower,
        )
    )


def _command_needs_node(command: str) -> bool:
    lower = f" {command.lower()} "
    command_pattern = "|".join(re.escape(cmd) for cmd in _NODE_COMMANDS)
    if re.search(rf"(?<![\w.-])(?:{command_pattern})(?:\.exe|\.cmd)?(?![\w.-])", lower):
        return True
    return bool(
        re.search(
            r"(?<![\w.-])[\w./\\:-]+\.(?:js|mjs|cjs|ts|mts|cts|tsx)(?:\s|$)",
            lower,
        )
        or re.search(
            r"(?<![\w.-])(?:package\.json|package-lock\.json|"
            r"npm-shrinkwrap\.json|pnpm-lock\.yaml|yarn\.lock|binding\.gyp)"
            r"(?![\w.-])",
            lower,
        ),
    )


def _safe_session_id(session_id: str | None) -> str:
    if not session_id:
        return "shell"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id).strip("._")
    return safe or "shell"
