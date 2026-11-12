# -*- coding: utf-8 -*-
"""Independent cloud/edge/device runtime configuration."""

from __future__ import annotations

import os
from copy import copy
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE_VALUES


def _csv(value: str | None) -> frozenset[str]:
    return frozenset(part.strip() for part in (value or "").split(",") if part.strip())


def _optional_token_limit(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer") from None
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class ModelConnection:
    api_base: str
    api_key: str
    model: str
    max_tokens: int | None = None

    @property
    def ready(self) -> bool:
        return bool(self.api_base and self.api_key and self.model)


@dataclass(frozen=True)
class EdgeCloudConfig:
    """Optional cloud/edge/device orchestration settings."""

    enabled: bool
    agent_ids: frozenset[str]
    edge_base_url: str
    edge_api_key: str
    edge_model: str
    device_max_tool_rounds: int = 2
    log_detail: str = "full"
    device_base_url: str = ""
    device_api_key: str = ""
    device_model: str = ""
    device_max_tokens: int | None = None
    edge_max_tokens: int | None = None
    trace_stream_enabled: bool = False
    session_process_enabled: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.device_max_tool_rounds) is not int
            or self.device_max_tool_rounds < 1
        ):
            raise ValueError("device.max_tool_rounds must be a positive integer")
        for tier in ("device", "edge"):
            limit = getattr(self, f"{tier}_max_tokens")
            if limit is not None and (type(limit) is not int or limit < 1):
                raise ValueError(f"{tier}.max_tokens must be a positive integer")

    def connection_for(self, tier: str) -> ModelConnection:
        if tier not in {"device", "edge"}:
            raise ValueError("Only device/edge connections may be overridden")
        if tier == "device":
            return ModelConnection(
                self.device_base_url, self.device_api_key, self.device_model,
                self.device_max_tokens,
            )
        return ModelConnection(
            self.edge_base_url, self.edge_api_key, self.edge_model,
            self.edge_max_tokens,
        )

    @property
    def ready(self) -> bool:
        return any(self.connection_for(tier).ready for tier in ("device", "edge"))


def resolve_edge_cloud_config(
    base: EdgeCloudConfig,
    override: Any = None,
) -> EdgeCloudConfig:
    """Merge explicit request fields without changing process configuration.

    Missing/null/empty overrides preserve startup settings. Error
    messages deliberately exclude supplied values (especially credentials).
    """
    if override is None:
        return base
    if not isinstance(override, dict):
        raise ValueError("request_context.edge_cloud must be an object")
    if not override:
        return base
    if override.keys() - {"enabled", "device", "edge", "trace_stream_enabled"}:
        raise ValueError("request_context.edge_cloud contains unsupported fields")
    updates = {}

    def validate(name, value):
        if name in {"enabled", "trace_stream_enabled"}:
            if not isinstance(value, bool):
                raise ValueError(f"request_context.edge_cloud.{name} must be a boolean")
        else:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"request_context.edge_cloud.{name} must be a non-empty string"
                )
            value = value.strip()
            if name == "api_base":
                try:
                    url = urlsplit(value)
                    valid = url.scheme in {"http", "https"} and bool(url.hostname)
                    valid = valid and not (
                        url.username or url.password or url.query or url.fragment
                    )
                    _ = url.port
                except ValueError:
                    valid = False
                if not valid:
                    raise ValueError(
                        "request_context.edge_cloud.api_base must be an HTTP(S) base URL without credentials, query or fragment"
                    )
        return value

    if "enabled" in override:
        updates["enabled"] = validate("enabled", override["enabled"])
    if "trace_stream_enabled" in override:
        updates["trace_stream_enabled"] = validate(
            "trace_stream_enabled", override["trace_stream_enabled"]
        )
    for name, value in override.items():
        if name in {"device", "edge"}:
            allowed = {"api_base", "api_key", "model"}
            if name == "device":
                allowed.add("max_tool_rounds")
            if not isinstance(value, dict) or value.keys() - allowed:
                raise ValueError(
                    f"request_context.edge_cloud.{name} contains unsupported fields or is not an object"
                )
            for field, item in value.items():
                if field == "max_tool_rounds":
                    updates["device_max_tool_rounds"] = item
                    continue
                target = "base_url" if field == "api_base" else field
                updates[f"{name}_{target}"] = validate(field, item)
    config = replace(base, **updates)
    if config.enabled and not config.ready:
        raise ValueError(
            "request_context.edge_cloud requires a complete device or edge connection (api_base, model and api_key)"
        )
    if config.enabled:
        for tier in ("device", "edge"):
            if tier in override and not config.connection_for(tier).ready:
                raise ValueError(
                    f"request_context.edge_cloud.{tier} requires api_base, model and api_key after merging startup configuration"
                )
    return config


def detach_edge_cloud_override(request: Any) -> tuple[Any, Any]:
    """Keep connection settings out of hooks, tools and agent transfers.

    Copy only modified containers: callers and queued payloads retain ownership
    of their original request, and unrelated context values are preserved.
    """
    rc = getattr(request, "request_context", None)
    meta = getattr(request, "channel_meta", None)
    meta_rc = meta.get("request_context") if isinstance(meta, dict) else None
    has_override = isinstance(rc, dict) and "edge_cloud" in rc
    has_meta_override = isinstance(meta_rc, dict) and "edge_cloud" in meta_rc
    if not has_override and not has_meta_override:
        return request, None
    request = copy(request)
    override = rc.get("edge_cloud") if has_override else None
    if has_override:
        request.request_context = {k: v for k, v in rc.items() if k != "edge_cloud"}
    if has_meta_override:
        request.channel_meta = dict(meta)
        request.channel_meta["request_context"] = {
            k: v for k, v in meta_rc.items() if k != "edge_cloud"
        }
    return request, override


def load_edge_cloud_config() -> EdgeCloudConfig:
    """Read cloud/edge/device settings from process environment."""

    enabled = _truthy(os.environ.get("QWENPAW_EDGE_CLOUD_ENABLED"))
    agent_ids = _csv(os.environ.get("QWENPAW_EDGE_CLOUD_AGENT_IDS"))
    if not agent_ids:
        agent_ids = frozenset({"default"})

    try:
        max_rounds = int(
            os.environ.get("QWENPAW_EDGE_CLOUD_DEVICE_MAX_TOOL_ROUNDS", "2"),
        )
    except ValueError:
        raise ValueError(
            "QWENPAW_EDGE_CLOUD_DEVICE_MAX_TOOL_ROUNDS must be a positive integer"
        ) from None

    log_detail = (
        os.environ.get(
            "QWENPAW_EDGE_CLOUD_LOG_DETAIL",
            "full",
        )
        .strip()
        .lower()
    )
    if log_detail not in {"off", "summary", "full"}:
        log_detail = "full"

    return EdgeCloudConfig(
        enabled=enabled,
        trace_stream_enabled=_truthy(
            os.environ.get("QWENPAW_EDGE_CLOUD_TRACE_STREAM_ENABLED")
        ),
        session_process_enabled=_truthy(
            os.environ.get("QWENPAW_EDGE_CLOUD_SESSION_PROCESS_ENABLED")
        ),
        agent_ids=agent_ids,
        edge_base_url=os.environ.get(
            "QWENPAW_EDGE_CLOUD_EDGE_BASE_URL",
            "",
        ).strip(),
        edge_api_key=os.environ.get(
            "QWENPAW_EDGE_CLOUD_EDGE_API_KEY",
            "",
        ).strip(),
        edge_model=os.environ.get(
            "QWENPAW_EDGE_CLOUD_EDGE_MODEL",
            "",
        ).strip(),
        device_base_url=os.environ.get(
            "QWENPAW_EDGE_CLOUD_DEVICE_BASE_URL", ""
        ).strip(),
        device_api_key=os.environ.get("QWENPAW_EDGE_CLOUD_DEVICE_API_KEY", "").strip(),
        device_model=os.environ.get("QWENPAW_EDGE_CLOUD_DEVICE_MODEL", "").strip(),
        device_max_tokens=_optional_token_limit("QWENPAW_EDGE_CLOUD_DEVICE_MAX_TOKENS"),
        edge_max_tokens=_optional_token_limit("QWENPAW_EDGE_CLOUD_EDGE_MAX_TOKENS"),
        device_max_tool_rounds=max_rounds,
        log_detail=log_detail,
    )
