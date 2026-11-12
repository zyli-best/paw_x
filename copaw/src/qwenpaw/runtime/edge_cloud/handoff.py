"""Read-only, versioned references to this request's handoff evidence."""

from __future__ import annotations

import copy
import json

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolBase, ToolChunk
from agentscope.permission import PermissionBehavior, PermissionDecision


class HandoffArtifacts(ToolBase):
    name = "read_handoff_artifact"
    description = "Read an authorized handoff manifest or a referenced tool result; no file or network access."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "ref": {"type": "string"},
            "view": {
                "type": "string",
                "enum": ["metadata", "content", "result"],
                "default": "metadata",
            },
        },
        "required": ["ref"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        flow_id: str,
        *,
        request: str,
        reason: str,
        journal,
        conditions: list[str],
        skill=None,
        executed_by="",
        execution_task="",
    ):
        super().__init__()
        self.ref = f"handoff://{flow_id}/manifest@v1"
        self.artifacts = {}
        for record in journal.records:
            ref = f"trace://{flow_id}/call-{record['call_seq']}@v1"
            self.artifacts[ref] = copy.deepcopy(record)
        self.manifest = {
            "original_request": request,
            "trigger": reason,
            "completion_conditions": list(conditions),
            "tool_results": list(self.artifacts),
            "previous_execution": {
                "agent": executed_by,
                "task": execution_task,
                "status": reason,
            },
            "calls": [
                {
                    "tool": record["tool"],
                    "arguments": record.get("arguments", {}),
                    "status": record["status"],
                    "result_ref": ref,
                }
                for ref, record in self.artifacts.items()
            ],
            "side_effects": copy.deepcopy(list(journal.operations.values())),
            "skill": skill or None,
            "failure_streak": {
                "tool": journal.last_failed_tool,
                "count": journal.failure_streak,
            },
        }
        self.artifacts[self.ref] = self.manifest

    async def check_permissions(self, *args, **kwargs):
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Read evidence already authorized for this request",
        )

    async def __call__(self, ref: str, view: str = "metadata") -> ToolChunk:
        if ref not in self.artifacts:
            raise ValueError("Reference does not belong to this request's handoff")
        if view not in {"metadata", "content", "result"}:
            raise ValueError("Unsupported handoff view")
        value = self.artifacts[ref]
        if view == "metadata" and ref != self.ref:
            value = {key: value[key] for key in ("call_seq", "tool", "status")}
        elif view == "metadata":
            value = {
                key: value[key]
                for key in (
                    "trigger",
                    "completion_conditions",
                    "tool_results",
                    "side_effects",
                    "skill",
                    "previous_execution",
                    "calls",
                )
            }
        return ToolChunk(
            state=ToolResultState.SUCCESS,
            content=[
                TextBlock(text=json.dumps(value, ensure_ascii=False, default=str))
            ],
        )
