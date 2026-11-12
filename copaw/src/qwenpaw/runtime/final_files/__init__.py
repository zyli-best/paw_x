"""Opt-in, request-scoped final-file submission and delivery.

No directory discovery and no changes to the legacy send_file_to_user tool.
Only explicitly submitted files enter the store. Internal handoff tickets
bind child responses to their actual caller, including background polling.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import unicodedata
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4
from urllib.parse import unquote

FEATURE = "final_files"
METADATA_KEY = "final_files_v1"
HANDOFF_KEY = "_final_files_handoff"
_current: ContextVar[DeliveryState | None] = ContextVar(
    "final_files", default=None
)


def enabled() -> bool:
    return os.environ.get(
        "QWENPAW_FINAL_FILES_ENABLED", "0"
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def store_root() -> Path:
    from ...constant import WORKING_DIR

    return Path(WORKING_DIR).resolve() / "final_files"


def source_path(raw: str) -> Path:
    """The same canonical path must be checked by policy and read by the tool."""
    from ...config.context import get_default_relative_file_dir

    path = Path(
        unicodedata.normalize("NFC", unquote(raw)).strip()
    ).expanduser()
    if not path.is_absolute():
        path = Path(get_default_relative_file_dir()) / path
    return path.resolve()


def _managed_path(*parts: str) -> Path:
    root = store_root()
    if root.is_symlink():
        raise ValueError("Final-file storage root cannot be a symlink")
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError("Final-file storage path escapes its root")
    return path


def _identity(value: str) -> str:
    if not isinstance(value, str) or len(value) != 32:
        raise ValueError("Invalid final-file identifier")
    if any(c not in "0123456789abcdef" for c in value):
        raise ValueError("Invalid final-file identifier")
    return value


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid final-file record")
    return data


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


@dataclass
class DeliveryState:
    agent_id: str
    session_id: str
    user_id: str = ""
    scope_id: str = field(default_factory=lambda: uuid4().hex)
    recipient: str = "user"
    handoff: str = ""
    stage: str = "ordinary"
    attempt_id: str = field(default_factory=lambda: uuid4().hex)
    requires_verification: bool = False
    verified: bool = False
    files: list[dict] = field(default_factory=list)
    submitted: bool = False
    blocked: bool = False
    finished: bool = False
    terminal_gate: str = ""
    invalid_context: bool = False
    submission_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def begin_attempt(self, stage: str, *, verify: bool = False) -> None:
        self.stage = stage
        self.attempt_id = uuid4().hex
        self.requires_verification = verify
        self.verified = False
        self.files = []
        self.submitted = False
        self.blocked = False
        self.terminal_gate = ""

    def manifest(self) -> dict:
        return {
            "status": (
                "pending_verification"
                if self.requires_verification and not self.verified
                else "pending_finalization"
            ),
            "attempt_id": self.attempt_id,
            "submitted": self.submitted,
            "files": [
                {
                    key: value
                    for key, value in item.items()
                    if key != "snapshot"
                }
                for item in self.files
            ],
        }


def current() -> DeliveryState | None:
    return _current.get()


def bind_request(ctx):
    """A fresh context for every request; never inherit a parent's mutable list."""
    state = None
    if enabled():
        request = getattr(ctx, "request", None)
        rc = getattr(request, "request_context", None) or {}
        state = DeliveryState(
            agent_id=str(getattr(ctx, "agent_id", "") or "default"),
            session_id=str(getattr(ctx, "session_id", "") or ""),
            user_id=str(
                rc.get("user_id")
                or getattr(request, "user_id", "")
                or getattr(ctx, "user_id", "")
                or ""
            ),
        )
        ticket = rc.get(HANDOFF_KEY)
        if ticket:
            state.recipient = "parent"
            try:
                grant = _read_json(
                    _managed_path("handoffs", _identity(ticket) + ".json")
                )
                if (grant["child_agent"], grant["child_session"]) != (
                    state.agent_id,
                    state.session_id,
                ):
                    raise ValueError(
                        "Final-file handoff does not match this request"
                    )
                state.handoff = ticket
            except (OSError, ValueError, KeyError, TypeError):
                state.invalid_context = True
        ctx.extras[FEATURE] = state
    return _current.set(state)


def reset_request(token) -> None:
    _current.reset(token)


def prepare_child_request(payload: dict, child_agent: str) -> None:
    """Called by delegation tools, never by model-supplied delivery arguments."""
    state = current()
    if state is None:
        return
    ticket = uuid4().hex
    _write_json(
        _managed_path("handoffs", ticket + ".json"),
        {
            "parent_agent": state.agent_id,
            "parent_session": state.session_id,
            "parent_user": state.user_id,
            "child_agent": child_agent,
            "child_session": payload["session_id"],
        },
    )
    payload.setdefault("request_context", {})[HANDOFF_KEY] = ticket


def snapshot_files(paths: list[Path], state: DeliveryState) -> list[dict]:
    """Copy only selected files; all-or-nothing manifest activation by caller."""
    files = []
    seen = set()
    for path in paths:
        key = os.path.normcase(str(path.resolve()))
        if key in seen:
            continue
        seen.add(key)
        identity = uuid4().hex
        target = _managed_path("artifacts", identity, "files", path.name)
        target.parent.mkdir(parents=True, exist_ok=False)
        before = path.stat()
        shutil.copyfile(path, target)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"File changed during submission: {path}")
        digest = _digest(target)
        if digest != _digest(path):
            raise ValueError(f"File changed during submission: {path}")
        import mimetypes

        item = {
            "artifact_id": identity,
            "name": path.name,
            "source_path": str(path),
            "snapshot": str(target),
            "media_type": mimetypes.guess_type(path.name)[0]
            or "application/octet-stream",
            "size": target.stat().st_size,
            "sha256": digest,
        }
        _write_json(
            _managed_path("artifacts", identity, "record.json"),
            {
                **item,
                "scope_id": state.scope_id,
                "attempt_id": state.attempt_id,
                "handoff": state.handoff,
                "ready": False,
            },
        )
        files.append(item)
    return files


def _artifact(identity: str) -> tuple[dict, Path]:
    record = _read_json(
        _managed_path("artifacts", _identity(identity), "record.json")
    )
    name = record["name"]
    if (
        not isinstance(name, str)
        or Path(name).name != name
        or name in {".", ".."}
    ):
        raise ValueError("Invalid final-file name")
    path = _managed_path("artifacts", identity, "files", name)
    if not path.is_file() or _digest(path) != record["sha256"]:
        raise ValueError(f"Final-file snapshot is missing or changed: {name}")
    # Always use the validated location, even if a record's cached path changed.
    record["artifact_id"] = identity
    record["snapshot"] = str(path)
    return record, path


def import_parent_files(response: dict) -> str:
    """Resolve trusted artifact records; never copy a path supplied by a model."""
    state = current()
    metadata = response.get("metadata") or {}
    manifest = (
        metadata.get(METADATA_KEY) if isinstance(metadata, dict) else None
    )
    if state is None or not isinstance(manifest, dict):
        return ""
    if manifest.get("status") != "ready_for_parent":
        return ""
    from ...config.context import get_default_relative_file_dir

    imported = []
    try:
        for item in manifest.get("files", []):
            identity = _identity(item["artifact_id"])
            record, source = _artifact(identity)
            ticket = _identity(record["handoff"])
            grant = _read_json(_managed_path("handoffs", ticket + ".json"))
            if not record.get("ready") or (
                grant["parent_agent"],
                grant["parent_session"],
                grant["parent_user"],
            ) != (state.agent_id, state.session_id, state.user_id):
                raise ValueError(
                    "Final-file handoff is not authorized for this caller"
                )
            root = Path(get_default_relative_file_dir()).resolve()
            target = root / "received_final_files" / identity / record["name"]
            if not target.resolve().is_relative_to(root):
                raise ValueError(
                    "Final-file import escapes the output directory"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            # Polling is idempotent; never overwrite a caller's later edits.
            if not target.exists():
                with source.open("rb") as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
            imported.append(
                {
                    "artifact_id": identity,
                    "name": record["name"],
                    "file_path": str(target),
                    "media_type": record["media_type"],
                    "sha256": record["sha256"],
                }
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return "\n\n[FINAL_FILES_ERROR]\n" + str(exc)
    if not imported:
        return ""
    return "\n\n[FINAL_FILES]\n" + json.dumps(imported, ensure_ascii=False)


def observe_event(event: Any) -> None:
    state = current()
    if state is None:
        return
    kind = getattr(event, "type", "")
    kind = getattr(kind, "value", kind)
    if kind in {
        "EXCEED_MAX_ITERS",
        "USER_INTERRUPT",
        "REQUIRE_EXTERNAL_EXECUTION",
    }:
        state.blocked = True


def observe_stop(result: Any) -> None:
    """Observe existing gates without modifying their decision or continuation."""
    state = current()
    if state is not None:
        action = getattr(result, "action", "")
        if getattr(action, "value", action) == "terminate":
            state.terminal_gate = getattr(result, "reason", "") or ""


def _can_finish(state: DeliveryState) -> bool:
    if (
        state.invalid_context
        or state.blocked
        or state.finished
        or not state.submitted
    ):
        return False
    if state.requires_verification and not state.verified:
        return False
    # Nonempty gate reasons must explicitly represent success. An ordinary
    # model stop has an empty reason; blocked/limit stops must not release files.
    reason = state.terminal_gate.lower()
    if reason and reason not in {
        "goal completed: satisfied",
        "mission completed",
        "all stories passed",
    }:
        return False
    return True


async def finish(ctx, envelope):
    """Called only after normal execution, before session saving/finalization."""
    state = current()
    if state is None or not _can_finish(state):
        return

    def prepare():
        records = []
        for item in state.files:
            record, path = _artifact(item["artifact_id"])
            if (record["scope_id"], record["attempt_id"]) != (
                state.scope_id,
                state.attempt_id,
            ):
                raise ValueError("Stale final-file submission")
            records.append((record, path))
        if state.recipient == "parent":
            for record, _path in records:
                record["ready"] = True
                _write_json(
                    _managed_path(
                        "artifacts", record["artifact_id"], "record.json"
                    ),
                    record,
                )
        return records

    try:
        records = await asyncio.to_thread(prepare)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Preserve completed business work, but explicitly report failed delivery.
        from agentscope.message import TextBlock

        block = TextBlock(text=f"\n最终文件交付失败：{exc}")
        state.finished = True
        _persist_blocks(ctx.agent, [block])
        async for event in envelope.append_final_files([], error=block.text):
            yield event
        return

    if state.recipient == "parent":
        metadata = dict(envelope.response.metadata or {})
        metadata[METADATA_KEY] = {
            "status": "ready_for_parent",
            "files": [
                {
                    key: record[key]
                    for key in (
                        "artifact_id",
                        "name",
                        "media_type",
                        "size",
                        "sha256",
                    )
                }
                for record, _path in records
            ],
        }
        envelope.response.metadata = metadata
    else:
        from agentscope.message import DataBlock, URLSource

        blocks = [
            DataBlock(
                source=URLSource(
                    url=path.as_uri(), media_type=record["media_type"]
                ),
                name=record["name"],
            )
            for record, path in records
        ]
        _persist_blocks(ctx.agent, blocks)
        async for event in envelope.append_final_files(
            [record for record, _path in records],
        ):
            yield event
    state.finished = True


def _persist_blocks(agent, blocks) -> None:
    from agentscope.message import Msg

    context = agent.state.context
    # Append to the final assistant text, preserving existing text extraction.
    if (
        context
        and context[-1].role == "assistant"
        and not context[-1].get_content_blocks("tool_call")
    ):
        msg = context[-1]
        content = msg.content
        if isinstance(content, str):
            from agentscope.message import TextBlock

            content = [TextBlock(text=content)]
        msg.content = [*(content or []), *blocks]
        msg.metadata = {**(msg.metadata or {}), METADATA_KEY: True}
    else:
        context.append(
            Msg(
                name="assistant",
                role="assistant",
                content=blocks,
                metadata={METADATA_KEY: True},
            )
        )
