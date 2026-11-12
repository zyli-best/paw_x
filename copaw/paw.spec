# -*- mode: python ; coding: utf-8 -*-
r"""
PyInstaller 规格文件：从项目根目录的 main.py 打包 QwenPaw（控制台 CLI）。

构建（在项目根目录执行，且已安装 pyproject 中的依赖，建议使用 venv）：
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\init-windows.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-paw.ps1

输出（目录模式 onedir）：
  - dist/paw/paw.exe 与同目录的 _internal/ 必须一起发布。
  - scripts/build-paw.ps1 另外生成应用包 dist/paw-runtime.zip 和独立工具包 dist/runtimes.zip。
  - Playwright 浏览器需另外安装，浏览器二进制不打进 exe。
"""
from __future__ import annotations

from importlib.util import find_spec
import os
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    copy_metadata,
)

# 项目根目录（与 paw.spec 同目录）
try:
    ROOT = os.path.dirname(os.path.abspath(SPEC))
except NameError:  # 直接以 python paw.spec 调试时
    ROOT = os.path.dirname(os.path.abspath(__file__))

_native_build = runpy.run_path(os.path.join(ROOT, "scripts", "native-components.py"))
_build_root = Path(ROOT) / "build"
_build_root.mkdir(exist_ok=True)
_stage = TemporaryDirectory(prefix="native-stage-", dir=_build_root)
_staged_root = Path(_stage.name)
_native_modules, _native_imports = _native_build["stage_components"](Path(ROOT), _staged_root)
SRC = str(_staged_root)
sys.path.insert(0, SRC)
_package_spec = find_spec("qwenpaw")
if (
    _package_spec is None
    or not _package_spec.origin
    or not Path(_package_spec.origin).resolve().is_relative_to(_staged_root)
):
    raise RuntimeError("PyInstaller must import qwenpaw from the native staging directory")

MAIN_SCRIPT = os.path.join(ROOT, "main.py")

# Web 控制台静态资源：与源码布局一致放进包内 qwenpaw/console（见 qwenpaw/app/_app.py::_resolve_console_static_dir）。
# 须先在 console/ 执行前端构建（npm ci && npm run build），生成 console/dist（index.html + assets/）。
# 运行时浏览器加载的是构建产物；npm 依赖已由 Vite 打进 dist，无需也不会打包 node_modules。
# hook 风格二元组 (源路径, 目标相对目录)，见 PyInstaller format_binaries_and_datas；
# 源为目录时会递归拷贝，不能用 Tree 对象（Analysis 仅接受二元组）。
_CONSOLE_DIST = os.path.normpath(os.path.join(ROOT, "console", "dist"))
_console_datas_entry = None
if os.path.isdir(_CONSOLE_DIST) and os.path.isfile(
    os.path.join(_CONSOLE_DIST, "index.html"),
):
    _console_datas_entry = (_CONSOLE_DIST, "qwenpaw/console")
else:
    raise FileNotFoundError(
        "未找到 console/dist/index.html。请运行 scripts/build-paw.ps1 构建前端。",
    )

# Discover staged modules by filename rather than importing every channel and
# plugin at build time. List the compiled modules explicitly for Analysis.
pkg_hiddenimports = list(_native_modules)
for _module in (_staged_root / "qwenpaw").rglob("*.py"):
    _relative = _module.relative_to(_staged_root).with_suffix("")
    _parts = _relative.parts[:-1] if _relative.name == "__init__" else _relative.parts
    if all(part.isidentifier() for part in _parts) and all(
        (parent / "__init__.py").is_file()
        for parent in _module.parents if parent.is_relative_to(_staged_root / "qwenpaw")
    ):
        pkg_hiddenimports.append(".".join(_parts))
pkg_datas = collect_data_files("qwenpaw", include_py_files=False) + copy_metadata("qwenpaw")
pkg_binaries = collect_dynamic_libs("qwenpaw")
pkg_datas = [
    entry
    for entry in pkg_datas
    if "qwenpaw/agents/bundled_workspaces" not in entry[1].replace("\\", "/")
]

# reme-ai（import: reme）：ReMeConfigParser 在 reme/config/ 下解析 light.yaml 等，未收集会 FileNotFoundError
reme_datas, reme_binaries, reme_hiddenimports = collect_all(
    "reme",
    include_py_files=False,
)

datas = list(pkg_datas) + list(reme_datas)
if _console_datas_entry is not None:
    datas.insert(0, _console_datas_entry)
_BUNDLED_AGENTS_ZIP = os.path.normpath(
    os.path.join(SRC, "qwenpaw", "agents", "bundled_workspaces.zip"),
)
if os.path.isfile(_BUNDLED_AGENTS_ZIP):
    datas.insert(
        0,
        (_BUNDLED_AGENTS_ZIP, "qwenpaw/agents"),
    )
binaries = list(pkg_binaries) + list(reme_binaries)
hiddenimports = list(pkg_hiddenimports) + list(reme_hiddenimports)
hiddenimports += _native_imports

# Glob 动态导入此资源包，再按文件路径读取/执行 _glob_helper.py；
# 必须同时收集包和原始 .py 文件，仅收集 PYZ 字节码不能提供脚本文件。
hiddenimports.append("agentscope.tool._builtin._scripts")
datas += collect_data_files(
    "agentscope.tool._builtin._scripts",
    include_py_files=True,
)

# 时区数据（tzdata）
try:
    datas += copy_metadata("tzdata")
except Exception:
    pass

# importlib.metadata / version 查询：需带上对应发行版的 .dist-info，否则报 PackageNotFoundError
for _meta_pkg in (
    "fastmcp",
    "mcp",
):
    try:
        datas += copy_metadata(_meta_pkg)
    except Exception:
        pass

# 若运行时报 SSL/证书相关错误，可取消下一行注释
# try:
#     import certifi
#     datas += [(certifi.where(), "certifi")]
# except Exception:
#     pass

# 部分依赖在运行时仍可能访问 pkg_resources
hiddenimports += ["pkg_resources"]

a = Analysis(
    [MAIN_SCRIPT],
    pathex=[ROOT, SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

_native_leaks = {entry[0] for entry in a.pure}.intersection(_native_modules)
if _native_leaks:
    raise RuntimeError(f"Native module bytecode would enter PYZ: {sorted(_native_leaks)}")

pyz = PYZ(a.pure)

# ---------- onedir（默认）：解释器/依赖直接放在 dist/paw，启动时不再自解压到临时目录 ----------
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="paw",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="paw",
)
_stage.cleanup()
