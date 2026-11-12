"""Shared request policy for shell instructions and atomicity enforcement."""

import os
import sys
from typing import Any

from .context import get_current_request_context


def force_direct_subprocess_sync_enabled() -> bool:
    """Whether Shell uses the forced host-subprocess execution route."""
    return (
        os.environ.get("QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC", "")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )


WINDOWS_NON_CODING_TOOL_DISPATCH_GUIDANCE = (
    "Classify from the current request/context under workspace rules. "
    "Conversation and direct answers need no discovery, routine planning "
    "or retrospectives; "
    "simple operations use necessary tools and discover only missing role "
    "or skill information. Analysis, topic difficulty, answer length or tool "
    "use alone does not make a task complex.\n"
    "Every independent complex task MUST actually call both list_agents "
    "and list_skills successfully before matching, Skill loading or business "
    "execution; only restoration of missing request/task/discovery state "
    "may precede them. The lists are independent, may share one response, "
    "and have no platform/mode, ordering or fixed page-size restriction. "
    "Known skills, self-sufficiency or another task's catalog do not exempt "
    "this prerequisite. If simple work becomes complex, complete both "
    "discoveries before continuing the complex work.\n"
    "Reuse valid same-task discoveries across messages, ReAct and Scroll. "
    "Retain necessary goal/discovery/selection state, page coverage and "
    "visible call IDs/seq in complex-task summaries; never invent fields. "
    "Recall missing same-task details before re-listing, refresh only "
    "affected lists, and page as needed before concluding no match. "
    "Preserve valid partial results; recover failures or report the blocker "
    "without bypassing required discovery. Conversation/simple operations "
    "need no dual-discovery state tracking. Load applicable Skill "
    "instructions/current-turn parameters and obtain required approvals.\n"
    "Submit necessary independent read-only queries with known arguments "
    "as separate tool calls in the SAME assistant response; no model round "
    "per query or extra checks to fill a batch. Runtime controls actual "
    "concurrency; same-response calls need not run simultaneously.\n"
    "Read-only does not mean independent. Await actual prerequisites "
    "before forming dependent arguments: search then read an unknown path, "
    "discover then select a version, load then use Skill parameters. Never "
    "guess them. Preserve workflow order, mount search priority, permissions, "
    "shared browser/desktop state, and the order of state changes and their "
    "observations. Keep valid partial results; correct the cause and retry "
    "only necessary failed work. Pending/background is not completion. "
    "Deliver when evidence and required validation suffice."
)


WINDOWS_NON_CODING_SHELL_GUIDANCE = (
    "Windows Non-Coding Mode: ONE atomic operation per call — one "
    "self-contained process invocation with one intent. No shell composition "
    "(`&&`, `&`, `||`, `;`, pipes `|`, redirection `>`, `>>`, `<`, or multiline "
    "commands). A leading PowerShell call operator `&` may invoke one program. "
    "Do not hide independent steps in `python -c`, `node -e`, "
    "`cmd /c`, `powershell -Command`, or scripts. Use `cwd`; prior-call "
    "cwd/env does not persist. Atomicity means call granularity, not rollback "
    "or one call per response. Prefer applicable dedicated tools. A script "
    "implementing one task is allowed, but cannot bundle independent "
    "operations to bypass these rules."
)

WINDOWS_NON_CODING_SHELL_DISPATCH_GUIDANCE = (
    "Complete applicable prerequisites: complex tasks require successful "
    "list_agents/list_skills discovery (reuse valid same-task results) "
    "and matching; all requests require applicable Skill loading and "
    "approvals. Simple operations need no mandatory dual discovery. "
    "Submit necessary independent inspections with "
    "known arguments as separate execute_shell_command calls in the SAME "
    "response; no shell composition or model round per command. Runtime "
    "controls concurrency. Issue state-changing commands (git add/commit/"
    "checkout/pull/merge, installs, builds/code generation that write output), "
    "uncertain commands, and result-dependent commands ONE per response, "
    "awaiting the result before proceeding. Program names or flags alone "
    "do not prove read-only behavior."
)


def requires_atomic_shell(*, is_coding_mode: bool | None = None) -> bool:
    """Restrict Windows Non-Coding requests, regardless of execution backend."""
    if is_coding_mode is None:
        is_coding_mode = bool(
            get_current_request_context().get("is_coding_mode"),
        )
    return sys.platform == "win32" and not is_coding_mode


def shell_tool_description(
    name: str,
    description: str,
    request_context: dict[str, Any] | None,
) -> str:
    """Specialize an instance description without mutating the shared function."""
    if name != "execute_shell_command":
        return description
    coding = bool((request_context or {}).get("is_coding_mode"))
    if requires_atomic_shell(is_coding_mode=coding):
        guidance = (
            WINDOWS_NON_CODING_SHELL_GUIDANCE
            + "\n\n"
            + WINDOWS_NON_CODING_SHELL_DISPATCH_GUIDANCE
        )
    else:
        guidance = (
            "Related prerequisites may be composed in one command when the "
            "configured shell supports them. Prefer `cwd` to changing "
            "directories in the command. Each call starts a fresh process; "
            "do not rely on shell state from a previous call."
        )
    return description + "\n\n" + guidance
