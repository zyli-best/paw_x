"""Offline proof against the separately prepared Windows runtime distribution."""

import json
from pathlib import Path
import shutil
import sys
import tarfile
import zipfile

import pytest
from agentscope.message import ToolResultState

from qwenpaw.config.config import BundledRuntimesConfig
from qwenpaw.config.context import (
    current_request_context,
    current_shell_command_executable,
    current_workspace_dir,
    get_current_shell_command_executable,
)
from qwenpaw.utils import bundled_runtime as br, runtime_storage


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Windows portable distribution")
@pytest.mark.parametrize("isolated", [True, False], ids=["isolated", "shared"])
async def test_real_offline_installs_and_task_output(
    monkeypatch, tmp_path, isolated,
):
    bundle = Path(__file__).resolve().parents[1] / "runtimes"
    if not (bundle / "manifest.json").is_file():
        pytest.skip("Prepare the locked runtime distribution first")
    from qwenpaw.agents.tools import shell
    from qwenpaw.agents.tools.file_io import write_file

    runtime_home = tmp_path / ".devco"
    monkeypatch.setattr(runtime_storage, "runtime_home", lambda: runtime_home)
    monkeypatch.setattr(br, "runtime_home", lambda: runtime_home)
    monkeypatch.setenv("QWENPAW_BUNDLED_RUNTIME_ISOLATION", "1" if isolated else "0")
    monkeypatch.setenv("QWENPAW_BUNDLED_RUNTIMES_DIR", str(bundle))
    if not isolated:
        shutil.copytree(bundle, runtime_home / "bin/runtime")
    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", "1")
    monkeypatch.setenv("QWENPAW_WINDOWS_DIRECT_SHELL_GUARD", "off")
    monkeypatch.setenv("QWENPAW_WINDOWS_DIRECT_SHELL_LLM_GUARD", "off")

    def inventory():
        return {
            p.relative_to(bundle).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in bundle.rglob("*")
            if p.is_file()
        }

    before = inventory()
    root = tmp_path / "项目 空格"
    root.mkdir()
    request = {
        "sandbox_mounts": [str(root)],
        "session_id": "offline",
        "is_coding_mode": False,
    }
    state = br.prepare_request_runtimes(
        request, tmp_path / "workspace", "offline", BundledRuntimesConfig(enabled=True)
    )
    assert state is not None
    python_exe = state.info["python"]["executable"]
    if isolated:
        python_env = state.storage_root / "p"
        assert (python_env / "Scripts/pip.exe").is_file()
        assert not any((python_env / "Lib/site-packages").rglob("*.pyc"))
    else:
        python_env = runtime_home / "bin/runtime/python"
        assert state.storage_root is None and not (runtime_home / "e").exists()
        assert python_exe == str(python_env / "python.exe")
    assert (
        br.prepare_request_runtimes(
            request,
            tmp_path / "workspace",
            "offline",
            BundledRuntimesConfig(enabled=True),
        ).storage_root
        == state.storage_root
    )
    token = br.current_runtimes.set(state)
    request_token = current_request_context.set(request)
    shell_token = current_shell_command_executable.set("cmd.exe")

    # Existing host installations/configuration must not redirect selected tools.
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "host-python"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "host-python-libraries"))
    monkeypatch.setenv("PIP_REQUIRE_VIRTUALENV", "1")
    monkeypatch.setenv("NODE_OPTIONS", "--require=nonexistent-host-hook")
    monkeypatch.setenv("NPM_CONFIG_PREFIX", str(tmp_path / "host-node-global"))

    async def execute(exe, *args, cwd=None):
        # All file arguments are quoted, including paths with Chinese/spaces.
        if get_current_shell_command_executable() == "powershell.exe":
            command = "& " + " ".join("'" + str(arg).replace("'", "''") + "'" for arg in (exe, *args))
        else:
            command = " ".join('"' + str(arg) + '"' for arg in (exe, *args))
        result = await shell.execute_shell_command(command, cwd=cwd)
        text = "\n".join(
            block.text for block in result.content if hasattr(block, "text")
        )
        assert result.state == ToolResultState.SUCCESS, text
        assert "Command failed" not in text, text
        return text

    try:
        wheel = root / "paw_dep_probe-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("paw_dep_probe.py", "VALUE = 'python-isolated'\n")
            archive.writestr(
                "paw_dep_probe-1.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: paw-dep-probe\nVersion: 1.0\n",
            )
            archive.writestr(
                "paw_dep_probe-1.0.dist-info/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            archive.writestr("paw_dep_probe-1.0.dist-info/RECORD", "")
        await execute("pip", "install", "--no-index", "--no-deps", wheel)
        (root / "task.py").write_text(
            "import paw_dep_probe,sys; print(paw_dep_probe.VALUE); print(sys.executable)\n",
            encoding="utf-8",
        )
        text = await execute("python", "task.py")
        assert (
            "python-isolated" in text
            and python_exe in text
        )
        assert (python_env / "Lib/site-packages/paw_dep_probe.py").is_file()
        assert not (bundle / "python/Lib/site-packages/paw_dep_probe.py").exists()

        package = root / "probe-package"
        package.mkdir()
        (package / "package.json").write_text(
            json.dumps(
                {"name": "paw-dep-probe", "version": "1.0.0", "main": "index.js"}
            ),
            encoding="utf-8",
        )
        (package / "index.js").write_text(
            "module.exports='node-isolated';\n", encoding="utf-8"
        )
        tarball = root / "probe.tgz"
        with tarfile.open(tarball, "w:gz") as archive:
            archive.add(package, arcname="package")
        node = state.info["nodejs"]["executable"]
        await execute(
            "npm",
            "install",
            tarball,
            "--prefix",
            state.info["nodejs"]["install_cwd"],
            "--global=false",
            "--offline",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            cwd=state.info["nodejs"]["install_cwd"],
        )
        script = root / "task.mjs"
        written = await write_file(
            "task.mjs",
            "import probe from 'paw-dep-probe';\n"
            "import { writeFileSync } from 'node:fs';\n"
            "writeFileSync('report.txt', probe); console.log(probe); console.log(process.execPath);\n",
        )
        assert written.state == ToolResultState.SUCCESS, written.content
        text = await execute(node, script)
        assert "node-isolated" in text and node in text
        assert (root / "report.txt").read_text(encoding="utf-8") == "node-isolated"
        node_root = Path(state.info["nodejs"]["install_cwd"])
        assert not (node_root / "report.txt").exists()
        denied_cwd = await shell.execute_shell_command(f'node "{script}"', cwd=node_root)
        assert denied_cwd.state == ToolResultState.ERROR
        legacy_script = node_root / "legacy.mjs"
        legacy_script.write_text("// old runtime script", encoding="utf-8")
        no_mounts = current_request_context.set({"session_id": "offline", "is_coding_mode": False})
        try:
            denied_write = await write_file(str(legacy_script), "// task data")
        finally:
            current_request_context.reset(no_mounts)
        assert denied_write.state == ToolResultState.DENIED
        assert legacy_script.read_text(encoding="utf-8") == "// old runtime script"
        denied_escape = await write_file("../escape.txt", "task data")
        assert denied_escape.state == ToolResultState.DENIED
        commonjs = await write_file("task.cjs", "console.log(require('paw-dep-probe'));\n")
        assert commonjs.state == ToolResultState.SUCCESS
        assert "node-isolated" in await execute(node, root / "task.cjs")
        assert (Path(state.info["nodejs"]["dependency_dir"]) / "paw-dep-probe/index.js").is_file()
        assert not (bundle / "node/node_modules/paw-dep-probe").exists()
        assert "git version" in await execute(
            state.info["git"]["executable"], "--version"
        )
        assert "GNU bash" in await execute(state.info["git"]["bash"], "--version")

        if not isolated:
            workspace = tmp_path / "workspace"
            next_session = {**request, "sandbox_mounts": [], "session_id": "next"}
            next_state = br.prepare_request_runtimes(
                next_session, workspace, "next", BundledRuntimesConfig(enabled=True),
            )
            next_token = br.current_runtimes.set(next_state)
            next_request_token = current_request_context.set(next_session)
            workspace_token = current_workspace_dir.set(workspace)
            try:
                next_state.root.mkdir(parents=True, exist_ok=True)
                for name in ("task.py", "task.mjs"):
                    shutil.copyfile(root / name, next_state.root / name)
                assert "python-isolated" in await execute("python", "task.py")
                assert "node-isolated" in await execute("node", "task.mjs")
                assert "node-isolated" in await execute(
                    "node", "-e", "console.log(require('paw-dep-probe'))",
                )
                assert (next_state.root / "report.txt").is_file()
            finally:
                current_workspace_dir.reset(workspace_token)
                current_request_context.reset(next_request_token)
                br.current_runtimes.reset(next_token)

        denied = await shell.execute_shell_command(
            f'"{sys.executable}" -m pip install --no-index --no-deps "{wheel}"',
        )
        assert denied.state == ToolResultState.ERROR
        assert denied.metadata["guard"] == "bundled_runtime_path"
        retried = await shell.execute_shell_command(
            denied.metadata["recommended_command"]
        )
        assert retried.state == ToolResultState.SUCCESS
        assert "Command failed" not in str(retried.content)

        # Verify actual PowerShell quoting too, with the same runtime selection.
        current_shell_command_executable.set("powershell.exe")
        text = await execute("python", "task.py")
        assert "python-isolated" in text
        text = await execute("node", script)
        assert "node-isolated" in text

        if not isolated:
            # Reuse the same base after shared installs, then verify that the
            # isolated request sees neither shared packages nor site hooks.
            marker = tmp_path / "shared-site-loaded"
            (python_env / "Lib/site-packages/paw_test_startup.pth").write_text(
                f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n",
                encoding="utf-8",
            )
            monkeypatch.setenv("QWENPAW_BUNDLED_RUNTIME_ISOLATION", "1")
            monkeypatch.delenv("QWENPAW_BUNDLED_RUNTIMES_DIR")
            private = br.prepare_request_runtimes(
                request, tmp_path / "workspace", "offline", BundledRuntimesConfig(enabled=True),
            )
            assert private.info["nodejs"]["executable"] == state.info["nodejs"]["executable"]
            assert private.info["git"]["executable"] == state.info["git"]["executable"]
            assert not marker.exists()
            (root / "check-private.cjs").write_text(
                "try { require.resolve('paw-dep-probe'); } catch (error) {\n"
                "  if (error.code === 'MODULE_NOT_FOUND') { console.log('node-private'); process.exit(0); }\n"
                "  throw error;\n"
                "}\n"
                "throw new Error('Shared package leaked into isolated environment');\n",
                encoding="utf-8",
            )
            private_token = br.current_runtimes.set(private)
            try:
                assert "python-private" in await execute(
                    "python", "-c",
                    "import importlib.util; assert importlib.util.find_spec('paw_dep_probe') is None; print('python-private')",
                )
                assert "node-private" in await execute("node", "check-private.cjs")
                assert not marker.exists()
            finally:
                br.current_runtimes.reset(private_token)
    finally:
        current_shell_command_executable.reset(shell_token)
        current_request_context.reset(request_token)
        br.current_runtimes.reset(token)
    assert inventory() == before
