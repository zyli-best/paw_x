"""Shared redaction and file-operation accounting for cloud/edge/device execution."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from types import SimpleNamespace

from ...observability.model_input_redaction_guard import TurnRedactionScope

DEVICE_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "append_file",
        "copy_file_or_dir",
        "move_file_or_dir",
        "create_directory",
    }
)
WRITE_TOOLS = DEVICE_TOOLS - {"read_file"}


class _Scope(TurnRedactionScope):
    @staticmethod
    def _fingerprint(value):
        # AgentScope updates timestamps/usage while executing. Those are not
        # new content and must not invalidate an already-made decision.
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        if isinstance(value, dict):
            value = {
                k: v
                for k, v in value.items()
                if k not in {"created_at", "finished_at", "usage"}
            }
        return TurnRedactionScope._fingerprint(value)

    async def prepare_initial_inputs(self, msgs):
        # Routing and fallback reuse the same structured messages. Keep
        # their decisions without broadening approval to new tool output.
        pending = [
            i
            for i, msg in enumerate(msgs)
            if self._fingerprint(msg) not in self._processed
        ]
        if not pending:
            return msgs
        prepared = await super().prepare_initial_inputs([msgs[i] for i in pending])
        result = list(msgs)
        for i, msg in zip(pending, prepared):
            result[i] = msg
        return result


class EdgeCloudRedaction:
    def __init__(self, request_context: dict):
        self.request_context = request_context
        self.processed: set[str] = set()
        self.decisions: dict = {}

    def scope(self, agent):
        scope = _Scope(agent=agent, request_context=self.request_context)
        scope._processed = self.processed
        scope._decision_cache = self.decisions
        return scope

    async def prepare(self, msgs):
        holder = SimpleNamespace(state=SimpleNamespace(context=[]))
        return await self.scope(holder).prepare_initial_inputs(msgs)

    def mark_prepared(self, msgs):
        """Preserve decisions when only runtime-owned metadata was updated."""
        self.processed.update(_Scope._fingerprint(msg) for msg in msgs)


class FileOperationJournal:
    """Identify exact writes across execution tiers within one request.

    Keys hash normalized arguments; this is not a cross-process transaction
    or semantic deduplication layer. ExecutionJournal owns tool wrapping.
    """

    def __init__(self):
        self.operations: dict[str, dict] = {}
        self.calls = 0

    @staticmethod
    def key(name, kwargs):
        from ...agents.tools.file_io import _resolve_file_path

        values = copy.deepcopy(kwargs)
        for field in ("file_path", "source_path", "destination_path"):
            if isinstance(values.get(field), str):
                # Keep keys stable after move removes the source.
                values[field] = os.path.normcase(
                    os.path.abspath(
                        _resolve_file_path(values[field]),
                    )
                )
        body = json.dumps([name, values], sort_keys=True, default=str)
        return hashlib.sha256(body.encode()).hexdigest()
