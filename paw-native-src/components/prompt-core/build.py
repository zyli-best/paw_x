"""Build the prompt-core artifact from this component's private sources."""
from __future__ import annotations

import argparse
import ast
from contextlib import chdir
import hashlib
from importlib.util import resolve_name
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import sysconfig
from tempfile import TemporaryDirectory

COMPONENT = "prompt-core"
API_VERSION = 1
ROOT = Path(__file__).resolve().parent


def template_bytes(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*.md"))
    }


def embed_templates(source: Path) -> None:
    templates = template_bytes(ROOT / "resources/md_files")
    if not any(name.startswith("en/") for name in templates):
        raise ValueError("Built-in English templates are missing")
    languages = sorted({PurePosixPath(name).parts[0] for name in templates
                        if len(PurePosixPath(name).parts) == 2})
    bundled: dict[str, dict[str, bytes]] = {}
    for name, content in template_bytes(ROOT / "resources/bundled-prompts").items():
        agent, filename = PurePosixPath(name).parts
        bundled.setdefault(agent, {})[filename] = content
    if not bundled:
        raise ValueError("Bundled workspace prompt templates are missing")
    with source.open("a", encoding="utf-8") as stream:
        stream.write(f"\n_TEMPLATES = {templates!r}\n_BUNDLED = {bundled!r}\n"
                     f"_LANGUAGES = frozenset({languages!r})\n")


def dependencies(module: str, source: Path) -> set[str]:
    package = module.rpartition(".")[0]
    imports: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Import):
            imports.update(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            name = node.module or ""
            if node.level:
                name = resolve_name("." * node.level + name, package)
            imports.add(name)
            if node.module is None:
                imports.update(f"{name}.{item.name}" for item in node.names if item.name not in {"*", "__file__"})
    return imports


def build(output_root: Path) -> Path:
    from Cython.Build import cythonize
    from setuptools import Distribution, Extension

    if sys.implementation.name != "cpython":
        raise ValueError("Build with the target CPython interpreter")
    version = f"cp{sys.version_info.major}{sys.version_info.minor}"
    platform = sysconfig.get_platform().replace("-", "_")
    target = f"{version}-{platform}"
    component_dir = output_root.resolve() / COMPONENT
    component_dir.mkdir(parents=True, exist_ok=True)
    output = component_dir / target
    with TemporaryDirectory(prefix="compile-", dir=component_dir) as temp:
        work = Path(temp)
        source = work / "src"
        artifact = work / "artifact"
        shutil.copytree(ROOT / "src", source, ignore=shutil.ignore_patterns("__pycache__"))
        embed_templates(source / "qwenpaw/prompt_resources.py")
        modules = {
            ".".join(path.relative_to(source).with_suffix("").parts): path
            for path in sorted(source.rglob("*.py"))
        }
        hidden_imports = set().union(*(dependencies(name, path) for name, path in modules.items()))
        extensions = [Extension(name, [str(path.relative_to(work))]) for name, path in modules.items()]
        # Relative compiler paths keep MSVC object filenames short.
        with chdir(work):
            extensions = cythonize(extensions, build_dir="c", quiet=True, compiler_directives={
                "language_level": 3,
                "binding": True,
                "annotation_typing": False,
                "emit_code_comments": False,
            })
            distribution = Distribution({"name": "paw-prompt-core", "ext_modules": extensions})
            command = distribution.get_command_obj("build_ext")
            command.build_lib = str(artifact)
            command.build_temp = "o"
            distribution.run_command("build_ext")
        suffix = sysconfig.get_config_var("EXT_SUFFIX")
        checksums = {}
        for name in modules:
            relative = Path(*name.split("."))
            library = artifact / relative.with_name(relative.name + suffix)
            with library.open("rb") as stream:
                checksums[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest = {
            "format": 1,
            "component": COMPONENT,
            "api_version": API_VERSION,
            "target": target,
            "modules": checksums,
            "hiddenimports": sorted(hidden_imports),
        }
        (artifact / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        backup = work / "previous"
        if output.exists():
            if output.resolve() != output:
                raise ValueError(f"Refusing to replace linked artifact directory: {output}")
            output.rename(backup)
        try:
            artifact.rename(output)
        except OSError:
            if backup.exists():
                backup.rename(output)
            raise
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True, type=Path)
    print(f"Built prompt-core: {build(parser.parse_args().output_root)}")
