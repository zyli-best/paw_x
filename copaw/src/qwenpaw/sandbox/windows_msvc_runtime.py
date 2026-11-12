# -*- coding: utf-8 -*-
"""Windows MSVC runtime discovery for AppContainer shell sandboxing."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class WindowsMsvcDiscovery:
    requested: bool = False
    rx_roots: list[str] = field(default_factory=list)
    admin_acl_roots: list[str] = field(default_factory=list)
    rw_roots: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


_CPP_FILE_RE = re.compile(
    r"(?<![\w.-])[\w./\\: -]+\."
    r"(?:c|cc|cpp|cxx|h|hh|hpp|hxx|ixx|cppm|rc|def|idl|"
    r"sln|vcxproj|props|targets)"
    r"(?:\s|$|[\"'])",
    re.IGNORECASE,
)
_MSVC_TOOL_RE = re.compile(
    r"(?<![\w.-])(?:cl|link|lib|nmake|msbuild|devenv|cmake|ctest|"
    r"ninja|jom|vcbuild|vcvarsall|vcvars64|vsdevcmd|node-gyp|"
    r"cmake-js|prebuild|prebuildify)(?:\.exe|\.bat|\.cmd)?(?![\w.-])",
    re.IGNORECASE,
)
_BUILD_LIKE_RE = re.compile(
    r"(?<![\w.-])(?:build|compile|install|cmake|make|pip|pip3|"
    r"npm|yarn|pnpm|cargo|poetry|uv|pdm)"
    r"(?![\w.-])",
    re.IGNORECASE,
)
_NATIVE_HINT_DIRS = (
    ".",
    "src",
    "source",
    "cpp",
    "native",
    "bindings",
    "include",
)
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
_CPP_SUFFIXES = {
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
}


def discover_windows_msvc_runtime(
    workspace_dir: str,
    session_id: str | None = None,
    command: str = "",
    cwd: str | None = None,
) -> WindowsMsvcDiscovery:
    """Discover MSVC/Windows SDK roots for C/C++ shell commands."""
    workspace = _resolve_dir(workspace_dir)
    effective_cwd = _resolve_dir(cwd) if cwd else workspace
    warnings: list[str] = []
    if workspace is None:
        return WindowsMsvcDiscovery(
            requested=False,
            warnings=["MSVC discovery skipped: workspace path is invalid."],
        )

    requested = _command_needs_msvc(command, workspace, effective_cwd)
    if not requested:
        return WindowsMsvcDiscovery()

    vs_install = _discover_vs_installation(warnings)
    sdk_root = _discover_windows_sdk_root(warnings)

    rx_roots: list[str] = []
    env: dict[str, str] = {}

    if vs_install:
        env["VSINSTALLDIR"] = _with_trailing_sep(vs_install)
        _add_existing(rx_roots, vs_install / "VC" / "Auxiliary" / "Build")
        _add_existing(rx_roots, vs_install / "MSBuild" / "Current" / "Bin")
        _add_existing(rx_roots, vs_install / "Common7" / "Tools")
        _add_existing(
            rx_roots,
            vs_install
            / "Common7"
            / "IDE"
            / "CommonExtensions"
            / "Microsoft"
            / "CMake",
        )
        msvc_root = _discover_msvc_toolset_root(vs_install)
        if msvc_root:
            env["VCToolsInstallDir"] = _with_trailing_sep(msvc_root)
            _add_existing(rx_roots, msvc_root)
            _extend_env_path(env, _msvc_path_roots(msvc_root, vs_install))
            _extend_env_list(env, "INCLUDE", [msvc_root / "include"])
            _extend_env_list(env, "LIB", [msvc_root / "lib" / "x64"])
            _extend_env_list(env, "LIBPATH", [msvc_root / "lib" / "x64"])
        else:
            warnings.append(
                "MSVC toolset was not found under the Visual Studio install.",
            )
    else:
        warnings.append(
            "Visual Studio Build Tools were not discovered. Install the "
            "'Desktop development with C++' workload or add VSINSTALLDIR.",
        )

    sdk_version = _discover_sdk_version(sdk_root) if sdk_root else None
    if sdk_root:
        env["WindowsSdkDir"] = _with_trailing_sep(sdk_root)
        _add_existing(rx_roots, sdk_root / "bin")
        _add_existing(rx_roots, sdk_root / "Include")
        _add_existing(rx_roots, sdk_root / "Lib")
        if sdk_version:
            env["WindowsSDKVersion"] = sdk_version + "\\"
            _extend_env_path(
                env,
                [
                    sdk_root / "bin" / sdk_version / "x64",
                    sdk_root / "bin" / "x64",
                    sdk_root / "bin",
                ],
            )
            _extend_env_list(
                env,
                "INCLUDE",
                [
                    sdk_root / "Include" / sdk_version / "ucrt",
                    sdk_root / "Include" / sdk_version / "shared",
                    sdk_root / "Include" / sdk_version / "um",
                    sdk_root / "Include" / sdk_version / "winrt",
                    sdk_root / "Include" / sdk_version / "cppwinrt",
                ],
            )
            _extend_env_list(
                env,
                "LIB",
                [
                    sdk_root / "Lib" / sdk_version / "ucrt" / "x64",
                    sdk_root / "Lib" / sdk_version / "um" / "x64",
                ],
            )
            _extend_env_list(
                env,
                "LIBPATH",
                [
                    sdk_root / "Lib" / sdk_version / "ucrt" / "x64",
                    sdk_root / "Lib" / sdk_version / "um" / "x64",
                ],
            )
    else:
        warnings.append("Windows SDK 10 was not discovered.")

    tmp_root = workspace / "session_tmp" / _safe_session_id(session_id)
    build_cache = tmp_root / "build-cache"
    build_cache.mkdir(parents=True, exist_ok=True)

    admin_acl_roots = [p for p in rx_roots if _is_admin_acl_candidate(p)]
    return WindowsMsvcDiscovery(
        requested=True,
        rx_roots=_dedupe_existing_dirs(rx_roots),
        admin_acl_roots=_dedupe_existing_dirs(admin_acl_roots),
        rw_roots=[str(build_cache)],
        env=env,
        warnings=warnings,
    )


def _command_needs_msvc(
    command: str,
    workspace: Path,
    cwd: Path | None,
) -> bool:
    if _MSVC_TOOL_RE.search(command) or _CPP_FILE_RE.search(command + " "):
        return True
    if not _BUILD_LIKE_RE.search(command):
        return False
    return _workspace_has_native_build_signal(cwd or workspace, workspace)


def _workspace_has_native_build_signal(cwd: Path, workspace: Path) -> bool:
    roots: list[Path] = []
    for root in (cwd, workspace):
        if root and root.is_dir() and root not in roots:
            roots.append(root)

    for root in roots:
        if _has_native_manifest(root):
            return True
        scanned = 0
        for rel in _NATIVE_HINT_DIRS:
            base = root / rel
            if not base.is_dir():
                continue
            for current, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                scanned += len(filenames)
                if scanned > 500:
                    break
                if any(Path(name).suffix.lower() in _CPP_SUFFIXES for name in filenames):
                    return True
            if scanned > 500:
                break
    return False


def _has_native_manifest(root: Path) -> bool:
    for name in (
        "CMakeLists.txt",
        "CMakePresets.json",
        "binding.gyp",
        "vcpkg.json",
        "conanfile.txt",
        "conanfile.py",
        "meson.build",
        "build.ninja",
        "Makefile",
        "Cargo.toml",
    ):
        if (root / name).is_file():
            return True
    if any(root.glob("*.sln")) or any(root.glob("*.vcxproj")):
        return True
    setup_py = root / "setup.py"
    if setup_py.is_file() and _file_contains(
        setup_py,
        ("build_ext", "scikit-build", "cmake", "meson"),
    ):
        return True
    pyproject = root / "pyproject.toml"
    if pyproject.is_file() and _file_contains(
        pyproject,
        ("build_ext", "scikit-build", "scikit_build", "cmake", "meson"),
    ):
        return True
    package_json = root / "package.json"
    if package_json.is_file() and _package_json_has_native_signal(package_json):
        return True
    return False


def _package_json_has_native_signal(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    text = json.dumps(data, ensure_ascii=False).lower()
    return any(token in text for token in ("node-gyp", "cmake-js", "prebuild"))


def _file_contains(path: Path, needles: tuple[str, ...]) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False
    return any(needle.lower() in text for needle in needles)


def _discover_vs_installation(warnings: list[str]) -> Path | None:
    for raw in (os.environ.get("VSINSTALLDIR"), os.environ.get("VCINSTALLDIR")):
        resolved = _resolve_dir(raw)
        if resolved:
            if resolved.name.lower() == "vc":
                return resolved.parent
            return resolved

    vswhere = _find_vswhere()
    if not vswhere:
        return None
    try:
        result = subprocess.run(
            [
                str(vswhere),
                "-products",
                "*",
                "-requires",
                "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "-latest",
                "-property",
                "installationPath",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        warnings.append(f"vswhere failed while discovering MSVC: {exc}")
        return None
    if result.returncode != 0:
        warnings.append((result.stderr or "vswhere did not find MSVC").strip())
        return None
    return _resolve_dir(result.stdout.strip())


def _find_vswhere() -> Path | None:
    for raw in (
        os.environ.get("VSWHERE"),
        str(
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Microsoft Visual Studio"
            / "Installer"
            / "vswhere.exe"
        ),
    ):
        path = _resolve_file(raw)
        if path:
            return path
    return None


def _discover_msvc_toolset_root(vs_install: Path) -> Path | None:
    default_file = (
        vs_install
        / "VC"
        / "Auxiliary"
        / "Build"
        / "Microsoft.VCToolsVersion.default.txt"
    )
    base = vs_install / "VC" / "Tools" / "MSVC"
    if default_file.is_file():
        try:
            version = default_file.read_text(encoding="utf-8").strip()
        except OSError:
            version = ""
        candidate = _resolve_dir(base / version)
        if candidate:
            return candidate
    if not base.is_dir():
        return None
    versions = [p for p in base.iterdir() if p.is_dir()]
    if not versions:
        return None
    return sorted(versions, key=lambda p: _version_key(p.name))[-1]


def _discover_windows_sdk_root(warnings: list[str]) -> Path | None:
    env_root = _resolve_dir(os.environ.get("WindowsSdkDir"))
    if env_root:
        return env_root
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows Kits\Installed Roots",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "KitsRoot10")
            root = _resolve_dir(value)
            if root:
                return root
    except Exception:
        pass
    fallback = _resolve_dir(
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Windows Kits"
        / "10",
    )
    if fallback is None:
        warnings.append("Windows SDK registry entry and fallback path are missing.")
    return fallback


def _discover_sdk_version(sdk_root: Path | None) -> str | None:
    if not sdk_root:
        return None
    include_root = sdk_root / "Include"
    if not include_root.is_dir():
        return None
    versions = [p.name for p in include_root.iterdir() if p.is_dir()]
    if not versions:
        return None
    return sorted(versions, key=_version_key)[-1]


def _msvc_path_roots(msvc_root: Path, vs_install: Path) -> list[Path]:
    return [
        msvc_root / "bin" / "Hostx64" / "x64",
        vs_install / "MSBuild" / "Current" / "Bin",
        vs_install
        / "Common7"
        / "IDE"
        / "CommonExtensions"
        / "Microsoft"
        / "CMake"
        / "CMake"
        / "bin",
    ]


def _extend_env_path(env: dict[str, str], paths: list[Path]) -> None:
    key = _path_env_key()
    existing = env.get(key) or os.environ.get(key, "")
    parts = [str(p) for p in paths if p.is_dir()]
    if existing:
        parts.append(existing)
    if parts:
        env[key] = os.pathsep.join(_dedupe_strings(parts))


def _extend_env_list(env: dict[str, str], key: str, paths: list[Path]) -> None:
    existing = env.get(key) or os.environ.get(key, "")
    parts = [str(p) for p in paths if p.is_dir()]
    if existing:
        parts.append(existing)
    if parts:
        env[key] = os.pathsep.join(_dedupe_strings(parts))


def _path_env_key() -> str:
    for key in os.environ:
        if key.upper() == "PATH":
            return key
    return "PATH"


def _add_existing(paths: list[str], path: Path) -> None:
    if path.is_dir():
        paths.append(str(path))


def _dedupe_existing_dirs(paths: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = _resolve_dir(raw)
        if not path:
            continue
        key = os.path.normcase(os.path.normpath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        result.append(str(path))
    return result


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = os.path.normcase(os.path.normpath(value))
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _is_admin_acl_candidate(path: str) -> bool:
    lower = path.lower()
    return (
        "\\microsoft visual studio\\" in lower
        or "\\windows kits\\" in lower
        or "\\vc\\tools\\msvc\\" in lower
        or "\\vc\\auxiliary\\build" in lower
        or "\\msbuild\\current\\bin" in lower
    )


def _resolve_dir(raw: object) -> Path | None:
    if not raw:
        return None
    try:
        path = Path(os.fspath(raw)).expanduser().resolve()
    except (TypeError, OSError):
        return None
    return path if path.is_dir() else None


def _resolve_file(raw: object) -> Path | None:
    if not raw:
        return None
    try:
        path = Path(os.fspath(raw)).expanduser().resolve()
    except (TypeError, OSError):
        return None
    return path if path.is_file() else None


def _with_trailing_sep(path: Path) -> str:
    value = str(path)
    return value if value.endswith(("\\", "/")) else value + "\\"


def _version_key(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value)
    return tuple(int(part) for part in parts) if parts else (0,)


def _safe_session_id(session_id: str | None) -> str:
    if not session_id:
        return "shell"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id).strip("._")
    return safe or "shell"
