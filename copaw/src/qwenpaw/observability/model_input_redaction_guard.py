# -*- coding: utf-8 -*-
"""Confirm sensitive model input handling before LLM calls."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable

from ..config.context import get_current_request_context
from .session_delta_redactor import (
    RedactionFinding,
    RedactionReport,
    redact_model_protocol_value,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 10.0
_MAX_TIMEOUT_SECONDS = 60.0
_SOURCE_TYPE = "model_input_redaction"
_APPROVAL_NAME = "模型上下文敏感数据确认"


def get_approval_service():
    from ..app.approvals import get_approval_service as _get_approval_service

    return _get_approval_service()


def maybe_wrap_model_for_redaction_confirm(
    agent: Any,
    *,
    turn_scope: "TurnRedactionScope | None" = None,
) -> Callable[[], None] | None:
    """Wrap ``agent.model`` when request-level model input redaction is on."""

    if turn_scope is not None:
        request_context = turn_scope.request_context
        config = turn_scope.config
        sensitive_words = turn_scope.sensitive_words
    else:
        request_context = get_current_request_context()
        config = _model_input_redaction_config(request_context)
        sensitive_words = request_context.get("sensitive_words")

    if not _enabled(config, sensitive_words):
        return None

    original_model = getattr(agent, "model", None)
    if original_model is None:
        return None

    guard = ModelInputRedactionGuard(
        original_model=original_model,
        request_context=request_context,
        sensitive_words=sensitive_words,
        config=config,
        turn_scope=turn_scope,
    )
    setattr(agent, "model", guard)

    def restore() -> None:
        setattr(agent, "model", original_model)

    return restore


class TurnRedactionScope:
    """Track and redact only data introduced during the current turn."""

    def __init__(
        self,
        *,
        agent: Any,
        request_context: dict[str, Any] | None = None,
    ) -> None:
        self.agent = agent
        self.request_context = request_context or get_current_request_context()
        self.config = _model_input_redaction_config(self.request_context)
        self.sensitive_words = self.request_context.get("sensitive_words")
        self.enabled = _enabled(self.config, self.sensitive_words)
        self._decision_cache: dict[str, Any] = {}
        context = self._context()
        self._baseline_len = len(context)
        self._baseline_ids = {id(item) for item in context}
        self._processed: set[str] = set()

    def _context(self) -> list[Any]:
        state = getattr(self.agent, "state", None)
        context = getattr(state, "context", None)
        return context if isinstance(context, list) else []

    async def prepare_initial_inputs(self, msgs: list[Any]) -> list[Any]:
        if not self.enabled or not msgs:
            return msgs

        redacted, report = redact_model_protocol_value(
            msgs,
            sensitive_words=self.sensitive_words,
            use_defaults=False,
        )
        if not report.matched:
            self._mark_processed(msgs)
            return msgs

        decision = await self._decision_for(report, redacted)
        if _decision_value(decision) == "approved":
            self._mark_processed(msgs)
            return msgs

        if isinstance(redacted, list):
            self._mark_processed(redacted)
            return redacted
        return msgs

    async def prepare_model_call(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        if not self.enabled:
            return args, kwargs

        current_messages = self._unprocessed_current_turn_messages()
        if not current_messages:
            return args, kwargs

        redacted_messages, report = redact_model_protocol_value(
            current_messages,
            sensitive_words=self.sensitive_words,
            use_defaults=False,
        )
        if not report.matched:
            self._mark_processed(current_messages)
            return args, kwargs

        decision = await self._decision_for(report, redacted_messages)
        if _decision_value(decision) == "approved":
            self._mark_processed(current_messages)
            return args, kwargs

        if isinstance(redacted_messages, list):
            self._replace_current_turn_messages(
                current_messages,
                redacted_messages,
            )
            self._mark_processed(redacted_messages)

        redacted_args, redacted_kwargs, _ = _redact_model_call_messages(
            args,
            kwargs,
            sensitive_words=self.sensitive_words,
        )
        return tuple(redacted_args), redacted_kwargs

    async def finalize(self) -> None:
        if self.enabled:
            await self.prepare_model_call((), {})

    def _unprocessed_current_turn_messages(self) -> list[Any]:
        messages = []
        for index, item in enumerate(self._context()):
            if index < self._baseline_len and id(item) in self._baseline_ids:
                continue
            if self._fingerprint(item) in self._processed:
                continue
            messages.append(item)
        return messages

    def _replace_current_turn_messages(
        self,
        originals: list[Any],
        redacted: list[Any],
    ) -> None:
        context = self._context()
        original_ids = {id(item): idx for idx, item in enumerate(originals)}
        original_fingerprints = {
            self._fingerprint(item): idx for idx, item in enumerate(originals)
        }
        for index, item in enumerate(context):
            replacement_index = original_ids.get(id(item))
            if replacement_index is None:
                replacement_index = original_fingerprints.get(
                    self._fingerprint(item),
                )
            if replacement_index is None or replacement_index >= len(redacted):
                continue
            context[index] = _clone_value(redacted[replacement_index])

    async def _decision_for(
        self,
        report: RedactionReport,
        redacted_payload: Any,
    ) -> Any:
        if str(self.config.get("mode") or "").strip().lower() == "auto":
            return "denied"
        return await _confirm_redaction(
            request_context=self.request_context,
            config=self.config,
            report=report,
            redacted_payload=redacted_payload,
            decision_cache=self._decision_cache,
        )

    def _mark_processed(self, value: Any) -> None:
        values = value if isinstance(value, list) else [value]
        for item in values:
            self._processed.add(self._fingerprint(item))

    @staticmethod
    def _fingerprint(value: Any) -> str:
        try:
            body = json.dumps(
                _serializable(value),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        except Exception:
            body = repr(value)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ModelInputRedactionGuard:
    """Proxy a model and confirm sensitive inputs before each call."""

    def __init__(
        self,
        *,
        original_model: Any,
        request_context: dict[str, Any],
        sensitive_words: dict[str, Any],
        config: dict[str, Any],
        turn_scope: TurnRedactionScope | None = None,
    ) -> None:
        self._original_model = original_model
        self._request_context = request_context
        self._sensitive_words = sensitive_words
        self._config = config
        self._decision_cache: dict[str, Any] = {}
        self._turn_scope = turn_scope

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original_model, name)

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._turn_scope is not None:
            redacted_args, redacted_kwargs = await self._turn_scope.prepare_model_call(
                args,
                kwargs,
            )
            return await self._original_model(*redacted_args, **redacted_kwargs)

        payload = {"args": args, "kwargs": kwargs}
        redacted_args, redacted_kwargs, report = _redact_model_call_messages(
            args,
            kwargs,
            sensitive_words=self._sensitive_words,
        )
        redacted_payload = {"args": redacted_args, "kwargs": redacted_kwargs}
        if not report.matched:
            return await self._original_model(*args, **kwargs)

        if str(self._config.get("mode") or "").strip().lower() == "auto":
            return await self._call_with_payload(redacted_payload)

        decision = await self._confirm(report, redacted_payload)
        if _decision_value(decision) == "approved":
            return await self._original_model(*args, **kwargs)
        return await self._call_with_payload(redacted_payload)

    async def _call_with_payload(self, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return await self._original_model(payload)
        redacted_args = payload.get("args") or ()
        redacted_kwargs = payload.get("kwargs") or {}
        if not isinstance(redacted_args, (list, tuple)):
            redacted_args = (redacted_args,)
        if not isinstance(redacted_kwargs, dict):
            redacted_kwargs = {}
        return await self._original_model(*tuple(redacted_args), **redacted_kwargs)

    async def _confirm(
        self,
        report: RedactionReport,
        redacted_payload: Any,
    ) -> Any:
        return await _confirm_redaction(
            request_context=self._request_context,
            config=self._config,
            report=report,
            redacted_payload=redacted_payload,
            decision_cache=self._decision_cache,
        )


async def _confirm_redaction(
    *,
    request_context: dict[str, Any],
    config: dict[str, Any],
    report: RedactionReport,
    redacted_payload: Any,
    decision_cache: dict[str, Any],
) -> Any:
    cache_key = _approval_cache_key(redacted_payload, report)
    cached = decision_cache.get(cache_key)
    if cached is not None:
        return cached

    from ..app.approvals.models import ApprovalRequestSummary
    from ..security.tool_guard.approval import ApprovalDecision

    timeout_seconds = _timeout_seconds(config)
    svc = get_approval_service()
    ctx = request_context
    session_id = str(ctx.get("session_id") or "")
    pending = await svc.create_pending_summary(
        session_id=session_id,
        root_session_id=str(ctx.get("root_session_id") or session_id),
        owner_agent_id=str(ctx.get("root_agent_id") or "unknown"),
        user_id=str(ctx.get("user_id") or ""),
        channel=str(ctx.get("channel") or ""),
        agent_id=str(
            ctx.get("agent_id")
            or ctx.get("root_agent_id")
            or "unknown",
        ),
        summary=ApprovalRequestSummary(
            source_type=_SOURCE_TYPE,
            name=_APPROVAL_NAME,
            severity="medium",
            findings_count=sum(item.count for item in report.findings),
            result_summary=_format_result_summary(report, timeout_seconds),
            payload={
                "action_on_approve": "use_original",
                "action_on_deny": "use_redacted",
                "action_on_timeout": "use_redacted",
            },
        ),
        timeout_seconds=timeout_seconds,
        extra={
            "channel_meta": ctx.get("channel_meta"),
            "_channel_instance": ctx.get("_channel_instance"),
            "display": {
                "tool_name": _APPROVAL_NAME,
                "tool_source": "Model Input Redaction",
                "exact_target": "使用原始上下文投喂大模型",
                "similar_target": "使用脱敏上下文投喂大模型",
                "is_generalized": False,
            },
        },
    )
    try:
        decision = await svc.wait_for_approval(
            pending.request_id,
            timeout_seconds,
        )
    except Exception:
        logger.warning(
            "model input redaction approval failed; using redacted input",
            exc_info=True,
        )
        decision = ApprovalDecision.TIMEOUT

    if _decision_value(decision) not in {"approved", "denied", "timeout"}:
        decision = ApprovalDecision.TIMEOUT
    decision_cache[cache_key] = decision
    return decision


def _redact_model_call_messages(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    sensitive_words: dict[str, Any],
) -> tuple[tuple[Any, ...], dict[str, Any], RedactionReport]:
    findings: list[RedactionFinding] = []
    redacted_args = []

    for item in args:
        if _is_model_message_payload(item):
            redacted, report = redact_model_protocol_value(
                item,
                sensitive_words=sensitive_words,
                use_defaults=False,
            )
            findings.extend(report.findings)
            redacted_args.append(redacted)
        else:
            redacted_args.append(item)

    redacted_kwargs = dict(kwargs)
    for key in ("messages", "input"):
        if key not in redacted_kwargs:
            continue
        redacted, report = redact_model_protocol_value(
            redacted_kwargs[key],
            sensitive_words=sensitive_words,
            use_defaults=False,
        )
        findings.extend(report.findings)
        redacted_kwargs[key] = redacted

    return (
        tuple(redacted_args),
        redacted_kwargs,
        RedactionReport(matched=bool(findings), findings=findings),
    )


def _is_model_message_payload(value: Any) -> bool:
    if isinstance(value, str):
        return True
    if isinstance(value, dict):
        return "content" in value or "content_blocks" in value
    if hasattr(value, "content") or hasattr(value, "content_blocks"):
        return True
    if isinstance(value, (list, tuple)):
        return any(_is_model_message_payload(item) for item in value)
    return False


def _model_input_redaction_config(
    request_context: dict[str, Any],
) -> dict[str, Any]:
    raw = request_context.get("model_input_redaction")
    return raw if isinstance(raw, dict) else {}


def _enabled(config: dict[str, Any], sensitive_words: Any) -> bool:
    if not isinstance(sensitive_words, dict):
        return False
    patterns = sensitive_words.get("patterns")
    if not isinstance(patterns, list) or not patterns:
        return False
    mode = str(config.get("mode") or "off").strip().lower()
    return mode in {"confirm", "auto"}


def _timeout_seconds(config: dict[str, Any]) -> float:
    raw = config.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = _DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        value = _DEFAULT_TIMEOUT_SECONDS
    return min(value, _MAX_TIMEOUT_SECONDS)


def _decision_value(decision: Any) -> str:
    value = getattr(decision, "value", decision)
    return str(value or "").strip().lower()


def _serializable(value: Any) -> Any:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:
            pass
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json")
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): _serializable(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_serializable(item) for item in value]
    if isinstance(value, tuple):
        return [_serializable(item) for item in value]
    return value


def _clone_value(value: Any) -> Any:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            value_dict = to_dict()
            from_dict = getattr(type(value), "from_dict", None)
            if callable(from_dict):
                return from_dict(value_dict)
        except Exception:
            pass
    return value


def _format_result_summary(
    report: RedactionReport,
    timeout_seconds: float,
) -> str:
    total = sum(item.count for item in report.findings)
    samples = []
    seen = set()
    for item in report.findings:
        if item.sample and item.sample not in seen:
            samples.append(item.sample)
            seen.add(item.sample)
        if len(samples) >= 3:
            break

    lines = [
        "检测到即将发送给大模型的上下文包含敏感数据。",
        f"共检测到 {total} 处。",
    ]
    if samples:
        lines.append("脱敏示例：" + "、".join(samples))
    lines.extend(
        [
            "",
            "是否允许使用原始数据投喂大模型？",
            "同意：使用原始数据",
            "拒绝：使用脱敏数据",
            f"{int(timeout_seconds)} 秒未确认：自动使用脱敏数据",
        ],
    )
    return "\n".join(lines)


def _approval_cache_key(payload: Any, report: RedactionReport) -> str:
    try:
        body = json.dumps(
            {
                "payload": payload,
                "findings": [
                    {
                        "key": item.key,
                        "count": item.count,
                        "sample": item.sample,
                    }
                    for item in report.findings
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    except Exception:
        body = repr(payload)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


__all__ = [
    "ModelInputRedactionGuard",
    "TurnRedactionScope",
    "maybe_wrap_model_for_redaction_confirm",
]
