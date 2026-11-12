"""Download, verify and stage the locked Windows x64 tool runtimes.

These interpreters are independent of the Python used to build paw.exe.
No packages are installed in the base runtimes during validation.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = ROOT / "scripts/bundled-runtimes.lock.json"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(spec: dict, cache: Path) -> Path:
    target = cache / spec["filename"]
    if target.is_file() and sha256(target) == spec["sha256"]:
        print(f"Cached SHA256 OK: {target.name}", flush=True)
        return target
    partial = target.with_name(target.name + ".part")
    for attempt in range(1, 4):
        print(f"Downloading ({attempt}/3): {target.name}", flush=True)
        try:
            req = urllib.request.Request(
                spec["url"], headers={"User-Agent": "Paw-runtime-builder/1.0"},
            )
            with urllib.request.urlopen(req, timeout=90) as response, partial.open("wb") as out:
                total = 0
                last_report = time.monotonic()
                while chunk := response.read(1024 * 1024):
                    out.write(chunk)
                    total += len(chunk)
                    if time.monotonic() - last_report > 15:
                        print(f"  {target.name}: {total / 1024**2:.1f} MiB", flush=True)
                        last_report = time.monotonic()
            actual = sha256(partial)
            if actual != spec["sha256"]:
                raise ValueError(f"SHA256 mismatch: {target.name}: {actual}")
            os.replace(partial, target)
            print(f"Downloaded SHA256 OK: {target.name} ({total / 1024**2:.1f} MiB)", flush=True)
            return target
        except Exception as error:
            print(f"Download attempt failed: {target.name}: {type(error).__name__}: {error}", flush=True)
            if attempt == 3:
                raise
    raise AssertionError("Unreachable")


def extract(archive_path: Path, destination: Path, archive_root: str) -> None:
    """Extract regular files under the expected top-level directory only."""
    def target(name: str) -> Path:
        rel = PurePosixPath(name)
        if "\\" in name or rel.is_absolute() or ":" in name or ".." in rel.parts:
            raise ValueError(f"Unsafe archive path: {name}")
        if not rel.parts or rel.parts[0] != archive_root:
            raise ValueError(f"Unexpected archive root: {name}")
        result = destination.joinpath(*rel.parts[1:])
        if not result.resolve().is_relative_to(destination.resolve()):
            raise ValueError(f"Archive path escapes destination: {name}")
        return result

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                path = target(member.filename)
                if member.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    if (member.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError(f"Unexpected symlink: {member.filename}")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as src, path.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive:
                path = target(member.name)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as src, path.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                else:
                    raise ValueError(f"Unexpected archive entry: {member.name}")


def clean_env(work: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.upper().startswith(("PYTHON", "NODE_", "NPM_", "GIT_", "VIRTUAL_ENV", "CONDA_")):
            env.pop(key)
    system = Path(os.environ["SystemRoot"])
    env.update({
        "PATH": os.pathsep.join(map(str, (system / "System32", system))),
        "HOME": str(work), "USERPROFILE": str(work),
        "APPDATA": str(work / "appdata"), "LOCALAPPDATA": str(work / "localappdata"),
        "TEMP": str(work), "TMP": str(work),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
        "PIP_CACHE_DIR": str(work / "pip-cache"),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_CONFIG_FILE": os.devnull,
        "NPM_CONFIG_CACHE": str(work / "npm-cache"),
        "NPM_CONFIG_PREFIX": str(work / "npm-global"),
        "NPM_CONFIG_USERCONFIG": str(work / "empty.npmrc"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    })
    for name in ("appdata", "localappdata"):
        (work / name).mkdir(exist_ok=True)
    return env


def run(arguments: list[str | Path], work: Path, env: dict, timeout: int = 120) -> str:
    result = subprocess.run(
        list(map(str, arguments)), cwd=work, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8",
        errors="replace", timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {arguments[0]}\n{result.stdout[-6000:]}")
    return result.stdout.strip()


def smoke_test(runtime: Path, lock: dict) -> dict:
    """Prove the portable tools and an offline dependency install work."""
    with tempfile.TemporaryDirectory(prefix="runtime-smoke-", dir=ROOT / "build") as name:
        work = Path(name)
        env = clean_env(work)
        python = runtime / "python/python.exe"
        node = runtime / "node/node.exe"
        probe = (
            "import json,sys,struct,ssl,sqlite3,ctypes,lzma,bz2,venv,ensurepip;"
            "print(json.dumps(dict(version='.'.join(map(str,sys.version_info[:3])),"
            "executable=sys.executable,bits=struct.calcsize('P')*8)))"
        )
        info = json.loads(run([python, "-I", "-B", "-c", probe], work, env))
        assert info["version"] == lock["runtimes"]["python"]["version"], info
        assert info["bits"] == 64 and Path(info["executable"]).samefile(python), info
        run([python, "-I", "-B", "-m", "venv", work / "venv"], work, env)
        vpython = work / "venv/Scripts/python.exe"
        vprobe = json.loads(run([vpython, "-I", "-B", "-c",
            "import json,sys; print(json.dumps(dict(base=sys.base_prefix,prefix=sys.prefix)))"], work, env))
        assert Path(vprobe["base"]).samefile(python.parent), vprobe
        assert Path(vprobe["prefix"]).samefile(vpython.parent.parent), vprobe

        # Use a tiny local wheel: no index access and no base-runtime install.
        wheel = work / "paw_runtime_probe-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("paw_runtime_probe.py", "VALUE = 'isolated'\n")
            archive.writestr("paw_runtime_probe-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: paw-runtime-probe\nVersion: 1.0\n")
            archive.writestr("paw_runtime_probe-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: paw\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr("paw_runtime_probe-1.0.dist-info/RECORD", "")
        pip_version = run([vpython, "-B", "-m", "pip", "--version"], work, env).split()[1]
        run([vpython, "-B", "-m", "pip", "install", "--no-index", "--no-deps", wheel], work, env)
        assert run([vpython, "-I", "-B", "-c", "import paw_runtime_probe; print(paw_runtime_probe.VALUE)"], work, env) == "isolated"
        assert run([python, "-I", "-B", "-c", "import importlib.util; print(importlib.util.find_spec('paw_runtime_probe') is None)"], work, env) == "True"
        print(f"PASS Python {info['version']}: stdlib, venv, pip and isolated offline install", flush=True)

        ninfo = json.loads(run([node, "-p", "JSON.stringify({version:process.versions.node,executable:process.execPath,arch:process.arch})"], work, env))
        assert ninfo["version"] == lock["runtimes"]["nodejs"]["version"] and ninfo["arch"] == "x64", ninfo
        assert Path(ninfo["executable"]).samefile(node), ninfo
        npm = runtime / "node/node_modules/npm/bin/npm-cli.js"
        npx = runtime / "node/node_modules/npm/bin/npx-cli.js"
        npm_version = run([node, npm, "--version"], work, env)
        npx_version = run([node, npx, "--version"], work, env)
        assert npm_version == npx_version == lock["runtimes"]["nodejs"]["npm_version"]
        cmd = Path(os.environ["SystemRoot"]) / "System32/cmd.exe"
        for wrapper in ("npm.cmd", "npx.cmd"):
            assert run([cmd, "/d", "/c", runtime / "node" / wrapper, "--version"], work, env) == npm_version
        package = work / "local-node-package"
        package.mkdir()
        (package / "package.json").write_text('{"name":"paw-runtime-probe","version":"1.0.0","main":"index.js"}', encoding="utf-8")
        (package / "index.js").write_text("module.exports = 'isolated';\n", encoding="utf-8")
        node_tarball = work / "paw-runtime-probe-1.0.0.tgz"
        with tarfile.open(node_tarball, "w:gz") as archive:
            archive.add(package, arcname="package")
        project = work / "node-project"
        project.mkdir()
        (project / "package.json").write_text('{"name":"paw-runtime-smoke","version":"1.0.0","private":true}', encoding="utf-8")
        node_env = dict(env)
        node_env["PATH"] = str(node.parent) + os.pathsep + env["PATH"]
        run([node, npm, "--prefix", project, "install", node_tarball, "--offline", "--ignore-scripts", "--no-audit", "--no-fund", "--global=false"], work, node_env)
        assert run([node, "-e", "console.log(require('./node_modules/paw-runtime-probe'))"], project, node_env) == "isolated"
        print(f"PASS Node {ninfo['version']}, npm/npx {npm_version}: isolated offline local install", flush=True)

        git = runtime / "git/cmd/git.exe"
        git_version = run([git, "--version"], work, env)
        assert git_version == "git version " + lock["runtimes"]["git"]["version"], git_version
        exec_path = Path(run([git, "--exec-path"], work, env))
        assert exec_path.resolve().is_relative_to((runtime / "git").resolve()), exec_path
        bash_version = run([runtime / "git/bin/bash.exe", "--noprofile", "--norc", "-c", "printf '%s' \"$BASH_VERSION\""], work, env)
        run([git, "init", "--quiet", work / "git-project"], work, env)
        print(f"PASS {git_version}, Bash {bash_version}: portable execution with system-only PATH", flush=True)
        return {"python": info["version"], "pip": pip_version, "nodejs": ninfo["version"],
                "npm": npm_version, "npx": npx_version, "git": git_version, "bash": bash_version,
                "checks": ["system-only PATH", "Python stdlib", "venv creation", "offline pip isolation",
                           "npm/npx Windows wrappers", "offline npm isolation", "Git init", "Bash execution"]}


def inventory(runtime: Path) -> dict[str, str]:
    return {path.relative_to(runtime).as_posix(): sha256(path)
            for path in sorted(runtime.rglob("*")) if path.is_file() and path != runtime / "manifest.json"}


def verify(runtime: Path, lock_hash: str) -> dict:
    manifest = json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))
    if manifest["lock_sha256"] != lock_hash:
        raise ValueError(f"Runtime lock mismatch: {runtime}")
    if inventory(runtime) != manifest["files"]:
        raise ValueError(f"Runtime file integrity mismatch: {runtime}")
    print(f"PASS runtime manifest: {len(manifest['files'])} files", flush=True)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/paw/runtimes")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        parser.error("Run with a 64-bit Python on Windows")
    lock_bytes = LOCK_FILE.read_bytes()
    lock = json.loads(lock_bytes)
    lock_hash = hashlib.sha256(lock_bytes).hexdigest()
    output = args.output.resolve()
    # Keep generated content and all temporary work within this checkout.
    if not output.is_relative_to(ROOT) or output == ROOT:
        parser.error("--output must be a directory below this checkout")
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    if args.verify_only:
        before = verify(output, lock_hash)["files"]
        report = smoke_test(output, lock)
        if inventory(output) != before:
            raise ValueError("Smoke tests modified the base runtimes")
        report_file = build / "bundled-runtimes-verification.json"
        report_file.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return
    cache = build / "runtime-downloads"
    cache.mkdir(exist_ok=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        names = list(lock["runtimes"])
        paths = list(pool.map(lambda key: download(lock["runtimes"][key], cache), names))
    downloads = dict(zip(names, paths))
    if args.download_only:
        return
    prepared = build / "bundled-runtimes" / lock_hash[:16] / "runtimes"
    if (prepared / "manifest.json").is_file():
        verify(prepared, lock_hash)
    else:
        stage = build / ("runtime-stage-" + uuid.uuid4().hex[:10])
        stage.mkdir()
        runtime = stage / "runtimes"
        runtime.mkdir()
        for name, directory in (("python", "python"), ("nodejs", "node")):
            print(f"Extracting {name}", flush=True)
            extract(downloads[name], runtime / directory, lock["runtimes"][name]["archive_root"])
        print("Extracting PortableGit (silent, application-local)", flush=True)
        env = clean_env(stage)
        # This upstream SFX defaults to <SFX directory>/PortableGit. Run a
        # private copy so its vendor post-install hook stays inside staging,
        # then relocate the complete portable tree without assuming -o support.
        git_sfx = stage / downloads["git"].name
        shutil.copyfile(downloads["git"], git_sfx)
        run([git_sfx, "-y", "-gm2"], stage, env, timeout=600)
        (stage / "PortableGit").rename(runtime / "git")
        for spec in lock["runtimes"].values():
            for relative in spec["executables"].values():
                if not (runtime / relative).is_file():
                    raise FileNotFoundError(runtime / relative)
        before = inventory(runtime)
        report = smoke_test(runtime, lock)
        if inventory(runtime) != before:
            raise ValueError("Smoke tests modified the base runtimes")
        manifest = {**lock, "lock_sha256": lock_hash, "validation": report, "files": before}
        (runtime / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        prepared.parent.mkdir(parents=True, exist_ok=True)
        runtime.rename(prepared)
    if output.exists():
        verify(output, lock_hash)
        print(f"Already prepared: {output}", flush=True)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(prepared, output)
    verify(output, lock_hash)
    print(f"Bundled runtimes ready: {output}", flush=True)


if __name__ == "__main__":
    main()
