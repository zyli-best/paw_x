# -*- coding: utf-8 -*-
"""Expand Windows shell scripts for runtime discovery.

This scanner is intentionally shallow: it only reads Windows shell wrapper
scripts (.cmd/.bat/.ps1) referenced by the command or by other scripts.
It does not recursively inspect Python/JavaScript/C++ source files.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class WindowsScriptRuntimeHints:
    expanded_command_text: str
    script_paths: list[str] = field(default_factory=list)
    project_signal_text: str = ""
    warnings: list[str] = field(default_factory=list)


_SCRIPT_SUFFIXES = {".cmd", ".bat", ".ps1"}
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".cache",
    "__pycache__",
}
_PROJECT_SIGNAL_FILES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "Pipfile",
    "poetry.lock",
    "pdm.lock",
    "uv.lock",
    "tox.ini",
    "noxfile.py",
    "pytest.ini",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "binding.gyp",
    "CMakeLists.txt",
    "CMakePresets.json",
    "vcpkg.json",
    "conanfile.txt",
    "conanfile.py",
    "meson.build",
    "build.ninja",
    "Makefile",
}
_PROJECT_SIGNAL_SUFFIXES = {
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".mts",
    ".cts",
    ".tsx",
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".ixx",
    ".cppm",
    ".rc",
    ".def",
    ".idl",
    ".sln",
    ".vcxproj",
    ".props",
    ".targets",
}
_COMMON_SIGNAL_DIRS = (
    ".",
    "src",
    "source",
    "scripts",
    "tools",
    "build",
    "native",
    "bindings",
    "include",
)
_SCRIPT_TOKEN_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|[\\/]|\w)"
    r"[\w .%${}()@~+,\-\\/]*\.(?:cmd|bat|ps1))",
    re.IGNORECASE,
)
_UNRESOLVED_VAR_RE = re.compile(r"%[^%]+%|\$env:[\w_]+|\$\{?[\w_]+\}?")


def expand_windows_script_runtime_hints(
    *,
    command: str,
    workspace_dir: str,
    coding_project_dir: str | None = None,
    cwd: str | None = None,
    extra_roots: list[str] | None = None,
    max_depth: int = 3,
    max_scripts: int = 20,
    max_bytes_per_file: int = 128 * 1024,
) -> WindowsScriptRuntimeHints:
    """Return command text augmented with referenced script contents."""
    warnings: list[str] = []
    workspace = _resolve_dir(workspace_dir)
    coding_project = _resolve_dir(coding_project_dir)
    cwd_path = _resolve_dir(cwd)
    roots = _dedupe_paths(
        [
            *(p for p in (workspace, coding_project) if p is not None),
            *(_resolve_dir(root) for root in (extra_roots or [])),
        ],
    )
    if not roots:
        return WindowsScriptRuntimeHints(
            expanded_command_text=command,
            warnings=["Windows script runtime scan skipped: no valid roots."],
        )

    base_dir = cwd_path or coding_project or workspace or roots[0]
    if base_dir and not _is_under_any_root(base_dir, roots):
        warnings.append(
            f"Windows script runtime scan ignored cwd outside allowed roots: {base_dir}",
        )
        base_dir = coding_project or workspace or roots[0]

    script_texts: list[str] = []
    script_paths: list[str] = []
    visited: set[str] = set()

    def visit_text(text: str, current_dir: Path, depth: int) -> None:
        if depth > max_depth or len(script_paths) >= max_scripts:
            return
        for raw in _extract_script_candidates(text):
            if _UNRESOLVED_VAR_RE.search(raw):
                warnings.append(
                    f"Windows script runtime scan skipped variable path: {raw}",
                )
                continue
            script = _resolve_script_path(raw, current_dir, roots)
            if script is None:
                continue
            key = os.path.normcase(os.path.normpath(str(script)))
            if key in visited:
                continue
            visited.add(key)
            if len(script_paths) >= max_scripts:
                warnings.append("Windows script runtime scan hit script limit.")
                return
            content = _read_script(script, max_bytes_per_file, warnings)
            if content is None:
                continue
            script_paths.append(str(script))
            script_texts.append(f"\n\n# script: {script}\n{content}")
            visit_text(content, script.parent, depth + 1)

    visit_text(command, base_dir, 0)
    project_signal_text = _collect_project_signal_text(roots, warnings)
    expanded = "\n".join(
        part
        for part in [command, *script_texts, project_signal_text]
        if part
    )
    return WindowsScriptRuntimeHints(
        expanded_command_text=expanded,
        script_paths=script_paths,
        project_signal_text=project_signal_text,
        warnings=warnings,
    )


def _extract_script_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for line in text.splitlines():
        stripped = _strip_comment(line)
        if not stripped:
            continue
        for raw in _script_tokens(stripped):
            if raw not in candidates:
                candidates.append(raw)
    return candidates


def _script_tokens(line: str) -> list[str]:
    results: list[str] = []
    try:
        tokens = shlex.split(line, posix=False)
    except ValueError:
        tokens = line.split()
    for token in tokens:
        cleaned = token.strip().strip("\"'").rstrip(";,")
        if Path(cleaned).suffix.lower() in _SCRIPT_SUFFIXES:
            results.append(cleaned)
    for match in _SCRIPT_TOKEN_RE.finditer(line):
        raw = match.group("path").strip().strip("\"'").rstrip(";,")
        if Path(raw).suffix.lower() in _SCRIPT_SUFFIXES and raw not in results:
            results.append(raw)
    return results


def _strip_comment(line: str) -> str:
    stripped = line.strip()
    lower = stripped.lower()
    if not stripped:
        return ""
    if lower.startswith(("rem ", "::", "#")):
        return ""
    return stripped


def _resolve_script_path(
    raw: str,
    current_dir: Path,
    roots: list[Path],
) -> Path | None:
    expanded = os.path.expandvars(raw.strip().strip("\"'"))
    try:
        candidate = Path(expanded)
    except (TypeError, ValueError):
        return None
    if not candidate.is_absolute():
        candidate = current_dir / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if resolved.suffix.lower() not in _SCRIPT_SUFFIXES:
        return None
    if not resolved.is_file():
        return None
    if not _is_under_any_root(resolved, roots):
        return None
    return resolved


def _read_script(
    path: Path,
    max_bytes: int,
    warnings: list[str],
) -> str | None:
    try:
        size = path.stat().st_size
        if size > max_bytes:
            warnings.append(
                f"Windows script runtime scan skipped large script: {path}",
            )
            return None
        data = path.read_bytes()
    except OSError as exc:
        warnings.append(f"Windows script runtime scan could not read {path}: {exc}")
        return None
    if b"\x00" in data[:4096]:
        warnings.append(f"Windows script runtime scan skipped binary file: {path}")
        return None
    for encoding in ("utf-8-sig", "utf-16", "mbcs", "latin-1"):
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _collect_project_signal_text(
    roots: list[Path],
    warnings: list[str],
) -> str:
    signals: list[str] = []
    seen: set[str] = set()
    scanned = 0
    for root in roots:
        for rel in _COMMON_SIGNAL_DIRS:
            base = root / rel
            if not base.is_dir():
                continue
            try:
                iterator = base.iterdir()
            except OSError:
                continue
            for child in iterator:
                if child.name in _SKIP_DIRS:
                    continue
                scanned += 1
                if scanned > 500:
                    warnings.append("Windows project signal scan hit file limit.")
                    return _format_project_signals(signals)
                if _is_project_signal(child):
                    key = os.path.normcase(os.path.normpath(str(child)))
                    if key not in seen:
                        seen.add(key)
                        signals.append(str(child))
                if len(signals) >= 100:
                    return _format_project_signals(signals)
    return _format_project_signals(signals)


def _is_project_signal(path: Path) -> bool:
    if path.is_dir():
        return False
    if path.name in _PROJECT_SIGNAL_FILES:
        return True
    return path.suffix.lower() in _PROJECT_SIGNAL_SUFFIXES


def _format_project_signals(signals: list[str]) -> str:
    if not signals:
        return ""
    return "\n\n# project runtime signals\n" + "\n".join(signals)


def _resolve_dir(raw: object) -> Path | None:
    if not raw:
        return None
    try:
        path = Path(os.fspath(raw)).expanduser().resolve()
    except (TypeError, OSError):
        return None
    return path if path.is_dir() else None


def _dedupe_paths(paths: list[Path | None]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        if path is None:
            continue
        key = os.path.normcase(os.path.normpath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _is_under_any_root(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False
