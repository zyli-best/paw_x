"""Coverage for build-time runtime discovery and optional prompt context."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from qwenpaw.utils import terminal_runtime as runtime


@pytest.fixture(autouse=True)
def isolated_runtime_env(monkeypatch, tmp_path):
    for key in (
        "PATH", "PYTHONHOME", "PYTHON_HOME", "NODE_HOME", "NVM_SYMLINK",
        "VIRTUAL_ENV", "CONDA_PREFIX", "NODE_OPTIONS",
    ):
        monkeypatch.delenv(key, raising=False)
    prefix = str(tmp_path / "app-python")
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(
        prefix=prefix, base_prefix=prefix, frozen=False,
    ))


def make_program(directory, kind):
    name = (
        ("python.exe" if kind == "python" else "node.exe")
        if os.name == "nt"
        else ("python3" if kind == "python" else "node")
    )
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / name
    executable.touch()
    executable.chmod(0o755)
    return executable


def runtime_info(path, runtime_kind, **overrides):
    info = {"kind": runtime_kind, "executable": str(path)}
    if runtime_kind == "python":
        info.update(prefix=str(path.parent), virtual=False)
    info.update(overrides)
    return info


def install_probe(monkeypatch, responses):
    def run(command, **kwargs):
        info = responses[command[0]]
        return subprocess.CompletedProcess(command, 0, json.dumps(info), "")

    probe = Mock(side_effect=run)
    monkeypatch.setattr(runtime.subprocess, "run", probe)
    return probe


@pytest.mark.parametrize("source", ["PATH", "home", "alternate"])
def test_confirmed_runtimes_from_path_and_environment(
    monkeypatch, tmp_path, source,
):
    python = make_program(tmp_path / "python", "python")
    node = make_program(tmp_path / "node" / "bin", "nodejs")
    if source == "PATH":
        monkeypatch.setenv("PATH", os.pathsep.join((
            str(python.parent), str(node.parent), str(python.parent),
        )))
    else:
        monkeypatch.setenv(
            "PYTHONHOME" if source == "home" else "PYTHON_HOME",
            f'"{python.parent}"',
        )
        monkeypatch.setenv(
            "NODE_HOME" if source == "home" else "NVM_SYMLINK",
            str(node.parent.parent),
        )
    probe = install_probe(monkeypatch, {
        str(python): runtime_info(python, "python"),
        str(node): runtime_info(node, "nodejs"),
    })

    detected = runtime.detect_runtime_environments()

    assert detected == {
        kind: {"executable": str(path.resolve()),
               "directory": str(path.resolve().parent)}
        for kind, path in (("python", python), ("nodejs", node))
    }
    assert probe.call_count == 2
    assert probe.call_args_list[0].args[0][1:4] == ["-I", "-B", "-c"]
    assert probe.call_args_list[1].args[0][1] == "-p"
    assert "process.execPath" in probe.call_args_list[1].args[0][2]
    for call in probe.call_args_list:
        assert call.kwargs["shell"] is False
        assert 0 < call.kwargs["timeout"] <= 1


@pytest.mark.parametrize("virtual_source", [
    "VIRTUAL_ENV", "CONDA_PREFIX", "app-prefix", "pyvenv.cfg", "conda-meta",
])
def test_virtual_python_in_path_is_skipped_for_global_python(
    monkeypatch, tmp_path, virtual_source,
):
    virtual_root = tmp_path / "virtual"
    virtual = make_program(virtual_root / "Scripts", "python")
    python = make_program(tmp_path / "global", "python")
    monkeypatch.setenv(
        "PATH", os.pathsep.join((str(virtual.parent), str(python.parent))),
    )
    if virtual_source in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        monkeypatch.setenv(virtual_source, str(virtual_root))
    elif virtual_source == "app-prefix":
        runtime.sys.prefix = str(virtual_root)
    elif virtual_source == "pyvenv.cfg":
        (virtual_root / "pyvenv.cfg").touch()
    else:
        (virtual_root / "conda-meta").mkdir()
    probe = install_probe(monkeypatch, {
        str(python): runtime_info(python, "python"),
    })

    assert runtime.detect_runtime_environments()["python"]["executable"] == (
        str(python.resolve())
    )
    assert probe.call_count == 1
    assert probe.call_args.args[0][0] == str(python)


def test_virtual_symlink_is_checked_before_resolving(monkeypatch, tmp_path):
    virtual = make_program(tmp_path / "venv" / "bin", "python")
    (virtual.parent.parent / "pyvenv.cfg").touch()
    python = make_program(tmp_path / "global", "python")
    original_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self: (
        python if self == virtual else original_resolve(self)
    ))

    assert runtime._is_virtual_python(virtual, []) is True
    assert runtime._is_virtual_python(python, []) is False


@pytest.mark.parametrize("overrides", [
    {"virtual": True}, {"virtual": None}, {"virtual": 0},
    {"executable": []}, {"executable": "relative/python"},
    {"prefix": None}, {"kind": "launcher"},
])
def test_unconfirmed_python_is_omitted(monkeypatch, tmp_path, overrides):
    python = make_program(tmp_path / "python", "python")
    monkeypatch.setenv("PATH", str(python.parent))
    install_probe(monkeypatch, {
        str(python): runtime_info(python, "python", **overrides),
    })
    assert runtime.detect_runtime_environments() == {}


@pytest.mark.parametrize("field", ["prefix", "executable"])
def test_python_reported_virtual_location_is_omitted(
    monkeypatch, tmp_path, field,
):
    python = make_program(tmp_path / "python", "python")
    virtual = make_program(tmp_path / "virtual" / "bin", "python")
    (virtual.parent.parent / "conda-meta").mkdir()
    monkeypatch.setenv("PATH", str(python.parent))
    reported = virtual.parent if field == "prefix" else virtual
    install_probe(monkeypatch, {
        str(python): runtime_info(python, "python", **{field: str(reported)}),
    })
    assert runtime.detect_runtime_environments() == {}


@pytest.mark.skipif(os.name != "nt", reason="Windows Store alias check")
def test_windowsapps_alias_is_never_probed(monkeypatch, tmp_path):
    alias = make_program(tmp_path / "WindowsApps", "python")
    monkeypatch.setenv("PATH", str(alias.parent))
    probe = install_probe(monkeypatch, {})
    assert runtime.detect_runtime_environments() == {}
    probe.assert_not_called()


def test_missing_env_and_frozen_app_do_not_imply_python(monkeypatch):
    runtime.sys.frozen = True
    runtime.sys.executable = "paw.exe"
    monkeypatch.setenv("PATH", ".")
    probe = install_probe(monkeypatch, {})
    assert runtime.detect_runtime_environments() == {}
    probe.assert_not_called()


def test_only_node_is_reported_when_no_global_python(monkeypatch, tmp_path):
    python = make_program(tmp_path / "virtual" / "bin", "python")
    (python.parent.parent / "pyvenv.cfg").touch()
    node = make_program(tmp_path / "node", "nodejs")
    monkeypatch.setenv(
        "PATH", os.pathsep.join((str(python.parent), str(node.parent))),
    )
    install_probe(monkeypatch, {str(node): runtime_info(node, "nodejs")})
    assert set(runtime.detect_runtime_environments()) == {"nodejs"}


@pytest.mark.parametrize("result", [
    subprocess.CompletedProcess([], 1, "", "failed"),
    subprocess.CompletedProcess([], 0, "bad json", ""),
    subprocess.CompletedProcess([], 0, "[]", ""),
    FileNotFoundError("gone"),
    subprocess.TimeoutExpired("probe", 1),
])
def test_failed_probe_returns_none(monkeypatch, result):
    probe = Mock(
        side_effect=result if isinstance(result, Exception) else None,
        return_value=result,
    )
    monkeypatch.setattr(runtime.subprocess, "run", probe)
    assert runtime._probe(["candidate"], timeout=1) is None


def test_node_options_are_removed_only_from_probe_copy(monkeypatch):
    monkeypatch.setenv("NODE_OPTIONS", "--require startup.js")
    probe = Mock(return_value=subprocess.CompletedProcess([], 0, "{}", ""))
    monkeypatch.setattr(runtime.subprocess, "run", probe)
    runtime._probe(["node", "-p", runtime._NODE_INFO], timeout=1)
    assert "NODE_OPTIONS" not in probe.call_args.kwargs["env"]
    assert os.environ["NODE_OPTIONS"] == "--require startup.js"


def test_python_probe_budget_does_not_prevent_node_probe(monkeypatch, tmp_path):
    first = make_program(tmp_path / "first", "python")
    second = make_program(tmp_path / "second", "python")
    node = make_program(tmp_path / "node", "nodejs")
    monkeypatch.setenv("PATH", os.pathsep.join(
        str(path.parent) for path in (first, second, node)
    ))
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=Mock(
        side_effect=[0, 0.1, 2.1, 10, 10.1],
    )))
    probe = Mock(side_effect=[None, runtime_info(node, "nodejs")])
    monkeypatch.setattr(runtime, "_probe", probe)

    assert set(runtime.detect_runtime_environments()) == {"nodejs"}
    assert [call.args[0][0] for call in probe.call_args_list] == [
        str(first), str(node),
    ]


@pytest.fixture
def env_renderer(monkeypatch):
    from qwenpaw.app.chats import utils

    monkeypatch.setattr(utils, "load_config", lambda: SimpleNamespace(
        user_timezone="UTC",
    ))
    return utils.build_env_context


def test_no_runtime_data_preserves_existing_context(env_renderer):
    original = env_renderer(add_hint=False, default_shell="cmd.exe")
    assert env_renderer(
        add_hint=False, default_shell="cmd.exe", runtime_environments={},
    ) == original
    assert "Python" not in original
    assert "Node.js" not in original


@pytest.mark.parametrize("kinds", [("python",), ("nodejs",), ("python", "nodejs")])
def test_context_contains_only_detected_runtimes(env_renderer, tmp_path, kinds):
    detected = {
        kind: {"executable": str(tmp_path / kind), "directory": str(tmp_path)}
        for kind in kinds
    }
    context = env_renderer(add_hint=False, runtime_environments=detected)
    for kind, label in (("python", "Python（非虚拟环境）"), ("nodejs", "Node.js")):
        assert (label in context) is (kind in kinds)
        if kind in kinds:
            assert f"{label} 可执行文件：{detected[kind]['executable']}" in context
            assert f"{label} 所在目录：{tmp_path}" in context


@pytest.fixture
def build_fixture(monkeypatch, tmp_path, env_renderer):
    from qwenpaw.agents import react_agent, skill_system
    from qwenpaw.config import config, context
    from qwenpaw.runtime import prompt_contributors
    from qwenpaw.runtime.builder import AgentBuilder

    agent_config = SimpleNamespace(
        name="test", language="zh", coding_mode=None,
        active_model=SimpleNamespace(provider_id="test", model="test"),
        running=SimpleNamespace(
            shell_command_executable="cmd.exe", max_iters=5,
            loop=SimpleNamespace(iteration=SimpleNamespace(max_iterations=5)),
        ),
    )
    monkeypatch.setattr(config, "load_agent_config", lambda _: agent_config)
    monkeypatch.setattr(skill_system, "ensure_skills_initialized", Mock())
    monkeypatch.setattr(skill_system, "resolve_effective_skills", Mock(
        return_value=[],
    ))
    monkeypatch.setattr(react_agent, "QwenPawAgent", lambda **kwargs: (
        SimpleNamespace(**kwargs)
    ))
    monkeypatch.setattr(
        prompt_contributors, "build_default_prompt_manager", lambda: (
            SimpleNamespace(build_sync=lambda ctx: ctx.extras["env_context"])
        ),
    )
    builder = AgentBuilder()
    for name, value in (
        ("_build_request_context", {}), ("_init_governor", None),
        ("_collect_coding_mode_tools", []), ("build_model", (object(), None)),
        ("_build_offloader", None), ("_build_scroll_components", None),
        ("_build_middlewares", []), ("_build_context_config", None),
    ):
        monkeypatch.setattr(builder, name, Mock(return_value=value))
    monkeypatch.setattr(builder, "_collect_driver_tools_and_prompts", AsyncMock(
        return_value=([], []),
    ))
    monkeypatch.setattr(builder, "build_toolkit", AsyncMock(
        return_value=SimpleNamespace(tool_groups=[SimpleNamespace(tools=[])]),
    ))
    ctx = SimpleNamespace(
        agent_id="test", workspace_dir=tmp_path, workspace=None,
        session_state={}, extras={},
    )
    token = context.current_request_context.set({})
    yield builder, ctx
    context.current_request_context.reset(token)


@pytest.mark.asyncio
async def test_build_detects_before_prompt_and_refreshes(monkeypatch, build_fixture):
    builder, ctx = build_fixture
    detected = {"python": {"executable": "C:/Python/python.exe",
                           "directory": "C:/Python"}}
    probe = Mock(side_effect=[detected, {}])
    monkeypatch.setattr(runtime, "detect_runtime_environments", probe)

    agent = await builder.build(ctx)
    assert "Python（非虚拟环境）" in agent.system_prompt
    assert "C:/Python/python.exe" in agent.system_prompt
    assert ctx.extras["runtime_environments"] == detected

    agent = await builder.build(ctx)
    assert "Python（非虚拟环境）" not in agent.system_prompt
    assert ctx.extras["runtime_environments"] == {}
    assert probe.call_count == 2


@pytest.mark.asyncio
async def test_build_probe_error_preserves_build(monkeypatch, build_fixture):
    builder, ctx = build_fixture
    ctx.extras["runtime_environments"] = {"python": {}}
    monkeypatch.setattr(runtime, "detect_runtime_environments", Mock(
        side_effect=OSError("unavailable environment"),
    ))
    agent = await builder.build(ctx)
    assert "Python（非虚拟环境）" not in agent.system_prompt
    assert "Node.js" not in agent.system_prompt
    assert ctx.extras["runtime_environments"] == {}


@pytest.mark.asyncio
async def test_explicit_prompt_override_preserves_original_behavior(
    monkeypatch, build_fixture,
):
    from qwenpaw.runtime.builder import AgentBuildOverride

    builder, ctx = build_fixture
    probe = Mock()
    monkeypatch.setattr(runtime, "detect_runtime_environments", probe)
    agent = await builder.build(ctx, AgentBuildOverride(system_prompt="custom"))
    assert agent.system_prompt == "custom"
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_managed_python_replaces_only_python_and_disable_restores_host(monkeypatch, build_fixture):
    import qwenpaw.config as config_module
    from qwenpaw.config.config import Config, BundledRuntimesConfig
    from qwenpaw.utils import bundled_runtime as br

    builder, ctx = build_fixture
    root = Path(ctx.workspace_dir)
    info = {"python": {"source": "bundled_venv", "version": "3.11.18",
                       "executable": str(root / ".paw-env/python/Scripts/python.exe"),
                       "directory": str(root / ".paw-env/python/Scripts"), "work_root": str(root)}}
    state = br.RequestRuntimes(root, info, (), {}, frozenset())
    cfg = Config(bundled_runtimes=BundledRuntimesConfig(enabled=True, nodejs=False, git=False))
    monkeypatch.setattr(config_module, "load_config", lambda: cfg)
    monkeypatch.setattr(br, "supported_request", lambda request: True)
    prepare = Mock(return_value=state)
    monkeypatch.setattr(br, "prepare_request_runtimes", prepare)
    local_node = {"nodejs": {"executable": "C:/system/node.exe", "directory": "C:/system"}}
    detect = Mock(side_effect=[local_node, {"python": {"executable": "C:/system/python.exe"}}])
    monkeypatch.setattr(runtime, "detect_runtime_environments", detect)
    token = br.current_runtimes.set(None)
    try:
        agent = await builder.build(ctx)
        assert "Python（托管虚拟环境）" in agent.system_prompt
        assert "pandas" in agent.system_prompt
        assert "C:/system/node.exe" in agent.system_prompt
        assert "axios" not in agent.system_prompt
        assert "Python（非虚拟环境）" not in agent.system_prompt
        detect.assert_called_once_with(kinds=("nodejs",))
        assert br.current_runtimes.get() is state

        cfg.bundled_runtimes.enabled = False
        agent = await builder.build(ctx)
        assert "C:/system/python.exe" in agent.system_prompt
        assert "Python（托管虚拟环境）" not in agent.system_prompt
        assert br.current_runtimes.get() is None
        assert ctx.extras["bundled_runtimes"] is None
        prepare.assert_called_once()
    finally:
        br.current_runtimes.reset(token)


@pytest.mark.asyncio
async def test_enabled_runtime_is_prepared_even_for_custom_prompt(monkeypatch, build_fixture):
    import qwenpaw.config as config_module
    from qwenpaw.config.config import Config, BundledRuntimesConfig
    from qwenpaw.runtime.builder import AgentBuildOverride
    from qwenpaw.utils import bundled_runtime as br

    builder, ctx = build_fixture
    root = Path(ctx.workspace_dir)
    info = {"git": {"source": "bundled", "executable": "C:/Paw/git.exe", "bash": "C:/Paw/bash.exe", "work_root": str(root)}}
    state = br.RequestRuntimes(root, info, (), {}, frozenset())
    monkeypatch.setattr(config_module, "load_config", lambda: Config(bundled_runtimes=BundledRuntimesConfig(enabled=True, python=False, nodejs=False)))
    monkeypatch.setattr(br, "supported_request", lambda request: True)
    monkeypatch.setattr(br, "prepare_request_runtimes", Mock(return_value=state))
    probe = Mock()
    monkeypatch.setattr(runtime, "detect_runtime_environments", probe)
    token = br.current_runtimes.set(None)
    try:
        agent = await builder.build(ctx, AgentBuildOverride(system_prompt="custom"))
        assert agent.system_prompt.startswith("custom")
        assert "C:/Paw/git.exe" in agent.system_prompt
        assert "status" in agent.system_prompt
        probe.assert_not_called()
    finally:
        br.current_runtimes.reset(token)
