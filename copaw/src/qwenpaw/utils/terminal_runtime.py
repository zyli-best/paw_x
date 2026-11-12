"""Detect external runtimes for build-time environment context."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

_PYTHON_INFO = (
    "import json, sys;"
    "print(json.dumps({"
    "'kind': 'python',"
    "'executable': sys.executable,"
    "'prefix': sys.prefix,"
    "'virtual': "
    "sys.prefix != getattr(sys, 'base_prefix', sys.prefix) "
    "or hasattr(sys, 'real_prefix')"
    "}))"
)
_NODE_INFO = (
    "JSON.stringify({kind: 'nodejs', executable: process.execPath})"
)


def _output_preview(output: str | bytes | None) -> str:
    """Keep probe diagnostics bounded, including partial timeout output."""
    if output is None:
        return ""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    return output[:1000] + ("...[truncated]" if len(output) > 1000 else "")


def _absolute_path(raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        text = os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))
        path = Path(text)
        return path if path.is_absolute() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _candidates(
    env: dict[str, str],
    hint_keys: tuple[str, ...],
    names: tuple[str, ...],
) -> Iterator[Path]:
    roots = [
        *(env.get(key, "") for key in hint_keys),
        *env.get("PATH", "").split(os.pathsep),
    ]
    seen: set[str] = set()
    for raw in roots:
        root = _absolute_path(raw)
        if root is None:
            continue
        for directory in (root, root / "bin"):
            for name in names:
                candidate = directory / name
                key = os.path.normcase(str(candidate))
                if key in seen:
                    continue
                seen.add(key)
                # Windows Store aliases are not confirmed installations.
                if os.name == "nt" and any(
                    part.casefold() == "windowsapps"
                    for part in candidate.parts
                ):
                    logger.debug(
                        "[RuntimeDetect] skipped candidate=%r "
                        "reason=windows_store_alias", str(candidate),
                    )
                    continue
                try:
                    if candidate.is_file() and (
                        os.name == "nt" or os.access(candidate, os.X_OK)
                    ):
                        yield candidate
                except (OSError, ValueError) as exc:
                    logger.info(
                        "[RuntimeDetect] skipped candidate=%r "
                        "reason=path_check_failed error=%r",
                        str(candidate), str(exc),
                    )
                    continue


def _is_virtual_python(path: Path, excluded_roots: list[Path]) -> bool:
    try:
        # Check before resolving: venv/bin/python may point to a global Python.
        for location in (path.absolute(), path.resolve()):
            if any(location.is_relative_to(root) for root in excluded_roots):
                logger.info(
                    "[RuntimeDetect] skipped Python path=%r "
                    "reason=excluded_virtual_root", str(location),
                )
                return True
            for parent in (location, *location.parents):
                if (parent / "pyvenv.cfg").is_file():
                    logger.info(
                        "[RuntimeDetect] skipped Python path=%r "
                        "reason=pyvenv.cfg root=%r", str(path), str(parent),
                    )
                    return True
                if (parent / "conda-meta").is_dir():
                    logger.info(
                        "[RuntimeDetect] skipped Python path=%r "
                        "reason=conda-meta root=%r", str(path), str(parent),
                    )
                    return True
        return False
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning(
            "[RuntimeDetect] skipped Python path=%r "
            "reason=virtual_check_failed error=%r", str(path), str(exc),
        )
        return True


def _probe(command: list[str], timeout: float) -> dict | None:
    started = time.perf_counter()
    logger.info(
        "[RuntimeDetect] probe start executable=%r timeout=%.3fs",
        command[0], timeout,
    )
    # Avoid Node auto-preload hooks during this metadata-only probe.
    probe_env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() != "NODE_OPTIONS"
    }
    result = None
    try:
        result = subprocess.run(
            command,
            shell=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=probe_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            logger.warning(
                "[RuntimeDetect] probe failed executable=%r "
                "reason=nonzero_exit returncode=%s elapsed=%.3fs "
                "stdout=%r stderr=%r",
                command[0], result.returncode, time.perf_counter() - started,
                _output_preview(result.stdout), _output_preview(result.stderr),
            )
            return None
        info = json.loads(result.stdout.strip())
        if not isinstance(info, dict):
            logger.warning(
                "[RuntimeDetect] probe failed executable=%r "
                "reason=invalid_payload elapsed=%.3fs stdout=%r stderr=%r",
                command[0], time.perf_counter() - started,
                _output_preview(result.stdout), _output_preview(result.stderr),
            )
            return None
        logger.info(
            "[RuntimeDetect] probe succeeded executable=%r "
            "elapsed=%.3fs stdout=%r stderr=%r",
            command[0], time.perf_counter() - started,
            _output_preview(result.stdout), _output_preview(result.stderr),
        )
        return info
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            "[RuntimeDetect] probe failed executable=%r reason=timeout "
            "timeout=%.3fs elapsed=%.3fs stdout=%r stderr=%r",
            command[0], timeout, time.perf_counter() - started,
            _output_preview(exc.stdout), _output_preview(exc.stderr),
        )
        return None
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.warning(
            "[RuntimeDetect] probe failed executable=%r reason=%s "
            "elapsed=%.3fs error_type=%s error=%r stdout=%r stderr=%r",
            command[0],
            "invalid_json" if isinstance(exc, json.JSONDecodeError)
            else "execution_error",
            time.perf_counter() - started, type(exc).__name__, str(exc),
            _output_preview(getattr(result, "stdout", None)),
            _output_preview(getattr(result, "stderr", None)),
        )
        return None


def detect_runtime_environments(
    kinds: tuple[str, ...] = ("python", "nodejs"),
) -> dict[str, dict[str, str]]:
    """Confirm Python outside virtual environments and Node.js from env/PATH.

    Each runtime has a two-second subprocess budget, with at most one second
    per candidate. Missing or invalid candidates are omitted from the result.
    """
    env = {key.upper(): value for key, value in os.environ.items()}
    excluded_roots: list[Path] = []
    raw_roots = [env.get("VIRTUAL_ENV", ""), env.get("CONDA_PREFIX", "")]
    if not getattr(sys, "frozen", False) and (
        sys.prefix != getattr(sys, "base_prefix", sys.prefix)
        or hasattr(sys, "real_prefix")
    ):
        raw_roots.append(sys.prefix)
    for raw in raw_roots:
        root = _absolute_path(raw)
        if root is not None:
            excluded_roots.append(root)
            try:
                excluded_roots.append(root.resolve())
            except (OSError, RuntimeError, ValueError):
                pass

    logger.info(
        "[RuntimeDetect] start pid=%s host_python=%r "
        "debugger_active=%s pydevd_loaded=%s "
        "path_entries=%d runtime_hints=%r excluded_python_roots=%r",
        os.getpid(), getattr(sys, "executable", None),
        getattr(sys, "gettrace", lambda: None)() is not None,
        "pydevd" in getattr(sys, "modules", {}),
        len([entry for entry in env.get("PATH", "").split(os.pathsep) if entry]),
        {key: env[key] for key in (
            "PYTHONHOME", "PYTHON_HOME", "NODE_HOME", "NVM_SYMLINK",
            "VIRTUAL_ENV", "CONDA_PREFIX",
        ) if env.get(key)},
        list(dict.fromkeys(str(root) for root in excluded_roots)),
    )

    names = {
        "python": ("python.exe", "python3.exe")
        if os.name == "nt" else ("python3", "python"),
        "nodejs": ("node.exe",) if os.name == "nt" else ("node",),
    }
    hints = {
        "python": ("PYTHONHOME", "PYTHON_HOME"),
        "nodejs": ("NODE_HOME", "NVM_SYMLINK"),
    }
    detected: dict[str, dict[str, str]] = {}
    for kind in kinds:
        candidate_count = 0
        deadline = time.monotonic() + 2.0
        for candidate in _candidates(env, hints[kind], names[kind]):
            candidate_count += 1
            logger.info(
                "[RuntimeDetect] candidate kind=%s path=%r",
                kind, str(candidate),
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "[RuntimeDetect] stopped kind=%s "
                    "reason=budget_exhausted next_candidate=%r",
                    kind, str(candidate),
                )
                break
            if kind == "python":
                if _is_virtual_python(candidate, excluded_roots):
                    continue
                arguments = ["-I", "-B", "-c", _PYTHON_INFO]
            else:
                arguments = ["-p", _NODE_INFO]
            info = _probe(
                [str(candidate), *arguments],
                timeout=min(1.0, remaining),
            )
            if not info:
                continue
            if info.get("kind") != kind:
                logger.warning(
                    "[RuntimeDetect] skipped candidate=%r "
                    "reason=kind_mismatch expected=%s actual=%r",
                    str(candidate), kind, info.get("kind"),
                )
                continue
            executable = _absolute_path(info.get("executable"))
            if executable is None:
                logger.warning(
                    "[RuntimeDetect] skipped candidate=%r "
                    "reason=invalid_executable value=%r",
                    str(candidate), info.get("executable"),
                )
                continue
            if kind == "python":
                prefix = _absolute_path(info.get("prefix"))
                if info.get("virtual") is not False or prefix is None:
                    logger.info(
                        "[RuntimeDetect] skipped candidate=%r "
                        "reason=unconfirmed_global_python virtual=%r prefix=%r",
                        str(candidate), info.get("virtual"), info.get("prefix"),
                    )
                    continue
                if (
                    _is_virtual_python(prefix, excluded_roots)
                    or _is_virtual_python(executable, excluded_roots)
                ):
                    continue
            try:
                executable = executable.resolve()
                if not executable.is_file():
                    logger.warning(
                        "[RuntimeDetect] skipped candidate=%r "
                        "reason=reported_executable_missing executable=%r",
                        str(candidate), str(executable),
                    )
                    continue
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning(
                    "[RuntimeDetect] skipped candidate=%r "
                    "reason=executable_check_failed error=%r",
                    str(candidate), str(exc),
                )
                continue
            detected[kind] = {
                "executable": str(executable),
                "directory": str(executable.parent),
            }
            logger.info(
                "[RuntimeDetect] confirmed kind=%s executable=%r",
                kind, str(executable),
            )
            break
        if kind not in detected:
            logger.info(
                "[RuntimeDetect] omitted kind=%s candidates=%d "
                "reason=no_confirmed_runtime", kind, candidate_count,
            )
    logger.info("[RuntimeDetect] complete detected=%r", detected)
    return detected
