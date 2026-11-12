"""Opt-in Windows runtime entry-point normalization, without executing commands."""

from dataclasses import replace
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from agentscope.message import ToolResultState
from qwenpaw.config.context import (
    current_request_context,
    current_shell_command_executable,
)
from qwenpaw.utils.bundled_runtime import RequestRuntimes, current_runtimes
from qwenpaw.utils.bundled_runtime_command import (
    check_runtime_command,
    runtime_command_env,
    runtime_command_guidance,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows command paths")


@pytest.fixture
def state(tmp_path):
    root, bundle = tmp_path / "项目 空格", tmp_path / "portable tools"
    root.mkdir()
    paths = {
        "python": root / ".paw-env/python/Scripts/python.exe",
        "node": bundle / "node/node.exe",
        "npm_cli": bundle / "node/node_modules/npm/bin/npm-cli.js",
        "npx_cli": bundle / "node/node_modules/npm/bin/npx-cli.js",
        "git": bundle / "git/cmd/git.exe",
        "bash": bundle / "git/bin/bash.exe",
    }
    for path in [
        *paths.values(),
        paths["python"].parent / "pip.exe",
        paths["node"].parent / "npm.cmd",
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    info = {
        "python": {
            "executable": str(paths["python"]),
            "version": "3.11.18",
            "source": "bundled_venv",
        },
        "nodejs": {
            "executable": str(paths["node"]),
            "npm_cli": str(paths["npm_cli"]),
            "npx_cli": str(paths["npx_cli"]),
            "dependency_dir": str(root / "node_modules"),
            "npm_global": str(root / ".paw-env/npm-global"),
            "source": "bundled",
        },
        "git": {
            "executable": str(paths["git"]),
            "bash": str(paths["bash"]),
            "source": "bundled",
        },
    }
    for item in info.values():
        item.update(directory=str(Path(item["executable"]).parent), work_root=str(root))
    return RequestRuntimes(
        root,
        info,
        (),
        {
            "VIRTUAL_ENV": str(root / ".paw-env/python"),
            "PIP_CONFIG_FILE": "nul",
            "NPM_CONFIG_PREFIX": str(root / ".paw-env/npm-global"),
            "NPM_CONFIG_GLOBAL": "false",
        },
        frozenset({"PYTHONHOME", "PYTHONPATH", "NODE_OPTIONS", "GIT_EXEC_PATH"}),
    )


def check(state, command, shell="cmd.exe", cwd=None):
    return check_runtime_command(command, cwd or state.root, shell, state)


@pytest.mark.parametrize(
    "alias", ["python", "python.exe", "python3", "python3.11", "pip", "pip3", "pip3.11"]
)
def test_python_aliases_use_selected_venv_and_preserve_arguments(state, alias):
    tail = " -c \"print('python stays an argument')\""
    result = check(state, alias + tail)
    assert not result.error
    assert result.command.startswith('"' + state.info["python"]["executable"] + '"')
    assert result.command.endswith(tail)
    assert (" -m pip" in result.command) == alias.startswith("pip")


@pytest.mark.parametrize(
    "alias,key", [("node", "executable"), ("npm", "npm_cli"), ("npx", "npx_cli")]
)
def test_node_aliases_pin_both_node_and_cli(state, alias, key):
    result = check(state, alias + " --version")
    assert not result.error
    assert state.info["nodejs"]["executable"] in result.command
    assert state.info["nodejs"][key] in result.command
    if alias != "node":
        assert '--prefix "' + str(state.root) + '"' in result.command
        assert "--global=false" in result.command


@pytest.mark.parametrize("alias", ["git", "git.exe", "bash"])
def test_git_aliases_are_absolute(state, alias):
    result = check(state, alias + " --version")
    assert not result.error
    assert (
        state.info["git"]["bash" if alias == "bash" else "executable"] in result.command
    )


def test_explicit_relative_path_resolves_against_tool_cwd(state):
    relative = r".\.paw-env\python\Scripts\python.exe task.py"
    assert not check(state, relative).error
    other = state.root / "child"
    other.mkdir()
    assert not check(
        state, r"..\.paw-env\python\Scripts\python.exe task.py", cwd=other
    ).error
    assert check(state, relative, cwd=other).error


def test_absolute_path_comparison_is_windows_normalized_and_checks_existence(state):
    path = state.info["python"]["executable"]
    result = check(state, '"' + path.upper().replace("\\", "/") + '" task.py')
    assert not result.error
    Path(path).unlink()
    result = check(state, "python task.py")
    assert "不存在" in result.error


@pytest.mark.parametrize(
    "other",
    [
        r"C:\Python312\python.exe",
        r".\.venv\Scripts\python.exe",
        r"C:\Paw\runtimes\python\python.exe",
        r"C:python.exe",
        r"%OTHER%\python.exe",
    ],
)
def test_explicit_other_python_is_guided_and_never_silently_run(state, other):
    result = check(state, '"' + other + '" task.py')
    assert result.error
    assert state.info["python"]["executable"] in result.command
    assert result.command.endswith(" task.py")
    assert "本次命令未执行" in runtime_command_guidance(
        result, state.root, "cmd.exe", state
    )


def test_existing_other_venv_is_not_accepted_just_because_it_exists(state):
    path = state.root / ".venv/Scripts/python.exe"
    path.parent.mkdir(parents=True)
    path.touch()
    assert check(state, f'"{path}" task.py').error


@pytest.mark.parametrize(
    "name,tail",
    [
        ("node.exe", "task.js"),
        ("npm.cmd", "install axios"),
        ("npx.cmd", "--version"),
        ("git.exe", "status"),
        ("bash.exe", "--version"),
    ],
)
def test_other_existing_node_and_git_entries_require_retry(state, name, tail):
    path = state.root / "other tools" / name
    path.parent.mkdir(exist_ok=True)
    path.touch()
    result = check(state, f'"{path}" {tail}')
    assert result.error and result.runtime in {"nodejs", "git"}
    assert not check(state, result.command).error


@pytest.mark.parametrize(
    "command", ["python3.12 task.py", "pip3.12 install pandas", "py -3.12 task.py"]
)
def test_conflicting_version_selection_requires_model_retry(state, command):
    result = check(state, command)
    assert result.error
    assert "-3.12" not in result.command


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh.exe"])
def test_powershell_invocation_operator_and_single_quote_escaping(state, shell):
    result = check(state, "python task.py", shell)
    assert result.command.startswith("& '")
    assert not check(state, result.command, shell).error
    assert not check(
        state, "& '" + state.info["python"]["executable"] + "' task.py", shell
    ).error


def test_node_cli_path_is_checked_alongside_node_executable(state):
    node = state.info["nodejs"]["executable"]
    result = check(state, f'"{node}" "C:\\old\\npm-cli.js" install axios')
    assert result.error
    assert state.info["nodejs"]["npm_cli"] in result.command
    assert not check(state, result.command).error


def test_node_options_are_preserved_before_checked_cli(state):
    result = check(
        state,
        'node --no-warnings --require "preload.js" "C:\\old\\npm-cli.js" install axios',
    )
    assert result.error
    assert '--no-warnings --require "preload.js"' in result.command
    assert not check(state, result.command).error
    assert not check(state, "node -e \"console.log('npm-cli.js')\"").error


@pytest.mark.parametrize(
    "flags",
    [
        "--prefix C:\\other",
        "--prefix=C:\\other",
        "-C C:\\other",
        "--global",
        "-g",
        "--location global",
        "--prefix",
    ],
)
def test_explicit_conflicting_npm_destination_is_guided_with_corrected_command(
    state, flags
):
    result = check(state, "npm install axios " + flags)
    assert result.error
    assert not check(state, result.command).error
    assert "--global=false" in result.command
    assert str(state.root) in result.command


def test_npm_correct_relative_prefix_is_normalized_and_script_arguments_are_not_inspected(
    state,
):
    result = check(
        state, "npm --prefix . --global false run report -- --prefix C:\\report"
    )
    assert not result.error
    assert result.command.endswith(r"run report -- --prefix C:\report")
    assert '--prefix "' + str(state.root) + '"' in result.command


def test_npx_options_with_values_do_not_hide_explicit_wrong_prefix(state):
    result = check(state, "npx --package foo --prefix C:\\other command")
    assert result.error
    assert not check(state, result.command).error


@pytest.mark.parametrize(
    "command",
    [
        'cmd /c "python task.py"',
        'powershell -NoProfile -Command "python task.py"',
        'cmd /c "powershell -Command python task.py"',
    ],
)
def test_shell_wrappers_return_direct_invocation_guidance(state, command):
    result = check(state, command)
    assert result.error
    assert result.runtime == "python"
    guidance = runtime_command_guidance(result, state.root, "cmd.exe", state)
    assert state.info["python"]["executable"] in guidance


@pytest.mark.parametrize(
    "command",
    [
        "echo python",
        'builder "node"',
        'cmd /c "builder --version"',
        'python "unterminated',
        "node --version",
        "npm install axios --prefix C:\\other",
        '"C:\\other\\node.exe" task.js',
    ],
)
def test_unselected_runtime_and_unrelated_commands_have_no_checks(
    state, monkeypatch, command
):
    state = replace(state, info={"git": state.info["git"]})
    monkeypatch.setattr(
        Path,
        "is_file",
        Mock(side_effect=AssertionError("must not inspect disabled tools")),
    )
    result = check(state, command)
    assert result.command == command and result.runtime is None and not result.error


@pytest.mark.parametrize("runtime", ["python", "nodejs", "git"])
def test_runtime_environment_only_applies_to_invoked_runtime_and_keeps_path(
    state, runtime
):
    base = {
        "Path": "host tools",
        "PYTHONHOME": "host python",
        "NODE_OPTIONS": "host node",
        "GIT_EXEC_PATH": "host git",
    }
    before = dict(base)
    env = runtime_command_env(state, runtime, base)
    assert base == before
    assert env["Path"] == base["Path"] and "PATH" not in env
    assert ("VIRTUAL_ENV" in env) == (runtime == "python")
    assert ("NPM_CONFIG_PREFIX" in env) == (runtime == "nodejs")
    assert ("PYTHONHOME" in env) == (runtime != "python")
    assert ("NODE_OPTIONS" in env) == (runtime != "nodejs")
    assert ("GIT_EXEC_PATH" in env) == (runtime != "git")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "coding,direct,enabled",
    [
        (False, True, True),
        (True, True, True),
        (False, False, True),
        (False, True, False),
    ],
)
async def test_shell_checks_only_enabled_windows_non_coding_direct_branch(
    state, monkeypatch, coding, direct, enabled
):
    from qwenpaw.agents.tools import shell

    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", str(int(direct)))
    monkeypatch.setenv("QWENPAW_WINDOWS_DIRECT_SHELL_GUARD", "off")
    monkeypatch.setenv("QWENPAW_WINDOWS_DIRECT_SHELL_LLM_GUARD", "off")
    execute = Mock(return_value=(0, "ok", ""))
    monkeypatch.setattr(shell, "_execute_subprocess_sync", execute)
    monkeypatch.setattr(shell, "_strict_windows_shell_sandbox_required", lambda: False)
    request_token = current_request_context.set({"is_coding_mode": coding})
    shell_token = current_shell_command_executable.set("cmd.exe")
    token = current_runtimes.set(state if enabled else None)
    try:
        result = await shell.execute_shell_command(
            r"C:\other\python.exe task.py", cwd=state.root
        )
        if enabled and not coding and direct:
            assert result.state == ToolResultState.ERROR
            assert result.metadata["guard"] == "bundled_runtime_path"
            execute.assert_not_called()
        else:
            execute.assert_called_once()
            assert execute.call_args.args[0] == r"C:\other\python.exe task.py"
    finally:
        current_runtimes.reset(token)
        current_shell_command_executable.reset(shell_token)
        current_request_context.reset(request_token)


@pytest.mark.asyncio
async def test_llm_review_precedes_runtime_guidance_and_retry_executes(
    state, monkeypatch
):
    from qwenpaw.agents.tools import shell

    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", "1")
    monkeypatch.setenv("QWENPAW_WINDOWS_DIRECT_SHELL_GUARD", "off")
    monkeypatch.setenv("QWENPAW_WINDOWS_DIRECT_SHELL_LLM_GUARD", "block")
    semantic = AsyncMock(return_value=None)
    execute = Mock(return_value=(0, "ok", ""))
    monkeypatch.setattr(
        shell, "_llm_guard_windows_forced_direct_shell_command", semantic
    )
    monkeypatch.setattr(shell, "_execute_subprocess_sync", execute)
    token = current_runtimes.set(state)
    request_token = current_request_context.set({"is_coding_mode": False})
    shell_token = current_shell_command_executable.set("cmd.exe")
    before = dict(os.environ)
    try:
        denied = await shell.execute_shell_command(
            r"C:\other\python.exe task.py", cwd=state.root
        )
        semantic.assert_awaited_once()
        assert semantic.call_args.kwargs["cmd"] == r"C:\other\python.exe task.py"
        execute.assert_not_called()
        escaped = await shell.execute_shell_command(
            r"\"C:\other\python.exe\" task.py",
            cwd=state.root,
        )
        assert escaped.state == ToolResultState.ERROR
        assert escaped.metadata["guard"] == "bundled_runtime_path"
        assert semantic.await_count == 2
        execute.assert_not_called()
        result = await shell.execute_shell_command(
            denied.metadata["recommended_command"], cwd=state.root
        )
        assert result.state == ToolResultState.SUCCESS
        assert semantic.await_count == 3
        execute.assert_called_once()
        assert semantic.call_args.kwargs["cmd"] == execute.call_args.args[0]
        assert (
            execute.call_args.args[3]["VIRTUAL_ENV"] == state.environment["VIRTUAL_ENV"]
        )
        assert (
            "NPM_CONFIG_PREFIX" not in execute.call_args.args[3]
            or "NPM_CONFIG_PREFIX" in before
        )
        assert dict(os.environ) == before
    finally:
        current_shell_command_executable.reset(shell_token)
        current_request_context.reset(request_token)
        current_runtimes.reset(token)


@pytest.mark.asyncio
async def test_llm_block_skips_runtime_parsing_and_subprocess(state, monkeypatch):
    from qwenpaw.agents.tools import shell
    from qwenpaw.utils import bundled_runtime_command

    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", "1")
    monkeypatch.setenv("QWENPAW_WINDOWS_DIRECT_SHELL_GUARD", "off")
    monkeypatch.setenv("QWENPAW_WINDOWS_DIRECT_SHELL_LLM_GUARD", "block")
    decision = shell._WindowsDirectShellLlmGuardDecision(
        verdict="split_instruction",
        category="mixed_instruction",
        confidence=1.0,
        reason="Multiple independent operations in inline code",
    )
    semantic = AsyncMock(return_value=decision)
    parse = Mock(side_effect=AssertionError("LLM rejected this command"))
    execute = Mock()
    monkeypatch.setattr(
        shell, "_llm_guard_windows_forced_direct_shell_command", semantic
    )
    monkeypatch.setattr(bundled_runtime_command, "check_runtime_command", parse)
    monkeypatch.setattr(shell, "_execute_subprocess_sync", execute)
    token = current_runtimes.set(state)
    request_token = current_request_context.set({"is_coding_mode": False})
    shell_token = current_shell_command_executable.set("cmd.exe")
    command = "python -c \"print('python'); print('node')\""
    try:
        result = await shell.execute_shell_command(command, cwd=state.root)
        semantic.assert_awaited_once()
        assert semantic.call_args.kwargs["cmd"] == command
        assert result.metadata["guard"] == "windows_direct_shell_semantic_routing"
        assert result.state == ToolResultState.ERROR
        parse.assert_not_called()
        execute.assert_not_called()
    finally:
        current_shell_command_executable.reset(shell_token)
        current_request_context.reset(request_token)
        current_runtimes.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["block", "warn", "off"])
async def test_guard_order_and_llm_modes_preserve_inline_code(state, monkeypatch, mode):
    from qwenpaw.agents.tools import shell
    from qwenpaw.utils import bundled_runtime_command

    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", "1")
    monkeypatch.setenv("QWENPAW_WINDOWS_DIRECT_SHELL_GUARD", "block")
    monkeypatch.setenv("QWENPAW_WINDOWS_DIRECT_SHELL_LLM_GUARD", mode)
    events = []
    command = "python -c \"print('python node npm')\""

    def route(**kwargs):
        events.append("route")
        assert kwargs["cmd"] == command

    async def semantic(**kwargs):
        events.append("llm")
        assert kwargs["cmd"] == command
        if mode == "warn":
            return shell._WindowsDirectShellLlmGuardDecision(
                verdict="uncertain_block",
                category="uncertain",
                confidence=0.5,
                reason="warning policy continues to the runtime check",
            )

    def parse(*args):
        events.append("runtime")
        assert args[0] == command
        return check_runtime_command(*args)

    def execute(actual, *args):
        events.append("execute")
        assert (
            actual
            == f'"{state.info["python"]["executable"]}"' + command[len("python") :]
        )
        return 0, "ok", ""

    monkeypatch.setattr(shell, "_guard_windows_forced_direct_shell_command", route)
    monkeypatch.setattr(
        shell, "_llm_guard_windows_forced_direct_shell_command", semantic
    )
    monkeypatch.setattr(bundled_runtime_command, "check_runtime_command", parse)
    monkeypatch.setattr(shell, "_execute_subprocess_sync", execute)
    token = current_runtimes.set(state)
    request_token = current_request_context.set({"is_coding_mode": False})
    shell_token = current_shell_command_executable.set("cmd.exe")
    try:
        result = await shell.execute_shell_command(command, cwd=state.root)
        assert result.state == ToolResultState.SUCCESS
        assert events == (
            ["route", "runtime", "execute"]
            if mode == "off"
            else ["route", "llm", "runtime", "execute"]
        )
    finally:
        current_shell_command_executable.reset(shell_token)
        current_request_context.reset(request_token)
        current_runtimes.reset(token)
