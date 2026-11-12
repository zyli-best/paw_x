"""Regression coverage for host Shell execution and sandbox compatibility."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolChunk

from qwenpaw.config import context as request_context
from qwenpaw.config.shell_policy import force_direct_subprocess_sync_enabled
from qwenpaw.governance import PolicyGuardedTool
from qwenpaw.governance import resource_governor, tool_adapter
from qwenpaw.governance.audit import AuditLog
from qwenpaw.governance.policy import (
    DetectionRuleConfig,
    GovernanceAction,
    GovernanceDecision,
    GovernanceRule,
    ToolCallSpec,
    load_governance_policy,
)
from qwenpaw.sandbox import SandboxCapability, SandboxConfig, SandboxMode


@pytest.fixture(autouse=True)
def isolated_request(monkeypatch, tmp_path):
    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", "0")
    monkeypatch.setenv("QWENPAW_STRICT_SHELL_SANDBOX", "1")
    monkeypatch.setattr(
        resource_governor, "is_windows_shell_sandbox_platform", lambda: True,
    )
    monkeypatch.setattr(
        tool_adapter, "is_windows_shell_sandbox_platform", lambda: True,
    )
    tokens = [
        (request_context.current_request_context,
         request_context.current_request_context.set({})),
        (request_context.current_workspace_dir,
         request_context.current_workspace_dir.set(tmp_path / "workspace")),
        (request_context.current_session_id,
         request_context.current_session_id.set(None)),
    ]
    yield
    for variable, token in reversed(tokens):
        variable.reset(token)


@pytest.fixture
def governor(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gov = resource_governor.ResourceGovernor(
        str(workspace), governance_dir=str(tmp_path / "governance"),
    )
    gov._policy = load_governance_policy(
        str(gov._policy_dir), str(workspace),
    )
    gov._sandbox_available = True
    gov._sandbox_capability = SandboxCapability(
        supported=True, mode=SandboxMode.APPCONTAINER, reason="test backend",
    )
    config = SandboxConfig(
        mode=SandboxMode.APPCONTAINER, workspace_dir=str(workspace),
    )
    gov.compile_shell_sandbox_config = Mock(return_value=config)
    gov.compile_sandbox_config = Mock(return_value=config)
    gov.audit = Mock()
    return gov


def shell_spec(command="builder --version", **params):
    return ToolCallSpec(
        "Bash", command, "test-agent", "test-session",
        raw_params={"command": command, **params},
    )


def allow_builder(governor):
    governor.policy.add_rule(GovernanceRule(
        match="Bash(builder *)", action=GovernanceAction.ALLOW,
        reason="explicit allowance",
    ))


def wrapped_tool(governor, *, name="execute_shell_command", level="AUTO",
                 result=None, calls=None, **ctx):
    async def fake_tool(command: str = "", source: str = "",
                        sandbox_config=None):
        """Record an invocation without executing a process or code."""
        if calls is not None:
            calls.append(sandbox_config)
        return result if result is not None else ToolChunk(
            content=[TextBlock(text="completed")],
            state=ToolResultState.SUCCESS,
        )

    return PolicyGuardedTool(
        fake_tool, name=name, governor=governor,
        request_context={
            "approval_level": level, "is_coding_mode": True, **ctx,
        },
    )


@pytest.mark.parametrize("value, expected", [
    ("1", True), (" true ", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("other", False),
])
def test_shared_direct_flag_preserves_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", value)
    assert force_direct_subprocess_sync_enabled() is expected


@pytest.mark.parametrize("available", [True, False])
def test_direct_fallback_asks_without_sandbox_dependency(governor, available,
                                                        caplog):
    governor._sandbox_available = available
    with caplog.at_level("INFO"):
        decision = governor.assert_policy(shell_spec(), allow_sandbox=False)
    assert decision.action is GovernanceAction.ASK
    assert decision.source == "direct_execution"
    assert decision.execution_mode == "forced_direct_subprocess_sync"
    assert decision.sandbox_config is None
    assert "sandbox=- execution_mode=forced_direct_subprocess_sync" in caplog.text
    assert "sandbox unavailable" not in decision.reason
    governor.compile_shell_sandbox_config.assert_not_called()
    governor.compile_sandbox_config.assert_not_called()


@pytest.mark.parametrize("action", [
    GovernanceAction.ALLOW, GovernanceAction.DENY, GovernanceAction.ASK,
])
def test_direct_preserves_explicit_policy_results(governor, action):
    governor.policy.add_rule(GovernanceRule(
        match="Bash(builder *)", action=action, reason="explicit policy",
    ))
    governor._sandbox_available = False
    decision = governor.assert_policy(shell_spec(), allow_sandbox=False)
    assert decision.action is action
    assert decision.source == "user_rules"
    assert decision.reason == "explicit policy"
    assert decision.sandbox_config is None
    governor.compile_shell_sandbox_config.assert_not_called()


@pytest.mark.parametrize("explicit_allow", [True, False])
def test_direct_external_path_is_denied(governor, tmp_path, explicit_allow):
    if explicit_allow:
        allow_builder(governor)
    target = tmp_path / "outside" / "input.bin"
    decision = governor.assert_policy(
        shell_spec(f'builder "{target}"'), allow_sandbox=False,
    )
    assert decision.action is GovernanceAction.DENY
    assert decision.source == "shell_file_boundary"
    governor.compile_shell_sandbox_config.assert_not_called()


def test_direct_relative_paths_use_explicit_cwd(governor, tmp_path):
    allow_builder(governor)
    # Use an explicit relative path so the existing path-token detector applies.
    decision = governor.assert_policy(
        shell_spec("builder ./input.bin", cwd=str(tmp_path / "outside")),
        allow_sandbox=False,
    )
    assert decision.action is GovernanceAction.DENY


def test_direct_relative_cwd_uses_server_process_directory(governor, tmp_path,
                                                          monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(tmp_path)
    allow_builder(governor)
    decision = governor.assert_policy(
        shell_spec("builder ./input.bin", cwd="outside"), allow_sandbox=False,
    )
    assert decision.action is GovernanceAction.DENY


def test_direct_default_cwd_uses_session_scratch_without_creating_it(governor):
    allow_builder(governor)
    decision = governor.assert_policy(
        shell_spec("builder ../input.bin"), allow_sandbox=False,
    )
    assert decision.action is GovernanceAction.ALLOW
    assert not (governor.workspace_dir / "session_tmp").exists()


def test_direct_default_cwd_uses_external_mount(governor, tmp_path):
    mount = tmp_path / "mounted"
    mount.mkdir()
    governor.extra_mounts = [mount]
    allow_builder(governor)
    decision = governor.assert_policy(
        shell_spec("builder ../input.bin"), allow_sandbox=False,
    )
    assert decision.action is GovernanceAction.DENY
    decision = governor.assert_policy(
        shell_spec("builder ./input.bin"), allow_sandbox=False,
    )
    assert decision.action is GovernanceAction.ALLOW


@pytest.mark.parametrize("command", ["sudo builder", "rm -rf /"])
def test_direct_keeps_dangerous_command_denials(governor, command):
    decision = governor.assert_policy(shell_spec(command), allow_sandbox=False)
    assert decision.action is GovernanceAction.DENY
    assert decision.source == "shell_danger_keywords"


def test_direct_keeps_critical_detection(governor):
    allow_builder(governor)
    governor.policy.detection_rules = [DetectionRuleConfig(
        id="critical-test", tools=["execute_shell_command"],
        severity="CRITICAL", patterns=["builder"], description="critical risk",
    )]
    decision = governor.assert_policy(shell_spec(), allow_sandbox=False)
    assert decision.action is GovernanceAction.DENY
    assert decision.source == "detection_rules"


@pytest.mark.parametrize("explicit_allow", [True, False])
def test_default_sandbox_flow_is_preserved(governor, tmp_path, explicit_allow):
    if explicit_allow:
        allow_builder(governor)
    decision = governor.assert_policy(
        shell_spec(f'builder "{tmp_path / "outside" / "input.bin"}"'),
    )
    assert decision.action is (
        GovernanceAction.ALLOW if explicit_allow
        else GovernanceAction.SANDBOX_FALLBACK
    )
    assert decision.sandbox_config is governor.compile_shell_sandbox_config.return_value
    assert decision.execution_mode is None
    governor.compile_shell_sandbox_config.assert_called_once()


def test_default_unavailable_sandbox_still_asks(governor):
    governor._sandbox_available = False
    decision = governor.assert_policy(shell_spec())
    assert decision.action is GovernanceAction.ASK
    assert "sandbox unavailable" in decision.reason
    governor.compile_shell_sandbox_config.assert_not_called()


@pytest.mark.asyncio
async def test_direct_off_does_not_require_or_compile_sandbox(governor,
                                                            monkeypatch):
    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", "1")
    governor._sandbox_available = False
    tool = wrapped_tool(governor, level="OFF")
    tool._qp_sandbox_mode = True
    tool._qp_sandbox_config = object()
    decision = await tool.check_permissions({"command": "builder --version"})
    assert decision.behavior is PermissionBehavior.ALLOW
    assert tool._qp_sandbox_mode is False
    assert tool._qp_sandbox_config is None
    governor.compile_shell_sandbox_config.assert_not_called()


@pytest.mark.asyncio
async def test_default_off_still_requires_windows_sandbox(governor):
    governor._sandbox_available = False
    tool = wrapped_tool(governor, level="OFF")
    decision = await tool.check_permissions({"command": "builder --version"})
    assert decision.behavior is PermissionBehavior.DENY


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["AUTO", "OFF"])
async def test_default_mode_preserves_repl_sandbox(governor, level):
    calls = []
    tool = wrapped_tool(
        governor, name="recall_history_python", level=level, calls=calls,
    )
    decision = await tool.check_permissions({"source": "pass"})
    assert decision.behavior is PermissionBehavior.ALLOW
    assert tool._qp_sandbox_mode is True
    await tool(source="pass")
    assert calls == [governor.compile_sandbox_config.return_value]
    governor.compile_sandbox_config.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", [
    PermissionBehavior.ALLOW, PermissionBehavior.DENY,
])
async def test_direct_fallback_uses_existing_approval_and_audit(governor,
                                                             monkeypatch,
                                                             behavior):
    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", "1")
    approval = AsyncMock(return_value=PermissionDecision(
        behavior=behavior, message="test approval result",
    ))
    monkeypatch.setattr(tool_adapter, "_ask_user_approval", approval)
    previous = {"marker": "outer context"}
    request_context.set_current_request_context(previous)
    tool = wrapped_tool(governor)
    decision = await tool.check_permissions({"command": "builder --version"})
    assert decision.behavior is behavior
    assert request_context.get_current_request_context() == previous
    approval.assert_awaited_once()
    assert approval.call_args.kwargs["execution_mode"] == "forced_direct_subprocess_sync"
    governor.audit.assert_called_once()
    assert governor.audit.call_args.args[1].action is GovernanceAction.ASK
    governor.compile_shell_sandbox_config.assert_not_called()


@pytest.mark.asyncio
async def test_direct_wrapper_preserves_denied_result_without_retry(governor,
                                                                  monkeypatch):
    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", "1")
    denied = ToolChunk(
        state=ToolResultState.DENIED, content=[TextBlock(text="RX path denied")],
        metadata={"guard": "rx_path"},
    )
    calls = []
    tool = wrapped_tool(governor, result=denied, calls=calls)
    tool._qp_sandbox_mode = True
    tool._qp_sandbox_config = object()
    approval = AsyncMock()
    monkeypatch.setattr(tool_adapter, "_ask_user_approval", approval)
    result = await tool(command="builder --version", sandbox_config=object())
    assert result is denied
    assert calls == [None]
    approval.assert_not_awaited()
    governor.compile_shell_sandbox_config.assert_not_called()


@pytest.mark.parametrize("forced", [False, True])
def test_governor_start_only_probes_in_normal_mode(governor, monkeypatch,
                                                 forced):
    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", str(int(forced)))
    capability = governor.sandbox_capability
    probe = Mock(return_value=capability)
    monkeypatch.setattr(resource_governor, "probe_sandbox_support", probe)
    governor.start()
    assert governor.policy.user_rules
    assert governor.sandbox_available is not forced
    assert probe.call_count == int(not forced)
    if forced:
        assert governor.sandbox_capability.mode is SandboxMode.NONE
    else:
        assert governor.sandbox_capability is capability


@pytest.mark.parametrize("forced, available, opt_in, expected", [
    (True, True, False, False),
    (True, False, False, False),
    (True, False, True, False),
    (False, True, False, True),
    (False, False, False, False),
    (False, False, True, True),
])
def test_repl_visibility_respects_execution_mode(
    monkeypatch, forced, available, opt_in, expected,
):
    from qwenpaw.runtime.builder import AgentBuilder

    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", str(int(forced)))
    monkeypatch.setenv("QWENPAW_ALLOW_UNSANDBOXED_RECALL", str(int(opt_in)))
    config = SimpleNamespace(running=SimpleNamespace(
        light_context_config=SimpleNamespace(
            scroll_config=SimpleNamespace(allow_unsandboxed=opt_in),
        ),
    ))
    governor = SimpleNamespace(sandbox_available=available)
    assert AgentBuilder._scroll_repl_runnable(config, governor) is expected


@pytest.mark.parametrize("forced", [False, True])
def test_structured_recall_remains_registered(governor, monkeypatch, forced):
    from qwenpaw.runtime.builder import AgentBuilder

    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", str(int(forced)))
    structured, repl = object(), object()
    scroll = SimpleNamespace(recall_tool=structured, repl_tool=repl)
    builder = AgentBuilder()
    builder._wrap_tool = Mock(side_effect=lambda fn, *_: fn)
    tools = []
    builder._append_scroll_recall_tools(
        tools, scroll, None, "test-agent", {}, governor,
    )
    assert tools == ([structured] if forced else [structured, repl])


@pytest.mark.asyncio
@pytest.mark.parametrize("forced", [False, True])
async def test_non_shell_denial_skips_sandbox_retry_only_in_forced_mode(
    governor, monkeypatch, forced,
):
    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", str(int(forced)))
    denied = ToolChunk(
        state=ToolResultState.DENIED, content=[TextBlock(text="Output path denied")],
    )
    calls = []
    tool = wrapped_tool(governor, name="write_file", result=denied, calls=calls)
    approval = AsyncMock(return_value=PermissionDecision(
        behavior=PermissionBehavior.DENY, message="test refusal",
    ))
    monkeypatch.setattr(tool_adapter, "_ask_user_approval", approval)
    result = await tool(command="unused fake input")
    assert result.state is ToolResultState.DENIED
    assert calls == [None]
    assert approval.await_count == int(not forced)
    if forced:
        assert result is denied
        governor.audit.assert_not_called()


def test_forced_off_mode_does_not_prepare_repl_sandbox(governor, monkeypatch):
    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", "1")
    tool = wrapped_tool(governor, name="recall_history_python", level="OFF")
    tool_adapter._prepare_off_mode_sandbox(tool, governor)
    governor.compile_sandbox_config.assert_not_called()
    governor.compile_shell_sandbox_config.assert_not_called()


@pytest.mark.asyncio
async def test_default_sandbox_violation_keeps_approval_retry(governor,
                                                           monkeypatch):
    denied = ToolChunk(
        state=ToolResultState.DENIED,
        content=[TextBlock(text="Sandbox violation: blocked path")],
        metadata={"sandbox_violation": "blocked path"},
    )
    success = ToolChunk(state=ToolResultState.SUCCESS, content=[])
    tool = wrapped_tool(governor)
    await tool.check_permissions({"command": "builder --version"})
    call = AsyncMock(side_effect=[denied, success])
    approval = AsyncMock(return_value=PermissionDecision(
        behavior=PermissionBehavior.ALLOW, message="test approval result",
    ))
    monkeypatch.setattr(tool_adapter, "_call_function_tool_with_request_context", call)
    monkeypatch.setattr(tool_adapter, "_ask_user_approval", approval)
    result = await tool(command="builder --version")
    assert result is success
    assert call.await_count == 2
    approval.assert_awaited_once()
    assert governor.compile_shell_sandbox_config.call_count == 2
    assert call.call_args.args[2]["sandbox_config"] is governor.compile_shell_sandbox_config.return_value


@pytest.mark.asyncio
async def test_same_wrapper_clears_isolation_when_switching_to_direct(governor,
                                                                   monkeypatch):
    allow_builder(governor)
    calls = []
    tool = wrapped_tool(governor, calls=calls)
    await tool.check_permissions({"command": "builder --version"})
    await tool(command="builder --version")
    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", "1")
    await tool.check_permissions({"command": "builder --version"})
    await tool(command="builder --version")
    assert calls == [governor.compile_shell_sandbox_config.return_value, None]
    assert tool._qp_sandbox_mode is False
    assert tool._qp_sandbox_config is None
    governor.compile_shell_sandbox_config.assert_called_once()


@pytest.mark.parametrize("mode", [None, "forced_direct_subprocess_sync"])
def test_audit_records_execution_mode_without_schema_changes(tmp_path, mode):
    audit = AuditLog._create(tmp_path / "audit.db")
    try:
        audit.record("test-workspace", shell_spec(), GovernanceDecision(
            action=GovernanceAction.ALLOW, reason="test", execution_mode=mode,
        ))
        events, total = audit.query()
        assert total == 1
        assert events[0].extra == ({"execution_mode": mode} if mode else {})
    finally:
        audit.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["approved", "denied", "timeout"])
async def test_approval_result_keeps_direct_execution_mode(governor, monkeypatch,
                                                         outcome):
    from qwenpaw.app import approvals
    from qwenpaw.governance import generalize
    from qwenpaw.security.tool_guard.approval import ApprovalDecision

    pending = SimpleNamespace(request_id="request", scope=None)
    service = SimpleNamespace(
        create_pending=AsyncMock(return_value=pending),
        wait_for_approval=AsyncMock(return_value=ApprovalDecision(outcome)),
    )
    monkeypatch.setattr(approvals, "get_approval_service", lambda: service)
    monkeypatch.setattr(
        generalize, "generalize_target_for_approval",
        AsyncMock(return_value="builder --version"),
    )
    governor.add_approved_rule = AsyncMock()
    decision = await tool_adapter._ask_user_approval(
        governor, shell_spec(), {}, execution_mode="forced_direct_subprocess_sync",
    )
    approved = outcome == "approved"
    assert decision.behavior is (
        PermissionBehavior.ALLOW if approved else PermissionBehavior.DENY
    )
    recorded = governor.audit.call_args.args[1]
    assert recorded.action is (
        GovernanceAction.ALLOW if approved else GovernanceAction.DENY
    )
    assert recorded.execution_mode == "forced_direct_subprocess_sync"
    assert governor.add_approved_rule.await_count == int(approved)


@pytest.mark.asyncio
async def test_real_shell_direct_route_ignores_sandbox_without_execution(
    governor, monkeypatch,
):
    from qwenpaw.agents.tools import shell

    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", "1")
    allow_builder(governor)
    governor._sandbox_available = False
    execute = Mock(return_value=(0, "completed", ""))
    sandbox = AsyncMock()
    monkeypatch.setattr(shell, "_execute_subprocess_sync", execute)
    monkeypatch.setattr(shell, "_execute_in_sandbox", sandbox)
    monkeypatch.setattr(
        shell, "_llm_guard_windows_forced_direct_shell_command",
        AsyncMock(return_value=None),
    )
    tool = PolicyGuardedTool(
        shell.execute_shell_command, governor=governor,
        request_context={"approval_level": "AUTO", "is_coding_mode": False},
    )
    params = {"command": "builder --version", "cwd": governor.workspace_dir}
    permission = await tool.check_permissions(params)
    assert permission.behavior is PermissionBehavior.ALLOW
    result = await tool(**params)
    assert result.metadata["execution_mode"] == "forced_direct_subprocess_sync"
    execute.assert_called_once()
    sandbox.assert_not_awaited()
    governor.compile_shell_sandbox_config.assert_not_called()


@pytest.mark.asyncio
async def test_real_shell_rx_denial_does_not_enter_sandbox_retry(
    governor, monkeypatch,
):
    from qwenpaw.agents.tools import shell

    monkeypatch.setenv("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", "1")
    denied = ToolChunk(
        state=ToolResultState.DENIED, content=[TextBlock(text="RX path denied")],
    )
    execute = Mock()
    approval = AsyncMock()
    monkeypatch.setattr(shell, "_execute_subprocess_sync", execute)
    monkeypatch.setattr(shell, "_extract_shell_write_targets", lambda _: ["target"])
    monkeypatch.setattr(shell, "check_rx_mount_write_denial", lambda _: denied)
    monkeypatch.setattr(tool_adapter, "_ask_user_approval", approval)
    tool = PolicyGuardedTool(
        shell.execute_shell_command, governor=governor,
        request_context={"approval_level": "OFF", "is_coding_mode": False},
    )
    result = await tool(command="builder --version", cwd=governor.workspace_dir)
    assert result is denied
    execute.assert_not_called()
    approval.assert_not_awaited()
