"""Local deletion refuses live work and never deletes before cleanup succeeds."""

import ast
import asyncio
import builtins
import errno
import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from qwenpaw.app.routers import agents
from qwenpaw.app.routers import local_agent_delete as feature
from qwenpaw.app.mcp import config_service
from qwenpaw.config.config import AgentProfileRef, AgentsConfig, Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "workspaces" / "research"
    path.mkdir(parents=True)
    (path / "agent.json").write_text("{}", encoding="utf-8")
    (path / "credentials.yaml").write_text("secret", encoding="utf-8")
    other = tmp_path / "workspaces" / "other"
    other.mkdir()
    (other / "keep.txt").write_text("keep", encoding="utf-8")
    cfg = Config(agents=AgentsConfig(
        profiles={key: AgentProfileRef(id=key, workspace_dir=str(folder))
                  for key, folder in (("research", path), ("other", other))},
        active_agent="research", agent_order=["research", "other"],
    ))
    events = []

    def save(config):
        nonlocal cfg
        cfg = config.model_copy(deep=True)

    for module in (feature, agents):
        monkeypatch.setattr(module, "load_config", lambda: cfg)
        monkeypatch.setattr(module, "save_config", save)
    monkeypatch.setattr(feature, "WORKING_DIR", tmp_path)
    monkeypatch.setattr(feature, "_callbacks", {})

    async def released(name):
        assert path.exists(), f"directory removed before {name}"
        assert not cfg.agents.profiles["research"].enabled
        events.append(name)

    service_manager = SimpleNamespace(
        services={}, stop_all=AsyncMock(),
    )
    async def stop_services(**kwargs):
        assert kwargs == {"final": True}
        await released("workspace")
    service_manager.stop_all.side_effect = stop_services
    workspace = SimpleNamespace(
        workspace_dir=path, _service_manager=service_manager,
        task_tracker=SimpleNamespace(list_active_tasks=AsyncMock(return_value=[])),
        driver_manager=None, cron_manager=None, channel_manager=None,
        stop=AsyncMock(),
    )
    manager = SimpleNamespace(agents={"research": workspace}, _pending_starts={})

    async def stop_agent(agent_id):
        current = manager.agents.pop(agent_id, None)
        if current:
            await current.stop()
        events.append("unregister")
        return current is not None
    manager.stop_agent = AsyncMock(side_effect=stop_agent)

    class MCPService:
        def __init__(self, owner):
            assert owner.workspace_dir == path

        async def list_cards(self):
            return [SimpleNamespace(name="search")]

        async def delete_client(self, key):
            assert key == "search"
            await released("mcp")
    monkeypatch.setattr(config_service, "MCPConfigService", MCPService)
    monkeypatch.setattr(feature, "_cleanup_acp", AsyncMock())
    monkeypatch.setattr(feature, "_cleanup_browsers", AsyncMock())
    cleanup_lsp = feature._cleanup_lsp
    monkeypatch.setattr(feature, "_cleanup_lsp", AsyncMock())
    app = FastAPI()
    app.state.multi_agent_manager = manager
    app.state.plugin_loader = SimpleNamespace(get_all_loaded_plugins=lambda: {})
    app.state.app_services = SimpleNamespace(tool_coordinator=None, approval_coordinator=None)
    feature.install(app)
    return SimpleNamespace(
        app=app, cfg=lambda: cfg, save=save, path=path, other=other, events=events,
        workspace=workspace, manager=manager, released=released,
        cleanup_lsp=cleanup_lsp,
    )


async def delete(env, agent_id="research"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://test",
    ) as client:
        return await client.delete(f"/api/agents/{agent_id}/local")


@pytest.mark.asyncio
async def test_cleanup_precedes_directory_and_registration_deletion(env):
    async def plugin(**kwargs):
        assert kwargs == {"agent_id": "research", "workspace_dir": env.path}
        await env.released("plugin")
    feature.register_agent_cleanup("my-plugin", plugin)
    response = await delete(env)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "success": True, "agent_id": "research", "workspace_removed": True,
    }
    assert env.events == ["plugin", "mcp", "workspace", "unregister", "unregister"]
    assert not env.path.exists()
    assert (env.other / "keep.txt").exists()
    assert "research" not in env.cfg().agents.profiles
    assert env.cfg().agents.active_agent == "default"
    assert env.manager.stop_agent.await_count == 2


@pytest.mark.asyncio
async def test_running_agent_is_unchanged(env):
    env.workspace.task_tracker.list_active_tasks.return_value = ["chat-1"]
    response = await delete(env)
    assert response.status_code == 409
    assert response.json()["detail"]["running_tasks"] == ["chat-1"]
    assert env.path.exists()
    assert env.cfg().agents.profiles["research"].enabled
    assert not env.events


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "offloaded"])
async def test_background_tools_block_deletion_even_if_chat_is_idle(env, status):
    env.app.state.app_services.tool_coordinator = SimpleNamespace(
        list_entries=lambda: [SimpleNamespace(
            ctx=SimpleNamespace(agent_id="research", tool_call_id="call-1"),
            status=status, background_task=None,
        )],
    )
    response = await delete(env)
    assert response.status_code == 409
    assert env.cfg().agents.profiles["research"].enabled
    assert env.path.exists()


@pytest.mark.asyncio
async def test_starting_agent_is_rejected_without_cleanup(env):
    env.manager._pending_starts["research"] = asyncio.Event()
    assert (await delete(env)).status_code == 409
    assert env.cfg().agents.profiles["research"].enabled
    assert not env.events


@pytest.mark.asyncio
async def test_offline_agent_is_not_started_for_cleanup(env):
    env.manager.agents.clear()
    env.manager.get_agent = AsyncMock(side_effect=AssertionError("must not start"))
    assert (await delete(env)).status_code == 200
    env.manager.get_agent.assert_not_called()
    env.workspace._service_manager.stop_all.assert_not_called()


@pytest.mark.asyncio
async def test_plugin_failure_keeps_disabled_registration_and_directory_for_retry(env):
    cleanup = AsyncMock(side_effect=RuntimeError("still connected"))
    feature.register_agent_cleanup("owned-plugin", cleanup)
    response = await delete(env)
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "RuntimeError: still connected" in detail["reason"]
    assert "owned-plugin" in detail["reason"]
    assert str(env.path) in detail["reason"]
    assert "尚未执行工作目录删除" in detail["reason"]
    assert "建议：" in detail["reason"]
    assert set(detail) == {"message", "agent_id", "reason", "retryable"}
    assert env.path.exists()
    assert not env.cfg().agents.profiles["research"].enabled
    env.manager.stop_agent.assert_not_called()
    cleanup.side_effect = None
    assert (await delete(env)).status_code == 200
    assert cleanup.await_count == 2


@pytest.mark.asyncio
async def test_legacy_swallowed_stop_error_prevents_directory_deletion(env):
    async def stop(**kwargs):
        logging.getLogger("qwenpaw.app.workspace.service_manager").warning("Error stopping service")
    env.workspace._service_manager.stop_all.side_effect = stop
    response = await delete(env)
    assert response.status_code == 503
    assert "Error stopping service" in response.json()["detail"]["reason"]
    assert env.path.exists()
    assert "research" in env.cfg().agents.profiles


@pytest.mark.asyncio
async def test_logged_teardown_exception_includes_underlying_failure_reason(env):
    async def stop(**kwargs):
        try:
            raise PermissionError("connection handle is locked")
        except PermissionError:
            logging.getLogger("qwenpaw.app.workspace.service_manager").exception(
                "Failed to stop service",
            )
    env.workspace._service_manager.stop_all.side_effect = stop
    response = await delete(env)
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "PermissionError: connection handle is locked" in detail["reason"]
    assert set(detail) == {"message", "agent_id", "reason", "retryable"}
    assert env.path.exists()


@pytest.mark.asyncio
async def test_timeout_returns_a_specific_reason_without_step_history(env, monkeypatch):
    monkeypatch.setattr(feature, "_STEP_TIMEOUT", 0.01)

    async def plugin(**kwargs):
        await asyncio.Event().wait()
    feature.register_agent_cleanup("slow-plugin", plugin)
    response = await delete(env)
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "操作未在 0.01 秒内完成" in detail["reason"]
    assert "slow-plugin" in detail["reason"]
    assert "插件释放回调是否阻塞" in detail["reason"]
    assert set(detail) == {"message", "agent_id", "reason", "retryable"}
    assert env.path.exists()


@pytest.mark.asyncio
async def test_exception_without_message_still_returns_a_failure_reason(env):
    feature.register_agent_cleanup("broken-plugin", AsyncMock(side_effect=RuntimeError()))
    response = await delete(env)
    assert response.status_code == 503
    reason = response.json()["detail"]["reason"]
    assert "RuntimeError: 未提供详细错误信息" in reason
    assert "broken-plugin" in reason


@pytest.mark.asyncio
async def test_configuration_read_failure_returns_its_reason(env, monkeypatch):
    monkeypatch.setattr(feature, "load_config", Mock(side_effect=PermissionError("config is locked")))
    response = await delete(env)
    assert response.status_code == 503
    reason = response.json()["detail"]["reason"]
    assert "PermissionError: config is locked" in reason
    assert "读取 Agent 注册配置失败" in reason
    assert str(env.path.parents[1] / "config.json") in reason
    assert env.path.exists()
    assert env.cfg().agents.profiles["research"].enabled


@pytest.mark.asyncio
async def test_grouped_connection_failure_returns_underlying_reasons(env):
    feature.register_agent_cleanup("broken-plugin", AsyncMock(side_effect=ExceptionGroup(
        "connections failed", [PermissionError("handle locked"), OSError("socket disconnected")],
    )))
    response = await delete(env)
    assert response.status_code == 503
    reason = response.json()["detail"]["reason"]
    assert "PermissionError: handle locked" in reason
    assert "OSError: socket disconnected" in reason
    assert env.path.exists()


@pytest.mark.asyncio
async def test_directory_failure_is_retryable(env, monkeypatch):
    original = feature.shutil.rmtree
    monkeypatch.setattr(feature.shutil, "rmtree", Mock(side_effect=PermissionError("locked")))
    response = await delete(env)
    assert response.status_code == 503
    reason = response.json()["detail"]["reason"]
    assert "PermissionError: locked" in reason
    assert str(env.path) in reason
    assert "目录删除未完成" in reason
    assert "部分文件可能已经删除" in reason
    assert env.path.exists()
    assert not env.cfg().agents.profiles["research"].enabled
    monkeypatch.setattr(feature.shutil, "rmtree", original)
    assert (await delete(env)).status_code == 200


@pytest.mark.asyncio
async def test_file_error_includes_os_code_path_and_recovery_advice(env, monkeypatch):
    target = env.path / "sessions" / "chat.json"
    error = PermissionError(errno.EACCES, "file is being used", str(target))
    error.winerror = 32
    monkeypatch.setattr(feature.shutil, "rmtree", Mock(side_effect=error))
    response = await delete(env)
    assert response.status_code == 503
    reason = response.json()["detail"]["reason"]
    assert "删除 Agent 工作目录失败" in reason
    assert str(target) in reason
    assert "errno=13" in reason
    assert "winerror=32" in reason
    assert "确认相关进程已释放句柄" in reason
    assert "部分文件可能已经删除" in reason


@pytest.mark.asyncio
async def test_wrapped_connection_error_keeps_original_cause(env):
    async def plugin(**kwargs):
        try:
            raise ConnectionResetError("peer closed the connection")
        except ConnectionResetError as exc:
            raise RuntimeError("SDK close failed") from exc
    feature.register_agent_cleanup("remote-plugin", plugin)
    response = await delete(env)
    assert response.status_code == 503
    reason = response.json()["detail"]["reason"]
    assert "remote-plugin" in reason
    assert "RuntimeError: SDK close failed" in reason
    assert "ConnectionResetError: peer closed the connection" in reason
    assert "检查所示服务连接和远端状态" in reason


@pytest.mark.asyncio
async def test_registration_failure_explains_that_directory_is_already_removed(env, monkeypatch):
    monkeypatch.setattr(feature, "_delete_registration", AsyncMock(
        side_effect=PermissionError("configuration is read-only"),
    ))
    response = await delete(env)
    assert response.status_code == 503
    reason = response.json()["detail"]["reason"]
    assert "移除 Agent 注册失败" in reason
    assert "工作目录删除操作已完成" in reason
    assert "配置未全部更新成功" in reason
    assert not env.path.exists()
    assert "research" in env.cfg().agents.profiles


@pytest.mark.asyncio
async def test_disk_full_error_gives_specific_recovery_advice(env, monkeypatch):
    monkeypatch.setattr(feature, "load_config", Mock(side_effect=OSError(
        errno.ENOSPC, "No space left", str(env.path.parents[1] / "config.json"),
    )))
    response = await delete(env)
    assert response.status_code == 503
    reason = response.json()["detail"]["reason"]
    assert f"errno={errno.ENOSPC}" in reason
    assert "检查配置文件所在磁盘的剩余空间" in reason
    assert env.path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("directory", ["root", "workspaces", "outside", "shared", "parent"])
async def test_rejects_unsafe_or_shared_workspaces_before_disabling(env, tmp_path, directory):
    paths = {"root": tmp_path, "workspaces": tmp_path / "workspaces",
             "outside": tmp_path.parent / "outside", "shared": env.other,
             "parent": env.other / "nested"}
    cfg = env.cfg().model_copy(deep=True)
    cfg.agents.profiles["research"].workspace_dir = str(paths[directory])
    env.save(cfg)
    assert (await delete(env)).status_code == 409
    assert env.cfg().agents.profiles["research"].enabled
    assert env.path.exists()


@pytest.mark.asyncio
async def test_refuses_parent_with_derived_agents(env):
    cfg = env.cfg().model_copy(deep=True)
    cfg.agents.profiles["child"] = AgentProfileRef(
        id="child", workspace_dir=str(env.path.parent / "child"), parent_agent_id="research",
    )
    env.save(cfg)
    response = await delete(env)
    assert response.status_code == 409
    assert response.json()["detail"]["child_agent_ids"] == ["child"]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id,status", [("default", 400), ("absent", 404)])
async def test_default_and_missing_agent(env, agent_id, status):
    assert (await delete(env, agent_id)).status_code == status
    assert env.path.exists()


@pytest.mark.asyncio
async def test_duplicate_delete_and_new_requests_are_blocked_during_cleanup(env):
    started, finish = asyncio.Event(), asyncio.Event()

    async def plugin(**kwargs):
        started.set()
        await finish.wait()
    feature.register_agent_cleanup("slow-plugin", plugin)

    @env.app.get("/api/agents/{agent_id}/probe")
    async def probe(agent_id: str):
        return {"id": agent_id}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://test",
    ) as client:
        first = asyncio.create_task(client.delete("/api/agents/research/local"))
        await asyncio.wait_for(started.wait(), 2)
        assert (await client.delete("/api/agents/research/local")).status_code == 409
        assert (await client.get("/api/agents/research/probe")).status_code == 409
        assert (await client.get("/api/agents/other/probe")).status_code == 200
        finish.set()
        assert (await first).status_code == 200


@pytest.mark.asyncio
async def test_plugin_instance_cleanup_does_not_uninstall_shared_plugin(env):
    instance = SimpleNamespace(cleanup_agent=AsyncMock())
    env.app.state.plugin_loader = SimpleNamespace(
        get_all_loaded_plugins=lambda: {"shared": SimpleNamespace(instance=instance)},
        unload_plugin=AsyncMock(),
    )
    assert (await delete(env)).status_code == 200
    instance.cleanup_agent.assert_awaited_once_with(agent_id="research", workspace_dir=env.path)
    env.app.state.plugin_loader.unload_plugin.assert_not_called()


@pytest.mark.asyncio
async def test_pending_approvals_are_resolved_before_files_removed(env):
    approvals = SimpleNamespace(
        get_all_pending_by_agent=AsyncMock(return_value=[SimpleNamespace(request_id="approval-1")]),
        resolve_request=AsyncMock(),
    )
    env.app.state.app_services.approval_coordinator = approvals
    assert (await delete(env)).status_code == 200
    approvals.resolve_request.assert_awaited_once()
    assert str(approvals.resolve_request.call_args.args[1]) in {"ApprovalDecision.DENIED", "denied"}


@pytest.mark.asyncio
async def test_acp_cleanup_is_awaited_before_unregistering(monkeypatch):
    events = []

    async def close():
        await asyncio.sleep(0)
        events.append("closed")
    service = SimpleNamespace(close_all_sessions=close, _sessions={})
    module = SimpleNamespace(
        get_acp_service=lambda agent_id: service,
        close_acp_service=lambda agent_id: events.append("unregistered"),
    )
    monkeypatch.setitem(sys.modules, "qwenpaw.agents.acp.service", module)
    await feature._cleanup_acp("research")
    assert events == ["closed", "unregistered"]


@pytest.mark.asyncio
async def test_mcp_shutdown_failure_is_not_hidden_by_best_effort_delete(env):
    handler = SimpleNamespace(shutdown=AsyncMock(side_effect=RuntimeError("connection alive")))
    env.workspace.driver_manager = SimpleNamespace(_handlers={"search": handler})
    response = await delete(env)
    assert response.status_code == 503
    reason = response.json()["detail"]["reason"]
    assert "RuntimeError: connection alive" in reason
    assert "MCP 客户端连接" in reason
    assert "search" in reason
    assert str(env.path) in reason
    assert env.path.exists()
    handler.shutdown.side_effect = None
    env.workspace.driver_manager.shutdown_all = AsyncMock()
    assert (await delete(env)).status_code == 200


@pytest.mark.asyncio
async def test_runtime_is_evicted_before_directory_removal(env, monkeypatch):
    original = feature._remove_data

    def remove(**kwargs):
        assert "research" not in env.manager.agents
        original(**kwargs)
    monkeypatch.setattr(feature, "_remove_data", remove)
    assert (await delete(env)).status_code == 200


@pytest.mark.asyncio
async def test_completed_tool_records_are_evicted_only_for_deleted_agent(env):
    own = SimpleNamespace(ctx=SimpleNamespace(agent_id="research", tool_call_id="own"),
                          status="completed", background_task=None)
    other = SimpleNamespace(ctx=SimpleNamespace(agent_id="other", tool_call_id="other"),
                            status="running", background_task=None)
    entries = {"own": own, "other": other}
    coordinator = SimpleNamespace(
        _entries=entries, _entries_lock=asyncio.Lock(),
        list_entries=lambda: list(entries.values()), clear_agent_tool_timeouts=Mock(),
    )
    env.app.state.app_services.tool_coordinator = coordinator
    assert (await delete(env)).status_code == 200
    assert entries == {"other": other}
    coordinator.clear_agent_tool_timeouts.assert_called_once_with("research")


@pytest.mark.asyncio
async def test_client_cancellation_does_not_release_gate_before_cleanup(env):
    started, finish = asyncio.Event(), asyncio.Event()

    async def plugin(**kwargs):
        started.set()
        await finish.wait()
    feature.register_agent_cleanup("slow", plugin)
    caller = asyncio.create_task(delete(env))
    await asyncio.wait_for(started.wait(), 2)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    state = feature._state(env.app)
    assert "research" in state.deleting
    assert env.path.exists()
    finish.set()
    await asyncio.wait_for(asyncio.gather(*state.tasks), 2)
    assert "research" not in state.deleting
    assert not env.path.exists()


@pytest.mark.asyncio
async def test_failed_acp_cleanup_retains_handles_for_retry(monkeypatch):
    session = object()
    service = SimpleNamespace(_sessions={("chat", "runner"): session})

    async def fail():
        service._sessions.clear()
        raise RuntimeError("process still open")
    service.close_all_sessions = fail
    module = SimpleNamespace(get_acp_service=lambda agent_id: service, close_acp_service=Mock())
    monkeypatch.setitem(sys.modules, "qwenpaw.agents.acp.service", module)
    with pytest.raises(RuntimeError, match="process still open"):
        await feature._cleanup_acp("research")
    assert service._sessions == {("chat", "runner"): session}
    module.close_acp_service.assert_not_called()


@pytest.mark.asyncio
async def test_scoped_lsp_cleanup_preserves_other_projects(env, monkeypatch):
    own, other = Mock(), Mock()
    pool = {(str(env.path), "python"): own, (str(env.other), "python"): other}
    module = SimpleNamespace(_POOL=pool, _POOL_LOCK=threading.Lock())
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, "qwenpaw.agents.tools._lsp_client", module)
        await env.cleanup_lsp(env.path)
    own.shutdown.assert_called_once()
    other.shutdown.assert_not_called()
    assert list(pool) == [(str(env.other), "python")]


@pytest.mark.asyncio
async def test_real_offline_mcp_api_removes_cards_and_oauth_credentials(tmp_path):
    from qwenpaw.app.mcp.schemas import MCPClientCreateRequest
    from qwenpaw.drivers.adapters.mcp_console import mcp_oauth_credential_ref
    from qwenpaw.drivers.credentials.types import CredentialRecord

    owner = SimpleNamespace(workspace_dir=tmp_path)
    service = config_service.MCPConfigService(owner)
    await service.create_client("search", MCPClientCreateRequest(
        name="Search", command="never-started", enabled=False,
    ))
    store = service._driver_config.credential_store
    await store.put(CredentialRecord(
        ref=mcp_oauth_credential_ref("search"), kind="oauth_auth_code",
        public={"scope": "test"}, secrets={},
    ))
    assert await store.list_refs()
    await feature._cleanup_drivers(owner, tmp_path)
    assert await service.list_cards() == []
    assert await store.list_refs() == []
    assert tmp_path.exists()


@pytest.mark.asyncio
async def test_running_scheduler_job_blocks_deletion(env):
    future = asyncio.get_running_loop().create_future()
    env.workspace.cron_manager = SimpleNamespace(
        _scheduler=SimpleNamespace(_executors={
            "default": SimpleNamespace(_pending_futures={future}),
        }),
        list_jobs=AsyncMock(return_value=[]),
    )
    try:
        assert (await delete(env)).status_code == 409
        assert env.cfg().agents.profiles["research"].enabled
    finally:
        future.cancel()


@pytest.mark.asyncio
async def test_old_workspace_cleanup_blocks_only_its_own_agent(env):
    finish = asyncio.Event()

    async def delayed_cleanup(agent_id):
        await finish.wait()
        return agent_id
    task = asyncio.create_task(delayed_cleanup("research"))
    env.manager._cleanup_tasks = {task}
    try:
        assert (await delete(env)).status_code == 409
        assert env.cfg().agents.profiles["research"].enabled
    finally:
        finish.set()
        await task


def test_optional_bootstrap_allows_removing_feature_file(monkeypatch):
    source = Path(feature.__file__).parents[1] / "_app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    block = next(node for node in tree.body if isinstance(node, ast.Try) and any(
        isinstance(item, ast.ImportFrom) and item.module == "routers.local_agent_delete"
        for item in node.body
    ))
    original = builtins.__import__

    def without_feature(name, *args, **kwargs):
        if name == "routers.local_agent_delete":
            raise ModuleNotFoundError(name="qwenpaw.app.routers.local_agent_delete")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_feature)
    exec(compile(ast.Module(body=[block], type_ignores=[]), str(source), "exec"),
         {"__package__": "qwenpaw.app", "app": FastAPI()})
