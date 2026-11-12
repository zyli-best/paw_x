"""Package Paw and its portable tools into two independently deployable ZIPs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_APP_FILES = {
    "paw.exe", "_internal/python311.dll",
    "_internal/qwenpaw/console/index.html",
    "_internal/qwenpaw/agents/bundled_workspaces.zip",
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def app_entry(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError(f"Unsafe package path: {name}")
    if name != "paw.exe" and (len(path.parts) < 2 or path.parts[0] != "_internal"):
        return False
    return not any(part.startswith((".devco_paw", ".qwenpaw", ".copaw"))
                   or part in {"logs", "__pycache__", ".devcopaw-launcher.lock"}
                   for part in path.parts)


def verify_archive(path: Path, expected: set[str]) -> None:
    with zipfile.ZipFile(path) as package:
        names = package.namelist()
        if len(names) != len(expected) or set(names) != expected:
            raise ValueError(f"Release entry mismatch: {path.name}")
        if bad := package.testzip():
            raise ValueError(f"ZIP CRC failed: {bad}")


def package_application(args: argparse.Namespace, temporary: Path) -> set[str]:
    app_files: set[str] = set()
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        if args.base_archive:
            print(f"Packaging existing application: {args.base_archive}", flush=True)
            with zipfile.ZipFile(args.base_archive) as base:
                for entry in base.infolist():
                    if entry.is_dir() or not app_entry(entry.filename):
                        continue
                    if entry.filename in app_files:
                        raise ValueError(f"Duplicate application file: {entry.filename}")
                    with base.open(entry) as src, package.open(entry.filename, "w", force_zip64=True) as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    app_files.add(entry.filename)
        else:
            for path in sorted(args.app_dir.rglob("*")):
                name = path.relative_to(args.app_dir).as_posix()
                if path.is_file() and app_entry(name):
                    package.write(path, name)
                    app_files.add(name)
    if missing := REQUIRED_APP_FILES - app_files:
        raise ValueError(f"Incomplete application: {sorted(missing)}")
    verify_archive(temporary, app_files)
    print(f"Application packaged: {len(app_files)} files", flush=True)
    return app_files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--app-dir", type=Path)
    sources.add_argument("--base-archive", type=Path)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/paw-runtime.zip")
    parser.add_argument("--runtimes-output", type=Path, default=ROOT / "dist/runtimes.zip")
    args = parser.parse_args()
    runtime = args.runtime_dir or (args.app_dir / "runtimes" if args.app_dir else None)
    if runtime is None:
        parser.error("--runtime-dir is required with --base-archive")
    runtime = runtime.resolve()
    output = args.output.resolve()
    runtimes_output = args.runtimes_output.resolve()
    for path in (output, runtimes_output):
        if not path.is_relative_to(ROOT / "dist"):
            parser.error("Archive outputs must be below this checkout's dist directory")
        if path.is_relative_to(runtime):
            parser.error("Archive outputs must be outside the runtime directory")
        if args.base_archive and args.base_archive.resolve() == path:
            parser.error("The base archive and outputs must be different files")
    if output == runtimes_output:
        parser.error("Application and runtime outputs must be different files")
    manifest = json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))
    if manifest["lock_sha256"] != digest(ROOT / "scripts/bundled-runtimes.lock.json"):
        raise ValueError("Runtime manifest does not match the checked-in lock")
    runtime_files = {p.relative_to(runtime).as_posix(): p
                     for p in runtime.rglob("*") if p.is_file()}
    if set(runtime_files) != set(manifest["files"]) | {"manifest.json"}:
        raise ValueError("Unexpected or missing files in the runtime directory")
    if {PurePosixPath(name).parts[0] for name in runtime_files} != {"manifest.json", "python", "node", "git"}:
        raise ValueError("Runtime archive must contain manifest.json, python/, node/ and git/")
    output.parent.mkdir(parents=True, exist_ok=True)
    runtimes_output.parent.mkdir(parents=True, exist_ok=True)
    # Validate both archives before replacing either existing release.
    with tempfile.TemporaryDirectory(prefix="paw-packaging-", dir=output.parent) as staging:
        temporary = Path(staging) / "application.zip"
        temporary_runtimes = Path(staging) / "runtimes.zip"
        app_files = package_application(args, temporary)
        with zipfile.ZipFile(temporary_runtimes, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as package:
            for index, (name, path) in enumerate(sorted(runtime_files.items()), 1):
                if name != "manifest.json" and digest(path) != manifest["files"][name]:
                    raise ValueError(f"Runtime SHA256 mismatch: {name}")
                package.write(path, name)
                if index % 2000 == 0:
                    print(f"Runtime files packaged: {index}/{len(runtime_files)}", flush=True)
        print("Verifying runtime ZIP CRC and layout", flush=True)
        verify_archive(temporary_runtimes, set(runtime_files))
        checksum = digest(temporary)
        runtimes_checksum = digest(temporary_runtimes)
        os.replace(temporary, output)
        os.replace(temporary_runtimes, runtimes_output)
    for path, value in ((output, checksum), (runtimes_output, runtimes_checksum)):
        path.with_name(path.name + ".sha256").write_text(
            value + "  " + path.name + "\n", encoding="ascii",
        )
    report = {
        "archive": str(output), "bytes": output.stat().st_size, "sha256": checksum,
        "runtime_archive": str(runtimes_output), "runtime_bytes": runtimes_output.stat().st_size,
        "runtime_sha256": runtimes_checksum,
        "application_files": len(app_files), "runtime_files": len(runtime_files),
        "application_source": str(args.base_archive or args.app_dir),
        "application_source_sha256": digest(args.base_archive) if args.base_archive else None,
        "runtimes": {key: value["version"] for key, value in manifest["runtimes"].items()},
        "zip_crc_passed": True,
    }
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build/paw-runtime-package-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8",
    )
    print(f"Release ready: {output} ({report['bytes'] / 1024**2:.1f} MiB)", flush=True)
    print(f"SHA256: {checksum}", flush=True)
    print(f"Tools ready: {runtimes_output} ({report['runtime_bytes'] / 1024**2:.1f} MiB)", flush=True)
    print(f"SHA256: {runtimes_checksum}", flush=True)


if __name__ == "__main__":
    main()
