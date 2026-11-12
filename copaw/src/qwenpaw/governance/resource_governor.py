# -*- coding: utf-8 -*-
"""Resource Governor — Policy evaluation + audit logging +
sandbox config compilation.

Core responsibilities: policy evaluation, audit recording, dynamic rule
addition, sandbox config compilation.
"""

from __future__ import annotations
import hashlib
import logging
import ntpath
import os
import posixpath
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from .policy import (
    GovernancePolicy,
    GovernanceRule,
    GovernanceAction,
    GovernanceDecision,
    ToolCallSpec,
    DEFAULT_SANDBOX_DENY_PATHS,
    FILE_ACCESS_TOOLS,
    load_governance_policy,
    save_governance_policy,
    _parse_match,
)
from .audit import AuditLog
from ..constant import WORKING_DIR
from ..constant import EnvVarLoader
from ..config.shell_policy import force_direct_subprocess_sync_enabled

from ..sandbox import (
    SandboxCapability,
    SandboxConfig,
    SandboxMode,
    MountSpec,
    probe_sandbox_support,
    detect_platform_mode,
)

logger = logging.getLogger(__name__)

_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WIN_UNC_RE = re.compile(r"^\\\\[^\\/?%*:|\"<>]+[\\/][^\\/?%*:|\"<>]+")
_WIN_DRIVE_FILE_NETLOC_RE = re.compile(r"^[A-Za-z]:$")
_SHELL_REDIRECT_OPERATORS = frozenset(
    {">", ">>", "1>", "1>>", "2>", "2>>", "&>", "&>>", "<"},
)
_REDIRECT_OPS_BY_LEN = tuple(
    sorted(_SHELL_REDIRECT_OPERATORS, key=len, reverse=True),
)
_SHELL_FILE_ARG_COMMANDS = frozenset(
    {
        "cat",
        "type",
        "more",
        "less",
        "head",
        "tail",
        "grep",
        "rg",
        "sed",
        "awk",
        "cp",
        "copy",
        "mv",
        "move",
        "rm",
        "del",
        "erase",
        "touch",
        "mkdir",
        "rmdir",
        "ls",
        "dir",
        "python",
        "python3",
        "node",
        "powershell",
        "pwsh",
    },
)
_WINDOWS_SWITCH_RE = re.compile(r"^/[A-Za-z?](?::.*)?$", re.IGNORECASE)


def strict_shell_sandbox_enabled() -> bool:
    """Whether shell commands must remain sandboxed when possible."""
    return EnvVarLoader.get_bool("QWENPAW_STRICT_SHELL_SANDBOX", True)


def runtime_auto_discover_enabled() -> bool:
    """Whether Windows shell runtime roots are discovered automatically."""
    return EnvVarLoader.get_bool("QWENPAW_RUNTIME_AUTO_DISCOVER", True)


def windows_path_rx_enabled() -> bool:
    """Whether Windows shell sandbox should grant RX to PATH directories."""
    return EnvVarLoader.get_bool("QWENPAW_WINDOWS_SANDBOX_PATH_RX", False)


def is_windows_shell_sandbox_platform() -> bool:
    """Whether Windows shell-specific sandbox hardening should apply."""
    return sys.platform == "win32"


def _is_windows_abs(path: str) -> bool:
    return bool(_WIN_ABS_RE.match(path) or _WIN_UNC_RE.match(path))


def _workspace_looks_windows(workspace_dir: str) -> bool:
    return _is_windows_abs(workspace_dir) or "\\" in workspace_dir


def _local_path_from_browser_target(target: str) -> str | None:
    """Extract local filesystem path from Browser target when applicable."""
    text = str(target or "").strip()
    if not text:
        return None
    if _is_windows_abs(text) or _WIN_UNC_RE.match(text):
        return text
    parsed = urlparse(text)
    if parsed.scheme.lower() != "file":
        return None
    netloc = unquote(parsed.netloc or "")
    path = unquote(parsed.path or "")
    if _WIN_DRIVE_FILE_NETLOC_RE.match(netloc):
        return netloc + path.replace("/", "\\")
    if netloc:
        return "\\\\" + netloc + path.replace("/", "\\")
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    if _is_windows_abs(path):
        return path.replace("/", "\\")
    return path


def _canonical_path(raw_path: str, workspace_dir: str) -> str:
    """Resolve *raw_path* relative to the agent workspace for comparison."""
    raw = os.path.expandvars(os.path.expanduser(str(raw_path).strip()))
    if not raw:
        raw = "."

    if _is_windows_abs(raw) or (
        _workspace_looks_windows(workspace_dir) and not raw.startswith("/")
    ):
        base = workspace_dir
        candidate = raw if _is_windows_abs(raw) else ntpath.join(base, raw)
        return ntpath.normpath(candidate).replace("\\", "/").lower()

    candidate = (
        raw
        if raw.startswith("/")
        else posixpath.join(workspace_dir.replace("\\", "/"), raw)
    )
    return posixpath.normpath(candidate)


def _is_under_any_root(path: str, roots: list[str]) -> bool:
    for root in roots:
        trimmed = root.rstrip("/\\")
        if path == trimmed or path.startswith(trimmed + "/"):
            return True
    return False


def _append_mount(
    mounts: list[MountSpec],
    path: str,
    *,
    writable: bool,
    executable: bool,
) -> None:
    """Append or widen a mount without duplicating the same path."""
    norm = os.path.normcase(os.path.normpath(path))
    for mount in mounts:
        if os.path.normcase(os.path.normpath(mount.path)) != norm:
            continue
        mount.writable = mount.writable or writable
        mount.executable = mount.executable or executable
        return
    mounts.append(
        MountSpec(path=path, writable=writable, executable=executable),
    )


def _dedupe_paths(paths: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        try:
            path = os.path.normpath(os.path.expanduser(str(raw)))
        except (TypeError, OSError):
            continue
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _path_env_key() -> str:
    for key in os.environ:
        if key.upper() == "PATH":
            return key
    return "PATH"


def _read_windows_user_path_env() -> str:
    """Read the current user's Windows Path from HKCU\\Environment."""
    if sys.platform != "win32":
        return ""
    try:
        import winreg
    except ImportError:
        return ""

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, "Path")
    except OSError:
        return ""
    return str(value or "")


def _windows_path_values() -> list[str]:
    path_key = _path_env_key()
    return [
        os.environ.get(path_key, ""),
        _read_windows_user_path_env(),
    ]


def _normalize_windows_path_rx_roots() -> list[str]:
    """Return existing absolute non-root directories from Windows PATHs."""
    roots: list[str] = []
    seen: set[str] = set()

    for path_value in _windows_path_values():
        for raw in path_value.split(os.pathsep):
            raw = raw.strip().strip('"')
            if not raw:
                continue

            expanded = os.path.expandvars(os.path.expanduser(raw))
            try:
                path = Path(expanded).resolve(strict=False)
            except (OSError, RuntimeError):
                continue

            if not path.is_absolute() or not path.is_dir():
                continue
            if path.parent == path:
                continue

            key = os.path.normcase(os.path.normpath(str(path)))
            if key in seen:
                continue

            seen.add(key)
            roots.append(str(path))

    return roots


def _merge_env_path(*path_values: str) -> str:
    """Merge PATH-like strings while preserving order and removing dupes."""
    parts: list[str] = []
    seen: set[str] = set()

    for value in path_values:
        for raw in value.split(os.pathsep):
            raw = raw.strip().strip('"')
            if not raw:
                continue
            expanded = os.path.expandvars(os.path.expanduser(raw))
            key = os.path.normcase(os.path.normpath(expanded))
            if key in seen:
                continue
            seen.add(key)
            parts.append(expanded)

    return os.pathsep.join(parts)


def _extract_shell_cwd(raw_params: dict[str, Any]) -> str | None:
    raw = raw_params.get("cwd") if isinstance(raw_params, dict) else None
    if raw is None:
        return None
    try:
        value = os.fspath(raw).strip()
    except (TypeError, AttributeError):
        return None
    return value or None


def _strip_shell_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _looks_like_path_token(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    if _WINDOWS_SWITCH_RE.match(token):
        return False
    lowered = token.lower()
    if lowered.startswith(("http://", "https://", "ftp://", "data:")):
        return False
    return (
        token.startswith(("~", "/", "./", "../", ".\\", "..\\", "\\"))
        or _is_windows_abs(token)
        or "/" in token
        or "\\" in token
    )


def _command_basename(token: str) -> str:
    cleaned = _strip_shell_quotes(token).replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1].lower()


def _extract_attached_redirect_path(token: str) -> str | None:
    for op in _REDIRECT_OPS_BY_LEN:
        if token.startswith(op) and len(token) > len(op):
            return token[len(op) :]
    return None


def _extract_shell_path_candidates(command: str) -> list[str]:
    """Extract explicit file path candidates from a shell command."""
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        tokens = command.split()

    candidates: list[str] = []
    command_mode = False
    expect_file_arg = False

    for i, token in enumerate(tokens):
        token = _strip_shell_quotes(token.strip())
        if not token:
            continue

        if token in {"|", "||", "&&", ";", "&"}:
            command_mode = False
            expect_file_arg = False
            continue

        if token in _SHELL_REDIRECT_OPERATORS:
            if i + 1 < len(tokens):
                candidates.append(_strip_shell_quotes(tokens[i + 1]))
            expect_file_arg = False
            continue

        redirect_path = _extract_attached_redirect_path(token)
        if redirect_path:
            candidates.append(_strip_shell_quotes(redirect_path))
            continue

        if not command_mode:
            command_mode = True
            expect_file_arg = _command_basename(token) in _SHELL_FILE_ARG_COMMANDS
            continue

        if token.startswith("-"):
            continue

        if expect_file_arg or _looks_like_path_token(token):
            candidates.append(token)

    deduped: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in deduped:
            deduped.append(candidate)
    return deduped


def governance_policy_dir(
    workspace_dir: str | Path,
    governance_dir: str | Path | None = None,
) -> Path:
    """Return the same per-workspace policy path for runtime and lifecycle."""
    workspace = Path(workspace_dir)
    root = (
        Path(governance_dir)
        if governance_dir is not None
        else WORKING_DIR / "governance"
    )
    digest = hashlib.sha256(
        str(workspace.resolve()).encode("utf-8"),
    ).hexdigest()[:12]
    return root / f"{workspace.name}_{digest}"


class ResourceGovernor:
    """ResourceGovernor — core of policy and audit.

    Responsibilities:
        1. Policy evaluation: assert_policy(tool_call) → GovernanceDecision
        2. Audit logging: audit(tool_call, decision) records an audit log entry
        3. Sandbox config compilation: compile_sandbox_config() → SandboxConfig
        4. Dynamic rule addition: add_rule(...) after user approval

    NOT responsible for (TBD):
        - sandbox creation/destruction → managed by orchestration layer
        - Runtime/Agent scheduling → TBD
    """

    def __init__(
        self,
        workspace_dir: str,
        governance_dir: Optional[str] = None,
        coding_project_dir: Optional[str] = None,
        extra_mounts: Optional[list[str]] = None,
        extra_executable_roots: Optional[list[str]] = None,
        extra_rx_mounts: Optional[list[str]] = None,
    ):
        self.workspace_dir = Path(workspace_dir)
        self.extra_mounts = self._normalize_extra_mounts(extra_mounts)
        self.extra_rx_mounts = self._normalize_extra_mounts(extra_rx_mounts)
        self.extra_executable_roots = self._normalize_extra_executable_roots(
            extra_executable_roots,
        )
        # Coding project dir (Coding Mode). Falls back to the workspace
        # when unset so the CODING_PROJECT_DIR policy placeholder always
        # resolves to a concrete path.
        self.coding_project_dir = Path(
            coding_project_dir or workspace_dir,
        )
        # Policy is stored outside the workspace to prevent agent tampering.
        # Use ``<basename>_<hash>`` so two workspaces with the same basename
        # but different absolute paths (e.g. ``/Users/a/project`` vs
        # ``/Users/b/project``) do not share the same policy directory.
        if governance_dir is not None:
            self._governance_dir = Path(governance_dir)
        else:
            self._governance_dir = WORKING_DIR / "governance"
        self._policy_dir = governance_policy_dir(
            self.workspace_dir,
            self._governance_dir,
        )
        self._policy: Optional[GovernancePolicy] = None
        self._sandbox_available: bool = False
        self._sandbox_capability: Optional[SandboxCapability] = None

    @staticmethod
    def _normalize_extra_mounts(raw_mounts: Any) -> list[Path]:
        """Return valid, deduped request-scoped external mount dirs."""
        if not isinstance(raw_mounts, (list, tuple)):
            return []

        mounts: list[Path] = []
        seen: set[str] = set()
        for raw in raw_mounts:
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                path = Path(raw.strip()).expanduser().resolve(strict=False)
            except (OSError, RuntimeError):
                continue
            if not path.is_absolute() or not path.is_dir():
                continue
            key = os.path.normcase(os.path.normpath(str(path)))
            if key in seen:
                continue
            seen.add(key)
            mounts.append(path)
        return mounts

    @staticmethod
    def _normalize_extra_executable_roots(raw_roots: Any) -> list[Path]:
        """Return valid, deduped request-scoped executable roots."""
        if not isinstance(raw_roots, (list, tuple)):
            return []

        roots: list[Path] = []
        seen: set[str] = set()
        for raw in raw_roots:
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                path = Path(raw.strip()).expanduser().resolve(strict=False)
            except (OSError, RuntimeError):
                continue
            if (
                not path.is_absolute()
                or not path.is_dir()
                or path.parent == path
            ):
                continue
            key = os.path.normcase(os.path.normpath(str(path)))
            if key in seen:
                continue
            seen.add(key)
            roots.append(path)
        return roots

    # ------------------------------------------------------------------
    # Lifecycle (kept but not expanded, overlaps with runtime)
    # ------------------------------------------------------------------

    @property
    def sandbox_available(self) -> bool:
        """Whether sandbox isolation is available in this execution mode.

        Readable after start().
        """
        return self._sandbox_available

    @property
    def sandbox_capability(self) -> Optional[SandboxCapability]:
        """Capability or disabled-mode result from start()."""
        return self._sandbox_capability

    def start(self) -> None:
        """Load policy; probe isolation only outside forced-subprocess mode."""
        self._policy_dir.mkdir(parents=True, exist_ok=True)
        self._policy = load_governance_policy(
            str(self._policy_dir),
            str(self.workspace_dir),
            str(self.coding_project_dir),
        )

        if force_direct_subprocess_sync_enabled():
            self._sandbox_capability = SandboxCapability(
                supported=False,
                mode=SandboxMode.NONE,
                reason="Sandbox disabled for forced subprocess execution",
            )
            self._sandbox_available = False
            return

        self._sandbox_capability = probe_sandbox_support()
        self._sandbox_available = self._sandbox_capability.supported
        if not self._sandbox_available:
            logger.warning(
                "ResourceGovernor: sandbox not available — %s. "
                "SANDBOX_FALLBACK will escalate to ASK.",
                self._sandbox_capability.reason,
            )

    def stop(self) -> None:
        """Persist policy (if modified) and close the audit log."""
        if self._policy and self._policy.rules:
            try:
                save_governance_policy(
                    self._policy,
                    str(self._policy_dir),
                    str(self.workspace_dir),
                    str(self.coding_project_dir),
                )
            except Exception:
                logger.exception(
                    "ResourceGovernor.stop: failed to persist policy.yaml",
                )
        # Close the global AuditLog: triggers the deferred VACUUM and
        # releases the SQLite handle. Without this, audit.db is only
        # closed on interpreter exit (best-effort) which is fragile
        # under supervised restarts and may leak WAL frames.
        try:
            self.audit_log.close()
        except Exception:
            logger.exception(
                "ResourceGovernor.stop: failed to close AuditLog",
            )

    # ------------------------------------------------------------------
    # Core interface 1: Policy evaluation
    # ------------------------------------------------------------------

    def assert_policy(
        self,
        tc_spec: ToolCallSpec,
        *,
        allow_sandbox: bool = True,
    ) -> GovernanceDecision:
        """Evaluate policy for a tool call.

        Flow:
            1. policy.evaluate(tc_spec) → GovernanceDecision
            2. Sandbox degradation: if SANDBOX_FALLBACK and sandbox
               unavailable → escalate to ASK
            3. If SANDBOX_FALLBACK → compile sandbox config and attach
            4. Log the governance decision (observability)
            5. Return decision (does NOT record audit)

        Returns GovernanceDecision:
            ALLOW            → explicit resource tool executes directly;
                               bash tool executes with sandbox
                               pre-authorization
            DENY             → rejected
            ASK              → ask user
            SANDBOX_FALLBACK → bash tool with no rule match, sandbox fallback

        ``allow_sandbox=False`` keeps policy and boundary evaluation but
        skips isolation setup. A shell fallback then requires host-execution
        approval. Platform capability probing remains available to other tools.
        """
        decision = self.policy.evaluate(tc_spec)
        if (
            decision.action is GovernanceAction.DENY
            and decision.source == "file_boundary"
        ):
            if self._file_tool_in_extra_mount(tc_spec):
                decision = GovernanceDecision(
                    action=GovernanceAction.ALLOW,
                    reason="Session sandbox_mounts",
                    findings=decision.findings,
                    source="request_context.sandbox_mounts",
                )
            elif self._file_tool_in_extra_rx_mount(tc_spec):
                decision = GovernanceDecision(
                    action=GovernanceAction.ALLOW,
                    reason="Session sandbox_rx_mounts (read/execute only)",
                    findings=decision.findings,
                    source="request_context.sandbox_rx_mounts",
                )

        direct_shell = tc_spec.tool_name == "Bash" and not allow_sandbox
        shell_sandbox_enforced = False
        if allow_sandbox:
            shell_sandbox_enforced = self._attach_shell_sandbox_if_required(
                tc_spec,
                decision,
            )
        shell_cwd = None
        if direct_shell:
            shell_cwd = _extract_shell_cwd(tc_spec.raw_params)
            if shell_cwd is None:
                from ..config.context import describe_default_relative_file_dir

                shell_cwd = str(
                    describe_default_relative_file_dir(
                        workspace_dir=self.workspace_dir,
                        session_id=tc_spec.session_id or None,
                        sandbox_mounts=self.extra_mounts,
                    ).path,
                )
            # Explicit relative cwd is interpreted by subprocess relative to
            # the server process, rather than the agent workspace.
            shell_cwd = os.path.abspath(shell_cwd)
        shell_boundary = self._check_shell_file_boundary(
            tc_spec,
            sandbox_enforced=shell_sandbox_enforced,
            cwd=shell_cwd,
        )
        if (
            shell_boundary is not None
            and decision.action
            in (GovernanceAction.ALLOW, GovernanceAction.SANDBOX_FALLBACK)
        ):
            decision = shell_boundary

        browser_file_boundary = self._check_browser_file_boundary(tc_spec)
        if browser_file_boundary is not None:
            decision = browser_file_boundary

        # Early probe degradation: if sandbox is unavailable, escalate
        # SANDBOX_FALLBACK to ASK. In Windows strict-shell mode, Bash ALLOW
        # also requires a sandbox, so an unavailable sandbox becomes ASK too.
        if (
            allow_sandbox
            and (
                decision.action is GovernanceAction.SANDBOX_FALLBACK
                or (
                    tc_spec.tool_name == "Bash"
                    and is_windows_shell_sandbox_platform()
                    and strict_shell_sandbox_enabled()
                    and decision.action is GovernanceAction.ALLOW
                )
            )
            and not self._sandbox_available
        ):
            logger.info(
                "ResourceGovernor: sandbox unavailable, escalating "
                "sandboxed shell execution to ASK for tool '%s'",
                tc_spec.tool_name,
            )
            decision = GovernanceDecision(
                action=GovernanceAction.ASK,
                reason=(
                    f"sandbox unavailable "
                    f"({self._sandbox_capability.reason}), ask user"
                ),
            )

        # compile sandbox config
        if (
            allow_sandbox
            and decision.action is GovernanceAction.SANDBOX_FALLBACK
        ):
            if tc_spec.tool_name == "Bash":
                if decision.sandbox_config is None:
                    decision.sandbox_config = (
                        self.compile_shell_sandbox_config(tc_spec)
                    )
            else:
                decision.sandbox_config = self.compile_sandbox_config(tc_spec)
        elif (
            allow_sandbox
            and tc_spec.tool_name == "Bash"
            and is_windows_shell_sandbox_platform()
            and strict_shell_sandbox_enabled()
            and decision.action is GovernanceAction.ALLOW
        ):
            if decision.sandbox_config is None:
                decision.sandbox_config = self.compile_shell_sandbox_config(
                    tc_spec,
                )

        if not allow_sandbox:
            decision.sandbox_config = None
            if decision.action is GovernanceAction.SANDBOX_FALLBACK:
                decision = GovernanceDecision(
                    action=GovernanceAction.ASK,
                    reason="Direct host execution requires user approval",
                    findings=decision.findings,
                    source="direct_execution",
                )
            if direct_shell:
                decision.execution_mode = "forced_direct_subprocess_sync"

        # Observability: log every governance decision so operators can
        # trace policy evaluation results without querying audit.db.
        # ``target`` is truncated to keep log lines bounded.
        target_repr = (tc_spec.target or "")[:120]
        # Sandbox backend planned for this call: the compiled
        # config's mode (bubblewrap/landlock/...), or "-" when the
        # decision does not route through a sandbox.
        sandbox_mode = (
            decision.sandbox_config.mode.value
            if decision.sandbox_config is not None
            else "-"
        )
        logger.info(
            "governance decision: tool=%s target=%r action=%s source=%s "
            "sandbox=%s execution_mode=%s reason=%s",
            tc_spec.tool_name,
            target_repr,
            decision.action.value,
            decision.source,
            sandbox_mode,
            decision.execution_mode or "-",
            decision.reason,
        )
        return decision

    # ------------------------------------------------------------------
    # Core interface 2: Audit logging
    # ------------------------------------------------------------------

    def audit(
        self,
        tc_spec: ToolCallSpec,
        decision: GovernanceDecision,
    ) -> None:
        """Record a governance decision to the audit log.

        Callers should invoke this after ``assert_policy()`` to persist
        the decision for compliance / forensics:

            decision = governor.assert_policy(tc_spec)
            governor.audit(tc_spec, decision)
        """
        self.audit_log.record(
            str(self.workspace_dir),
            tc_spec,
            decision,
        )

    def _should_sandbox_shell(
        self,
        tc_spec: ToolCallSpec,
        decision: GovernanceDecision,
    ) -> bool:
        """Whether this shell decision is expected to execute sandboxed."""
        if tc_spec.tool_name != "Bash":
            return False
        if not self._sandbox_available:
            return False
        if decision.action is GovernanceAction.SANDBOX_FALLBACK:
            return True
        return (
            is_windows_shell_sandbox_platform()
            and strict_shell_sandbox_enabled()
            and decision.action is GovernanceAction.ALLOW
        )

    def _attach_shell_sandbox_if_required(
        self,
        tc_spec: ToolCallSpec,
        decision: GovernanceDecision,
    ) -> bool:
        """Attach shell sandbox config early so boundary checks can trust it."""
        if not self._should_sandbox_shell(tc_spec, decision):
            return False
        if decision.sandbox_config is None:
            decision.sandbox_config = self.compile_shell_sandbox_config(
                tc_spec,
            )
        return decision.sandbox_config is not None

    # ------------------------------------------------------------------
    # Core interface 3: Compile sandbox config
    # ------------------------------------------------------------------

    def compile_sandbox_config(  # pylint: disable=unused-argument
        self,
        tc_spec: ToolCallSpec,
    ) -> SandboxConfig:
        """Compile sandbox filesystem permission config based on policy.

        Sandbox security model:
            - Workspace is the working directory, always mounted
              readwrite (Bash needs it to work)
            - Paths from FILE_READ_TOOLS / FILE_WRITE_TOOLS in
              user_rules are compiled into mounts
            - deny_paths block sensitive paths (defense-in-depth)
            - Policy decisions control whether a command can execute;
              sandbox controls filesystem boundaries

        Mounts compilation logic:
            Iterate over user_rules, for each rule:
              - Parse match → (tool_name, pattern)
              - If tool_name ∈ FILE_READ_TOOLS → readonly mount
              - If tool_name ∈ FILE_WRITE_TOOLS → readwrite mount
            Same path uses the most permissive access (write > read).

        Returns SandboxConfig dataclass (from qwenpaw.sandbox.config).
        """
        ws = str(self.workspace_dir)

        # ── Compile mounts from user_rules ──
        # path → writable mapping: same path uses the most permissive access
        mount_map: dict[str, bool] = {}

        for rule in self.policy.user_rules:
            try:
                rule_tool, rule_pattern = _parse_match(rule.match)
            except (ValueError, IndexError):
                continue

            # Extract path from pattern: strip trailing * and other
            # wildcards to get directory prefix
            path = self._resolve_mount_path(rule_pattern, ws)
            if not path:
                continue

            if rule_tool in FILE_ACCESS_TOOLS:
                mount_map[path] = True

        mounts = [MountSpec(path=p, writable=w) for p, w in mount_map.items()]

        try:
            coding_project = str(self.coding_project_dir.resolve())
            workspace_root = str(self.workspace_dir.resolve())
        except Exception:
            coding_project = str(self.coding_project_dir)
            workspace_root = ws

        if coding_project and coding_project != workspace_root:
            _append_mount(
                mounts,
                coding_project,
                writable=True,
                executable=False,
            )
        for mount_path in self.extra_mounts:
            _append_mount(
                mounts,
                str(mount_path),
                writable=True,
                executable=True,
            )
        for mount_path in self.extra_rx_mounts:
            _append_mount(
                mounts,
                str(mount_path),
                writable=False,
                executable=True,
            )
        # Workspace is always readwrite
        mounts.insert(0, MountSpec(path=ws, writable=True))

        return SandboxConfig(
            mode=detect_platform_mode(),
            workspace_dir=ws,
            mounts=mounts,
            allow_read_all=False,
            deny_paths=list(DEFAULT_SANDBOX_DENY_PATHS),
            # NOTE: network_allow=["*"] grants full network access inside
            # the sandbox. This is intentional for now because:
            #   1. Many common commands need network (pip, git, npm, curl).
            #   2. Landlock network restriction requires ABI v4 (kernel 6.7+),
            #      which is not yet widely available in production.
            #   3. macOS Seatbelt can deny network but lacks domain-level
            #      filtering, making blanket denial too disruptive.
            # TODO: revisit default when Landlock ABI v4 is mainstream;
            #       consider making this configurable in policy.yaml.
            network_allow=["*"],
            timeout_seconds=60,
            env_vars={k: "" for k in self.policy.env_blacklist},
        )

    def compile_shell_sandbox_config(
        self,
        tc_spec: ToolCallSpec,
    ) -> SandboxConfig:
        """Compile sandbox config for shell command process-tree execution.

        This deliberately extends only the Bash/execute_shell_command path.
        File tools keep their existing path-level governance strategy.
        """
        config = self.compile_sandbox_config(tc_spec)

        if (
            not is_windows_shell_sandbox_platform()
            or not runtime_auto_discover_enabled()
        ):
            return config

        try:
            from ..sandbox.windows_script_runtime import (
                expand_windows_script_runtime_hints,
            )
            from ..sandbox.windows_runtime import (
                discover_windows_shell_runtime,
            )

            script_hints = expand_windows_script_runtime_hints(
                command=tc_spec.target or "",
                workspace_dir=str(self.workspace_dir),
                coding_project_dir=str(self.coding_project_dir),
                cwd=_extract_shell_cwd(tc_spec.raw_params),
                extra_roots=[str(path) for path in self.extra_mounts],
            )
            runtime_command = script_hints.expanded_command_text
            discovered = discover_windows_shell_runtime(
                str(self.workspace_dir),
                tc_spec.session_id or None,
                runtime_command,
            )
            if discovered.python_requested:
                _print_windows_shell_python_runtime(discovered)
            if discovered.node_requested:
                _print_windows_shell_node_runtime(discovered)
        except Exception:
            logger.exception("Windows shell runtime discovery failed.")
            return config

        config.platform_hints["skip_default_python_dir_rx"] = True
        if discovered.rx_roots:
            config.platform_hints["shell_runtime_rx_roots"] = (
                discovered.rx_roots
            )
        for root in discovered.rw_roots:
            _append_mount(config.mounts, root, writable=True, executable=False)
        config.env_vars.update(discovered.env)
        if discovered.warnings:
            config.platform_hints["runtime_warnings"] = discovered.warnings
        if script_hints.warnings:
            warnings = config.platform_hints.setdefault(
                "runtime_warnings",
                [],
            )
            warnings.extend(script_hints.warnings)
        try:
            from ..sandbox.windows_msvc_runtime import (
                discover_windows_msvc_runtime,
            )

            msvc = discover_windows_msvc_runtime(
                str(self.workspace_dir),
                tc_spec.session_id or None,
                runtime_command,
                cwd=_extract_shell_cwd(tc_spec.raw_params),
            )
            if msvc.requested:
                config.platform_hints["shell_runtime_rx_roots"] = (
                    _dedupe_paths(
                        [
                            *config.platform_hints.get(
                                "shell_runtime_rx_roots",
                                [],
                            ),
                            *msvc.rx_roots,
                        ],
                    )
                )
                config.platform_hints["admin_acl_roots"] = _dedupe_paths(
                    [
                        *config.platform_hints.get("admin_acl_roots", []),
                        *msvc.admin_acl_roots,
                    ],
                )
                config.platform_hints["admin_acl_permission"] = "RX"
                for root in msvc.rw_roots:
                    _append_mount(
                        config.mounts,
                        root,
                        writable=True,
                        executable=False,
                    )
                config.env_vars.update(msvc.env)
                if msvc.warnings:
                    warnings = config.platform_hints.setdefault(
                        "runtime_warnings",
                        [],
                    )
                    warnings.extend(msvc.warnings)
        except Exception:
            logger.exception("Windows MSVC runtime discovery failed.")
        try:
            from ..sandbox.windows_svn_runtime import (
                discover_windows_svn_runtime,
            )

            svn = discover_windows_svn_runtime(
                str(self.workspace_dir),
                tc_spec.session_id or None,
                runtime_command,
                cwd=_extract_shell_cwd(tc_spec.raw_params),
            )
            if svn.requested:
                config.platform_hints["shell_runtime_rx_roots"] = (
                    _dedupe_paths(
                        [
                            *config.platform_hints.get(
                                "shell_runtime_rx_roots",
                                [],
                            ),
                            *svn.rx_roots,
                        ],
                    )
                )
                for root in svn.rw_roots:
                    _append_mount(
                        config.mounts,
                        root,
                        writable=True,
                        executable=False,
                    )
                config.env_vars.update(svn.env)
                if svn.warnings:
                    warnings = config.platform_hints.setdefault(
                        "runtime_warnings",
                        [],
                    )
                    warnings.extend(svn.warnings)
        except Exception:
            logger.exception("Windows SVN runtime discovery failed.")
        if self.extra_executable_roots:
            config.platform_hints["shell_runtime_rx_roots"] = _dedupe_paths(
                [
                    *config.platform_hints.get(
                        "shell_runtime_rx_roots",
                        [],
                    ),
                    *[
                        str(path)
                        for path in self.extra_executable_roots
                    ],
                ],
            )
        if windows_path_rx_enabled():
            path_roots = _normalize_windows_path_rx_roots()
            if path_roots:
                config.platform_hints["shell_runtime_rx_roots"] = (
                    _dedupe_paths(
                        [
                            *config.platform_hints.get(
                                "shell_runtime_rx_roots",
                                [],
                            ),
                            *path_roots,
                        ],
                    )
                )
                path_key = _path_env_key()
                config.env_vars[path_key] = _merge_env_path(
                    os.pathsep.join(path_roots),
                    config.env_vars.get(path_key, ""),
                    os.environ.get(path_key, ""),
                    _read_windows_user_path_env(),
                )
        return config

    def _allowed_file_roots(self) -> list[str]:
        """Return canonical agent workspace + explicit mount roots."""
        ws = str(self.workspace_dir)
        roots: list[str] = []
        for root_path in [
            ws,
            str(self.coding_project_dir),
            *self._explicit_mount_paths(),
            *self._extra_mount_paths(),
            *self._extra_rx_mount_paths(),
        ]:
            root = _canonical_path(root_path, ws)
            if root not in roots:
                roots.append(root)
        return roots

    def _extra_mount_paths(self) -> list[str]:
        """Return request-scoped external mount roots."""
        return [str(path) for path in self.extra_mounts]

    def _extra_rx_mount_paths(self) -> list[str]:
        """Return request-scoped read/execute-only mount roots."""
        return [str(path) for path in self.extra_rx_mounts]

    def _file_tool_in_extra_mount(self, tc_spec: ToolCallSpec) -> bool:
        """Whether a file tool target is inside a request extra mount."""
        if (
            not self.extra_mounts
            or tc_spec.tool_name not in FILE_ACCESS_TOOLS
            or not tc_spec.target
        ):
            return False
        ws = str(self.workspace_dir)
        target = _canonical_path(tc_spec.target, ws)
        roots = [_canonical_path(str(path), ws) for path in self.extra_mounts]
        return _is_under_any_root(target, roots)

    def _file_tool_in_extra_rx_mount(self, tc_spec: ToolCallSpec) -> bool:
        """Whether a file tool target is inside a read/execute-only mount."""
        if (
            not self.extra_rx_mounts
            or tc_spec.tool_name not in FILE_ACCESS_TOOLS
            or not tc_spec.target
        ):
            return False
        ws = str(self.workspace_dir)
        target = _canonical_path(tc_spec.target, ws)
        roots = [
            _canonical_path(str(path), ws) for path in self.extra_rx_mounts
        ]
        return _is_under_any_root(target, roots)

    def _explicit_mount_paths(self) -> list[str]:
        """Return explicit file mount roots derived from user rules."""
        ws = str(self.workspace_dir)
        paths: list[str] = []
        for rule in self.policy.user_rules:
            try:
                rule_tool, rule_pattern = _parse_match(rule.match)
            except (ValueError, IndexError):
                continue
            if rule_tool not in FILE_ACCESS_TOOLS:
                continue
            path = self._resolve_mount_path(rule_pattern, ws)
            if path and path not in paths:
                paths.append(path)
        return paths

    def _check_shell_file_boundary(
        self,
        tc_spec: ToolCallSpec,
        *,
        sandbox_enforced: bool = False,
        cwd: str | None = None,
    ) -> GovernanceDecision | None:
        """Deny shell commands with explicit paths outside allowed roots."""
        if tc_spec.tool_name != "Bash" or not tc_spec.target:
            return None

        if sandbox_enforced:
            return None

        candidates = _extract_shell_path_candidates(tc_spec.target)
        if not candidates:
            return None

        workspace = str(self.workspace_dir)
        allowed_roots = self._allowed_file_roots()
        managed_paths = set()
        if cwd is not None and sys.platform == "win32":
            from ..agents.tools.shell import managed_shell_boundary_paths

            managed_paths = {
                _canonical_path(str(path), workspace)
                for path in managed_shell_boundary_paths(
                    tc_spec.target, cwd, explicit_cwd=_extract_shell_cwd(tc_spec.raw_params) is not None,
                )
            }
        for raw_path in candidates:
            resolved = _canonical_path(raw_path, cwd or workspace)
            if resolved not in managed_paths and not _is_under_any_root(resolved, allowed_roots):
                return GovernanceDecision(
                    action=GovernanceAction.DENY,
                    reason=(
                        "Shell command file access is only allowed within "
                        "the agent workspace or explicit mounts."
                    ),
                    source="shell_file_boundary",
                )

        return None

    def _check_browser_file_boundary(
        self,
        tc_spec: ToolCallSpec,
    ) -> GovernanceDecision | None:
        """Apply file-tool boundaries to Browser file:// targets."""
        if tc_spec.tool_name != "Browser" or not tc_spec.target:
            return None

        raw_path = _local_path_from_browser_target(tc_spec.target)
        if raw_path is None:
            return None

        workspace = str(self.workspace_dir)
        resolved = _canonical_path(raw_path, workspace)
        allowed_roots = self._allowed_file_roots()
        if not _is_under_any_root(resolved, allowed_roots):
            return GovernanceDecision(
                action=GovernanceAction.DENY,
                reason=(
                    "Browser file:// access is only allowed within the "
                    "agent workspace or explicit mounts."
                ),
                source="browser_file_boundary",
            )

        if self.policy.execution_level == "strict":
            return GovernanceDecision(
                action=GovernanceAction.ASK,
                reason="STRICT mode: browser file:// access requires approval",
                source="browser_file_boundary",
            )

        return GovernanceDecision(
            action=GovernanceAction.ALLOW,
            reason="Browser file:// access within allowed roots",
            source="browser_file_boundary",
        )

    @staticmethod
    def _resolve_mount_path(pattern: str, workspace_dir: str) -> str:
        """Derive a mount path from a rule pattern.

        Strategy:
            - WORKSPACE_DIR/* → workspace_dir (mount as a whole)
            - /absolute/path/* → /absolute/path (take directory part)
            - relative path → workspace_dir / relative (take directory part)
            - Pure wildcards (*, **) → skip, cannot derive a concrete path
        """
        p = pattern.rstrip("*").rstrip("/")

        if not p or p == ".":
            return ""

        # WORKSPACE_DIR placeholder (defensive: should already be
        # replaced at load time)
        if "WORKSPACE_DIR" in p:
            p = p.replace("WORKSPACE_DIR", workspace_dir)

        # Absolute path
        if p.startswith("/"):
            return p

        # Relative path → resolve based on workspace
        return str(Path(workspace_dir) / p)

    # ------------------------------------------------------------------
    # Core interface 4: Dynamic rule addition
    # ------------------------------------------------------------------

    def add_rule(self, rule: GovernanceRule) -> None:
        """Dynamically append a rule to the policy after user approval.

        Approved rules carry a duration (session / permanent).
        The rule is also persisted to policy.yaml.
        Note: rules are only appended to user_rules; builtin_rules are
        immutable.
        """
        self.policy.add_rule(rule)
        if self._policy is not None:
            save_governance_policy(
                self._policy,
                str(self._policy_dir),
                str(self.workspace_dir),
                str(self.coding_project_dir),
            )

    async def add_approved_rule(
        self,
        tc_spec: ToolCallSpec,
        *,
        generalized_target: str,
    ) -> bool:
        """Add an ALLOW rule for a user-approved tool call.

        Args:
            generalized_target: the generalized target/pattern (e.g.
                ``"git *"``), already computed upstream by
                ``generalize_target_for_approval``.

        Returns True if a rule was actually added, False if skipped
        (e.g. builtin ask, empty target pattern).
        """
        if self.is_builtin_ask(tc_spec):
            return False

        try:
            if not generalized_target:
                logger.debug(
                    "ResourceGovernor: empty pattern, skipping rule "
                    "for tool=%s target=%s",
                    tc_spec.tool_name,
                    tc_spec.target,
                )
                return False

            match = f"{tc_spec.tool_name}({generalized_target})"
            rule = GovernanceRule(
                match=match,
                action=GovernanceAction.ALLOW,
                reason="user approved",
                grantee=tc_spec.agent_id or "*",
                duration="session",
                session_id=tc_spec.session_id,
            )
            self.add_rule(rule)
            logger.info(
                "ResourceGovernor: added approved rule: %s",
                rule.match,
            )
            return True
        except Exception:
            logger.debug(
                "ResourceGovernor: failed to persist approved rule",
                exc_info=True,
            )
            return False

    def is_builtin_ask(self, tc_spec: ToolCallSpec) -> bool:
        """Determine whether a tool call's ASK comes from builtin_rules.

        builtin ask → no rule recorded on approval (asks every time)
        user ask   → rule recorded on approval (won't ask next time)

        Called by tool_adapter's approval flow to decide whether to
        persist a new rule.
        """
        if not self._policy:
            return False
        source = self._policy.evaluate_source(tc_spec)
        return source == "builtin_rules"

    # ------------------------------------------------------------------
    # Property access
    # ------------------------------------------------------------------

    @property
    def policy(self) -> GovernancePolicy:
        if self._policy is None:
            raise RuntimeError("ResourceGovernor not started")
        return self._policy

    @property
    def audit_log(self) -> AuditLog:
        """Get the global AuditLog singleton."""
        return AuditLog.get_instance(
            db_dir=self._governance_dir,
        )

def _print_windows_shell_python_runtime(discovered: object) -> None:
    """Print Python runtime roots used by strict Windows shell sandboxing."""
    lines = ["[QwenPaw Sandbox] Python runtime discovery for shell sandbox:"]
    python_roots = list(getattr(discovered, "python_rx_roots", []) or [])
    if python_roots:
        lines.append("  executable/read-only roots:")
        lines.extend(f"    - {root}" for root in python_roots)
    else:
        lines.append("  executable/read-only roots: <none discovered>")

    env = getattr(discovered, "env", {}) or {}
    env_keys = ("TEMP", "TMP", "PIP_CACHE_DIR", "PYTHONPYCACHEPREFIX")
    lines.append("  redirected Python temp/cache env:")
    for key in env_keys:
        if key in env:
            lines.append(f"    {key}={env[key]}")

    warnings = [
        warning
        for warning in (getattr(discovered, "warnings", []) or [])
        if "Python" in str(warning)
    ]
    if warnings:
        lines.append("  warnings:")
        lines.extend(f"    - {warning}" for warning in warnings)

    print("\n".join(lines), flush=True)


def _print_windows_shell_node_runtime(discovered: object) -> None:
    """Print Node.js runtime roots used by strict Windows shell sandboxing."""
    lines = ["[QwenPaw Sandbox] Node.js runtime discovery for shell sandbox:"]
    node_roots = list(getattr(discovered, "node_rx_roots", []) or [])
    if node_roots:
        lines.append("  executable/read-only roots:")
        lines.extend(f"    - {root}" for root in node_roots)
    else:
        lines.append("  executable/read-only roots: <none discovered>")

    env = getattr(discovered, "env", {}) or {}
    lines.append("  redirected Node.js cache env:")
    if "npm_config_cache" in env:
        lines.append(f"    npm_config_cache={env['npm_config_cache']}")

    warnings = [
        warning
        for warning in (getattr(discovered, "warnings", []) or [])
        if "Node.js" in str(warning)
    ]
    if warnings:
        lines.append("  warnings:")
        lines.extend(f"    - {warning}" for warning in warnings)

    print("\n".join(lines), flush=True)

