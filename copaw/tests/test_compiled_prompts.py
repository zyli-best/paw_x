"""Native prompt release integrity and editable workspace compatibility."""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sysconfig
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def packaging():
    spec = importlib.util.spec_from_file_location("native_packaging", ROOT / "scripts/native-components.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_component(root, module):
    native = root / "native-libs"
    target = f"cp{sysconfig.get_python_version().replace('.', '')}-{sysconfig.get_platform().replace('-', '_')}"
    artifact = native / "example-component" / target
    relative = Path(*module.split("."))
    library = artifact / relative.with_name(relative.name + sysconfig.get_config_var("EXT_SUFFIX"))
    library.parent.mkdir(parents=True)
    library.write_bytes(b"example native library")
    (native / "required.json").write_text(json.dumps({
        "example-component": {"api_version": 1, "modules": [module]},
    }), encoding="utf-8")
    manifest = artifact / "manifest.json"
    manifest.write_text(json.dumps({
        "format": 1, "component": "example-component", "api_version": 1,
        "target": target, "modules": {module: hashlib.sha256(library.read_bytes()).hexdigest()},
        "hiddenimports": ["pathlib"],
    }), encoding="utf-8")
    return library, manifest


def test_staging_uses_prebuilt_artifact_and_keeps_console_channel(packaging, tmp_path):
    native_module = "qwenpaw.example"
    artifact, _ = fake_component(tmp_path, native_module)
    source = tmp_path / "src"
    for relative in ("qwenpaw/console/index.html", "qwenpaw/app/channels/console/channel.py",
                     "qwenpaw/agents/__pycache__/prompt.pyc"):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"example")
    library = tmp_path / "lib"
    modules, imports = packaging.stage_components(tmp_path, library)
    assert modules == {native_module: artifact}
    assert imports == ["pathlib"]
    assert (library / "qwenpaw" / artifact.name).read_bytes() == artifact.read_bytes()
    assert (library / "qwenpaw/app/channels/console/channel.py").read_bytes() == b"example"
    assert not (library / "qwenpaw/console").exists()
    assert not (library / "qwenpaw/agents/__pycache__").exists()


@pytest.mark.parametrize("defect", ["missing", "corrupt", "incompatible"])
def test_invalid_artifact_stops_staging(packaging, tmp_path, defect):
    artifact, manifest = fake_component(tmp_path, "qwenpaw.example")
    if defect == "missing":
        artifact.unlink()
        error, message = FileNotFoundError, "Missing native module"
    elif defect == "corrupt":
        artifact.write_bytes(b"wrong artifact")
        error, message = ValueError, "checksum mismatch"
    else:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["api_version"] += 1
        manifest.write_text(json.dumps(data), encoding="utf-8")
        error, message = ValueError, "Incompatible Paw component"
    destination = tmp_path / "stage"
    with pytest.raises(error, match=message):
        packaging.stage_components(tmp_path, destination)
    assert not destination.exists()


def test_workspace_defaults_work_without_template_sources(monkeypatch, tmp_path):
    from qwenpaw.agents.utils import setup_utils

    expected = {"AGENTS.md": b"rules\r\n", "SOUL.md": b"personality"}
    monkeypatch.setattr(setup_utils, "templates", lambda lang, group="": expected if lang == "en" else {})
    assert set(setup_utils.copy_md_files("missing", workspace_dir=tmp_path)) == set(expected)
    assert (tmp_path / "AGENTS.md").read_bytes() == expected["AGENTS.md"]
    (tmp_path / "AGENTS.md").write_bytes(b"user edits")
    assert setup_utils.copy_md_files("en", skip_existing=True, workspace_dir=tmp_path) == []
    assert (tmp_path / "AGENTS.md").read_bytes() == b"user edits"


def test_template_language_fallback_is_per_file(monkeypatch, tmp_path):
    from qwenpaw.agents.utils import setup_utils

    entries = {
        ("zh", "qa"): {"AGENTS.md": b"zh rules"},
        ("en", "qa"): {"AGENTS.md": b"en rules", "SOUL.md": b"en soul"},
    }
    monkeypatch.setattr(setup_utils, "templates", lambda lang, group="": entries.get((lang, group), {}))
    (tmp_path / "BOOTSTRAP.md").write_bytes(b"bootstrap")
    assert set(setup_utils.copy_template_md_files("qa", "zh", tmp_path)) == {"AGENTS.md", "SOUL.md"}
    assert (tmp_path / "AGENTS.md").read_bytes() == b"zh rules"
    assert (tmp_path / "SOUL.md").read_bytes() == b"en soul"
    assert not (tmp_path / "BOOTSTRAP.md").exists()


def test_bundled_prompts_restore_from_native_resources_without_overwriting(monkeypatch, tmp_path):
    from qwenpaw import prompt_resources
    from qwenpaw.app.bundled_agents import _ZipAgentSource

    archive = tmp_path / "agents.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("assistant/agent.json", '{"id":"assistant"}')
    monkeypatch.setattr(prompt_resources, "bundled_agent_prompts", lambda name: {"AGENTS.md": b"native defaults"})
    source = _ZipAgentSource(archive, "assistant")
    target = tmp_path / "workspace"
    source.copy_missing_to(target)
    assert (target / "AGENTS.md").read_bytes() == b"native defaults"
    (target / "AGENTS.md").write_bytes(b"custom rules")
    source.copy_missing_to(target)
    assert (target / "AGENTS.md").read_bytes() == b"custom rules"


def test_default_agent_startup_sync_uses_compiled_template_provider(monkeypatch, tmp_path):
    from qwenpaw import config, prompt_resources
    from qwenpaw.config import config as agent_configs

    monkeypatch.setattr(config, "load_config", lambda: SimpleNamespace(agents=SimpleNamespace(
        profiles={"default": SimpleNamespace(workspace_dir=str(tmp_path))},
    )))
    monkeypatch.setattr(agent_configs, "load_agent_config", lambda name: SimpleNamespace(language="en"))
    monkeypatch.setattr(prompt_resources, "templates", lambda lang: {
        "AGENTS.md": b"compiled rules", "SOUL.md": b"compiled personality",
    })
    entry = ROOT / "main.py"
    tree = ast.parse(entry.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_sync_default_prompt_files")
    namespace = {"Path": Path, "os": os}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(entry), "exec"), namespace)
    (tmp_path / "AGENTS.md").write_bytes(b"previous rules")
    namespace[function.name]()
    assert (tmp_path / "AGENTS.md").read_bytes() == b"compiled rules"
    assert (tmp_path / "SOUL.md").read_bytes() == b"compiled personality"


@pytest.mark.parametrize("leak", ["source", "pyz", "markdown", "archive", None])
def test_frozen_audit_rejects_sources_and_bytecode(packaging, monkeypatch, tmp_path, leak):
    from PyInstaller.archive import readers

    name = "qwenpaw.prompt_resources"
    artifact, _ = fake_component(tmp_path, name)
    lib = tmp_path / "_internal"
    library = lib / "qwenpaw" / artifact.name
    library.parent.mkdir(parents=True)
    library.write_bytes(artifact.read_bytes())
    archive = lib / "qwenpaw/agents/bundled_workspaces.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("assistant/agent.json", "{}")
        if leak == "archive":
            stream.writestr("assistant/AGENTS.md", "leak")
    if leak == "source":
        (lib / "qwenpaw/prompt_resources.py").touch()
    if leak == "markdown":
        (lib / "qwenpaw/agents/md_files").mkdir()
    monkeypatch.setattr(readers, "CArchiveReader", lambda path: SimpleNamespace(
        toc={"PYZ.pyz": ()},
        open_embedded_archive=lambda entry: SimpleNamespace(toc={name: ()} if leak == "pyz" else {}),
    ))
    if leak:
        with pytest.raises(ValueError):
            packaging.audit_frozen(tmp_path, tmp_path)
    else:
        packaging.audit_frozen(tmp_path, tmp_path)


def test_checkout_loads_precompiled_prompts_and_finds_portable_runtimes(monkeypatch):
    from qwenpaw import prompt_resources
    from qwenpaw.runtime import prompt_manager
    from qwenpaw.utils import bundled_runtime

    for module in (prompt_resources, prompt_manager, bundled_runtime):
        assert Path(module.__file__).resolve().is_relative_to(ROOT / "native-libs")
    assert prompt_resources.templates("zh")["AGENTS.md"]
    monkeypatch.delenv("QWENPAW_BUNDLED_RUNTIMES_DIR", raising=False)
    monkeypatch.delenv("QWENPAW_BUNDLED_RUNTIME_ISOLATION", raising=False)
    assert bundled_runtime.runtime_directory() == Path.home().resolve() / ".devco/bin/runtime"
    monkeypatch.setenv("QWENPAW_BUNDLED_RUNTIME_ISOLATION", "1")
    assert bundled_runtime.runtime_directory() == Path.home().resolve() / ".devco/bin/runtime"
    monkeypatch.setenv("QWENPAW_BUNDLED_RUNTIMES_DIR", str(ROOT / "runtimes"))
    assert bundled_runtime.runtime_directory() == ROOT / "runtimes"
