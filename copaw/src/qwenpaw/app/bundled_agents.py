# -*- coding: utf-8 -*-
"""Seed bundled agent workspaces during initialization.

This module is copied into ``qwenpaw.app`` by ``fetch_qwenpaw.py``. It keeps
DevCo-bundled agents external to upstream source while making them appear as
normal QwenPaw workspaces on first run.

Bundled workspaces are shipped as one zip file in frozen builds. A directory
source is still supported for editable/source-tree runs.
"""
from __future__ import annotations

import io
import json
import logging
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from ..config.config import (
    AgentProfileRef,
    ModelSlotConfig,
    load_agent_config,
    save_agent_config,
)
from ..config.utils import load_config, save_config
from ..constant import WORKING_DIR

logger = logging.getLogger(__name__)

_BUNDLED_AGENTS_ROOT = Path(__file__).resolve().parent.parent / "agents"
_BUNDLED_WORKSPACES_DIR = _BUNDLED_AGENTS_ROOT / "bundled_workspaces"
_BUNDLED_WORKSPACES_ZIP = _BUNDLED_AGENTS_ROOT / "bundled_workspaces.zip"

_IGNORED_NAMES = {
    "__pycache__",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
}


class _AgentSource(Protocol):
    name: str

    def read_json(self, relative_path: str) -> Any:
        ...

    def copy_missing_to(self, target_dir: Path) -> None:
        ...


class _DirAgentSource:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.name = path.name

    def read_json(self, relative_path: str) -> Any:
        return _read_json(self.path / relative_path)

    def copy_missing_to(self, target_dir: Path) -> None:
        _copy_missing_tree(self.path, target_dir)


class _ZipAgentSource:
    def __init__(self, zip_path: Path, name: str) -> None:
        self.zip_path = zip_path
        self.name = name
        self._prefix = f"{name}/"

    def read_json(self, relative_path: str) -> Any:
        member = self._prefix + relative_path.replace("\\", "/")
        try:
            with zipfile.ZipFile(self.zip_path) as archive:
                with archive.open(member) as file:
                    text = io.TextIOWrapper(file, encoding="utf-8-sig")
                    return json.load(text)
        except Exception:
            return None

    def copy_missing_to(self, target_dir: Path) -> None:
        try:
            with zipfile.ZipFile(self.zip_path) as archive:
                for info in archive.infolist():
                    name = info.filename.replace("\\", "/")
                    if not name.startswith(self._prefix):
                        continue
                    relative = name[len(self._prefix) :]
                    if not relative:
                        continue
                    rel_path = _safe_zip_relative_path(relative)
                    if rel_path is None:
                        logger.warning(
                            "Skipping unsafe bundled agent zip entry: %s",
                            info.filename,
                        )
                        continue
                    target = target_dir / rel_path
                    if target.exists():
                        continue
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with archive.open(info) as source, open(target, "wb") as dest:
                            shutil.copyfileobj(source, dest)
                    except OSError as exc:
                        logger.warning(
                            "Failed to copy bundled agent file %s -> %s: %s",
                            info.filename,
                            target,
                            exc,
                        )
        except zipfile.BadZipFile as exc:
            logger.error("Invalid bundled agent zip %s: %s", self.zip_path, exc)

        # Native releases keep root workspace prompts in the compiled resource
        # module; the ZIP still supplies agent configuration and skill assets.
        from ..prompt_resources import bundled_agent_prompts

        for filename, content in bundled_agent_prompts(self.name).items():
            target = target_dir / filename
            if not target.exists():
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                except OSError as exc:
                    logger.warning("Failed to copy bundled prompt %s: %s", target, exc)


def ensure_bundled_agents_exist() -> int:
    """Copy packaged agent workspaces into WORKING_DIR and register them.

    Existing user workspaces are treated as user-owned: files are only copied
    when the target path is missing. If an agent profile already exists, its
    workspace is respected.

    Returns:
        Number of newly registered agent profiles.
    """
    try:
        return _do_ensure_bundled_agents()
    except Exception as exc:  # pragma: no cover - defensive startup guard
        logger.error(
            "Failed to ensure bundled agents exist: %s",
            exc,
            exc_info=True,
        )
        return 0


def sync_bundled_agents_active_model(active_model: Any | None = None) -> int:
    """Sync bundled agents' active model from the default agent.

    Args:
        active_model: Optional model slot to use. When omitted, the current
            ``default`` agent active_model is loaded from disk.

    Returns:
        Number of bundled agent configs updated.
    """
    try:
        return _do_sync_bundled_agents_active_model(active_model)
    except Exception as exc:  # pragma: no cover - defensive startup guard
        logger.error(
            "Failed to sync bundled agent active models: %s",
            exc,
            exc_info=True,
        )
        return 0


def _do_ensure_bundled_agents() -> int:
    sources = _iter_bundled_agent_sources()
    if not sources:
        logger.debug(
            "No bundled agent workspace source found at %s or %s",
            _BUNDLED_WORKSPACES_ZIP,
            _BUNDLED_WORKSPACES_DIR,
        )
        return 0

    config = load_config()
    default_active_model = _load_default_active_model_dict()
    registered = 0

    for source in sources:
        agent_data = source.read_json("agent.json")
        if not isinstance(agent_data, dict):
            logger.warning(
                "Skipping bundled agent %s: invalid or missing agent.json",
                source.name,
            )
            continue

        agent_id = str(agent_data.get("id") or source.name).strip()
        if not agent_id or agent_id == "default":
            logger.warning(
                "Skipping bundled agent %s: invalid agent id %r",
                source.name,
                agent_id,
            )
            continue

        profile_existed = agent_id in config.agents.profiles
        if profile_existed:
            workspace_dir = Path(
                config.agents.profiles[agent_id].workspace_dir,
            ).expanduser()
        else:
            workspace_dir = Path(
                f"{WORKING_DIR}/workspaces/{agent_id}",
            ).expanduser()
            other_id = _other_agent_owns_workspace(
                config.agents.profiles,
                workspace_dir,
                agent_id,
            )
            if other_id is not None:
                logger.warning(
                    "Skipping bundled agent %r: workspace %s is already "
                    "used by agent %r",
                    agent_id,
                    workspace_dir,
                    other_id,
                )
                continue
            config.agents.profiles[agent_id] = AgentProfileRef(
                id=agent_id,
                workspace_dir=str(workspace_dir),
                enabled=True,
            )
            registered += 1

        had_agent_json = (workspace_dir / "agent.json").is_file()
        workspace_dir.mkdir(parents=True, exist_ok=True)
        source.copy_missing_to(workspace_dir)
        _ensure_workspace_json_files(workspace_dir)

        if (registered and not profile_existed) or not had_agent_json:
            _rewrite_agent_json(
                workspace_dir / "agent.json",
                agent_id,
                active_model=default_active_model,
            )

    if registered:
        config.agents.agent_order = _normalized_agent_order(config)
        save_config(config)
        logger.info("Registered %d bundled agent(s)", registered)

    return registered


def _do_sync_bundled_agents_active_model(
    active_model: Any | None = None,
) -> int:
    model_data = (
        _active_model_to_dict(active_model)
        if active_model is not None
        else _load_default_active_model_dict()
    )
    if not model_data:
        return 0

    config = load_config()
    updated = 0
    for agent_id in _bundled_agent_ids():
        if agent_id not in config.agents.profiles:
            continue
        try:
            agent_config = load_agent_config(agent_id)
            if _active_model_to_dict(agent_config.active_model) == model_data:
                continue
            agent_config.active_model = ModelSlotConfig(**model_data)
            save_agent_config(agent_id, agent_config)
            updated += 1
        except Exception as exc:
            logger.warning(
                "Failed to sync bundled agent %r active model: %s",
                agent_id,
                exc,
            )
    return updated


def _iter_bundled_agent_sources() -> list[_AgentSource]:
    if _BUNDLED_WORKSPACES_ZIP.is_file():
        return _iter_zip_agent_sources(_BUNDLED_WORKSPACES_ZIP)
    if _BUNDLED_WORKSPACES_DIR.is_dir():
        return [
            _DirAgentSource(path)
            for path in sorted(
                _BUNDLED_WORKSPACES_DIR.iterdir(),
                key=lambda p: p.name.lower(),
            )
            if path.is_dir()
            and path.name not in _IGNORED_NAMES
            and (path / "agent.json").is_file()
        ]
    return []


def _iter_zip_agent_sources(zip_path: Path) -> list[_AgentSource]:
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = {info.filename.replace("\\", "/") for info in archive.infolist()}
    except zipfile.BadZipFile as exc:
        logger.error("Invalid bundled agent zip %s: %s", zip_path, exc)
        return []

    agent_names = sorted(
        {
            name.split("/", 1)[0]
            for name in names
            if "/" in name
            and name.split("/", 1)[0]
            and name.split("/", 1)[0] not in _IGNORED_NAMES
            and f"{name.split('/', 1)[0]}/agent.json" in names
        },
        key=str.lower,
    )
    return [_ZipAgentSource(zip_path, name) for name in agent_names]


def _bundled_agent_ids() -> list[str]:
    agent_ids: list[str] = []
    for source in _iter_bundled_agent_sources():
        agent_data = source.read_json("agent.json")
        if isinstance(agent_data, dict):
            agent_id = str(agent_data.get("id") or source.name).strip()
        else:
            agent_id = source.name
        if agent_id and agent_id != "default":
            agent_ids.append(agent_id)
    return agent_ids


def _active_model_to_dict(active_model: Any | None) -> dict[str, str] | None:
    if active_model is None:
        return None
    if hasattr(active_model, "model_dump"):
        data = active_model.model_dump(exclude_none=True)
    elif isinstance(active_model, dict):
        data = dict(active_model)
    else:
        return None

    provider_id = str(data.get("provider_id") or "").strip()
    model = str(data.get("model") or "").strip()
    if not provider_id or not model:
        return None
    return {"provider_id": provider_id, "model": model}


def _load_default_active_model_dict() -> dict[str, str] | None:
    try:
        default_config = load_agent_config("default")
    except Exception:
        return None
    return _active_model_to_dict(default_config.active_model)


def _read_json(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8-sig") as file:
            return json.load(file)
    except Exception:
        return None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    try:
        tmp_path.replace(path)
    except PermissionError:
        if path.exists():
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
        try:
            tmp_path.replace(path)
        except PermissionError:
            with open(path, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
            try:
                tmp_path.chmod(tmp_path.stat().st_mode | stat.S_IWRITE)
                tmp_path.unlink(missing_ok=True)
            except OSError:
                logger.debug("Failed to remove temp file %s", tmp_path)


def _copy_missing_tree(source_dir: Path, target_dir: Path) -> None:
    for source in sorted(source_dir.iterdir(), key=lambda p: p.name.lower()):
        if source.name in _IGNORED_NAMES:
            continue
        target = target_dir / source.name
        if target.exists():
            if source.is_dir() and target.is_dir():
                _copy_missing_tree(source, target)
            continue
        if source.is_dir():
            shutil.copytree(source, target, copy_function=shutil.copy)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, target)


def _safe_zip_relative_path(relative: str) -> Path | None:
    parts = PurePosixPath(relative).parts
    if not parts:
        return None
    if any(part in ("", ".", "..") or part in _IGNORED_NAMES for part in parts):
        return None
    return Path(*parts)


def _rewrite_agent_json(
    agent_json_path: Path,
    agent_id: str,
    active_model: dict[str, str] | None = None,
) -> None:
    data = _read_json(agent_json_path)
    if not isinstance(data, dict):
        data = {}

    workspace_dir = agent_json_path.parent
    data["id"] = agent_id
    data["workspace_dir"] = str(workspace_dir)
    data.setdefault("name", agent_id.replace("_", " ").replace("-", " "))
    data.setdefault("description", "")
    if active_model:
        data["active_model"] = dict(active_model)

    _write_json_atomic(agent_json_path, data)


def _ensure_workspace_json_files(workspace_dir: Path) -> None:
    defaults = {
        "chats.json": {"version": 1, "chats": []},
        "jobs.json": {"version": 1, "jobs": []},
    }
    for filename, payload in defaults.items():
        path = workspace_dir / filename
        if not path.exists():
            _write_json_atomic(path, payload)


def _other_agent_owns_workspace(
    profiles: dict[str, AgentProfileRef],
    workspace: Path,
    agent_id: str,
) -> str | None:
    try:
        target = workspace.resolve()
    except OSError:
        target = workspace.expanduser()

    for existing_id, ref in profiles.items():
        if existing_id == agent_id:
            continue
        other = Path(ref.workspace_dir).expanduser()
        try:
            other_resolved = other.resolve()
        except OSError:
            other_resolved = other
        if other_resolved == target:
            return existing_id
    return None


def _normalized_agent_order(config) -> list[str]:
    ordered_ids: list[str] = []
    for agent_id in config.agents.agent_order:
        if agent_id in config.agents.profiles and agent_id not in ordered_ids:
            ordered_ids.append(agent_id)
    for agent_id in config.agents.profiles:
        if agent_id not in ordered_ids:
            ordered_ids.append(agent_id)
    return ordered_ids
