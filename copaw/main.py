# -*- coding: utf-8 -*-
"""Allow running CoPaw via ``python -m copaw``."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import time


def _configure_windows_utf8_stdio() -> None:
    if os.name != "nt":
        return
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


_configure_windows_utf8_stdio()

_INIT_ONLY_ENV = "QWENPAW_INIT_ONLY"

# ReAct 详细耗时日志：True 打印，False 关闭；环境变量和命令行可覆盖。
REACT_TIMING_ENABLED = False
os.environ.setdefault(
    "QWENPAW_REACT_TIMING_ENABLED", str(REACT_TIMING_ENABLED).lower(),
)


def _setup_entry_logging():
    from qwenpaw.constant import LOG_LEVEL_ENV
    from qwenpaw.utils.logging import (
        LOG_FILE_PATH,
        add_project_file_handler,
        setup_logger,
    )

    logger = setup_logger(os.environ.get(LOG_LEVEL_ENV, "info"))
    add_project_file_handler(LOG_FILE_PATH)
    return logger


def _application_root() -> Path:
    """应用根目录：源码运行时为 main.py 所在目录；PyInstaller 打包后勿用 __file__（会落在 _MEI 临时目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


_APP_ROOT = _application_root()


def _ensure_devcopaw_launcher() -> None:
    """Prepare the CLI alias before initialization or service startup."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    executable = Path(sys.executable)
    if executable.name.lower() != "paw.exe":
        return

    import hashlib
    import msvcrt
    import shutil
    import tempfile

    root = executable.absolute().parent
    source = root / "paw.exe"
    target = root / "devcopaw.exe"
    lock_file = None
    locked = False
    temporary = None

    def digest(path: Path) -> str:
        result = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                result.update(block)
        return result.hexdigest()

    try:
        # Keep the lock file: deleting it can let processes lock different files.
        lock_file = (root / ".devcopaw-launcher.lock").open("a+b")
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        deadline = time.monotonic() + 5.0
        while True:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for the CLI launcher lock")
                time.sleep(0.1)

        source_hash = digest(source)
        if target.exists():
            if not target.is_file():
                raise OSError(f"CLI launcher path is not a file: {target}")
            if digest(target) == source_hash:
                return

        fd, name = tempfile.mkstemp(
            prefix=".devcopaw-", suffix=".tmp", dir=str(root),
        )
        os.close(fd)
        temporary = Path(name)
        shutil.copyfile(source, temporary)
        if digest(temporary) != source_hash or digest(source) != source_hash:
            raise OSError("CLI launcher copy failed validation or paw.exe changed")
        # Never remove the previous alias first; a running EXE may be locked.
        os.replace(temporary, target)
        temporary = None
    except OSError as exc:
        print(
            f"[launcher] Cannot prepare devcopaw.exe: {exc}. "
            "Use paw.exe cli ... instead; close any old CLI process and retry.",
            file=sys.stderr,
            flush=True,
        )
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if lock_file is not None:
            try:
                if locked:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                lock_file.close()


# 单文件/打包后：进程「当前目录」默认仍是启动时的 cwd，但不少逻辑依赖「与 exe 同目录」；与可执行文件对齐便于路径一致
if getattr(sys, "frozen", False):
    try:
        os.chdir(_APP_ROOT)
    except OSError:
        pass

# 工作数据目录：应用根下 .devco_paw
COPAW_WORKING_DIR = os.path.join(_APP_ROOT, ".devco_paw")
os.environ["COPAW_WORKING_DIR"] = str(COPAW_WORKING_DIR)
_FIRST_RUN_READY_MARKER = ".first_run_initialized"
_REQUIRE_AGENT_MODEL_READY = True
_QA_AGENT_ID = "QwenPaw_QA_Agent_0.2"
_BUNDLED_AGENT_IDS = (
    "code_generator",
    "code_keeper",
    "firmware_planner",
    "personal_assistant",
    "svn_guard",
    "vuln_scan",
)


def _read_json_file(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _has_active_model(secret_dir: Path) -> bool:
    data = _read_json_file(secret_dir / "providers" / "active_model.json")
    if not data:
        return False
    provider_id = str(data.get("provider_id") or "").strip()
    model = str(data.get("model") or "").strip()
    return bool(provider_id and model)


def _expected_provider_and_model() -> tuple[str, str]:
    provider_id = str(
        os.environ.get("DEVCO_BUNDLE_PROVIDER_ID") or "lcfc",
    ).strip()
    raw_models = str(
        os.environ.get("OPENAI_MODEL")
        or os.environ.get("LLM_MODEL")
        or "gpt-oss-120b"
    )
    model = ""
    for part in raw_models.split(","):
        part = part.strip()
        if part:
            model = part
            break
    return provider_id, model


def _active_model_matches(path: Path, provider_id: str, model: str) -> bool:
    data = _read_json_file(path)
    if not data:
        return False
    active = data.get("active_model", data)
    if not isinstance(active, dict):
        return False
    return (
        str(active.get("provider_id") or "").strip() == provider_id
        and str(active.get("model") or "").strip() == model
    )


def _agent_active_model_matches(
    working_dir: Path,
    agent_id: str,
    provider_id: str,
    model: str,
) -> bool:
    return _active_model_matches(
        working_dir / "workspaces" / agent_id / "agent.json",
        provider_id,
        model,
    )


def _agent_models_ready(working_dir: Path) -> bool:
    if not _REQUIRE_AGENT_MODEL_READY:
        return True
    provider_id, model = _expected_provider_and_model()
    if not provider_id or not model:
        return False
    active_path = _active_model_path(working_dir)
    if not _active_model_matches(active_path, provider_id, model):
        return False
    agent_ids = (
        ("default",)
        if _init_default_only_enabled()
        else ("default", _QA_AGENT_ID) + _BUNDLED_AGENT_IDS
    )
    return all(
        _agent_active_model_matches(working_dir, agent_id, provider_id, model)
        for agent_id in agent_ids
    )


def _active_model_path(working_dir: Path) -> Path:
    secret_dir = Path(
        os.environ.get("QWENPAW_SECRET_DIR")
        or os.environ.get("COPAW_SECRET_DIR")
        or f"{working_dir}.secret",
    )
    return secret_dir / "providers" / "active_model.json"


def _has_agent_workspace(working_dir: Path, agent_id: str) -> bool:
    data = _read_json_file(working_dir / "workspaces" / agent_id / "agent.json")
    if not data:
        return False
    return bool(str(data.get("id") or "").strip())


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _init_default_only_enabled() -> bool:
    return _env_truthy("QWENPAW_INIT_DEFAULT_ONLY")


def _has_only_default_agent_profile(working_dir: Path) -> bool:
    data = _read_json_file(working_dir / "config.json")
    if not isinstance(data, dict):
        return False
    agents = data.get("agents")
    if not isinstance(agents, dict):
        return False
    profiles = agents.get("profiles")
    if not isinstance(profiles, dict):
        return False
    return set(profiles.keys()) == {"default"}


def _fast_first_run_ready(working_dir: Path) -> bool:
    marker = working_dir / _FIRST_RUN_READY_MARKER
    if not marker.is_file():
        return False

    required_files = [
        working_dir / "config.json",
        working_dir / "workspaces" / "default" / "agent.json",
    ]
    if not _init_default_only_enabled():
        required_files.append(
            working_dir / "workspaces" / _QA_AGENT_ID / "agent.json",
        )
    if not all(path.is_file() for path in required_files):
        return False
    # if (
    #     _init_default_only_enabled()
    #     and not _has_only_default_agent_profile(working_dir)
    # ):
    #     return False

    if getattr(sys, "frozen", False) and not _active_model_path(working_dir).is_file():
        return False
    # if not _agent_models_ready(working_dir):
    #     return False
    return True


def _mark_first_run_ready(working_dir: Path) -> None:
    try:
        (working_dir / _FIRST_RUN_READY_MARKER).touch()
    except OSError:
        pass


def _is_first_run_initialization_needed() -> bool:
    """Return True when the bundled first-run init must still be executed."""
    working_dir = Path(COPAW_WORKING_DIR)
    if _fast_first_run_ready(working_dir):
        return False

    config = _read_json_file(working_dir / "config.json")
    if not config:
        return True
    # if (
    #     _init_default_only_enabled()
    #     and not _has_only_default_agent_profile(working_dir)
    # ):
    #     return True

    ready_checks = [
        _has_agent_workspace(working_dir, "default"),
        _agent_models_ready(working_dir),
    ]
    # if not _init_default_only_enabled():
    #     ready_checks.append(_has_agent_workspace(working_dir, _QA_AGENT_ID))
    ready = all(ready_checks)
    if ready and getattr(sys, "frozen", False):
        ready = _has_active_model(_active_model_path(working_dir).parents[1])

    if ready:
        _mark_first_run_ready(working_dir)
    return not ready


def _run_first_run_initialization() -> None:
    from qwenpaw.cli.init_cmd import init_cmd

    # init_cmd is a Click command, so run it through Click's argv parser.
    init_cmd.main(["--defaults", "--accept-security"], standalone_mode=False)
    if not _is_first_run_initialization_needed():
        _mark_first_run_ready(Path(COPAW_WORKING_DIR))



def apply_devco_bundle_env(
    *,
    openai_base_url: str | None = None,
    openai_model: str | None = None,
    llm_model: str | None = None,
    openai_api_key: str | None = None,
    llm_api_key: str | None = None,
    devco_bundle_provider_id: str | None = None,
    devco_bundle_provider_name: str | None = None,
    devco_init_skip_seed: str | None = None,
    session_delta_langfuse_enabled: str | None = None,
    session_delta_langfuse_host: str | None = None,
    session_delta_langfuse_public_key: str | None = None,
    session_delta_langfuse_secret_key: str | None = None,
    qwenpaw_compact_historical_tool_context: str | None = None,
    qwenpaw_force_direct_subprocess_sync: str | None = None,
    bundled_runtime_isolation: str | None = None,
    qwenpaw_auto_approve_ask: str | None = None,
    shell_guard_disable_thinking: str | None = None,
    react_timing_enabled: str | None = None,
    final_files_enabled: str | None = None,
    edge_cloud_enabled: str | None = None,
    edge_cloud_trace_stream_enabled: str | None = None,
    edge_cloud_session_process_enabled: str | None = None,
    edge_cloud_agent_ids: str | None = None,
    edge_cloud_edge_base_url: str | None = None,
    edge_cloud_edge_api_key: str | None = None,
    edge_cloud_edge_model: str | None = None,
    edge_cloud_edge_max_tokens: str | None = None,
    edge_cloud_device_base_url: str | None = None,
    edge_cloud_device_api_key: str | None = None,
    edge_cloud_device_model: str | None = None,
    edge_cloud_device_max_tokens: str | None = None,
    edge_cloud_device_max_tool_rounds: str | None = None,
) -> None:
    """将 DevCo 冻结包初始化所需配置写入环境变量。

    `devco_bundle_init.maybe_devco_bundle_seed_llm` 只读 ``os.environ``；
    此处把调用方传入的参数落到环境变量（传入 ``None`` 表示不覆盖进程里已有值）。

    对应键名：``OPENAI_BASE_URL``、``OPENAI_MODEL``（逗号可多模型）、``LLM_MODEL``、
    ``OPENAI_API_KEY``、``LLM_API_KEY``、``DEVCO_BUNDLE_PROVIDER_ID``、
    ``DEVCO_BUNDLE_PROVIDER_NAME``、``DEVCO_INIT_SKIP_SEED``。
    """
    mapping: list[tuple[str, str | None]] = [
        ("OPENAI_BASE_URL", openai_base_url),
        ("OPENAI_MODEL", openai_model),
        ("LLM_MODEL", llm_model),
        ("OPENAI_API_KEY", openai_api_key),
        ("LLM_API_KEY", llm_api_key),
        ("DEVCO_BUNDLE_PROVIDER_ID", devco_bundle_provider_id),
        ("DEVCO_BUNDLE_PROVIDER_NAME", devco_bundle_provider_name),
        ("DEVCO_INIT_SKIP_SEED", devco_init_skip_seed),
        ("QWENPAW_SESSION_DELTA_LANGFUSE_ENABLED", session_delta_langfuse_enabled),
        ("QWENPAW_SESSION_DELTA_LANGFUSE_HOST", session_delta_langfuse_host),
        ("QWENPAW_SESSION_DELTA_LANGFUSE_PUBLIC_KEY", session_delta_langfuse_public_key),
        ("QWENPAW_SESSION_DELTA_LANGFUSE_SECRET_KEY", session_delta_langfuse_secret_key),
        ("QWENPAW_COMPACT_HISTORICAL_TOOL_CONTEXT", qwenpaw_compact_historical_tool_context),
        ("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", qwenpaw_force_direct_subprocess_sync),
        ("QWENPAW_BUNDLED_RUNTIME_ISOLATION", bundled_runtime_isolation),
        ("QWENPAW_AUTO_APPROVE_ASK", qwenpaw_auto_approve_ask),
        ("QWENPAW_SHELL_GUARD_DISABLE_THINKING", shell_guard_disable_thinking),
        ("QWENPAW_REACT_TIMING_ENABLED", react_timing_enabled),
        ("QWENPAW_FINAL_FILES_ENABLED", final_files_enabled),
        ("QWENPAW_EDGE_CLOUD_ENABLED", edge_cloud_enabled),
        ("QWENPAW_EDGE_CLOUD_TRACE_STREAM_ENABLED", edge_cloud_trace_stream_enabled),
        ("QWENPAW_EDGE_CLOUD_SESSION_PROCESS_ENABLED", edge_cloud_session_process_enabled),
        ("QWENPAW_EDGE_CLOUD_AGENT_IDS", edge_cloud_agent_ids),
        ("QWENPAW_EDGE_CLOUD_EDGE_BASE_URL", edge_cloud_edge_base_url),
        ("QWENPAW_EDGE_CLOUD_EDGE_API_KEY", edge_cloud_edge_api_key),
        ("QWENPAW_EDGE_CLOUD_EDGE_MODEL", edge_cloud_edge_model),
        ("QWENPAW_EDGE_CLOUD_EDGE_MAX_TOKENS", edge_cloud_edge_max_tokens),
        ("QWENPAW_EDGE_CLOUD_DEVICE_BASE_URL", edge_cloud_device_base_url),
        ("QWENPAW_EDGE_CLOUD_DEVICE_API_KEY", edge_cloud_device_api_key),
        ("QWENPAW_EDGE_CLOUD_DEVICE_MODEL", edge_cloud_device_model),
        ("QWENPAW_EDGE_CLOUD_DEVICE_MAX_TOKENS", edge_cloud_device_max_tokens),
        ("QWENPAW_EDGE_CLOUD_DEVICE_MAX_TOOL_ROUNDS", edge_cloud_device_max_tool_rounds),
        ("QWENPAW_INIT_DEFAULT_ONLY", "1"),
        ("QWENPAW_INIT_DEFAULT_SKILLS", "skill-creator,make-skill,platform-kb-search"),
        ("QWENPAW_CORS_ORIGINS", "*"),
        ("QWENPAW_CONSOLE_ENABLED", "0"),
        ("QWENPAW_WINDOWS_SANDBOX_PATH_RX", "1"),
        ("QWENPAW_WINDOWS_DIRECT_SHELL_LLM_GUARD_TIMEOUT", "180"),
        ("QWENPAW_UPLOAD_MAX_SIZE_MB", "10000"),
    ]
    for key, val in mapping:
        if val is not None:
            os.environ[key] = val


# CLI 子命令白名单：首 token 命中即认为本次启动是 CLI 调用而非 server 启动。
_CLI_SUBCOMMANDS = frozenset({
    "acp", "app", "channels", "channel", "daemon", "chats", "chat", "clean",
    "cron", "env", "init", "models", "skills", "tui", "uninstall", "desktop",
    "update", "shutdown", "auth", "agents", "agent", "plugin", "task",
    "doctor", "auto",
})


def _should_dispatch_cli(app_argv: list[str]) -> bool:
    """判断本次入口调用是否应分流到 CLI 组（而非 server）。"""
    # 1) 打包态：以 devcopaw/qwenpaw/copaw 命名的启动器一律走 CLI
    if getattr(sys, "frozen", False):
        if Path(sys.executable).stem.lower() in {"devcopaw", "qwenpaw", "copaw"}:
            return True
    # 2) 显式子命令：覆盖源码 `python main.py cron ...` 与 `paw.exe cron ...`
    first = app_argv[0] if app_argv else None
    return bool(first) and not first.startswith("-") and first in _CLI_SUBCOMMANDS


def parse_entry_cli_args(argv: list[str] | None = None) -> list[str]:
    """解析入口命令行：DevCo/LLM 环境变量 + ``app_cmd`` 的 ``--host`` / ``--port`` 等。

    本函数消费的选项不会进入返回值；未声明的选项（如 ``--reload``）原样交给 ``app_cmd``。

    示例::

        paw.exe --openai-api-key sk-... --host 0.0.0.0 --port 9000 --reload
    """
    parser = argparse.ArgumentParser(add_help=False)
    # --- DevCo / LLM（写入环境变量，供 init seed 等读取）---
    parser.add_argument("--openai-base-url", default=os.environ.get("OPENAI_BASE_URL", ""), metavar="URL")
    parser.add_argument("--openai-model", default=os.environ.get("OPENAI_MODEL") or os.environ.get("LLM_MODEL") or "gpt-oss-120b", metavar="MODEL")
    parser.add_argument("--llm-model", default=os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-oss-120b", metavar="MODEL")
    parser.add_argument("--openai-api-key", default=os.environ.get("OPENAI_API_KEY", ""), metavar="KEY")
    parser.add_argument("--llm-api-key", default=os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", ""), metavar="KEY")
    parser.add_argument("--devco-bundle-provider-id", default="lcfc", metavar="ID")
    parser.add_argument("--devco-bundle-provider-name", default="lcfc", metavar="NAME")
    parser.add_argument(
        "--session-delta-langfuse-enabled",
        "--qwenpaw-session-delta-langfuse-enabled",
        default=os.environ.get("QWENPAW_SESSION_DELTA_LANGFUSE_ENABLED", "1"),
        metavar="BOOL",
    )
    parser.add_argument(
        "--session-delta-langfuse-host",
        "--qwenpaw-session-delta-langfuse-host",
        default=os.environ.get("QWENPAW_SESSION_DELTA_LANGFUSE_HOST", "http://10.159.252.181:3000"),
        metavar="URL",
    )
    parser.add_argument(
        "--session-delta-langfuse-public-key",
        "--qwenpaw-session-delta-langfuse-public-key",
        default=os.environ.get("QWENPAW_SESSION_DELTA_LANGFUSE_PUBLIC_KEY", ""),
        metavar="KEY",
    )
    parser.add_argument(
        "--session-delta-langfuse-secret-key",
        "--qwenpaw-session-delta-langfuse-secret-key",
        default=os.environ.get("QWENPAW_SESSION_DELTA_LANGFUSE_SECRET_KEY", ""),
        metavar="KEY",
    )
    parser.add_argument(
        "--compact-historical-tool-context",
        "--qwenpaw-compact-historical-tool-context",
        default="1",
        metavar="BOOL",
    )
    parser.add_argument(
        "--bundled-runtime-isolation",
        choices=("0", "1"),
        default=os.environ.get("QWENPAW_BUNDLED_RUNTIME_ISOLATION", "0"),
        help="Use shared (0, default) or isolated (1) bundled dependencies for direct subprocess calls.",
    )
    parser.add_argument(
        "--auto-approve-ask",
        "--qwenpaw-auto-approve-ask",
        default="1",
        metavar="BOOL",
    )
    parser.add_argument(
        "--shell-guard-disable-thinking",
        "--qwenpaw-shell-guard-disable-thinking",
        default=os.environ.get("QWENPAW_SHELL_GUARD_DISABLE_THINKING", "1"),
        metavar="BOOL",
        help="Disable thinking for online shell guard models; local models keep their settings.",
    )
    parser.add_argument(
        "--react-timing-enabled",
        "--qwenpaw-react-timing-enabled",
        default=os.environ.get("QWENPAW_REACT_TIMING_ENABLED", "true"),
        metavar="BOOL",
        help="Print per-round reasoning/thinking, Act and individual tool timings when true.",
    )
    parser.add_argument(
        "--final-files-enabled",
        default=os.environ.get("QWENPAW_FINAL_FILES_ENABLED", "1"),
        metavar="BOOL",
        help="Enable final-file delivery for completed tasks.",
    )
    parser.add_argument(
        "--edge-cloud-enabled",
        default=os.environ.get("QWENPAW_EDGE_CLOUD_ENABLED", "0"),
        metavar="BOOL",
    )
    parser.add_argument(
        "--edge-cloud-trace-stream-enabled",
        default=os.environ.get("QWENPAW_EDGE_CLOUD_TRACE_STREAM_ENABLED", "1"),
        metavar="BOOL",
        help="Show RouterAgent/VerifyAgent decisions in content and label execution Agent/model output.",
    )
    parser.add_argument(
        "--edge-cloud-session-process-enabled",
        default=os.environ.get("QWENPAW_EDGE_CLOUD_SESSION_PROCESS_ENABLED", "1"),
        metavar="BOOL",
        help="Persist emitted cloud/edge/device process only for collaborative requests (opt-in).",
    )
    parser.add_argument(
        "--edge-cloud-agent-ids",
        default=os.environ.get("QWENPAW_EDGE_CLOUD_AGENT_IDS", "default"),
        metavar="AGENT_IDS",
    )
    parser.add_argument(
        "--edge-cloud-edge-base-url",
        default=os.environ.get("QWENPAW_EDGE_CLOUD_EDGE_BASE_URL", "http://10.199.230.180:30061/v1"),
        metavar="URL",
    )  
    parser.add_argument(
        "--edge-cloud-edge-api-key",
        default=os.environ.get("QWENPAW_EDGE_CLOUD_EDGE_API_KEY", ""),
        metavar="KEY",
    )
    parser.add_argument(
        "--edge-cloud-edge-model",
        default=os.environ.get("QWENPAW_EDGE_CLOUD_EDGE_MODEL", "Mr-27B-0919"),
        metavar="MODEL",
    )
    parser.add_argument(
        "--edge-cloud-edge-max-tokens",
        default=os.environ.get("QWENPAW_EDGE_CLOUD_EDGE_MAX_TOKENS", ""),
        metavar="TOKENS",
    )
    parser.add_argument(
        "--edge-cloud-device-max-tool-rounds",
        default=os.environ.get("QWENPAW_EDGE_CLOUD_DEVICE_MAX_TOOL_ROUNDS", "5"),
        metavar="N",
    )
    parser.add_argument(
        "--edge-cloud-device-base-url",
        default=os.environ.get("QWENPAW_EDGE_CLOUD_DEVICE_BASE_URL", "http://10.199.230.180:30061/v1"),
        metavar="URL",
    )
    parser.add_argument(
        "--edge-cloud-device-api-key",
        default=os.environ.get("QWENPAW_EDGE_CLOUD_DEVICE_API_KEY", ""),
        metavar="KEY",
    )
    parser.add_argument(
        "--edge-cloud-device-model",
        default=os.environ.get("QWENPAW_EDGE_CLOUD_DEVICE_MODEL", "Mr-27B-0919"),
        metavar="MODEL",
    )
    parser.add_argument(
        "--edge-cloud-device-max-tokens",
        default=os.environ.get("QWENPAW_EDGE_CLOUD_DEVICE_MAX_TOKENS", ""),
        metavar="TOKENS",
    )
    parser.add_argument(
        "--devco-init-skip-seed",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--init-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    # --- Web 服务（与 app_cmd 一致；未传则由 Click 使用默认值）---
    parser.add_argument(
        "--host",
        default=None,
        metavar="HOST",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--port",
        default=None,
        type=int,
        metavar="PORT",
        help=argparse.SUPPRESS,
    )
    args, rest = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    apply_devco_bundle_env(
        openai_base_url=args.openai_base_url,
        openai_model=args.openai_model,
        llm_model=args.llm_model,
        openai_api_key=args.openai_api_key,
        llm_api_key=args.llm_api_key,
        devco_bundle_provider_id=args.devco_bundle_provider_id,
        devco_bundle_provider_name=args.devco_bundle_provider_name,
        devco_init_skip_seed="1" if args.devco_init_skip_seed else None,
        session_delta_langfuse_enabled=args.session_delta_langfuse_enabled,
        session_delta_langfuse_host=args.session_delta_langfuse_host,
        session_delta_langfuse_public_key=args.session_delta_langfuse_public_key,
        session_delta_langfuse_secret_key=args.session_delta_langfuse_secret_key,
        qwenpaw_compact_historical_tool_context=args.compact_historical_tool_context,
        qwenpaw_force_direct_subprocess_sync="1",
        bundled_runtime_isolation=args.bundled_runtime_isolation,
        qwenpaw_auto_approve_ask=args.auto_approve_ask,
        shell_guard_disable_thinking=args.shell_guard_disable_thinking,
        react_timing_enabled=args.react_timing_enabled,
        final_files_enabled=args.final_files_enabled,
        edge_cloud_enabled=args.edge_cloud_enabled,
        edge_cloud_trace_stream_enabled=args.edge_cloud_trace_stream_enabled,
        edge_cloud_session_process_enabled=args.edge_cloud_session_process_enabled,
        edge_cloud_agent_ids=args.edge_cloud_agent_ids,
        edge_cloud_edge_base_url=args.edge_cloud_edge_base_url,
        edge_cloud_edge_api_key=args.edge_cloud_edge_api_key,
        edge_cloud_edge_model=args.edge_cloud_edge_model,
        edge_cloud_edge_max_tokens=args.edge_cloud_edge_max_tokens,
        edge_cloud_device_base_url=args.edge_cloud_device_base_url,
        edge_cloud_device_api_key=args.edge_cloud_device_api_key,
        edge_cloud_device_model=args.edge_cloud_device_model,
        edge_cloud_device_max_tokens=args.edge_cloud_device_max_tokens,
        edge_cloud_device_max_tool_rounds=args.edge_cloud_device_max_tool_rounds,
    )
    if args.init_only:
        os.environ[_INIT_ONLY_ENV] = "1"
    app_prefix: list[str] = []
    if args.host is not None:
        app_prefix.extend(["--host", args.host])
    if args.port is not None:
        app_prefix.extend(["--port", str(args.port)])
    return app_prefix + rest


def _resolve_entry_arguments() -> tuple[bool, list[str]]:
    """Keep arguments after the explicit cli marker out of the server parser."""
    raw_args = sys.argv[1:]
    if raw_args and raw_args[0] == "cli":
        parse_entry_cli_args([])
        return True, raw_args[1:]
    return False, parse_entry_cli_args(raw_args)


def _sync_default_prompt_files() -> None:
    """启动前同步默认 Agent 提示词；失败仅打印，不阻断启动。"""
    import hashlib
    import tempfile

    def report(message: str) -> None:
        try:
            print(f"[prompt-sync] {message}", flush=True)
        except Exception:
            pass

    try:
        from qwenpaw.config import load_config
        from qwenpaw.config.config import load_agent_config
        from qwenpaw.constant import SUPPORTED_AGENT_LANGUAGES
        from qwenpaw.prompt_resources import templates

        config = load_config()
        default_ref = config.agents.profiles.get("default")
        if default_ref is None:
            report("未找到 default Agent，跳过同步。")
            return

        workspace_dir = Path(default_ref.workspace_dir).expanduser().resolve()
        agent_config = load_agent_config("default")
        language = agent_config.language or "zh"
        if language not in SUPPORTED_AGENT_LANGUAGES:
            report(f"不支持的语言 {language!r}，跳过同步。")
            return

        template_files = templates(language) or templates("en")
    except Exception as exc:
        report(f"准备同步失败：{exc}；继续启动。")
        return

    for filename in ("AGENTS.md", "SOUL.md"):
        temp_path = None
        try:
            target = workspace_dir / filename
            if target.resolve().parent != workspace_dir:
                raise ValueError(f"目标文件指向工作区之外：{target}")

            new_content = template_files.get(filename, b"")
            if not new_content.decode("utf-8-sig").strip():
                raise ValueError(f"模板缺失或为空：{filename}")

            new_md5 = hashlib.md5(new_content).hexdigest()
            old_md5 = (
                hashlib.md5(target.read_bytes()).hexdigest()
                if target.exists()
                else None
            )
            if old_md5 == new_md5:
                report(f"{filename} 内容一致，跳过。")
                continue

            workspace_dir.mkdir(parents=True, exist_ok=True)
            # 先写同目录临时文件，再替换，避免写入失败破坏原文件。
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{filename}.",
                suffix=".tmp",
                dir=workspace_dir,
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                temp_file.write(new_content)
                temp_file.flush()
                os.fsync(temp_file.fileno())

            os.replace(temp_path, target)
            temp_path = None
            report(f"{filename} 同步成功：{old_md5 or '缺失'} → {new_md5}")
        except Exception as exc:
            report(f"{filename} 同步失败：{exc}；继续启动。")
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception as exc:
                    report(f"临时文件清理失败：{exc}")


def _sync_default_react_limit() -> None:
    """启动时将 default 的 ReAct 上限设为 300，失败不阻断启动。"""
    def report(message: str) -> None:
        try:
            print(f"[react-limit] {message}", flush=True)
        except Exception:
            pass

    try:
        from qwenpaw.config import load_config
        from qwenpaw.config.config import load_agent_config, save_agent_config

        if "default" not in load_config().agents.profiles:
            report("未找到 default Agent，跳过。")
            return

        # 使用副本，保存失败时不改变缓存中的配置对象。
        config = load_agent_config("default").model_copy(deep=True)
        running = config.running
        iteration = running.loop.iteration
        if running.max_iters == 300 and iteration.max_iterations == 300:
            report("default ReAct 上限已为 300，跳过。")
            return

        old_max = running.max_iters
        old_gate_max = iteration.max_iterations
        running.max_iters = 500
        iteration.max_iterations = 500
        save_agent_config("default", config) 
        report(
            f"default ReAct 上限更新成功：max_iters {old_max} → 500，"
            f"iteration.max_iterations {old_gate_max} → 500"
        )
    except Exception as exc:
        report(f"更新失败：{exc}；继续启动。")


if __name__ == "__main__":
    _ensure_devcopaw_launcher()
    _explicit_cli, _app_argv = _resolve_entry_arguments()
    if _explicit_cli or _should_dispatch_cli(_app_argv):
        from qwenpaw.cli.main import cli

        if _explicit_cli:
            cli.main(
                args=_app_argv,
                prog_name="paw.exe cli",
                standalone_mode=True,
            )
            sys.exit(0)
        cli(args=_app_argv, prog_name="devcopaw", standalone_mode=False)
        sys.exit(0)
    _logger = _setup_entry_logging()

    # init_cmd 是 Click Command，不能写 init_cmd(force=...)；需走 argv 解析          
    # Skip the expensive default init once the bundled workspace is ready.
    time_start = time.time() * 1000
    if _is_first_run_initialization_needed():
        _logger.info("initialization needed")
        _run_first_run_initialization()
    time_finish_init = time.time() * 1000
    _logger.info("init cost time: %.2f ms", time_finish_init - time_start)
    if os.environ.get(_INIT_ONLY_ENV) == "1":
        _logger.info("init-only completed; app startup skipped.")
        sys.exit(0)
    _sync_default_prompt_files()
    _sync_default_react_limit()
    from qwenpaw.cli.app_cmd import app_cmd

    app_cmd.main(args=_app_argv, standalone_mode=False)
