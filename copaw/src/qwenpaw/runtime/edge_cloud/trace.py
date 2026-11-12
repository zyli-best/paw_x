"""Request-local flow identity and cloud usage; no per-role session payloads."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

_CLOUD_STAGES = ("router", "verifier", "cloud")


@dataclass
class EdgeCloudTrace:
    base_agent_id: str
    flow_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    usage_by_stage: dict[str, dict[str, Any]] = field(default_factory=dict)
    final_agent: str = ""
    fallback: bool = False
    fallback_reason: str = ""

    def record_usage(
        self,
        *,
        stage: str,
        provider_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        if stage not in _CLOUD_STAGES:
            return
        usage = self.usage_by_stage.setdefault(
            stage,
            {
                "call_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        )
        usage["call_count"] += 1
        usage["prompt_tokens"] += max(0, int(prompt_tokens or 0))
        usage["completion_tokens"] += max(0, int(completion_tokens or 0))
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        usage["provider_id"], usage["model"] = provider_id, model

    def cloud_usage_total(self) -> dict[str, Any]:
        return {
            "counted": True,
            **{
                key: sum(usage[key] for usage in self.usage_by_stage.values())
                for key in (
                    "call_count",
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                )
            },
        }
