"""Resolve the prebuilt native components required by this Paw checkout."""
from __future__ import annotations

import hashlib
from importlib.util import spec_from_file_location
import json
from pathlib import Path
import sys
import sysconfig


def load_components(root: Path) -> tuple[dict[str, Path], list[str]]:
    required = json.loads((root / "required.json").read_text(encoding="utf-8"))
    platform = sysconfig.get_platform().replace("-", "_")
    target = f"cp{sys.version_info.major}{sys.version_info.minor}-{platform}"
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    if sys.implementation.name != "cpython" or not suffix:
        raise RuntimeError("Paw native components require CPython")

    libraries: dict[str, Path] = {}
    hidden_imports: set[str] = set()
    for component, contract in required.items():
        directory = (root / component / target).resolve()
        manifest_file = directory / "manifest.json"
        if not manifest_file.is_file():
            raise FileNotFoundError(f"Missing prebuilt Paw component: {manifest_file}")
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        checksums = manifest.get("modules")
        if (
            manifest.get("format") != 1
            or manifest.get("component") != component
            or manifest.get("target") != target
            or manifest.get("api_version") != contract["api_version"]
            or not isinstance(checksums, dict)
            or checksums.keys() != set(contract["modules"])
        ):
            raise ValueError(f"Incompatible Paw component manifest: {manifest_file}")
        imports = manifest.get("hiddenimports")
        if not isinstance(imports, list) or any(
            not isinstance(name, str) or not all(part.isidentifier() for part in name.split("."))
            for name in imports
        ):
            raise ValueError(f"Invalid hidden imports in {manifest_file}")
        hidden_imports.update(imports)

        for name, checksum in checksums.items():
            parts = name.split(".")
            if parts[0] != "qwenpaw" or len(parts) < 2 or not all(part.isidentifier() for part in parts):
                raise ValueError(f"Invalid native module name: {name}")
            if name in libraries:
                raise ValueError(f"Native module supplied by multiple components: {name}")
            library = directory.joinpath(*parts[:-1], parts[-1] + suffix).resolve()
            if not library.is_relative_to(directory) or not library.is_file():
                raise FileNotFoundError(f"Missing native module {name}: {library}")
            with library.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
                    raise ValueError(f"Native module checksum mismatch: {library}")
            libraries[name] = library
    return libraries, sorted(hidden_imports)


class _CheckoutNativeFinder:
    def __init__(self, libraries: dict[str, Path]) -> None:
        self.libraries = libraries

    def find_spec(self, fullname, path=None, target=None):
        library = self.libraries.get(fullname)
        if library is not None:
            return spec_from_file_location(fullname, library)
        return None


def register_checkout_components() -> None:
    """Enable editable checkout imports under the original module names."""
    if getattr(sys, "frozen", False):
        return
    root = Path(__file__).resolve().parents[2] / "native-libs"
    if (root / "required.json").is_file():
        libraries, _ = load_components(root)
        sys.meta_path.insert(0, _CheckoutNativeFinder(libraries))
