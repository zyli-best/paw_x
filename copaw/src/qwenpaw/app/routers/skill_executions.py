# -*- coding: utf-8 -*-
"""Create disposable, hidden copies of ordinary agents.

Execution IDs are agent IDs. Chat and other agent APIs remain unchanged.
Mode/revision are accepted for wire compatibility, not version selection.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import shutil
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...agents.skill_system.pool_service import SkillPoolService
from ...agents.skill_system.store import (
    copy_skill_dir,
    default_workspace_manifest,
    normalize_skill_dir_name,
    read_skill_manifest,
    safe_skill_dir,
    scan_skill_dir_or_raise,
)
from ...config.config import (
    AgentProfileConfig,
    AgentProfileRef,
    ChannelConfig,
    HeartbeatConfig,
    load_agent_config,
    validate_agent_id,
)
from ...config.utils import load_config, save_config
from ...constant import WORKING_DIR
from ...drivers.storage import list_card_paths
from ...exceptions import SkillsError
from ...governance.policy import load_governance_policy, save_governance_policy
from ...governance.resource_governor import governance_policy_dir
from ...security.skill_scanner import SkillScanError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/skills/executions", tags=["skill-executions"])
_lifecycle_lock = asyncio.Lock()


class ExecutionSkill(BaseModel):
    name: str = Field(min_length=1)
    revision: str | None = None


class PrepareExecutionRequest(BaseModel):
    mode: str | None = None
    parent_agent_id: str = Field(min_length=1)
    source: Literal["pool", "agent_workspace"]
    skills: list[ExecutionSkill]


class ExecutionResponse(BaseModel):
    execution_id: str
    agent_id: str
    skills: list[ExecutionSkill]
    revision: str | None = None
    temporary: bool = True
    status: Literal["ready"] = "ready"


def _root() -> Path:
    return Path(WORKING_DIR).expanduser().resolve() / "workspaces"


def _owned_path(agent_id: str, workspace: str | Path) -> Path:
    """Never delete a caller-selected directory or follow a junction."""
    validate_agent_id(agent_id, set())
    if not agent_id.startswith("system_agent_"):
        raise HTTPException(409, "Not an execution agent ID")
    # Existing executions remain deletable; new ones only use workspaces.
    candidates = [
        _root() / agent_id,
        _root().parent / "execution_workspaces" / agent_id,
    ]
    actual = Path(workspace).expanduser().resolve()
    if not any(
        actual == path and path.resolve() == path for path in candidates
    ):
        raise HTTPException(
            409, "Execution workspace is outside its owned directory"
        )
    return actual


def _policy_path(workspace: Path) -> Path:
    root = _root().parent / "governance"
    path = governance_policy_dir(workspace, root)
    if path.resolve() != path:
        raise HTTPException(409, "Governance directory must not be redirected")
    return path


def _copy_policy(parent_dir: Path, target: Path) -> None:
    # Keep placeholders portable and explicitly written absolute paths intact.
    # Resolving then unresolving the parent path would also rewrite literals.
    policy = load_governance_policy(str(_policy_path(parent_dir)), "")
    destination = _policy_path(target)
    destination.mkdir(parents=True, exist_ok=False)
    save_governance_policy(policy, str(destination))


def _remove_policy(target: Path) -> None:
    policy_dir = _policy_path(target)
    if policy_dir.exists():
        shutil.rmtree(policy_dir)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _build_workspace(
    parent: AgentProfileConfig,
    parent_dir: Path,
    target: Path,
    agent_id: str,
    names: list[str],
    source: str,
) -> None:
    """Only write under target; never reconcile or rename parent storage."""
    derived = parent.model_copy(deep=True)
    derived.id = agent_id
    derived.name = parent.name
    derived.workspace_dir = str(target)
    derived.channels = ChannelConfig()
    derived.heartbeat = HeartbeatConfig(enabled=False)
    derived.last_dispatch = None

    # Current MCP configuration lives in DriverCards, not only agent.json.
    cards_dir = parent_dir / "drivers"
    for origin in list_card_paths(cards_dir):
        destination = target / "drivers" / origin.relative_to(cards_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, destination)
    credentials = parent_dir / "credentials.yaml"
    if credentials.is_file():
        shutil.copy2(credentials, target / "credentials.yaml")

    # Explicitly copy prompt inputs, including custom prompt filenames.
    prompt_files = list(
        dict.fromkeys(
            ["AGENTS.md", "SOUL.md", "PROFILE.md"]
            + parent.system_prompt_files,
        )
    )
    prompt_mapping = {}
    for index, filename in enumerate(prompt_files):
        origin = (parent_dir / filename).resolve()
        if not origin.is_file():
            continue
        relative = Path(filename)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or (
                relative.parts
                and relative.parts[0].lower() in {"skills", "skill"}
            )
        ):
            relative = Path("prompts") / f"{index}_{origin.name}"
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, destination)
        prompt_mapping[filename] = relative.as_posix()
    derived.system_prompt_files = [
        prompt_mapping.get(name, name) for name in parent.system_prompt_files
    ]
    # Do not leave the copy's coding tools pointed into its parent's workspace.
    project = derived.coding_mode.project_dir
    if project and Path(project).expanduser().resolve().is_relative_to(
        parent_dir
    ):
        derived.coding_mode.project_dir = None

    parent_skills = parent_dir / "skills"
    if not parent_skills.exists():
        parent_skills = parent_dir / "skill"
    parent_entries = read_skill_manifest(parent_dir).get("skills", {})
    manifest = default_workspace_manifest()
    entries = manifest["skills"]
    (target / "skills").mkdir()
    # Only requested skills are installed. Parent wins for requested conflicts.
    for name in names:
        origin = safe_skill_dir(parent_skills, name)
        if origin.exists() or name in parent_entries:
            if not (origin / "SKILL.md").is_file():
                raise HTTPException(409, f"Invalid parent skill: {name}")
            copy_skill_dir(origin, safe_skill_dir(target / "skills", name))
            entries[name] = copy.deepcopy(parent_entries.get(name) or {})
    _write_json(target / "skill.json", manifest)
    pool = SkillPoolService()
    for name in names:
        if name not in entries:
            if source == "agent_workspace":
                raise HTTPException(404, f"Parent skill not found: {name}")
            result = pool.download_to_workspace(name, target, overwrite=False)
            if not result.get("success"):
                raise HTTPException(404, f"Pool skill unavailable: {name}")
    manifest = copy.deepcopy(read_skill_manifest(target))
    for name in names:
        manifest["skills"][name]["enabled"] = True
        # Explicitly selected skills must be usable from console chat.
        manifest["skills"][name]["channels"] = ["all"]
    for name in manifest["skills"]:
        scan_skill_dir_or_raise(safe_skill_dir(target / "skills", name), name)
    _write_json(target / "skill.json", manifest)
    for directory in ("sessions", "memory"):
        (target / directory).mkdir()
    _write_json(target / "chats.json", {"version": 1, "chats": []})
    _write_json(target / "jobs.json", {"version": 1, "jobs": []})
    _write_json(target / "agent.json", derived.model_dump(mode="json"))
    _copy_policy(parent_dir, target)


@router.post("/prepare", response_model=ExecutionResponse)
async def prepare_execution(body: PrepareExecutionRequest, request: Request):
    try:
        names = list(
            dict.fromkeys(
                normalize_skill_dir_name(s.name) for s in body.skills
            )
        )
    except SkillsError as exc:
        raise HTTPException(422, str(exc)) from exc
    config = load_config()
    ref = config.agents.profiles.get(body.parent_agent_id)
    if ref is None:
        raise HTTPException(404, "Parent agent not found")
    if ref.hidden:
        raise HTTPException(422, "Parent must be an ordinary agent")
    parent = load_agent_config(body.parent_agent_id).model_copy(deep=True)
    parent_dir = Path(ref.workspace_dir).expanduser().resolve()
    manager = request.app.state.multi_agent_manager
    for _attempt in range(10):
        agent_id = "system_agent_" + uuid4().hex
        if agent_id in load_config().agents.profiles:
            continue
        target = _owned_path(agent_id, _root() / agent_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.mkdir(exist_ok=False)
            break
        except FileExistsError:
            continue
    else:
        raise HTTPException(409, "Could not allocate a unique agent workspace")
    registered = False
    build = asyncio.create_task(
        asyncio.to_thread(
            _build_workspace,
            parent,
            parent_dir,
            target,
            agent_id,
            names,
            body.source,
        )
    )
    try:
        # Wait for a cancelled request's worker before deleting its files.
        try:
            await asyncio.shield(build)
        except asyncio.CancelledError:
            try:
                await build
            except Exception:
                pass
            raise
        async with _lifecycle_lock:
            latest = load_config().model_copy(deep=True)
            current_parent = latest.agents.profiles.get(body.parent_agent_id)
            if current_parent is None or current_parent.hidden:
                raise HTTPException(
                    409, "Parent agent changed during preparation"
                )
            validate_agent_id(agent_id, set(latest.agents.profiles))
            latest.agents.profiles[agent_id] = AgentProfileRef(
                id=agent_id,
                workspace_dir=str(target),
                hidden=True,
                parent_agent_id=body.parent_agent_id,
            )
            # No await between reading and writing the shared config.
            registered = True
            save_config(latest)
            await manager.get_agent(agent_id)
        return ExecutionResponse(
            execution_id=agent_id,
            agent_id=agent_id,
            skills=[ExecutionSkill(name=name) for name in names],
        )
    except BaseException as exc:
        if registered:
            await manager.stop_agent(agent_id)
            latest = load_config().model_copy(deep=True)
            latest.agents.profiles.pop(agent_id, None)
            latest.agents.agent_order = [
                value
                for value in latest.agents.agent_order
                if value != agent_id
            ]
            save_config(latest)
        shutil.rmtree(_owned_path(agent_id, target))
        _remove_policy(target)
        if isinstance(exc, (HTTPException, asyncio.CancelledError)):
            raise
        if isinstance(exc, SkillScanError):
            raise HTTPException(422, "Skill security scan failed") from exc
        logger.exception("Failed to prepare derived agent %s", agent_id)
        raise HTTPException(
            503, "Derived agent could not be prepared"
        ) from exc


@router.delete("/{execution_id}")
async def release_execution(execution_id: str, request: Request):
    async with _lifecycle_lock:
        config = load_config()
        ref = config.agents.profiles.get(execution_id)
        if ref is None:
            return {"success": True, "execution_id": execution_id}
        if not ref.hidden or not ref.parent_agent_id:
            raise HTTPException(403, "Only derived agents can be released")
        target = _owned_path(execution_id, ref.workspace_dir)
        manager = request.app.state.multi_agent_manager
        workspace = manager.agents.get(execution_id)
        if workspace and await workspace.task_tracker.has_active_tasks():
            raise HTTPException(
                409, "Stop running tasks before releasing this agent"
            )
        # Existing HTTP requests reject disabled agents during shutdown.
        latest = load_config().model_copy(deep=True)
        latest.agents.profiles[execution_id].enabled = False
        save_config(latest)
        await manager.stop_agent(execution_id)
        # Keep disabled registration on cleanup failure so DELETE can retry.
        if target.exists():
            shutil.rmtree(_owned_path(execution_id, ref.workspace_dir))
        _remove_policy(target)
        latest = load_config().model_copy(deep=True)
        latest.agents.profiles.pop(execution_id, None)
        latest.agents.agent_order = [
            value
            for value in latest.agents.agent_order
            if value != execution_id
        ]
        if latest.agents.active_agent == execution_id:
            latest.agents.active_agent = "default"
        save_config(latest)
        return {"success": True, "execution_id": execution_id}
