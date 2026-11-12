"""Normalize literal runtime invocations against the current Chat selection.

This is an entry-point check, not a parser for arbitrary programs or scripts.
Arguments retain their original spelling; only known executable/CLI paths and
npm's project flags are edited. No command is executed while checking it.
"""

from __future__ import annotations

from dataclasses import dataclass
import ntpath
import os
from pathlib import Path
import re
import shlex
from typing import Iterator

from .bundled_runtime import RequestRuntimes, render_runtime_instructions


@dataclass(frozen=True)
class RuntimeCommandCheck:
    command: str
    runtime: str | None = None
    error: str = ""


@dataclass(frozen=True)
class _Token:
    value: str
    start: int
    end: int
    literal: bool = True


def _basename(value: str) -> str:
    return ntpath.basename(value.replace("\\", "/")).lower()


def _shell_kind(shell: str | None) -> str:
    name = _basename(shell or "cmd.exe")
    if name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return "powershell"
    return "cmd" if name in {"cmd", "cmd.exe"} else "posix"


def _tokens(command: str, shell: str) -> Iterator[_Token]:
    """Read literal token spans without treating Windows backslashes as escapes."""
    i = 0
    while i < len(command):
        if command[i].isspace():
            i += 1
            continue
        start, value, quote, literal = i, [], None, True
        while i < len(command):
            ch = command[i]
            if quote is None and ch.isspace():
                break
            if ch == '"' or (ch == "'" and shell != "cmd"):
                if quote is None:
                    quote = ch
                elif quote == ch:
                    if (
                        shell == "powershell"
                        and ch == "'"
                        and command[i : i + 2] == "''"
                    ):
                        value.append("'")
                        i += 2
                        continue
                    quote = None
                else:
                    value.append(ch)
            elif shell == "posix" and ch == "\\" and quote != "'":
                i += 1
                if i >= len(command):
                    raise ValueError("不完整的命令转义")
                value.append(command[i])
            elif shell == "powershell" and ch == "`" and quote != "'":
                literal = False
                i += 1
                if i >= len(command):
                    raise ValueError("不完整的 PowerShell 转义")
                value.append(command[i])
            else:
                if shell == "cmd" and ch in "%!^":
                    literal = False
                elif shell != "cmd" and quote != "'" and ch in "$`(){}":
                    literal = False
                value.append(ch)
            i += 1
        if quote is not None:
            raise ValueError("命令引号未闭合")
        yield _Token("".join(value), start, i, literal)


def _quote(value: str, shell: str) -> str:
    if shell == "powershell":
        return "'" + value.replace("'", "''") + "'"
    if shell == "posix":
        return shlex.quote(value.replace("\\", "/"))
    return '"' + value + '"'


def _invocation(paths: list[str], shell: str) -> str:
    return ("& " if shell == "powershell" else "") + " ".join(
        _quote(path, shell) for path in paths
    )


def _tool(value: str) -> tuple[str, str] | None:
    name = re.sub(r"\.(exe|cmd|bat|ps1)$", "", _basename(value))
    if re.fullmatch(r"pythonw?(?:\d+(?:\.\d+)?)?", name) or name == "py":
        return "python", "python"
    if re.fullmatch(r"pip(?:\d+(?:\.\d+)?)?", name):
        return "python", "pip"
    if name in {"node", "npm", "npx"}:
        return "nodejs", name
    if name in {"git", "bash"}:
        return "git", name
    return None


def _path(value: str, cwd: Path) -> Path:
    drive, tail = ntpath.splitdrive(value)
    if (drive and not tail.startswith(("\\", "/"))) or (
        not drive and value.startswith("\\")
    ):
        raise ValueError("请使用完整绝对路径，不使用盘符相对路径")
    path = Path(value)
    return (path if path.is_absolute() else cwd / path).resolve()


def _same_file(candidate: Path, expected: Path) -> bool:
    return (
        candidate.is_file()
        and expected.is_file()
        and os.path.normcase(str(candidate.resolve()))
        == os.path.normcase(str(expected.resolve()))
    )


def _is_bare(value: str) -> bool:
    return not any(ch in value for ch in "\\/:")


def _entry_error(
    token: _Token, expected: Path, cwd: Path, *, bare=True, anchor: Path | None = None
) -> str:
    if anchor is not None and not expected.resolve().is_relative_to(anchor.resolve()):
        return f"本轮运行时文件已指向对应目录之外：{expected}。请重新准备环境。"
    if not expected.is_file():
        return f"本轮运行时文件不存在：{expected}。请重新准备本轮环境。"
    if not token.literal:
        return "运行时路径包含变量或表达式，请使用本轮明确的绝对路径。"
    if bare and _is_bare(token.value):
        return ""
    try:
        if _same_file(_path(token.value, cwd), expected):
            return ""
    except (OSError, ValueError) as exc:
        return str(exc)
    return f"提交路径与本轮运行时不一致：{token.value}；应使用：{expected}"


def _wrapper_body(command: str, tokens: list[_Token]) -> tuple[str, str] | None:
    """Expose common literal wrappers for guidance, without rebuilding quoting."""
    name = _basename(tokens[0].value)
    name = name.removesuffix(".exe")
    marker = {
        "cmd": {"/c", "/k"},
        "powershell": {"-command", "-c"},
        "pwsh": {"-command", "-c"},
        "bash": {"-c", "-lc"},
    }.get(name)
    if marker is None:
        return None
    for index, token in enumerate(tokens[1:], 1):
        if token.value.lower() in marker:
            body = command[token.end :].strip()
            following = tokens[index + 1 :]
            if len(following) == 1 and following[0].end == len(command):
                body = following[0].value
            inner = (
                "cmd" if name == "cmd" else "posix" if name == "bash" else "powershell"
            )
            return body, inner
    return None


def _npm_flags(
    tokens: list[_Token],
    start: int,
    root: Path,
    cwd: Path,
    shell: str,
    edits: list,
    *,
    npx=False,
) -> tuple[list[str], str]:
    """Fix omitted defaults; explicit conflicting destinations require a retry."""
    errors, prefix_seen, global_seen = [], False, False
    i = start
    while i < len(tokens):
        token = tokens[i]
        lower = token.value.lower()
        if lower == "--":
            break
        if npx and not lower.startswith("-"):
            break
        if npx and lower in {
            "-p",
            "--package",
            "-c",
            "--call",
            "--registry",
            "--cache",
            "--userconfig",
        }:
            i += 2
            continue
        if lower == "--prefix" or token.value == "-C" or lower.startswith("--prefix="):
            prefix_seen = True
            end = token.end
            if "=" in token.value:
                value = token.value.split("=", 1)[1]
                literal = token.literal
            elif i + 1 < len(tokens) and not tokens[i + 1].value.startswith("-"):
                i += 1
                value, literal, end = tokens[i].value, tokens[i].literal, tokens[i].end
            else:
                value, literal = "", False
            try:
                valid = literal and bool(value) and _path(value, cwd) == root.resolve()
            except (OSError, ValueError):
                valid = False
            if not valid:
                errors.append(f"npm 安装目录必须是本轮工作目录：{root}")
            edits.append((token.start, end, "--prefix " + _quote(str(root), shell)))
        elif lower in {"-g", "--global"} or lower.startswith(("--global=", "-g=")):
            global_seen = True
            value = lower.split("=", 1)[1] if "=" in lower else "true"
            end = token.end
            if (
                "=" not in lower
                and i + 1 < len(tokens)
                and tokens[i + 1].value.lower() in {"true", "false"}
            ):
                i += 1
                value, end = tokens[i].value.lower(), tokens[i].end
            if value != "false":
                errors.append("当前只允许本轮项目安装，请使用 --global=false。")
            edits.append((token.start, end, "--global=false"))
        elif lower == "--location" or lower.startswith("--location="):
            value = lower.split("=", 1)[1] if "=" in lower else ""
            end = token.end
            if not value and i + 1 < len(tokens):
                i += 1
                value, end = tokens[i].value.lower(), tokens[i].end
            if value != "project":
                errors.append("npm --location 必须使用 project。")
            edits.append((token.start, end, "--location=project"))
        i += 1
    defaults = []
    if not prefix_seen:
        defaults.extend(("--prefix", _quote(str(root), shell)))
    if not global_seen:
        defaults.append("--global=false")
    return errors, " ".join(defaults)


def _node_script_index(tokens: list[_Token]) -> int | None:
    index = 1
    while index < len(tokens):
        arg = tokens[index].value
        if arg in {"-e", "--eval", "-p", "--print", "-c", "--check"} or arg.startswith(
            ("--eval=", "--print=")
        ):
            return None
        if arg == "--":
            return index + 1 if index + 1 < len(tokens) else None
        if arg in {
            "-r",
            "--require",
            "--import",
            "--loader",
            "--experimental-loader",
            "--conditions",
            "-C",
        }:
            index += 2
        elif arg.startswith("-"):
            index += 1
        else:
            return index
    return None


def check_runtime_command(
    command: str, cwd: Path, shell: str | None, prepared: RequestRuntimes, *, _depth=0
) -> RuntimeCommandCheck:
    """Caller gates this on Windows non-Coding forced subprocess + opt-in state."""
    kind = _shell_kind(shell)
    stream = _tokens(command, kind)
    try:
        head = next(stream)
        if head.value == "&" and kind == "powershell":
            head = next(stream)
        selected = _tool(head.value)
        name = _basename(head.value).removesuffix(".exe")
        wrapper = name in {"cmd", "powershell", "pwsh"} or (
            name == "bash" and "git" not in prepared.info
        )
        if not wrapper and (selected is None or selected[0] not in prepared.info):
            return RuntimeCommandCheck(command)
        tokens = [head, *stream]
    except (StopIteration, ValueError) as exc:
        # Do not inspect/deny unrelated commands just because an opt-in exists.
        match = re.match(r"\s*(?:&\s*)?['\"]?([^\s'\"]+)", command)
        selected = _tool(match[1]) if match else None
        if selected and selected[0] in prepared.info:
            return RuntimeCommandCheck("", selected[0], str(exc) or "命令为空")
        return RuntimeCommandCheck(command)

    wrapped = _wrapper_body(command, tokens) if wrapper else None
    if wrapped is not None:
        if _depth >= 3:
            return RuntimeCommandCheck(
                command,
                next(iter(prepared.info)),
                "Shell 包装层数过多，请直接调用本轮运行时。",
            )
        body, inner = wrapped
        inner_shell = {
            "cmd": "cmd.exe",
            "powershell": "powershell.exe",
            "posix": "bash.exe",
        }[inner]
        checked = check_runtime_command(
            body, cwd, inner_shell, prepared, _depth=_depth + 1
        )
        if checked.runtime is not None:
            # A model retry is safer than changing two nested quoting dialects.
            return RuntimeCommandCheck(
                "",
                checked.runtime,
                (checked.error + "\n" if checked.error else "")
                + "请去掉额外的 Shell 包装，按当前默认 Shell 的示例直接调用本轮运行时。",
            )
        return RuntimeCommandCheck(command)
    if selected is None or selected[0] not in prepared.info:
        return RuntimeCommandCheck(command)

    runtime, tool = selected
    info, errors, edits = prepared.info[runtime], [], []
    exe = Path(info["bash"] if tool == "bash" else info["executable"])
    anchor = (
        Path(info["work_root"])
        if runtime == "python"
        else (exe.parent.parent if runtime == "nodejs" else exe.parents[2])
    )
    expected = exe
    if tool == "pip":
        expected = exe.parent / _basename(head.value)
        if not expected.suffix:
            expected = expected.with_suffix(".exe")
    elif tool in {"npm", "npx"}:
        expected = exe.parent / _basename(head.value)
        if not expected.suffix:
            expected = expected.with_suffix(".cmd")
    # Bare aliases need only the canonical executable, not a matching shim file.
    error = _entry_error(
        head, exe if _is_bare(head.value) else expected, cwd, anchor=anchor
    )
    if error:
        errors.append(error)
    if runtime == "python":
        version = re.search(r"(?:pythonw?|pip)(\d+(?:\.\d+)?)", _basename(head.value))
        if version and not info["version"].startswith(version[1] + "."):
            errors.append(f"本轮 Python 版本为 {info['version']}，请使用对应解释器。")
        if _basename(head.value) in {"py", "py.exe"}:
            errors.append("请直接使用本轮 Python，不通过 py 启动器选择其他解释器。")
            if len(tokens) > 1 and re.fullmatch(
                r"-(?:\d+(?:\.\d+)?|V:.+)", tokens[1].value, re.IGNORECASE
            ):
                edits.append((tokens[1].start, tokens[1].end, ""))

    replacement = _invocation([str(exe)], kind)
    npm_index = None
    if tool == "pip":
        replacement += " -m pip"
    elif tool in {"npm", "npx"}:
        npm_index = 0
    elif tool == "node":
        script_index = _node_script_index(tokens)
        if script_index is not None and _basename(tokens[script_index].value) in {
            "npm-cli.js",
            "npx-cli.js",
        }:
            npm_index = script_index

    if npm_index is not None:
        cli_kind = (
            "npx"
            if (tool == "npx" or _basename(tokens[npm_index].value) == "npx-cli.js")
            else "npm"
        )
        cli = Path(info[cli_kind + "_cli"])
        if npm_index:
            error = _entry_error(tokens[npm_index], cli, cwd, bare=False, anchor=anchor)
            if error:
                errors.append(error)
        else:
            error = _entry_error(
                _Token(str(cli), 0, 0), cli, cwd, bare=False, anchor=anchor
            )
            if error:
                errors.append(error)
        flags_errors, defaults = _npm_flags(
            tokens,
            npm_index + 1,
            Path(info["work_root"]),
            cwd,
            kind,
            edits,
            npx=cli_kind == "npx",
        )
        errors.extend(flags_errors)
        replacement = _invocation([str(exe), str(cli)], kind)
        if npm_index:
            # Keep any Node options between the executable and CLI script.
            node_options = command[head.end : tokens[npm_index].start].strip()
            if node_options:
                replacement = (
                    _invocation([str(exe)], kind)
                    + " "
                    + node_options
                    + " "
                    + _quote(str(cli), kind)
                )
        if defaults:
            replacement += " " + defaults

    # Retain argument bytes/quoting, including Python -c and Node -e bodies.
    end = tokens[npm_index].end if npm_index is not None else head.end
    edits.append((0, end, replacement))
    rewritten = command
    for start, end, value in sorted(edits, reverse=True):
        rewritten = rewritten[:start] + value + rewritten[end:]
    return RuntimeCommandCheck(rewritten, runtime, "\n".join(dict.fromkeys(errors)))


def runtime_command_guidance(
    checked: RuntimeCommandCheck,
    cwd: Path,
    shell: str | None,
    prepared: RequestRuntimes,
) -> str:
    lines = [
        "本次命令未执行：内置运行时路径检查未通过。",
        checked.error,
        f"本次 cwd：{cwd}",
    ]
    if checked.command:
        lines.extend(("请修正后重新调用 execute_shell_command：", checked.command))
    else:
        selected = (
            {checked.runtime: prepared.info[checked.runtime]}
            if checked.runtime
            else prepared.info
        )
        lines.append(render_runtime_instructions(selected, shell))
    return "\n".join(lines)


def runtime_command_env(
    prepared: RequestRuntimes, runtime: str, base: dict[str, str]
) -> dict[str, str]:
    """Apply only the invoked runtime's config; do not replace the Shell PATH."""

    def belongs(key: str) -> bool:
        key = key.upper()
        if runtime == "python":
            return key.startswith(("PYTHON", "PIP_")) or key in {
                "VIRTUAL_ENV",
                "CONDA_PREFIX",
            }
        if runtime == "nodejs":
            return key.startswith(("NODE_", "NPM_CONFIG_"))
        return key == "GIT_EXEC_PATH"

    updates = {
        key: value for key, value in prepared.environment.items() if belongs(key)
    }
    removed = {key.upper() for key in prepared.remove_keys if belongs(key)} | {
        key.upper() for key in updates
    }
    return {
        **{key: value for key, value in base.items() if key.upper() not in removed},
        **updates,
    }
