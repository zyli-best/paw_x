# -*- coding: utf-8 -*-
"""Structured router and verifier protocols."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RouterDecision(BaseModel):
    protocol_version: Literal["edge_cloud_router.v1"]
    task_type: Literal[
        "device_text",
        "device_tools",
        "edge_skill",
        "complex_or_negative",
    ]
    target_agent: Literal["device", "edge", "cloud"] = Field(
        description="device: simple text/basic file tasks; edge: exactly one existing Skill; cloud: complex or creative tasks, negative feedback and fallback"
    )
    need_verifier: bool
    reason: str = ""
    need_tools: bool = False
    estimated_tool_rounds: int = Field(
        default=0,
        ge=0,
        description="For device file tasks, a positive estimate no greater than the supplied device_max_tool_rounds; 0 for device text, edge or cloud",
    )
    execution_task: str = Field(
        default="",
        description="For device/edge: a complete, self-contained task derived from available history and the latest request, including relevant facts, exact inputs/paths/constraints and required outputs; no access to original conversation is assumed. Empty for cloud.",
    )
    skill: str = Field(
        default="",
        description="For edge, the exact existing Skill name whose full instructions Route has read with Skill; empty for device or cloud",
    )
    required_tools: list[str] = Field(default_factory=list)
    completion_conditions: list[str] = Field(default_factory=list)


class VerifierDecision(BaseModel):
    protocol_version: Literal["edge_cloud_verifier.v1"]
    passed: bool
    failure_reason: str = ""
    missing_requirements: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
