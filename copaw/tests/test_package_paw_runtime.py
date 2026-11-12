"""Release layout and integrity checks for separately deployable Paw ZIPs."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest


@pytest.fixture
def packaging(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts/package-paw-runtime.py"
    spec = importlib.util.spec_from_file_location("package_paw_runtime", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    lock = tmp_path / "scripts/bundled-runtimes.lock.json"
    lock.parent.mkdir()
    lock.write_text('{"test": true}', encoding="utf-8")

    app = tmp_path / "dist/paw"
    for name in module.REQUIRED_APP_FILES | {
        "devcopaw.exe", ".devcopaw-launcher.lock", "old-release.zip",
        ".devco_paw/config.json", "logs/server.log",
        "_internal/__pycache__/module.pyc",
    }:
        path = app / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())

    runtime = app / "runtimes"
    files = {}
    for name in (
        "python/python.exe", "python/Lib/venv/__init__.py",
        "node/node.exe", "node/node_modules/npm/bin/npm-cli.js",
        "node/node_modules/npm/bin/npx-cli.js",
        "git/cmd/git.exe", "git/bin/bash.exe",
    ):
        path = runtime / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
        files[name] = module.digest(path)
    manifest = {
        "lock_sha256": module.digest(lock),
        "files": files,
        "runtimes": {
            kind: {"version": "test"} for kind in ("python", "nodejs", "git")
        },
    }
    (runtime / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(script), "--app-dir", str(app)])
    return module, app, runtime


@pytest.mark.parametrize("source", ["directory", "archive"])
def test_split_release_round_trip_and_checksums(packaging, monkeypatch, source):
    module, app, runtime = packaging
    original_exe = (app / "paw.exe").read_bytes()
    if source == "archive":
        base = module.ROOT / "dist/old-paw.zip"
        with zipfile.ZipFile(base, "w") as package:
            for path in app.rglob("*"):
                if path.is_file():
                    package.write(path, path.relative_to(app).as_posix())
        monkeypatch.setattr(sys, "argv", [
            "package", "--base-archive", str(base), "--runtime-dir", str(runtime),
        ])
    module.main()
    application_zip = module.ROOT / "dist/paw-runtime.zip"
    runtimes_zip = module.ROOT / "dist/runtimes.zip"
    with zipfile.ZipFile(application_zip) as package:
        assert set(package.namelist()) == module.REQUIRED_APP_FILES
        assert package.read("paw.exe") == original_exe
        package.extractall(module.ROOT / "installed")
    expected = {
        path.relative_to(runtime).as_posix(): path.read_bytes()
        for path in runtime.rglob("*") if path.is_file()
    }
    with zipfile.ZipFile(runtimes_zip) as package:
        assert set(package.namelist()) == set(expected)
        assert {Path(name).parts[0] for name in package.namelist()} == {
            "manifest.json", "python", "node", "git",
        }
        package.extractall(module.ROOT / "installed/runtimes")
    for name, content in expected.items():
        assert (module.ROOT / "installed/runtimes" / name).read_bytes() == content
    for path in (application_zip, runtimes_zip):
        checksum = path.with_name(path.name + ".sha256").read_text(encoding="ascii")
        assert checksum == hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n"
    report = json.loads((module.ROOT / "build/paw-runtime-package-report.json").read_text())
    assert report["application_files"] == len(module.REQUIRED_APP_FILES)
    assert report["runtime_files"] == len(expected)
    assert report["runtime_archive"] == str(runtimes_zip)


@pytest.mark.parametrize("damage", ["content", "missing", "extra", "lock", "application"])
def test_invalid_inputs_preserve_existing_releases(packaging, damage):
    module, app, runtime = packaging
    outputs = [module.ROOT / "dist" / name for name in ("paw-runtime.zip", "runtimes.zip")]
    for output in outputs:
        output.write_bytes(b"previous release")
        output.with_name(output.name + ".sha256").write_bytes(b"previous checksum")
    if damage == "content":
        (runtime / "python/python.exe").write_bytes(b"corrupted")
    elif damage == "missing":
        (runtime / "node/node.exe").unlink()
    elif damage == "extra":
        (runtime / "unexpected.txt").write_bytes(b"extra")
    elif damage == "lock":
        (module.ROOT / "scripts/bundled-runtimes.lock.json").write_bytes(b"changed")
    else:
        (app / "_internal/python311.dll").unlink()
    with pytest.raises(ValueError):
        module.main()
    for output in outputs:
        assert output.read_bytes() == b"previous release"
        assert output.with_name(output.name + ".sha256").read_bytes() == b"previous checksum"
    assert not list((module.ROOT / "dist").glob("paw-packaging-*"))


@pytest.mark.parametrize("flag", ["--output", "--runtimes-output"])
def test_outputs_cannot_overwrite_input_archive(packaging, monkeypatch, flag):
    module, app, runtime = packaging
    base = module.ROOT / "dist/input.zip"
    base.write_bytes(b"preserve input")
    monkeypatch.setattr(sys, "argv", [
        "package", "--base-archive", str(base), "--runtime-dir", str(runtime), flag, str(base),
    ])
    with pytest.raises(SystemExit):
        module.main()
    assert base.read_bytes() == b"preserve input"
