"""Per-request opt-in environments, without real downloads or host activation."""

import asyncio
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qwenpaw.config.config import BundledRuntimesConfig, Config
from qwenpaw.utils import bundled_runtime as br, runtime_storage


@pytest.fixture
def bundle(monkeypatch, tmp_path):
    monkeypatch.setenv("QWENPAW_BUNDLED_RUNTIME_ISOLATION", "1")
    monkeypatch.setattr(runtime_storage, "runtime_home", lambda: tmp_path / ".devco")
    monkeypatch.setattr(br, "runtime_home", lambda: tmp_path / ".devco")
    root = tmp_path / "portable tools"
    root.mkdir()
    versions = {"python": "3.11.18", "nodejs": "22.23.4", "git": "2.56.0.windows.4"}
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "platform": "windows-x86_64",
                "runtimes": {
                    kind: {"version": value} for kind, value in versions.items()
                },
            }
        )
    )
    for entries in br._ENTRIES.values():
        for relative in entries.values():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
    wheel_dir = root / "python/Lib/ensurepip/_bundled"
    wheel_dir.mkdir(parents=True)
    for package in ("pip", "setuptools"):
        (wheel_dir / f"{package}-1.0-py3-none-any.whl").touch()
    monkeypatch.setenv("QWENPAW_BUNDLED_RUNTIMES_DIR", str(root))
    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", "1")
    monkeypatch.setattr(br, "sys", SimpleNamespace(platform="win32", frozen=False))
    created = []

    def run(args, env, *, read_json=False):
        assert "PYTHONHOME" not in {key.upper() for key in env}
        if "venv" in args:
            assert "--without-pip" in args
            directory = Path(args[-1])
            for name in ("Scripts/python.exe", "pyvenv.cfg"):
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            created.append(directory)
            return {}
        if "install" in args:
            assert {"--no-index", "--no-deps", "--no-compile"}.issubset(args)
            assert [Path(path).parent for path in args[-2:]] == [wheel_dir] * 2
            assert Path(args[0]) == created[-1] / "Scripts/python.exe"
            package = created[-1] / "Lib/site-packages/pip"
            package.mkdir(parents=True)
            (package / "__init__.py").touch()
            return {}
        assert read_json
        return {
            "base": str(root / "python"),
            "prefix": str(created[-1]),
            "version": "3.11.18",
        }

    runner = Mock(side_effect=run)
    monkeypatch.setattr(br, "_run", runner)
    token = br.current_runtimes.set(None)
    yield root, runner
    br.current_runtimes.reset(token)


def prepare(tmp_path, **flags):
    return br.prepare_request_runtimes(
        {"sandbox_mounts": [str(tmp_path / "mount")], "is_coding_mode": False},
        tmp_path / "workspace",
        "session",
        BundledRuntimesConfig(enabled=True, **flags),
    )


def test_defaults_preserve_existing_behavior():
    assert Config().bundled_runtimes == BundledRuntimesConfig(enabled=False)


def test_isolation_cli_is_forwarded_to_backend(monkeypatch):
    import argparse
    import ast
    import sys

    entry_path = Path(__file__).resolve().parents[1] / "main.py"
    entry = ast.parse(entry_path.read_text(encoding="utf-8"))
    entry.body = [
        node for node in entry.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"apply_devco_bundle_env", "parse_entry_cli_args"}
    ]
    namespace = {"argparse": argparse, "os": os, "sys": sys}
    exec(compile(entry, str(entry_path), "exec"), namespace)
    # Restore all environment values the launcher changes during parsing.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.delenv("QWENPAW_BUNDLED_RUNTIME_ISOLATION", raising=False)
    parse = namespace["parse_entry_cli_args"]
    assert parse([]) == []
    assert os.environ["QWENPAW_BUNDLED_RUNTIME_ISOLATION"] == "0"
    assert parse(["--bundled-runtime-isolation", "0", "--reload"]) == ["--reload"]
    assert os.environ["QWENPAW_BUNDLED_RUNTIME_ISOLATION"] == "0"
    assert parse(["--bundled-runtime-isolation", "1"]) == []
    assert os.environ["QWENPAW_BUNDLED_RUNTIME_ISOLATION"] == "1"
    with pytest.raises(SystemExit):
        parse(["--bundled-runtime-isolation", "invalid"])


def test_shared_preparation_uses_runtime_without_allocating_environment(bundle, monkeypatch, tmp_path):
    installed, runner = bundle
    root = tmp_path / ".devco/bin/runtime"
    shutil.copytree(installed, root)
    monkeypatch.delenv("QWENPAW_BUNDLED_RUNTIME_ISOLATION")

    state = prepare(tmp_path)
    assert state.shared_root == root
    assert state.storage_root is None
    assert state.info["python"]["executable"] == str(root / "python/python.exe")
    assert state.info["nodejs"]["install_cwd"] == str(root / "node/deps")
    assert state.info["git"]["executable"] == str(root / "git/cmd/git.exe")
    assert state.environment["PIP_REQUIRE_VIRTUALENV"] == "0"
    assert not (tmp_path / ".devco/e").exists()
    runner.assert_not_called()
    prompt = br.render_runtime_instructions(state.info, "powershell.exe")
    assert "会话共用" in prompt and "虚拟环境" not in prompt
    assert f"安装 Python 库示例（cwd={state.root}）" in prompt
    assert str(root / "node/deps") in prompt


@pytest.mark.parametrize(
    "flags",
    [
        {"enabled": False},
        {"enabled": True, "python": False, "nodejs": False, "git": False},
    ],
)
def test_off_has_no_inventory_or_filesystem_side_effects(monkeypatch, tmp_path, flags):
    status = Mock(side_effect=AssertionError("must not inspect bundled tools"))
    monkeypatch.setattr(br, "bundle_status", status)
    result = br.prepare_request_runtimes(
        {}, tmp_path / "absent", "s", BundledRuntimesConfig(**flags)
    )
    assert result is None
    assert not (tmp_path / "absent").exists()
    status.assert_not_called()


@pytest.mark.parametrize(
    "platform,coding,direct",
    [
        ("linux", False, "1"),
        ("win32", True, "1"),
        ("win32", False, "0"),
    ],
)
def test_unsupported_requests_do_not_prepare(
    bundle, monkeypatch, tmp_path, platform, coding, direct
):
    monkeypatch.setattr(br, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", direct)
    assert (
        br.prepare_request_runtimes(
            {"is_coding_mode": coding},
            tmp_path / "absent",
            "s",
            BundledRuntimesConfig(enabled=True),
        )
        is None
    )
    assert not (tmp_path / "absent").exists()
    bundle[1].assert_not_called()


@pytest.mark.parametrize(
    "python,nodejs,git",
    [
        (True, False, False),
        (False, True, False),
        (False, False, True),
        (True, True, False),
        (True, False, True),
        (False, True, True),
        (True, True, True),
    ],
)
def test_only_selected_tools_are_prepared(bundle, tmp_path, python, nodejs, git):
    state = prepare(tmp_path, python=python, nodejs=nodejs, git=git)
    root = state.storage_root
    assert state.root == tmp_path / "mount"
    assert set(state.info) == {
        kind
        for kind, flag in (("python", python), ("nodejs", nodejs), ("git", git))
        if flag
    }
    assert (root / "p").exists() is python
    assert (root / "n/node_modules").exists() is nodejs
    assert (root / "n/package.json").exists() is nodejs
    assert (root / "t").is_dir()
    assert all(not (root / path).exists() for path in ("g", "n/s", "c"))
    assert bundle[1].call_count == (3 if python else 0)


def test_existing_environment_is_reused_without_reinstall_or_state_write(
    bundle, tmp_path
):
    first = prepare(tmp_path)
    marker = first.storage_root / "state.json"
    initial = marker.stat().st_mtime_ns
    installed = first.storage_root / "p/Lib/site-packages/user_package.py"
    installed.touch()
    second = prepare(tmp_path)
    assert first.storage_root == second.storage_root
    assert first.info == second.info
    assert bundle[1].call_count == 3
    assert marker.stat().st_mtime_ns == initial
    assert installed.is_file()


def test_concurrent_initialization_of_one_project_creates_one_venv(bundle, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        states = [
            future.result()
            for future in [pool.submit(prepare, tmp_path) for _ in range(2)]
        ]
    assert states[0].storage_root == states[1].storage_root
    assert bundle[1].call_count == 3


def test_failed_python_seed_can_be_retried(bundle, tmp_path):
    _, runner = bundle
    run = runner.side_effect

    def fail_install(args, env, **kwargs):
        if "install" in args:
            raise br.BundledRuntimeError("wheel install failed")
        return run(args, env, **kwargs)

    runner.side_effect = fail_install
    with pytest.raises(br.BundledRuntimeError, match="wheel install failed"):
        prepare(tmp_path, nodejs=False, git=False)

    managed, = (tmp_path / ".devco/e").glob("m-*")
    assert not (managed / "p").exists()
    assert not (managed / "state.json").exists()
    runner.side_effect = run
    assert prepare(tmp_path, nodejs=False, git=False).info["python"]


def test_mount_changes_and_readonly_only_uses_session_scratch(bundle, tmp_path):
    roots = []
    environments = []
    for mount in (tmp_path / "project-a", tmp_path / "project-b", None):
        ctx = {
            "sandbox_mounts": [str(mount)] if mount else [],
            "sandbox_rx_mounts": [str(tmp_path / "references")],
        }
        state = br.prepare_request_runtimes(
            ctx,
            tmp_path / "workspace",
            "chat-1",
            BundledRuntimesConfig(enabled=True, python=False, git=False),
        )
        roots.append(state.root)
        environments.append(state.storage_root)
    assert roots == [
        tmp_path / "project-a",
        tmp_path / "project-b",
        tmp_path / "workspace/session_tmp/chat-1",
    ]
    assert len(set(environments)) == 3
    assert all((path / "n/node_modules").is_dir() for path in environments)
    assert not (tmp_path / "references").exists()


def test_snowid_survives_clock_rollback_and_reuses_full_owner(bundle, monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    mount = tmp_path / "mount"
    owner = runtime_storage.environment_owner(workspace, "chat:123", None)
    first, generation = runtime_storage.allocate_environment(owner, workspace)
    monkeypatch.setattr(runtime_storage, "time_ns", lambda: 0)
    other_owner = runtime_storage.environment_owner(workspace, "chat--123", None)
    other, _ = runtime_storage.allocate_environment(other_owner, workspace)
    shared, _ = runtime_storage.allocate_environment(
        runtime_storage.environment_owner(workspace, "chat:123", mount), mount,
    )
    assert int(first.name[2:]) < int(other.name[2:]) < int(shared.name[2:])
    assert runtime_storage.allocate_environment(owner, workspace) == (first, generation)


def test_retirement_waits_for_subprocess_and_preserves_scratch_and_mount(bundle, tmp_path):
    workspace = tmp_path / "workspace"
    scratch = workspace / "session_tmp/chat--123"
    scratch.mkdir(parents=True)
    (scratch / "result.txt").write_text("keep", encoding="utf-8")
    owner = runtime_storage.environment_owner(workspace, "chat:123", None)
    private, generation = runtime_storage.allocate_environment(owner, scratch)
    shared = prepare(tmp_path, python=False, git=False).storage_root
    with runtime_storage.use_environment(private, generation):
        runtime_storage.retire_session_environment(workspace, "chat:123")
        assert private.exists()
    runtime_storage.reclaim_if_retired(private, generation)
    assert not private.exists()
    assert (scratch / "result.txt").read_text(encoding="utf-8") == "keep"
    assert shared.is_dir()
    rebuilt, _ = runtime_storage.allocate_environment(owner, scratch)
    assert rebuilt != private


@pytest.mark.asyncio
@pytest.mark.parametrize("mount_scratch", [False, True])
async def test_chat_delete_retires_only_after_last_reference(
    bundle, monkeypatch, tmp_path, mount_scratch,
):
    from qwenpaw.app.chats.manager import ChatManager
    from qwenpaw.app.chats.models import ChatSpec
    from qwenpaw.app.chats.repo.json_repo import JsonChatRepository

    workspace = tmp_path / "workspace"
    scratch = workspace / "session_tmp/chat--123"
    scratch.mkdir(parents=True)
    artifact = scratch / "result.txt"
    artifact.write_text("keep", encoding="utf-8")
    manager = ChatManager(repo=JsonChatRepository(str(workspace / "chats.json")), workspace_dir=workspace)
    for chat_id in ("first", "second"):
        await manager.create_chat(ChatSpec(id=chat_id, session_id="chat:123", user_id="u"))
    state = br.prepare_request_runtimes(
        {"sandbox_mounts": [str(scratch)] if mount_scratch else []}, workspace, "chat:123",
        BundledRuntimesConfig(enabled=True, python=True, nodejs=False, git=False),
    )
    environment = state.storage_root
    shutil.copytree(bundle[0], tmp_path / ".devco/bin/runtime")
    monkeypatch.setenv("QWENPAW_BUNDLED_RUNTIME_ISOLATION", "0")
    shared = br.prepare_request_runtimes(
        {}, workspace, "chat:123", BundledRuntimesConfig(enabled=True),
    )
    shared_package = Path(shared.info["nodejs"]["dependency_dir"]) / "shared-package"
    shared_package.mkdir()
    assert environment.name.startswith("s-")
    assert Path(state.info["python"]["executable"]).is_file()
    assert await manager.delete_chats(["first"])
    assert environment.exists()
    assert await manager.delete_chats(["second"])
    assert shared_package.is_dir()
    assert not environment.exists()
    assert artifact.read_text(encoding="utf-8") == "keep"


def test_existing_project_files_are_preserved(bundle, tmp_path):
    root = tmp_path / "mount"
    root.mkdir()
    package = root / "package.json"
    package.write_text('{"name":"user-project","dependencies":{"axios":"1.0.0"}}')
    original = package.read_bytes()
    (root / ".venv").mkdir()
    (root / ".venv/keep.txt").write_text("user environment")
    prepare(tmp_path)
    assert package.read_bytes() == original
    assert (root / ".venv/keep.txt").read_text() == "user environment"


def test_bad_owned_venv_reports_error_instead_of_falling_back(bundle, tmp_path):
    state = prepare(tmp_path, nodejs=False, git=False)
    (state.storage_root / "p/Lib/site-packages/pip/__init__.py").unlink()
    with pytest.raises(br.BundledRuntimeError, match="incomplete"):
        prepare(tmp_path, nodejs=False, git=False)
    assert bundle[1].call_count == 3


def test_failed_mount_does_not_silently_choose_scratch(bundle, monkeypatch, tmp_path):
    monkeypatch.setattr(
        br, "_prepare_python", Mock(side_effect=PermissionError("mount not writable"))
    )
    with pytest.raises(br.BundledRuntimeError, match="mount not writable"):
        prepare(tmp_path, nodejs=False, git=False)
    assert not (tmp_path / "workspace/session_tmp/session").exists()


def test_missing_runtime_never_falls_back_to_system(bundle, tmp_path):
    (bundle[0] / "node/node.exe").unlink()
    with pytest.raises(br.BundledRuntimeError, match="nodejs is unavailable"):
        prepare(tmp_path, python=False, git=False)
    assert not (tmp_path / "mount").exists()


def test_subprocess_environment_is_private_and_case_insensitive(bundle, tmp_path):
    state = prepare(tmp_path)
    host = {
        "Path": "system",
        "PYTHONHOME": "host-python",
        "PYTHONPATH": "host-libs",
        "NODE_OPTIONS": "--require unwanted",
        "npm_config_prefix": "host-global",
        "GIT_EXEC_PATH": "host-git",
        "PIP_TARGET": "host-packages",
    }
    original = dict(host)
    env = state.process_env(host)
    assert host == original
    assert "Path" not in env and env["PATH"].startswith(
        str(state.storage_root / "p/Scripts")
    )
    assert all(
        key not in env
        for key in (
            "PYTHONHOME",
            "PYTHONPATH",
            "NODE_OPTIONS",
            "PIP_TARGET",
            "GIT_EXEC_PATH",
            "npm_config_prefix",
        )
    )
    assert env["VIRTUAL_ENV"] == str(state.storage_root / "p")
    assert env["NPM_CONFIG_PREFIX"] == str(state.storage_root / "n")
    assert env["PIP_REQUIRE_VIRTUALENV"] == "1"


def test_disabled_tool_environment_and_examples_remain_untouched(bundle, tmp_path):
    state = prepare(tmp_path, python=False, nodejs=False)
    host = {
        "PATH": "host-path",
        "PYTHONHOME": "host-python",
        "NODE_OPTIONS": "host-node",
    }
    env = state.process_env(host)
    assert env["PYTHONHOME"] == "host-python" and env["NODE_OPTIONS"] == "host-node"
    prompt = br.render_runtime_instructions(state.info, "cmd.exe")
    assert "Git（内置）" in prompt and "Bash" in prompt
    assert "pandas" not in prompt and "axios" not in prompt


@pytest.mark.parametrize(
    "shell,prefix", [("cmd.exe", '"'), ("powershell.exe", "& '"), ("pwsh.exe", "& '")]
)
def test_prompt_examples_follow_runtime_paths_and_shell(
    bundle, tmp_path, shell, prefix
):
    state = prepare(tmp_path)
    prompt = br.render_runtime_instructions(state.info, shell)
    assert "Python（托管虚拟环境）" in prompt
    assert state.info["python"]["executable"] in prompt
    assert state.info["nodejs"]["npm_cli"] in prompt
    assert "--prefix" in prompt and str(state.root) in prompt
    assert "--global=false" in prompt and "-m" in prompt
    assert f"执行 Node 脚本示例（cwd={state.root}）" in prompt
    assert "本轮专用路径" not in prompt and "npm 全局工具目录" not in prompt
    assert f"`{prefix}" in prompt


@pytest.mark.parametrize("shared", [False, True], ids=["isolated", "shared"])
def test_shell_file_boundary_accepts_only_current_managed_node_paths(
    bundle, monkeypatch, tmp_path, shared,
):
    from qwenpaw.governance.policy import ToolCallSpec, load_governance_policy
    from qwenpaw.governance.resource_governor import ResourceGovernor

    if shared:
        shutil.copytree(bundle[0], tmp_path / ".devco/bin/runtime")
        monkeypatch.setenv("QWENPAW_BUNDLED_RUNTIME_ISOLATION", "0")
    state = prepare(tmp_path, python=False, git=False)
    node = state.info["nodejs"]
    governor = ResourceGovernor(str(tmp_path / "workspace"), governance_dir=str(tmp_path / "governance"))
    governor._policy = load_governance_policy(str(governor._policy_dir), str(governor.workspace_dir))
    command = f'"{node["executable"]}" "{node["npm_cli"]}" install axios --prefix "{node["install_cwd"]}" --global=false'
    token = br.current_runtimes.set(state)
    try:
        spec = ToolCallSpec(tool_name="Bash", target=command, agent_id="default", session_id="session")
        assert governor._check_shell_file_boundary(spec, cwd=node["install_cwd"]) is None
        forbidden = str(tmp_path / "unrelated" / "axios.tgz")
        spec.target += f' "{forbidden}"'
        assert governor._check_shell_file_boundary(spec, cwd=node["install_cwd"]).source == "shell_file_boundary"
    finally:
        br.current_runtimes.reset(token)


def test_selected_host_kinds_only(monkeypatch):
    from qwenpaw.utils import terminal_runtime

    probe = Mock()
    monkeypatch.setattr(terminal_runtime, "_probe", probe)
    assert terminal_runtime.detect_runtime_environments(kinds=()) == {}
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_requests_keep_separate_environment_snapshots(
    bundle, monkeypatch, tmp_path
):
    from qwenpaw.runtime.builder import AgentBuilder
    import qwenpaw.config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: Config(
            bundled_runtimes=BundledRuntimesConfig(enabled=True, python=False)
        ),
    )

    async def request(name):
        ctx = SimpleNamespace(workspace_dir=tmp_path / "workspace", extras={})
        state = await AgentBuilder._prepare_bundled_runtimes(
            ctx,
            {
                "session_id": name,
                "sandbox_mounts": [str(tmp_path / name)],
            },
        )
        await asyncio.sleep(0)
        assert br.current_runtimes.get() is state
        return state.root

    roots = await asyncio.gather(request("a"), request("b"))
    assert roots == [tmp_path / "a", tmp_path / "b"]
    assert br.current_runtimes.get() is None


def test_configuration_api_persists_without_preparing_projects(
    bundle, monkeypatch, tmp_path
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from qwenpaw.app.routers import config as routes
    from qwenpaw.config import utils

    path = tmp_path / "config.json"
    monkeypatch.setattr(utils, "_config_cache", None)
    monkeypatch.setattr(utils, "_config_mtime", None)
    monkeypatch.setattr(routes, "load_config", lambda: utils.load_config(path))
    monkeypatch.setattr(routes, "save_config", lambda cfg: utils.save_config(cfg, path))
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    client = TestClient(app)
    endpoint = "/api/config/bundled-runtimes"
    assert client.get(endpoint).json() == {
        "enabled": False,
        "python": True,
        "nodejs": True,
        "git": True,
    }
    value = {"enabled": True, "python": False, "nodejs": True, "git": False}
    assert client.put(endpoint, json=value).json() == value
    assert client.get(endpoint).json() == value
    assert json.loads(path.read_text(encoding="utf-8"))["bundled_runtimes"] == value
    assert client.put(endpoint, json={"enabled": False}).json() == {
        **value,
        "enabled": False,
    }
    assert client.put(endpoint, json={"enabled": True}).json() == value
    assert (
        client.get(endpoint + "/status").json()["runtimes"]["python"]["available"]
        is True
    )
    assert client.put(endpoint, json={**value, "enabled": "true"}).status_code == 422
    assert (
        client.put(endpoint, json={**value, "directory": "elsewhere"}).status_code
        == 422
    )
    bundle[1].assert_not_called()
    assert not (tmp_path / ".paw-env").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("python", [True, False])
async def test_shell_only_applies_invoked_runtime_config_without_changing_server_env(
    bundle, monkeypatch, tmp_path, python
):
    from qwenpaw.agents.tools import shell
    from qwenpaw.config.context import current_request_context

    state = prepare(tmp_path, python=python, git=False)
    calls = []

    def execute(command, cwd, timeout, env, executable, *, capture_dir=None):
        if python:
            assert capture_dir == env["TEMP"] == str(state.storage_root / "t")
        else:
            assert capture_dir is None
        calls.append((cwd, env))
        return 0, "selected-runtime", ""

    monkeypatch.setattr(shell, "_execute_subprocess_sync", execute)
    monkeypatch.setenv("QWENPAW_WINDOWS_DIRECT_SHELL_GUARD", "off")
    monkeypatch.setenv("QWENPAW_WINDOWS_DIRECT_SHELL_LLM_GUARD", "off")
    token = br.current_runtimes.set(state)
    request_token = current_request_context.set(
        {"sandbox_mounts": [str(state.root)], "is_coding_mode": False}
    )
    before = dict(os.environ)
    try:
        await shell.execute_shell_command("python --version")
    finally:
        br.current_runtimes.reset(token)
        current_request_context.reset(request_token)
    assert dict(os.environ) == before
    cwd, env = calls[0]
    assert Path(cwd) == state.root
    assert env.get("NPM_CONFIG_PREFIX") == before.get("NPM_CONFIG_PREFIX")
    if python:
        assert env["PIP_REQUIRE_VIRTUALENV"] == "1"
        assert "VIRTUAL_ENV" not in env
    else:
        assert env.get("VIRTUAL_ENV") == before.get("VIRTUAL_ENV")
    assert str(Path(shell.sys.executable).parent) in env["PATH"].split(os.pathsep)
