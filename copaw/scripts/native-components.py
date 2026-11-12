"""Stage and audit Paw builds using precompiled components only."""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import runpy
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
load_components = runpy.run_path(str(ROOT / "src/qwenpaw/_native.py"))["load_components"]


def verify_components(root: Path) -> tuple[dict[str, Path], list[str]]:
    return load_components(root / "native-libs")


def check_sources(package_root: Path, modules: dict[str, Path]) -> None:
    for name in modules:
        relative = Path(*name.split("."))
        parent = package_root / relative.parent
        if any((parent / (relative.name + suffix)).exists() for suffix in (".py", ".pyc", ".pyx", ".c", ".cpp")):
            raise ValueError(f"Native module source or bytecode present: {name}")
        if any((parent / "__pycache__").glob(relative.name + ".*.pyc")):
            raise ValueError(f"Native module bytecode present: {name}")
    if "qwenpaw.prompt_resources" in modules:
        if (package_root / "qwenpaw/agents/md_files").exists():
            raise ValueError("Raw built-in prompt templates present")
        with zipfile.ZipFile(package_root / "qwenpaw/agents/bundled_workspaces.zip") as archive:
            if any(len(PurePosixPath(name).parts) == 2 and name.lower().endswith(".md")
                   for name in archive.namelist()):
                raise ValueError("Raw bundled-agent prompt templates present")


def stage_components(root: Path, destination: Path) -> tuple[dict[str, Path], list[str]]:
    modules, hidden_imports = verify_components(root)
    source = root / "src"

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name == "__pycache__"
                   or Path(name).suffix in {".pyc", ".pyo", ".pyd", ".so"}}
        if Path(directory) == source / "qwenpaw":
            ignored.add("console")  # Only built frontend files, not app/channels/console.
        if Path(directory) == source / "qwenpaw/agents":
            ignored.add("bundled_workspaces")
        return ignored

    shutil.copytree(source / "qwenpaw", destination / "qwenpaw", ignore=ignore)
    check_sources(destination, modules)
    for name, library in modules.items():
        target = destination.joinpath(*name.split(".")[:-1], library.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(library, target)
    return modules, hidden_imports


def audit_frozen(root: Path, app_dir: Path) -> None:
    from PyInstaller.archive.readers import CArchiveReader

    modules, _ = verify_components(root)
    lib = app_dir / "_internal"
    check_sources(lib, modules)
    for name, library in modules.items():
        installed = lib.joinpath(*name.split(".")[:-1], library.name)
        if not installed.is_file() or installed.read_bytes() != library.read_bytes():
            raise ValueError(f"Missing or unexpected native module in release: {name}")
    reader = CArchiveReader(str(app_dir / "paw.exe"))
    if modules.keys() & reader.toc.keys():
        raise ValueError("Native module bytecode present in executable")
    pyz_names = [name for name in reader.toc if name.lower().endswith(".pyz")]
    if not pyz_names:
        raise ValueError("Cannot audit executable: PYZ archive not found")
    for name in pyz_names:
        if leaked := modules.keys() & reader.open_embedded_archive(name).toc.keys():
            raise ValueError(f"Native module bytecode present in PYZ: {sorted(leaked)}")
    print(f"PASS: {len(modules)} native modules in release, with no source or bytecode copies")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--verify", action="store_true")
    action.add_argument("--audit-frozen", type=Path)
    args = parser.parse_args()
    if args.audit_frozen is not None:
        audit_frozen(ROOT, args.audit_frozen)
    else:
        libraries, _ = verify_components(ROOT)
        print(f"Verified {len(libraries)} precompiled native modules")
