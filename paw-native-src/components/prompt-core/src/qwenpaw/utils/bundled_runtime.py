"""Request-local portable runtimes; never activate or mutate the server env."""

from __future__ import annotations

from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import shlex
import shutil
import struct
import subprocess
import sys
from typing import Any
from uuid import uuid4

from ..config.config import BundledRuntimesConfig
from ..config.context import describe_default_relative_file_dir
from ..config.shell_policy import force_direct_subprocess_sync_enabled
from .runtime_storage import (
    allocate_environment,
    environment_owner,
    runtime_home,
    shared_runtime_lock,
    use_environment,
)


class BundledRuntimeError(RuntimeError):
    """An explicitly enabled runtime could not be prepared; no host fallback."""


@dataclass(frozen=True)
class RequestRuntimes:
    root: Path
    info: dict[str, dict[str, str]]
    path_entries: tuple[str, ...]
    environment: dict[str, str]
    remove_keys: frozenset[str]
    storage_root: Path | None = None
    generation: str = ""
    shared_root: Path | None = None

    def process_env(self, base: dict[str, str]) -> dict[str, str]:
        """PATH names and runtime variables are case-insensitive on Windows."""
        replaced = self.remove_keys | {key.upper() for key in self.environment}
        existing_path = next(
            (v for k, v in base.items() if k.upper() == "PATH"),
            "",
        )
        env = {
            k: v
            for k, v in base.items()
            if k.upper() not in replaced and k.upper() != "PATH"
        }
        env.update(self.environment)
        env["PATH"] = os.pathsep.join((*self.path_entries, existing_path))
        return env


current_runtimes: ContextVar[RequestRuntimes | None] = ContextVar(
    "bundled_runtimes",
    default=None,
)


def supported_request(request: dict[str, Any]) -> bool:
    return (
        sys.platform == "win32"
        and struct.calcsize("P") == 8
        and not request.get("is_coding_mode", False)
        and force_direct_subprocess_sync_enabled()
    )


def _isolation_enabled() -> bool:
    return os.environ.get("QWENPAW_BUNDLED_RUNTIME_ISOLATION", "0").strip() == "1"


def runtime_directory() -> Path:
    """An override can supply the read-only base for isolated environments."""
    if _isolation_enabled():
        override = os.environ.get("QWENPAW_BUNDLED_RUNTIMES_DIR", "").strip()
        if override:
            return Path(override).expanduser().resolve()
    return runtime_home() / "bin" / "runtime"


@lru_cache(maxsize=8)
def _read_manifest(path: str, mtime_ns: int, size: int) -> dict:
    del mtime_ns, size  # cache invalidation follows the actual manifest file
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or data.get("platform") != "windows-x86_64"
    ):
        raise ValueError("Unsupported runtime manifest/platform")
    return data


_ENTRIES = {
    "python": {"executable": "python/python.exe"},
    "nodejs": {
        "executable": "node/node.exe",
        "npm_cli": "node/node_modules/npm/bin/npm-cli.js",
        "npx_cli": "node/node_modules/npm/bin/npx-cli.js",
    },
    "git": {"executable": "git/cmd/git.exe", "bash": "git/bin/bash.exe"},
}


def bundle_status() -> dict:
    root = runtime_directory()
    result = {
        "directory": str(root),
        "supported_platform": sys.platform == "win32" and struct.calcsize("P") == 8,
        "runtimes": {},
    }
    try:
        path = root / "manifest.json"
        stat = path.stat()
        manifest = _read_manifest(str(path), stat.st_mtime_ns, stat.st_size)
        manifest_error = ""
    except (OSError, ValueError, TypeError) as exc:
        manifest, manifest_error = {}, str(exc)
    for kind, entries in _ENTRIES.items():
        try:
            version = manifest["runtimes"][kind]["version"]
            if kind == "python" and not version.startswith("3.11."):
                raise ValueError("Bundled Python must be 3.11.x")
            if kind == "nodejs" and not version.startswith("22."):
                raise ValueError("Bundled Node.js must be 22.x")
            for relative in entries.values():
                path = root / relative
                if not path.resolve().is_relative_to(root) or not path.is_file():
                    raise FileNotFoundError(path)
            error = manifest_error
        except (KeyError, AttributeError, OSError, ValueError, TypeError) as exc:
            version, error = "", manifest_error or str(exc)
        result["runtimes"][kind] = {
            "available": not error,
            "version": version,
            "error": error,
            **{key: str(root / relative) for key, relative in entries.items()},
        }
    return result


def _inside(root: Path, path: Path) -> Path:
    if not path.resolve().is_relative_to(root):
        raise BundledRuntimeError(f"Environment path escapes work directory: {path}")
    return path


def _write_json(path: Path, data: dict) -> None:
    temporary = path.with_name(f".{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _run(args: list[str], env: dict[str, str], *, read_json: bool = False) -> dict:
    result = subprocess.run(
        args,
        env=env,
        cwd=env.get("TEMP"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise BundledRuntimeError(
            f"Runtime preparation failed: {args[0]}: "
            f"{(result.stderr or result.stdout)[-2000:]}",
        )
    return json.loads(result.stdout) if read_json else {}


def _ensurepip_wheel(base_dir: Path, package: str) -> Path:
    wheels_dir = _inside(base_dir, base_dir / "Lib/ensurepip/_bundled")
    wheels = [
        wheel for wheel in wheels_dir.glob(f"{package}-*.whl")
        if _inside(base_dir, wheel).is_file()
    ]
    if len(wheels) != 1:
        raise BundledRuntimeError(
            f"Expected one bundled {package} wheel in {wheels_dir}"
        )
    return wheels[0]


def _prepare_python(
    root: Path, managed: Path, status: dict, state: dict, env: dict[str, str]
) -> dict[str, str]:
    base = Path(status["executable"])
    directory = _inside(root, managed / "p")
    python = _inside(root, directory / "Scripts/python.exe")
    stamp = {
        "base": str(base),
        "version": status["version"],
        "size": base.stat().st_size,
        "mtime_ns": base.stat().st_mtime_ns,
    }
    if directory.exists() and state.get("python") and state["python"] != stamp:
        shutil.rmtree(directory)
    if directory.exists():
        if state.get("python") != stamp or not all(
            _inside(root, directory / relative).is_file()
            for relative in (
                "Scripts/python.exe",
                "pyvenv.cfg",
                "Lib/site-packages/pip/__init__.py",
            )
        ):
            raise BundledRuntimeError(
                f"Python environment is incomplete or its base changed: {directory}. "
                "Move this managed environment aside and retry to recreate it.",
            )
    else:
        base_dir = base.parent.resolve()
        pip = str(_ensurepip_wheel(base_dir, "pip"))
        setuptools = str(_ensurepip_wheel(base_dir, "setuptools"))
        directory.mkdir()
        try:
            _run(
                [
                    # The base site can contain shared packages and startup hooks.
                    str(base), "-I", "-S", "-B", "-m", "venv", "--copies",
                    "--without-pip", str(directory),
                ],
                env,
            )
            # Run pip from the wheel, then install both wheels in the venv.
            _run(
                [
                    str(python), "-I", "-B", "-c",
                    "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
                    "runpy.run_module('pip',run_name='__main__',alter_sys=True)",
                    pip, "--isolated", "--disable-pip-version-check",
                    "--require-virtualenv", "install", "--no-cache-dir",
                    "--no-index", "--no-deps", "--no-compile", pip, setuptools,
                ],
                env,
            )
            probe = _run(
                [
                    str(python),
                    "-I",
                    "-B",
                    "-c",
                    "import json,sys,pip; print(json.dumps(dict(base=sys.base_prefix,"
                    "prefix=sys.prefix,version='.'.join(map(str,sys.version_info[:3])))))",
                ],
                env,
                read_json=True,
            )
            if (
                Path(probe["base"]).resolve() != base_dir
                or Path(probe["prefix"]).resolve() != directory.resolve()
                or probe["version"] != status["version"]
            ):
                raise BundledRuntimeError(
                    f"Python environment validation failed: {directory}"
                )
        except Exception:
            shutil.rmtree(_inside(root, directory), ignore_errors=True)
            raise
        state["python"] = stamp
    return {
        "source": "bundled_venv",
        "version": status["version"],
        "executable": str(python),
        "directory": str(python.parent),
        "work_root": str(root),
    }


def prepare_request_runtimes(
    request: dict[str, Any],
    workspace_dir: Path,
    session_id: str | None,
    config: BundledRuntimesConfig,
) -> RequestRuntimes | None:
    """Only selected tools create directories; initialization performs no downloads."""
    selected = [kind for kind in _ENTRIES if getattr(config, kind)]
    if not config.enabled or not selected or not supported_request(request):
        return None
    try:
        inventory = bundle_status()
        statuses = inventory["runtimes"]
        for kind in selected:
            if not statuses[kind]["available"]:
                raise BundledRuntimeError(
                    f"Bundled {kind} is unavailable: {statuses[kind]['error']}"
                )
        mounts = [mount for mount in (request.get("sandbox_mounts") or []) if str(mount).strip()]
        root = describe_default_relative_file_dir(
            workspace_dir=workspace_dir,
            session_id=session_id,
            is_coding_mode=False,
            sandbox_mounts=mounts,
        ).path.resolve()
        shared_root = None if _isolation_enabled() else Path(inventory["directory"])
        storage_root, generation = None, ""
        if shared_root is None:
            owner = environment_owner(workspace_dir, session_id, Path(mounts[0]) if mounts else None)
            storage_root, generation = allocate_environment(owner, root)
            managed = storage_root
            environment_guard = use_environment(storage_root, generation)
        else:
            managed = shared_root
            environment_guard = nullcontext()
        info, paths, updates, remove = {}, [], {}, set()
        with environment_guard:
            temp = _inside(managed, managed / "t")
            temp.mkdir(exist_ok=True)
            updates.update(TEMP=str(temp), TMP=str(temp))
            if "python" in selected or "nodejs" in selected:
                state_file = _inside(managed, managed / "state.json") if storage_root else None
                state = (
                    json.loads(state_file.read_text(encoding="utf-8"))
                    if state_file is not None and state_file.exists()
                    else {}
                )
                if not isinstance(state, dict):
                    raise ValueError(f"Invalid environment state: {state_file}")
                saved_state = dict(state)
                if "python" in selected:
                    remove.update(
                        {
                            "PYTHONHOME",
                            "PYTHONPATH",
                            "PYTHONUSERBASE",
                            "PYTHONSTARTUP",
                            "VIRTUAL_ENV",
                            "CONDA_PREFIX",
                            "PIP_TARGET",
                            "PIP_PREFIX",
                            "PIP_USER",
                        }
                    )
                    cache = _inside(managed, managed / "c/pip")
                    updates.update(
                        PIP_CACHE_DIR=str(cache),
                        PIP_CONFIG_FILE=os.devnull,
                        PIP_REQUIRE_VIRTUALENV="1" if storage_root else "0",
                        PYTHONNOUSERSITE="1",
                        PYTHONDONTWRITEBYTECODE="1",
                        PYTHONUTF8="1",
                    )
                    if storage_root is not None:
                        child_env = RequestRuntimes(
                            root, {}, (), updates, frozenset(remove)
                        ).process_env(dict(os.environ))
                        info["python"] = _prepare_python(
                            managed, managed, statuses["python"], state, child_env
                        )
                        info["python"].update(work_root=str(root), install_cwd=str(managed / "p"))
                        if state_file is not None and state != saved_state:
                            _write_json(state_file, state)
                            saved_state = dict(state)
                        updates["VIRTUAL_ENV"] = str(managed / "p")
                    else:
                        executable = Path(statuses["python"]["executable"])
                        info["python"] = {
                            "source": "bundled",
                            "version": statuses["python"]["version"],
                            "executable": str(executable),
                            "directory": str(executable.parent),
                            "work_root": str(root),
                            "install_cwd": str(root),
                        }
                    paths.append(info["python"]["directory"])
                if "nodejs" in selected:
                    node_root = _inside(managed, managed / ("node/deps" if shared_root else "n"))
                    npm_user = _inside(managed, managed / "npm-user.npmrc")
                    npm_global = _inside(managed, managed / "npm-global.npmrc")
                    modules = _inside(managed, node_root / "node_modules")
                    package = _inside(managed, node_root / "package.json")
                    if not (modules.is_dir() and all(p.is_file() for p in (package, npm_user, npm_global))):
                        node_guard = shared_runtime_lock(shared_root, "nodejs") if shared_root else nullcontext()
                        with node_guard:
                            modules.mkdir(parents=True, exist_ok=True)
                            for path in (npm_user, npm_global):
                                if not path.exists():
                                    path.touch(exist_ok=False)
                            if not package.exists():
                                _write_json(package, {"private": True})
                    info["nodejs"] = {
                        "source": "bundled",
                        "version": statuses["nodejs"]["version"],
                        "executable": statuses["nodejs"]["executable"],
                        "directory": str(Path(statuses["nodejs"]["executable"]).parent),
                        "npm_cli": statuses["nodejs"]["npm_cli"],
                        "npx_cli": statuses["nodejs"]["npx_cli"],
                        "dependency_dir": str(modules),
                        "work_root": str(root),
                        "install_cwd": str(node_root),
                    }
                    paths.extend(
                        (
                            info["nodejs"]["directory"],
                            str(modules / ".bin"),
                        )
                    )
                    remove.update(
                        {
                            "NODE_OPTIONS",
                            "NODE_PATH",
                            "NPM_CONFIG_WORKSPACE",
                            "NPM_CONFIG_WORKSPACES",
                        }
                    )
                    updates.update(
                        NPM_CONFIG_CACHE=str(_inside(managed, managed / "c/npm")),
                        NPM_CONFIG_PREFIX=str(node_root),
                        NPM_CONFIG_GLOBAL="false",
                        NPM_CONFIG_USERCONFIG=str(npm_user),
                        NPM_CONFIG_GLOBALCONFIG=str(npm_global),
                    )
                    state["nodejs"] = {"version": statuses["nodejs"]["version"]}
                if state_file is not None and state != saved_state:
                    _write_json(state_file, state)
        if "git" in selected:
            info["git"] = {
                **{
                    k: str(v)
                    for k, v in statuses["git"].items()
                    if k in {"executable", "version", "bash"}
                },
                "source": "bundled",
                "directory": str(Path(statuses["git"]["executable"]).parent),
                "work_root": str(root),
            }
            paths.append(info["git"]["directory"])
            remove.add("GIT_EXEC_PATH")
        if shared_root is not None:
            for runtime_info in info.values():
                runtime_info["scope"] = "shared"
        return RequestRuntimes(
            root, info, tuple(paths), updates, frozenset(remove),
            storage_root, generation, shared_root,
        )
    except BundledRuntimeError:
        raise
    except Exception as exc:
        raise BundledRuntimeError(f"Cannot prepare bundled runtimes: {exc}") from exc


def render_runtime_instructions(
    infos: dict[str, dict[str, str]], shell: str | None
) -> str:
    """Examples are regenerated from this request's paths and Shell syntax."""
    name = (shell or "cmd.exe").replace("\\", "/").rsplit("/", 1)[-1].lower()
    powershell = name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}
    posix = name not in {"cmd", "cmd.exe"} and not powershell

    def quoted(value: str) -> str:
        if powershell:
            return "'" + value.replace("'", "''") + "'"
        if posix:
            return shlex.quote(value.replace("\\", "/"))
        return '"' + value + '"'

    def command(executable: str, args: list[str]) -> str:
        return ("& " if powershell else "") + " ".join(map(quoted, [executable, *args]))

    lines = []
    for kind, info in infos.items():
        if info.get("source") not in {"bundled", "bundled_venv"}:
            continue
        root, exe = info["work_root"], info["executable"]
        shared = info.get("scope") == "shared"
        if not lines:
            lines.extend(
                (
                    "",
                    "## 本轮内置运行时使用规则",
                    f"- 本轮工作根目录：{root}",
                    "- 本轮直接使用内置程序和共享依赖。Python 和 Node 的依赖由同一 Windows 用户下的会话共用，安装、升级或卸载会影响其他会话。"
                    if shared else
                    "- 环境已由后端准备，无需重新创建、激活虚拟环境或初始化 Node 项目。",
                    "- 每条示例单独调用 execute_shell_command，并使用示例标注的 cwd。PowerShell 的前置 & 用于调用单个程序。",
                    "- 后端列出的托管程序和安装目录仅供运行程序和安装依赖。原始资料的副本、临时脚本、中间文件和最终产物只写入本轮可写挂载或 session scratch。",
                    "- 使用本轮真实路径，不沿用历史挂载路径；安装依赖与执行脚本使用同一套环境。",
                )
            )
        if kind == "python":
            label = "内置，共享依赖" if shared else "托管虚拟环境"
            lines.extend(
                (
                    f"- Python（{label}）可执行文件：{exe}",
                    f"- Python（{label}）所在目录：{info['directory']}",
                    f"- 安装 Python 库示例（cwd={info.get('install_cwd', root)}）：`{command(exe, ['-m', 'pip', 'install', 'pandas'])}`",
                    f"- 执行 Python 示例（cwd={root}）：`{command(exe, ['task.py'])}`",
                    "- 安装库使用上述 Python 的 -m pip，不使用系统 pip。",
                )
            )
        elif kind == "nodejs":
            node_root = info.get("install_cwd", root)
            lines.extend(
                (
                    f"- Node.js（内置）可执行文件：{exe}",
                    f"- Node.js 本地依赖目录：{info['dependency_dir']}",
                    f"- 安装托管 Node 依赖示例（cwd={node_root}）：`{command(exe, [info['npm_cli'], 'install', 'axios', '--prefix', node_root, '--global=false'])}`",
                    f"- 执行 Node 脚本示例（cwd={root}）：`{command(exe, ['task.mjs'])}`",
                    "- 执行任务脚本时，cwd 使用获授权的任务目录，使相对输入和输出落在任务目录。",
                    "- 共享模式的托管 npm 安装使用上述共享前缀。"
                    if shared else
                    "- 已有 Node 仓库的依赖安装显式将 cwd 和 --prefix 都指向其已获授权的仓库根。",
                )
            )
        else:
            lines.extend(
                (
                    f"- Git（内置）可执行文件：{exe}",
                    f"- Git Bash 所在路径：{info['bash']}",
                    f"- Git 示例（cwd=已获授权的目标仓库）：`{command(exe, ['status'])}`",
                    "- Git/Bash 已提供，无需安装；Git 开关不改变默认 Shell。",
                )
            )
    return "\n".join(lines)
