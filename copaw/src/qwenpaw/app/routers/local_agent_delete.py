# -*- coding: utf-8 -*-
"""Optional, self-contained local agent deletion.

DELETE /api/agents/{agent_id}/local refuses active agents (409), releases
owned runtime resources, removes local data, then calls the existing agent
deletion endpoint. A failed cleanup keeps the registration disabled for retry.
An enabled but idle workspace is deletable; it is not started for deletion.

Plugin instances may implement ``cleanup_agent(*, agent_id, workspace_dir)``
or register an idempotent callback with ``register_agent_cleanup``. These
callbacks must await all owned tasks/connections before returning, and raise
on failure. Global plugin uninstall/shutdown hooks are deliberately not used.
Resources created outside the host's lifecycle require such a callback.

Only install(app) is imported by the host, optionally. No monkey-patching or
changes to the existing delete endpoint, CLI, or lifecycle classes are needed.
"""

from __future__ import annotations

import asyncio
import errno
import inspect
import logging
import shutil
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, FastAPI, HTTPException, Request
from starlette.responses import JSONResponse

from ...config.config import validate_agent_id
from ...config.utils import load_config, save_config
from ...constant import WORKING_DIR

logger = logging.getLogger(__name__)
router = APIRouter(tags=["local-agent-delete"])
_callbacks: dict[str, Callable[..., Any]] = {}
_cleanup_scope: ContextVar[object | None] = ContextVar(
    "local_agent_cleanup_scope", default=None,
)
_STEP_TIMEOUT = 30.0


@dataclass
class _FailureLocation:
    operation: str
    resource: str


_failure_location: ContextVar[_FailureLocation | None] = ContextVar(
    "local_agent_failure_location", default=None,
)
_OPERATIONS = {
    "quiesce": "停止 Agent 的消息入口、调度和配置监听",
    "plugins": "释放 Agent 的插件资源",
    "external_acp": "关闭外部 ACP 会话",
    "browsers": "关闭工作区浏览器",
    "lsp": "关闭工作区语言服务",
    "mcp": "释放 MCP 连接和本地配置",
    "approvals": "结束 Agent 的待处理审批",
    "workspace": "关闭 Agent 工作区服务",
}


def _locate(operation: str, resource: str | Path) -> None:
    """Track the actual resource across child tasks and worker threads."""
    location = _failure_location.get()
    if location is not None:
        location.operation = operation
        location.resource = str(resource)


def register_agent_cleanup(name: str, callback: Callable[..., Any]) -> None:
    """Register a sync/async idempotent cleanup, scoped to one agent.

    Signature: callback(*, agent_id: str, workspace_dir: Path).
    Registrations use a stable name so a plugin reload replaces its callback.
    """
    if not name or not callable(callback):
        raise ValueError("A cleanup name and callable are required")
    _callbacks[name] = callback


def unregister_agent_cleanup(name: str) -> None:
    """Remove a callback when its owner is unloaded."""
    _callbacks.pop(name, None)


@dataclass
class _Deletion:
    workspace_dir: Path
    workspace: Any = None
    completed: list[str] = field(default_factory=list)


@dataclass
class _State:
    deleting: set[str] = field(default_factory=set)
    requests: dict[str, int] = field(default_factory=dict)
    pending: dict[str, _Deletion] = field(default_factory=dict)
    tasks: set[asyncio.Task] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _state(app: Any) -> _State:
    state = getattr(app.state, "local_agent_delete_state", None)
    if state is None:
        state = _State()
        app.state.local_agent_delete_state = state
    return state


def _conflict(message: str, **details: Any) -> None:
    raise HTTPException(409, detail={"message": message, **details})


def _owned_directory(agent_id: str, config: Any) -> Path:
    """Require an exclusive workspace below the managed workspaces root."""
    raw = Path(config.agents.profiles[agent_id].workspace_dir).expanduser()
    target = raw.absolute()
    root = (Path(WORKING_DIR) / "workspaces").absolute()
    resolved = target.resolve()
    if (
        resolved == root.resolve()
        or not resolved.is_relative_to(root.resolve())
        or target != resolved
        or root != root.resolve()
    ):
        _conflict("只能删除 workspaces 下独占且未重定向的本地工作目录")
    # Reject junctions/symlinks even when they resolve to an in-root location.
    for path in (target, *target.parents):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            _conflict("工作目录路径包含符号链接或 junction，不能自动删除")
    for other_id, ref in config.agents.profiles.items():
        if other_id == agent_id:
            continue
        other = Path(ref.workspace_dir).expanduser().resolve()
        if resolved.is_relative_to(other) or other.is_relative_to(resolved):
            _conflict("工作目录与其他 Agent 重叠", other_agent_id=other_id)
    if target.exists() and not target.is_dir():
        _conflict("Agent 工作目录不是目录")
    return target


def _loaded_module(suffix: str) -> Any:
    """Inspect live resources without importing/initializing optional tools."""
    return sys.modules.get(f"qwenpaw.{suffix}")


def _coordinator(request: Request) -> Any:
    services = getattr(request.app.state, "app_services", None)
    return getattr(services, "tool_coordinator", None)


async def _ensure_idle(
    agent_id: str, request: Request, workspace: Any,
) -> None:
    manager = request.app.state.multi_agent_manager
    # There is no public lifecycle query yet; keep this compatibility read
    # here instead of changing/patching MultiAgentManager.
    if agent_id in getattr(manager, "_pending_starts", {}):
        _conflict("Agent 正在启动，请稍后重试", agent_id=agent_id)
    for task in getattr(manager, "_cleanup_tasks", ()):
        if task.done():
            continue
        frame = getattr(task.get_coro(), "cr_frame", None)
        if frame is not None and frame.f_locals.get("agent_id") == agent_id:
            _conflict("Agent 的旧实例尚未退出，请稍后重试", agent_id=agent_id)
    if _state(request.app).requests.get(agent_id, 0):
        _conflict("Agent 尚有请求未结束，请稍后重试", agent_id=agent_id)
    if workspace is not None:
        _locate("查询 Agent 的运行任务", agent_id)
        active = await workspace.task_tracker.list_active_tasks()
        if active:
            _conflict(
                "Agent 正在运行，请先停止任务再删除",
                agent_id=agent_id, running_tasks=active,
            )
    coordinator = _coordinator(request)
    if coordinator is not None:
        calls = [
            entry.ctx.tool_call_id
            for entry in coordinator.list_entries()
            if entry.ctx.agent_id == agent_id
            and (
                str(entry.status) != "completed"
                or (entry.background_task and not entry.background_task.done())
            )
        ]
        if calls:
            _conflict("Agent 有未结束的工具调用", tool_call_ids=calls)
    module = _loaded_module("agents.acp.service")
    service = module.get_acp_service(agent_id) if module else None
    if service is not None:
        # ACP currently exposes no session listing API. Use its index only
        # for inspection; release goes through close_all_sessions().
        if any(
            session.prompt_task and not session.prompt_task.done()
            for session in list(service._sessions.values())
        ):
            _conflict("Agent 有未结束的外部 ACP 调用", agent_id=agent_id)
    if workspace is not None and workspace.cron_manager is not None:
        cron = workspace.cron_manager
        scheduler = getattr(cron, "_scheduler", None)
        for executor in getattr(scheduler, "_executors", {}).values():
            if any(not future.done() for future in getattr(executor, "_pending_futures", ())):
                _conflict("Agent 有正在执行的调度任务", agent_id=agent_id)
        _locate("查询 Agent 的定时任务", agent_id)
        for job in await cron.list_jobs():
            if cron.get_state(job.id).last_status == "running":
                _conflict("Agent 有正在执行的定时任务", job_id=job.id)


class _CleanupWarnings(logging.Handler):
    """Turn legacy best-effort teardown warnings into a deletion failure."""

    def __init__(self, scope: object):
        super().__init__(logging.WARNING)
        self.scope = scope
        self.failed = False
        self.reason = ""
        self.error: BaseException | None = None

    def emit(self, record: logging.LogRecord) -> None:
        if _cleanup_scope.get() is self.scope:
            self.failed = True
            self.reason = record.getMessage()
            self.error = None
            if record.exc_info and record.exc_info[1] is not None:
                self.error = record.exc_info[1]
                self.reason += f": {_failure_reason(self.error)}"


def _failure_reason(exc: BaseException, seen: set[int] | None = None) -> str:
    """Return the error detail without exposing a traceback or step list."""
    seen = set() if seen is None else seen
    if id(exc) in seen:
        return type(exc).__name__
    seen.add(id(exc))
    if isinstance(exc, TimeoutError):
        return f"TimeoutError: 操作未在 {_STEP_TIMEOUT:g} 秒内完成"
    if isinstance(exc, BaseExceptionGroup):
        reasons = "; ".join(_failure_reason(error, seen) for error in exc.exceptions)
        return f"{type(exc).__name__}: {reasons}"
    message = str(exc).strip() or "未提供详细错误信息"
    reason = f"{type(exc).__name__}: {message}"
    if isinstance(exc, OSError):
        details = []
        for field_name in ("errno", "winerror", "filename", "filename2"):
            value = getattr(exc, field_name, None)
            if value is not None:
                details.append(f"{field_name}={value}")
        if details:
            reason += f"（{'；'.join(details)}）"
    cause = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    if cause is not None and not isinstance(cause, asyncio.CancelledError):
        cause_reason = _failure_reason(cause, seen)
        if cause_reason not in reason:
            reason += f"；底层原因：{cause_reason}"
    return reason


def _error_chain(exc: BaseException):
    pending, seen = [exc], set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        yield error
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
        cause = error.__cause__ or (
            None if error.__suppress_context__ else error.__context__
        )
        if cause is not None:
            pending.append(cause)


def _recovery_advice(exc: BaseException) -> str:
    errors = list(_error_chain(exc))
    if any(getattr(error, "winerror", None) in (32, 33) for error in errors):
        return "关闭占用所示文件的程序，确认相关进程已释放句柄后重试。"
    if any(isinstance(error, PermissionError) for error in errors):
        return "检查所示文件或目录的访问权限、只读属性及程序占用情况，解除限制后重试。"
    if any(isinstance(error, TimeoutError) for error in errors):
        return "检查对应服务是否响应、插件释放回调是否阻塞，确认资源可释放后重试。"
    if any(getattr(error, "errno", None) == errno.ENOSPC for error in errors):
        return "检查配置文件所在磁盘的剩余空间，释放空间后重试。"
    if any(isinstance(error, ConnectionError) for error in errors):
        return "检查所示服务连接和远端状态，确认连接可正常关闭后重试。"
    return "检查所示插件或服务的释放接口及对应日志，排除底层错误后重试此接口。"


def _failure_detail(agent_id: str, exc: BaseException, stage: str | None = None) -> dict:
    location = _failure_location.get()
    context = (
        f"{location.operation}失败，涉及资源：{location.resource}。"
        if location is not None else "删除本地 Agent 失败。"
    )
    if stage == "directory":
        state = "目录删除未完成，部分文件可能已经删除；Agent 注册清理尚未执行。"
    elif stage == "registration":
        state = "工作目录删除操作已完成，但 Agent 注册或当前 Agent 配置未全部更新成功。"
    else:
        state = "本次请求尚未执行工作目录删除。"
    return {
        "message": "删除 Agent 失败，请根据失败原因处理后重试",
        "agent_id": agent_id,
        "reason": (
            f"{context}系统返回：{_failure_reason(exc)}。{state}"
            f"建议：{_recovery_advice(exc)}"
        ),
        "retryable": True,
    }


@contextmanager
def _check_cleanup_logs():
    scope = object()
    token = _cleanup_scope.set(scope)
    handler = _CleanupWarnings(scope)
    # These owners log teardown exceptions rather than returning them.
    names = (
        "qwenpaw.app.workspace.service_manager", "qwenpaw.drivers.manager",
        "qwenpaw.drivers.handlers.mcp_stateful_client",
        "qwenpaw.app.channels.manager", "qwenpaw.agents.tools.browser_control",
    )
    loggers = [logging.getLogger(name) for name in names]
    for owner in loggers:
        owner.addHandler(handler)
    try:
        yield handler
    finally:
        for owner in loggers:
            owner.removeHandler(handler)
        _cleanup_scope.reset(token)


async def _invoke(callback: Callable[..., Any], **kwargs: Any) -> Any:
    if inspect.iscoroutinefunction(callback):
        return await callback(**kwargs)
    # Wait for a synchronous callback even if the caller is cancelled: its
    # worker must never outlive the deletion gate and race a retry.
    worker = asyncio.create_task(asyncio.to_thread(callback, **kwargs))
    try:
        result = await asyncio.shield(worker)
    except asyncio.CancelledError:
        await worker
        raise
    if inspect.isawaitable(result):
        return await result
    return result


async def _step(deletion: _Deletion, name: str, callback: Callable) -> None:
    _locate(_OPERATIONS.get(name, name), deletion.workspace_dir)
    with _check_cleanup_logs() as warnings:
        await asyncio.wait_for(callback(), timeout=_STEP_TIMEOUT)
    if warnings.failed:
        raise RuntimeError(warnings.reason) from warnings.error
    if name not in deletion.completed:
        deletion.completed.append(name)


async def _stop_sources(workspace: Any) -> None:
    if workspace is None:
        return
    services = workspace._service_manager.services
    for name in ("driver_config_watcher", "agent_config_watcher"):
        service = services.get(name)
        if service is not None:
            _locate("停止配置监听服务", name)
            await service.stop()
    if workspace.cron_manager is not None:
        _locate("停止 Agent 调度器", workspace.workspace_dir)
        await workspace.cron_manager.stop()
    if workspace.channel_manager is not None:
        _locate("停止 Agent 消息渠道", workspace.workspace_dir)
        await workspace.channel_manager.stop_all()


async def _cleanup_plugins(agent_id: str, request: Request, path: Path) -> None:
    callbacks = dict(_callbacks)
    loader = getattr(request.app.state, "plugin_loader", None)
    if loader is not None:
        for plugin_id, record in loader.get_all_loaded_plugins().items():
            callback = getattr(record.instance, "cleanup_agent", None)
            if callable(callback):
                callbacks[f"plugin:{plugin_id}"] = callback
    for name, callback in callbacks.items():
        _locate("调用插件资源释放回调", f"{name}（工作目录：{path}）")
        await _invoke(callback, agent_id=agent_id, workspace_dir=path)


async def _cleanup_acp(agent_id: str) -> None:
    module = _loaded_module("agents.acp.service")
    service = module.get_acp_service(agent_id) if module else None
    if service is not None:
        # close_acp_service() alone schedules cleanup without awaiting it.
        # Await sessions first, then unregister the already drained service.
        sessions = dict(service._sessions)
        session_names = ", ".join(f"{key[0]}/{key[1]}" for key in sessions)
        _locate("关闭外部 ACP 会话", f"Agent {agent_id}，会话：{session_names or '无'}")
        try:
            await service.close_all_sessions()
        except BaseException:
            # ACP pops sessions before awaiting their release. Keep failed
            # handles reachable for a retry rather than reporting success
            # against an empty index on the next DELETE.
            for key, session in sessions.items():
                service._sessions.setdefault(key, session)
            raise
        module.close_acp_service(agent_id)


async def _cleanup_browsers(path: Path) -> None:
    module = _loaded_module("agents.tools.browser_control")
    if module is not None:
        await module.stop_browsers_for_workspace_dirs([path])


async def _cleanup_lsp(path: Path) -> None:
    module = _loaded_module("agents.tools._lsp_client")
    if module is None:
        return
    # The only pool-wide API shuts down other workspaces as well. Limit the
    # adapter to clients rooted in the directory being deleted.
    with module._POOL_LOCK:
        clients = [
            (key, client) for key, client in module._POOL.items()
            if Path(key[0]).resolve().is_relative_to(path)
        ]
    for key, client in clients:
        _locate("关闭语言服务进程", f"语言：{key[1]}，项目目录：{key[0]}")
        await _invoke(client.shutdown)
        with module._POOL_LOCK:
            if module._POOL.get(key) is client:
                module._POOL.pop(key)


async def _cleanup_drivers(workspace: Any, path: Path) -> None:
    from ..mcp.config_service import MCPConfigService

    owner = workspace or type("OfflineWorkspace", (), {"workspace_dir": path})()
    manager = getattr(owner, "driver_manager", None)
    if manager is not None:
        # The bulk/delete APIs swallow handler shutdown errors and drop the
        # reference. Drain through the handler's public API first, retaining
        # the index on failure so a retry can still reach the resource.
        for name, handler in list(manager._handlers.items()):
            _locate("关闭 MCP 客户端连接", f"客户端 {name}（工作目录：{path}）")
            with _check_cleanup_logs() as warnings:
                await handler.shutdown()
            if warnings.failed:
                raise RuntimeError(f"MCP 连接关闭失败：{warnings.reason}") from warnings.error
    service = MCPConfigService(owner)
    _locate("读取 MCP 客户端配置", path / "drivers")
    for card in await service.list_cards():
        _locate("删除 MCP 客户端配置及凭据", f"客户端 {card.name}（工作目录：{path}）")
        await service.delete_client(card.name)
    if manager is not None:
        _locate("关闭外部能力驱动管理器", path)
        await manager.shutdown_all()


async def _cleanup_approvals(agent_id: str, request: Request) -> None:
    services = getattr(request.app.state, "app_services", None)
    approvals = getattr(services, "approval_coordinator", None)
    if approvals is not None:
        from ...security.tool_guard.approval import ApprovalDecision

        for pending in await approvals.get_all_pending_by_agent(agent_id):
            _locate("结束待处理审批", pending.request_id)
            await approvals.resolve_request(pending.request_id, ApprovalDecision.DENIED)


async def _stop_workspace(agent_id: str, request: Request, workspace: Any) -> None:
    if workspace is not None:
        # Retry even if a previous Workspace.stop() marked _started=False
        # after a best-effort service failure.
        await workspace._service_manager.stop_all(final=True)
    manager = request.app.state.multi_agent_manager
    if agent_id in manager.agents:
        # Evict the runtime before removing its directory. The existing
        # DELETE endpoint can subsequently unregister without touching files
        # through a still-cached workspace.
        _locate("停止并移除 Agent 运行时实例", agent_id)
        await manager.stop_agent(agent_id)


async def _forget_local_indexes(agent_id: str, request: Request, path: Path) -> None:
    coordinator = _coordinator(request)
    if coordinator is not None:
        coordinator.clear_agent_tool_timeouts(agent_id)
        # No per-agent history eviction API exists on ToolCoordinator yet.
        async with coordinator._entries_lock:
            for key, entry in list(coordinator._entries.items()):
                if entry.ctx.agent_id == agent_id:
                    coordinator._entries.pop(key, None)
    # These modules currently have no per-agent eviction API. Keep the few
    # compatibility adapters local, and never evict another agent's entries.
    module = _loaded_module("app.routers.mcp_oauth")
    if module is not None:
        for key, session in list(module._state_store.items()):
            if session.agent_id == agent_id:
                module._state_store.pop(key, None)
    module = _loaded_module("agents.tools.delegate_external_agent")
    if module is not None:
        for key in list(module._runner_states):
            if key[0] == agent_id:
                module._runner_states.pop(key, None)
    module = _loaded_module("app.channels.access_control")
    if module is not None:
        with module._stores_lock:
            module._stores.pop(str(path.resolve()), None)


def _remove_data(agent_id: str, path: Path) -> None:
    from ...governance.resource_governor import governance_policy_dir

    # Recheck immediately before destructive filesystem operations.
    current = _owned_directory(agent_id, load_config())
    if current != path:
        _conflict("Agent 工作目录已改变，请重新发起删除")
    root = Path(WORKING_DIR) / "governance"
    policy = governance_policy_dir(path, root)
    if policy.resolve() != policy or not policy.resolve().is_relative_to(root.resolve()):
        _conflict("Agent 的治理目录发生重定向，不能自动删除")
    if policy.exists():
        _locate("删除 Agent 治理策略目录", policy)
        shutil.rmtree(policy)
    if path.exists():
        _locate("删除 Agent 工作目录", path)
        shutil.rmtree(path)


async def _delete_registration(agent_id: str, request: Request) -> None:
    from .agents import delete_agent

    _locate("移除 Agent 注册并保存配置", Path(WORKING_DIR) / "config.json")
    await delete_agent(agentId=agent_id, request=request)
    config = load_config()
    if config.agents.active_agent == agent_id:
        config.agents.active_agent = "default"
        _locate("更新当前 Agent 配置", Path(WORKING_DIR) / "config.json")
        save_config(config)


async def _delete(agent_id: str, request: Request, deletion: _Deletion) -> dict:
    state = _state(request.app)
    stage = "quiesce"
    try:
        workspace = deletion.workspace
        _locate("禁用 Agent 并保存配置", Path(WORKING_DIR) / "config.json")
        config = load_config().model_copy(deep=True)
        config.agents.profiles[agent_id].enabled = False
        save_config(config)
        await _step(deletion, stage, lambda: _stop_sources(workspace))
        # Catch work admitted between the first check and disabling sources.
        await _ensure_idle(agent_id, request, workspace)
        steps = (
            ("plugins", lambda: _cleanup_plugins(agent_id, request, deletion.workspace_dir)),
            ("external_acp", lambda: _cleanup_acp(agent_id)),
            ("browsers", lambda: _cleanup_browsers(deletion.workspace_dir)),
            ("lsp", lambda: _cleanup_lsp(deletion.workspace_dir)),
            ("mcp", lambda: _cleanup_drivers(workspace, deletion.workspace_dir)),
            ("approvals", lambda: _cleanup_approvals(agent_id, request)),
            ("workspace", lambda: _stop_workspace(agent_id, request, workspace)),
        )
        for stage, callback in steps:
            await _step(deletion, stage, callback)
        stage = "indexes"
        _locate("移除 Agent 的本地索引", agent_id)
        await _forget_local_indexes(agent_id, request, deletion.workspace_dir)
        stage = "directory"
        _locate("删除 Agent 本地数据目录", deletion.workspace_dir)
        await _invoke(_remove_data, agent_id=agent_id, path=deletion.workspace_dir)
        stage = "registration"
        _locate("移除 Agent 注册", Path(WORKING_DIR) / "config.json")
        await _delete_registration(agent_id, request)
        state.pending.pop(agent_id, None)
        return {
            "success": True, "agent_id": agent_id,
            "workspace_removed": True,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "Local deletion failed: agent=%s stage=%s completed_steps=%s",
            agent_id, stage, deletion.completed,
        )
        raise HTTPException(503, detail=_failure_detail(agent_id, exc, stage)) from exc
    finally:
        state.deleting.discard(agent_id)


async def _start_delete(agent_id: str, request: Request) -> dict:
    """Return 409 while running; cleanup failures retain a disabled agent."""
    if agent_id == "default":
        raise HTTPException(400, "不能删除默认 Agent")
    try:
        validate_agent_id(agent_id, set())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    manager = getattr(request.app.state, "multi_agent_manager", None)
    if manager is None:
        raise HTTPException(503, "Agent manager is not ready")
    state = _state(request.app)
    async with state.lock:
        if agent_id in state.deleting:
            _conflict("Agent 正在删除，请勿重复操作", agent_id=agent_id)
        _locate("读取 Agent 注册配置", Path(WORKING_DIR) / "config.json")
        config = load_config()
        if agent_id not in config.agents.profiles:
            raise HTTPException(404, "Agent 不存在")
        children = [
            key for key, ref in config.agents.profiles.items()
            if ref.parent_agent_id == agent_id
        ]
        if children:
            _conflict("请先释放派生 Agent 再删除父 Agent", child_agent_ids=children)
        path = _owned_directory(agent_id, config)
        workspace = manager.agents.get(agent_id)
        deletion = state.pending.get(agent_id) or _Deletion(path, workspace)
        if deletion.workspace_dir != path:
            _conflict("上次清理后工作目录已改变，不能自动重试")
        if workspace is not None:
            deletion.workspace = workspace
        await _ensure_idle(agent_id, request, deletion.workspace)
        state.deleting.add(agent_id)
        state.pending[agent_id] = deletion
    # Keep the gate until cleanup finishes even if the HTTP client disconnects.
    task = asyncio.create_task(_delete(agent_id, request, deletion))
    state.tasks.add(task)

    def done(result: asyncio.Task) -> None:
        state.tasks.discard(result)
        if not result.cancelled():
            result.exception()

    task.add_done_callback(done)
    return await asyncio.shield(task)


@router.delete(
    "/agents/{agent_id}/local",
    summary="Delete an idle local agent and its data",
    responses={
        409: {"description": "Agent is busy, shared, or already deleting"},
        503: {"description": "Deletion failed; reason included in the response"},
    },
)
async def delete_local_agent(agent_id: str, request: Request) -> dict:
    """Expose error reasons without returning internal cleanup progress."""
    token = _failure_location.set(_FailureLocation("检查 Agent 删除条件", agent_id))
    try:
        return await _start_delete(agent_id, request)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Local deletion preparation failed: agent=%s", agent_id)
        raise HTTPException(503, detail=_failure_detail(agent_id, exc)) from exc
    finally:
        _failure_location.reset(token)


class _DeletionGate:
    """Block new agent-scoped HTTP work and account for existing requests."""

    def __init__(self, app: Any, state: _State):
        self.app, self.state = app, state

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        parts = scope["path"].strip("/").split("/")
        agent_id = None
        if len(parts) >= 3 and parts[:2] == ["api", "agents"]:
            agent_id = parts[2]
        if not agent_id:
            headers = dict(scope.get("headers", []))
            agent_id = headers.get(b"x-agent-id", b"").decode("utf-8")
        is_delete = (
            scope["method"] == "DELETE" and len(parts) == 4
            and parts[:2] == ["api", "agents"] and parts[3] == "local"
        )
        if not agent_id or is_delete:
            await self.app(scope, receive, send)
            return
        if agent_id in self.state.deleting:
            await JSONResponse(status_code=409, content={"detail": "Agent 正在删除"})(scope, receive, send)
            return
        self.state.requests[agent_id] = self.state.requests.get(agent_id, 0) + 1
        try:
            await self.app(scope, receive, send)
        finally:
            remaining = self.state.requests[agent_id] - 1
            if remaining:
                self.state.requests[agent_id] = remaining
            else:
                self.state.requests.pop(agent_id, None)


def install(app: FastAPI) -> None:
    """Optional host integration; call before serving the application."""
    if getattr(app.state, "local_agent_delete_installed", False):
        return
    app.include_router(router, prefix="/api")
    app.add_middleware(_DeletionGate, state=_state(app))
    app.state.local_agent_delete_installed = True
