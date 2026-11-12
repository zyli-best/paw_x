"""Per-user storage for portable tools and their request environments."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
from time import time_ns
from uuid import uuid4

from filelock import FileLock, Timeout


def runtime_home() -> Path:
    return (Path.home() / ".devco").resolve()


def shared_runtime_lock(root: Path, runtime: str) -> FileLock:
    path = root / f".{runtime}.lock"
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"Shared runtime lock escapes the tool directory: {path}")
    return FileLock(str(path), timeout=60)


def environment_owner(workspace: Path, session_id: str | None, mount: Path | None) -> list[str]:
    session_id = (session_id or "").strip()
    if mount is not None:
        mount_path = mount.resolve()
        if not session_id:
            return ["m", os.path.normcase(str(mount_path))]
        from ..config.context import get_session_scoped_work_dir

        scratch = get_session_scoped_work_dir(workspace, session_id, create=False)
        # Desktop clients may include their own session scratch in sandbox_mounts.
        if mount_path != scratch.resolve():
            return ["m", os.path.normcase(str(mount_path))]
    if not session_id:
        raise ValueError("A session ID is required for the runtime scratch directory")
    return ["s", os.path.normcase(str(workspace.resolve())), session_id]


def _next_snow_id(root: Path) -> int:
    # All allocators for one profile share the storage lock and sequence.
    # Persisting the last value also handles restarts and clock rollback.
    sequence = root / ".sequence"
    last = int(sequence.read_text(encoding="ascii")) if sequence.exists() else 0
    milliseconds = time_ns() // 1_000_000 - 1_735_689_600_000  # 2025-01-01 UTC
    value = max(milliseconds << 22, last + 1)
    while any(os.path.lexists(root / f"{kind}-{value}") for kind in ("s", "m")):
        value += 1
    pending = sequence.with_suffix(".tmp")
    pending.write_text(str(value), encoding="ascii")
    pending.replace(sequence)
    return value


def _metadata(path: Path) -> dict:
    data = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid runtime environment metadata: {path}")
    return data


def _write_metadata(path: Path, data: dict) -> None:
    pending = path / f".{uuid4().hex}.tmp"
    try:
        pending.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        pending.replace(path / "meta.json")
    finally:
        pending.unlink(missing_ok=True)


def _owned_environment(root: Path, owner: list[str]) -> Path | None:
    for path in root.glob(f"{owner[0]}-*"):
        if not path.name[2:].isdigit():
            continue
        if path.is_dir() and path.resolve() == path:
            try:
                if _metadata(path)["owner"] == owner:
                    return path
            except (OSError, ValueError, KeyError):
                continue
    return None


def _environment_lock(path: Path, timeout: float) -> FileLock:
    return FileLock(str(path.parent / f".{path.name}.lock"), timeout=timeout)


def _reclaim(path: Path) -> bool:
    try:
        with _environment_lock(path, 0):
            # Keep the ownership and pending marker until all payload files are gone.
            for entry in path.iterdir():
                if entry.name != "meta.json":
                    if entry.is_dir() and entry.resolve() == entry:
                        shutil.rmtree(entry)
                    elif entry.is_dir():
                        entry.rmdir()
                    else:
                        entry.unlink()
            meta = _metadata(path)
            (path / "meta.json").unlink()
            try:
                path.rmdir()
            except OSError:
                _write_metadata(path, meta)
                raise
    except (OSError, Timeout):
        return False
    return True


def allocate_environment(owner: list[str], task_root: Path) -> tuple[Path, str]:
    root = runtime_home() / "e"
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / ".index.lock"), timeout=60):
        existing = _owned_environment(root, owner)
        if existing is not None:
            meta = _metadata(existing)
            if not meta.get("retired"):
                return existing, meta["generation"]
            if not _reclaim(existing):
                raise ValueError(f"Previous session environment is still being cleaned: {existing}")

        path = root / f"{owner[0]}-{_next_snow_id(root)}"
        generation = uuid4().hex
        path.mkdir()
        try:
            _write_metadata(path, {
                "owner": owner, "task_root": str(task_root), "generation": generation,
            })
        except OSError:
            path.rmdir()
            raise
        return path, generation


@contextmanager
def use_environment(path: Path, generation: str):
    with _environment_lock(path, 60):
        meta = _metadata(path)
        if meta["generation"] != generation or meta.get("retired"):
            raise ValueError(f"Session runtime environment was retired: {path}")
        yield


def retire_session_environment(workspace: Path, session_id: str) -> None:
    root = runtime_home() / "e"
    if not root.is_dir() or not session_id.strip():
        return
    owner = environment_owner(workspace, session_id, None)
    with FileLock(str(root / ".index.lock"), timeout=60):
        path = _owned_environment(root, owner)
        if path is not None:
            _write_metadata(path, {**_metadata(path), "retired": True})
            _reclaim(path)


def retry_retired_environments() -> None:
    root = runtime_home() / "e"
    if not root.is_dir():
        return
    with FileLock(str(root / ".index.lock"), timeout=60):
        for path in root.glob("s-*"):
            if not path.is_dir() or path.resolve() != path:
                continue
            try:
                meta = _metadata(path)
                owner = meta["owner"]
                if owner[0] == "s" and meta.get("retired") and path.name[2:].isdigit():
                    _reclaim(path)
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                continue


def reclaim_if_retired(path: Path, generation: str) -> None:
    try:
        with FileLock(str(path.parent / ".index.lock"), timeout=0):
            meta = _metadata(path)
            if meta.get("retired") and meta.get("generation") == generation:
                _reclaim(path)
    except (OSError, ValueError, Timeout):
        return


