"""Local cloud/edge/device diagnostics without per-role dialogue uploads."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)
_SECRET_KEY = re.compile(
    r"api[-_]?key|authorization|password|secret|credential|access[-_]?token|"
    r"refresh[-_]?token|cookie", re.I,
)
_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+)[^\s\"',}]+|"
    r"((?:api[-_]?key|password|secret|access[-_]?token)"
    r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',}&]+|"
    r"(https?://)[^/@\s]+:[^/@\s]+@",
)


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): "***" if _SECRET_KEY.search(str(k)) else _safe(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe(v) for v in value]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if not isinstance(value, str):
        for method in ("model_dump", "to_dict"):
            convert = getattr(value, method, None)
            if callable(convert):
                return _safe(convert())
        value = str(value)
    return _SECRET_TEXT.sub(
        lambda m: (m.group(1) or m.group(2) or m.group(3)) + "***",
        value,
    )


def log_event(
    detail: str, event: str, *, flow_id: str = "", agent_id: str = "",
    stage: str = "flow", **payload: Any,
) -> None:
    """Never let diagnostics change request execution or serialize credentials."""
    if detail == "off" or not logger.isEnabledFor(logging.INFO):
        return
    try:
        logger.info(
            "benjamin [edge-cloud] flow_id=%s agent_id=%s stage=%s event=%s %s",
            flow_id, agent_id, stage, event,
            json.dumps(_safe(payload), ensure_ascii=False, default=str),
        )
    except Exception:
        logger.debug("benjamin [edge-cloud] diagnostic serialization failed")
