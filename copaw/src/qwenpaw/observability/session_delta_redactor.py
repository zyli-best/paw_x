# -*- coding: utf-8 -*-
"""Redact sensitive data before session deltas leave the local machine."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Callable

URL_VALUE_KEYS = {"file_url", "image_url", "video_url"}
SECRET_EXACT_VALUE_KEYS = {
    "api_key",
    "api-key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "secret_key",
    "access_token",
    "bearer_token",
    "refresh_token",
    "token",
}
AGENT_CALL_TEXT_KEYS = {
    "input",
    "output",
    "text",
    "content",
    "arguments",
    "error",
    "message",
    "summary",
}
MAX_RULES = 20
MAX_PATTERN_LENGTH = 256
MAX_KEYWORD_LENGTH = 128
MAX_NAME_LENGTH = 64
MAX_KEEP = 16
REDACTION_MARK = "*"


@dataclass(frozen=True)
class RedactionRule:
    type: str
    key: str
    name: str
    keyword: str | None
    pattern: str
    keep_head: int
    keep_tail: int
    validator: Callable[[str], bool] | None = None
    trusted_pattern: bool = False


@dataclass(frozen=True)
class RedactionFinding:
    key: str
    name: str
    count: int
    sample: str


@dataclass(frozen=True)
class RedactionReport:
    matched: bool
    findings: list[RedactionFinding]


@dataclass
class _FindingCounter:
    key: str
    name: str
    count: int = 0
    sample: str = ""


DEFAULT_RULES: tuple[RedactionRule, ...] = (
    RedactionRule(
        type="builtin",
        key="phone",
        name="phone",
        keyword=None,
        pattern=r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)",
        keep_head=3,
        keep_tail=4,
        trusted_pattern=True,
    ),
    RedactionRule(
        type="builtin",
        key="id_card",
        name="id_card",
        keyword=None,
        pattern=(
            r"(?<!\d)\d{6}(?:19|20)\d{2}"
            r"(?:0[1-9]|1[0-2])"
            r"(?:0[1-9]|[12]\d|3[01])"
            r"\d{3}[\dXx](?!\d)"
        ),
        keep_head=4,
        keep_tail=4,
        validator=lambda value: _valid_cn_id_card(value),
        trusted_pattern=True,
    ),
    RedactionRule(
        type="builtin",
        key="email",
        name="email",
        keyword=None,
        pattern=r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])",
        keep_head=1,
        keep_tail=0,
        trusted_pattern=True,
    ),
    RedactionRule(
        type="builtin",
        key="bank_card",
        name="bank_card",
        keyword=None,
        pattern=r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)",
        keep_head=4,
        keep_tail=4,
        validator=lambda value: _valid_luhn(_digits_only(value)),
        trusted_pattern=True,
    ),
    RedactionRule(
        type="builtin",
        key="secret",
        name="secret",
        keyword=None,
        pattern=(
            r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"
            r"|\bsk-[A-Za-z0-9_-]{16,}\b"
            r"|\bAKIA[0-9A-Z]{16}\b"
            r"|\bgithub_pat_[A-Za-z0-9_]{20,}\b"
            r"|\bgh[pousr]_[A-Za-z0-9_]{20,}\b"
            r"|\beyJ[A-Za-z0-9_-]{10,}"
            r"\.[A-Za-z0-9_-]{10,}"
            r"\.[A-Za-z0-9_-]{10,}\b"
        ),
        keep_head=0,
        keep_tail=0,
        trusted_pattern=True,
    ),
)


def _redact_edge_cloud_payload(
    payload: dict[str, Any],
    *,
    rules: tuple[RedactionRule, ...],
    findings: dict[str, _FindingCounter] | None = None,
) -> None:
    """Redact v2 session-level configs and per-stage model requests.

    Config prompts are natural-language instructions; tool schemas can carry
    user-provided defaults. Stage messages use the same protocol-aware rules
    as the legacy delta, and stage outcomes may contain model-authored text.
    """

    configs = payload.get("configs")
    if isinstance(configs, list):
        for config in configs:
            if not isinstance(config, dict):
                continue
            system_prompt = config.get("system_prompt")
            if isinstance(system_prompt, str):
                config["system_prompt"] = _redact_text(
                    system_prompt,
                    rules,
                    findings=findings,
                )
            if "tools" in config:
                config["tools"] = _redact_value(
                    config["tools"],
                    path=("tools",),
                    rules=rules,
                    findings=findings,
                )

    for key in list(payload):
        if not key.startswith("stage."):
            continue
        stage = payload.get(key)
        if not isinstance(stage, dict):
            continue
        messages = stage.get("messages")
        if messages is not None:
            stage["messages"] = _redact_model_protocol_value_with_rules(
                messages,
                rules=rules,
                findings=findings,
            )
        model = stage.get("model")
        if isinstance(model, dict):
            for field in (
                "params",
                "model_parameters",
                "generate_kwargs",
            ):
                if field in model:
                    model[field] = _redact_value(
                        model[field],
                        path=(key, "model", field),
                        rules=rules,
                        findings=findings,
                    )
        outcome = stage.get("outcome")
        if isinstance(outcome, dict):
            stage["outcome"] = _redact_value(
                outcome,
                path=(key, "outcome"),
                rules=rules,
                findings=findings,
            )

    edge_cloud = payload.get("edge_cloud")
    if isinstance(edge_cloud, dict):
        fallback_reason = edge_cloud.get("fallback_reason")
        if isinstance(fallback_reason, str):
            edge_cloud["fallback_reason"] = _redact_text(
                fallback_reason,
                rules,
                findings=findings,
            )


def redact_model_protocol_value(
    value: Any,
    *,
    sensitive_words: dict[str, Any] | None = None,
    use_defaults: bool = False,
) -> tuple[Any, RedactionReport]:
    """Redact only ReAct/model protocol-visible content.

    This intentionally avoids scanning protocol fields such as message ids,
    tool call ids, metadata, roles, and session identifiers.  It handles
    AgentScope ``Msg`` objects, dict-shaped messages, text/thinking blocks,
    and tool-result output blocks.
    """

    rules = _load_rules(sensitive_words, use_defaults=use_defaults)
    if not rules:
        return copy.deepcopy(value), RedactionReport(matched=False, findings=[])

    counters: dict[str, _FindingCounter] = {}
    redacted = _redact_model_protocol_value_with_rules(
        value,
        rules=rules,
        findings=counters,
    )
    return redacted, _report_from_counters(counters)


def redact_sensitive_value(
    value: Any,
    *,
    sensitive_words: dict[str, Any] | None = None,
    use_defaults: bool = False,
) -> tuple[Any, RedactionReport]:
    """Return a redacted copy plus a compact match report.

    ``use_defaults=False`` is intentionally supported for model-input
    redaction: only the patterns explicitly supplied by the request should
    affect what is sent to the LLM.
    """

    rules = _load_rules(sensitive_words, use_defaults=use_defaults)
    if not rules:
        return copy.deepcopy(value), RedactionReport(matched=False, findings=[])

    counters: dict[str, _FindingCounter] = {}
    redacted = _redact_value(
        value,
        path=(),
        rules=rules,
        findings=counters,
    )
    return redacted, _report_from_counters(counters)


def _report_from_counters(
    counters: dict[str, _FindingCounter],
) -> RedactionReport:
    findings = [
        RedactionFinding(
            key=item.key,
            name=item.name,
            count=item.count,
            sample=item.sample,
        )
        for item in counters.values()
        if item.count > 0
    ]
    return RedactionReport(matched=bool(findings), findings=findings)


def redact_model_protocol_value(
    value: Any,
    *,
    sensitive_words: dict[str, Any] | None = None,
    use_defaults: bool = False,
) -> tuple[Any, RedactionReport]:
    """Redact only model-visible protocol text fields.

    This is intentionally narrower than :func:`redact_sensitive_value`.
    AgentScope/QwenPaw messages contain protocol identifiers such as message
    ids, tool call ids, thinking ids, trace ids, and metadata. Those fields
    are not natural-language context and must not be regex-redacted.
    """

    rules = _load_rules(sensitive_words, use_defaults=use_defaults)
    if not rules:
        return copy.deepcopy(value), RedactionReport(matched=False, findings=[])

    counters: dict[str, _FindingCounter] = {}
    redacted = copy.deepcopy(value)
    if isinstance(redacted, str):
        redacted = _redact_text(redacted, rules, findings=counters)
    else:
        _redact_protocol_messages_in_place(
            redacted,
            rules=rules,
            findings=counters,
        )
    findings = [
        RedactionFinding(
            key=item.key,
            name=item.name,
            count=item.count,
            sample=item.sample,
        )
        for item in counters.values()
        if item.count > 0
    ]
    return redacted, RedactionReport(
        matched=bool(findings),
        findings=findings,
    )


def _load_rules(
    sensitive_words: dict[str, Any] | None,
    *,
    use_defaults: bool = True,
) -> tuple[RedactionRule, ...]:
    rules_by_key = {rule.key: rule for rule in DEFAULT_RULES} if use_defaults else {}
    custom_rules: list[RedactionRule] = []

    raw_patterns = None
    if isinstance(sensitive_words, dict):
        raw_patterns = sensitive_words.get("patterns")
    if not isinstance(raw_patterns, list):
        return tuple(rules_by_key.values())

    for raw in raw_patterns[:MAX_RULES]:
        rule = _coerce_rule(raw)
        if rule is None:
            continue
        if rule.type == "builtin" and rule.key:
            base = rules_by_key.get(rule.key)
            rules_by_key[rule.key] = _merge_builtin_rule(base, rule)
        else:
            custom_rules.append(rule)

    return tuple(rules_by_key.values()) + tuple(custom_rules)


def _merge_builtin_rule(
    base: RedactionRule | None,
    override: RedactionRule,
) -> RedactionRule:
    validator = base.validator if base is not None else _validator_for_key(override.key)
    pattern = override.pattern or (base.pattern if base is not None else "")
    name = override.name or (base.name if base is not None else override.key)
    keep_head = (
        base.keep_head
        if base is not None and override.keep_head < 0
        else max(0, override.keep_head)
    )
    keep_tail = (
        base.keep_tail
        if base is not None and override.keep_tail < 0
        else max(0, override.keep_tail)
    )
    trusted_pattern = bool(
        base is not None and not override.pattern and base.trusted_pattern,
    )
    return RedactionRule(
        type="builtin",
        key=override.key,
        name=name,
        keyword=override.keyword,
        pattern=pattern,
        keep_head=keep_head,
        keep_tail=keep_tail,
        validator=validator,
        trusted_pattern=trusted_pattern,
    )


def _coerce_rule(raw: Any) -> RedactionRule | None:
    if not isinstance(raw, dict):
        return None

    rule_type = str(raw.get("type") or "custom").strip().lower()
    if rule_type not in {"builtin", "custom"}:
        rule_type = "custom"

    key = str(raw.get("key") or "").strip().lower()[:MAX_NAME_LENGTH]
    name = str(raw.get("name") or key or rule_type).strip()[:MAX_NAME_LENGTH]
    keyword = raw.get("keyword")
    if keyword is not None:
        keyword = str(keyword)[:MAX_KEYWORD_LENGTH]
    pattern = str(raw.get("pattern") or "")
    if len(pattern) > MAX_PATTERN_LENGTH:
        return None

    if rule_type == "custom" and not (keyword or pattern):
        return None
    if rule_type == "builtin" and not key:
        return None

    keep_default = -1 if rule_type == "builtin" else 0
    return RedactionRule(
        type=rule_type,
        key=key,
        name=name,
        keyword=keyword,
        pattern=pattern,
        keep_head=_bounded_keep(raw.get("keep_head"), keep_default),
        keep_tail=_bounded_keep(raw.get("keep_tail"), keep_default),
        validator=_validator_for_key(key) if rule_type == "builtin" else None,
        trusted_pattern=False,
    )


def _bounded_int(value: Any, default: int) -> int:
    try:
        raw = int(value)
    except (TypeError, ValueError):
        raw = default
    return max(0, min(raw, MAX_KEEP))


def _bounded_keep(value: Any, default: int) -> int:
    if value is None and default < 0:
        return default
    return _bounded_int(value, default)


def _validator_for_key(key: str) -> Callable[[str], bool] | None:
    if key == "id_card":
        return _valid_cn_id_card
    if key == "bank_card":
        return lambda value: _valid_luhn(_digits_only(value))
    return None


def _get_protocol_field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _set_protocol_field(value: Any, key: str, new_value: Any) -> None:
    if isinstance(value, dict):
        value[key] = new_value
        return
    try:
        setattr(value, key, new_value)
    except (AttributeError, ValueError):
        pass


def _protocol_block_type(value: Any) -> str:
    raw = _get_protocol_field(value, "type", "")
    if hasattr(raw, "value"):
        raw = raw.value
    return str(raw or "")


def _has_protocol_field(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value
    return hasattr(value, key)


def _redact_model_protocol_value_with_rules(
    value: Any,
    *,
    rules: tuple[RedactionRule, ...],
    findings: dict[str, _FindingCounter] | None = None,
) -> Any:
    if isinstance(value, str):
        return _redact_text(value, rules, findings=findings)
    if isinstance(value, list):
        return [
            _redact_protocol_message_or_block(
                item,
                rules=rules,
                findings=findings,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _redact_protocol_message_or_block(
                item,
                rules=rules,
                findings=findings,
            )
            for item in value
        )
    return _redact_protocol_message_or_block(
        value,
        rules=rules,
        findings=findings,
    )


def _redact_protocol_message_or_block(
    value: Any,
    *,
    rules: tuple[RedactionRule, ...],
    findings: dict[str, _FindingCounter] | None = None,
) -> Any:
    if isinstance(value, str):
        return _redact_text(value, rules, findings=findings)
    if isinstance(value, list):
        return [
            _redact_protocol_message_or_block(
                item,
                rules=rules,
                findings=findings,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _redact_protocol_message_or_block(
                item,
                rules=rules,
                findings=findings,
            )
            for item in value
        )

    redacted = copy.deepcopy(value)
    if isinstance(redacted, dict) and (
        "args" in redacted or "kwargs" in redacted
    ):
        if "args" in redacted:
            redacted["args"] = _redact_model_protocol_value_with_rules(
                redacted["args"],
                rules=rules,
                findings=findings,
            )
        kwargs = redacted.get("kwargs")
        if isinstance(kwargs, dict) and "messages" in kwargs:
            kwargs["messages"] = _redact_model_protocol_value_with_rules(
                kwargs["messages"],
                rules=rules,
                findings=findings,
            )
        return redacted

    if _has_protocol_field(redacted, "content"):
        _set_protocol_field(
            redacted,
            "content",
            _redact_protocol_content(
                _get_protocol_field(redacted, "content"),
                rules=rules,
                findings=findings,
            ),
        )
        return redacted

    return _redact_protocol_block(
        redacted,
        rules=rules,
        findings=findings,
    )


def _redact_protocol_content(
    content: Any,
    *,
    rules: tuple[RedactionRule, ...],
    findings: dict[str, _FindingCounter] | None = None,
) -> Any:
    if isinstance(content, str):
        return _redact_text(content, rules, findings=findings)
    if isinstance(content, list):
        return [
            _redact_protocol_block(item, rules=rules, findings=findings)
            for item in content
        ]
    if isinstance(content, tuple):
        return tuple(
            _redact_protocol_block(item, rules=rules, findings=findings)
            for item in content
        )
    return copy.deepcopy(content)


def _redact_protocol_block(
    block: Any,
    *,
    rules: tuple[RedactionRule, ...],
    findings: dict[str, _FindingCounter] | None = None,
) -> Any:
    if isinstance(block, str):
        return _redact_text(block, rules, findings=findings)

    redacted = copy.deepcopy(block)
    block_type = _protocol_block_type(redacted)

    if (
        block_type == "text"
        or (not block_type and _has_protocol_field(redacted, "text"))
    ):
        text = _get_protocol_field(redacted, "text")
        if isinstance(text, str):
            _set_protocol_field(
                redacted,
                "text",
                _redact_text(text, rules, findings=findings),
            )
        return redacted

    if block_type == "thinking":
        thinking = _get_protocol_field(redacted, "thinking")
        if isinstance(thinking, str):
            _set_protocol_field(
                redacted,
                "thinking",
                _redact_text(thinking, rules, findings=findings),
            )
        return redacted

    if block_type == "tool_result":
        output = _get_protocol_field(redacted, "output")
        _set_protocol_field(
            redacted,
            "output",
            _redact_tool_result_output(
                output,
                rules=rules,
                findings=findings,
            ),
        )
        return redacted

    return redacted


def _redact_tool_result_output(
    output: Any,
    *,
    rules: tuple[RedactionRule, ...],
    findings: dict[str, _FindingCounter] | None = None,
) -> Any:
    if isinstance(output, str):
        return _redact_text(output, rules, findings=findings)
    if isinstance(output, list):
        return [
            _redact_protocol_block(item, rules=rules, findings=findings)
            for item in output
        ]
    if isinstance(output, tuple):
        return tuple(
            _redact_protocol_block(item, rules=rules, findings=findings)
            for item in output
        )
    if isinstance(output, dict):
        redacted = copy.deepcopy(output)
        for key in ("text", "content", "output"):
            if key not in redacted:
                continue
            if key == "content":
                redacted[key] = _redact_protocol_content(
                    redacted[key],
                    rules=rules,
                    findings=findings,
                )
            elif isinstance(redacted[key], str):
                redacted[key] = _redact_text(
                    redacted[key],
                    rules,
                    findings=findings,
                )
        return redacted
    return copy.deepcopy(output)


def _redact_agent_calls_with_rules(
    value: Any,
    *,
    rules: tuple[RedactionRule, ...],
    findings: dict[str, _FindingCounter] | None = None,
) -> Any:
    if isinstance(value, list):
        return [
            _redact_agent_calls_with_rules(
                item,
                rules=rules,
                findings=findings,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return copy.deepcopy(value)

    redacted = copy.deepcopy(value)
    for key in AGENT_CALL_TEXT_KEYS:
        if key not in redacted:
            continue
        item = redacted[key]
        if isinstance(item, str):
            redacted[key] = _redact_text(item, rules, findings=findings)
        elif key == "content":
            redacted[key] = _redact_protocol_content(
                item,
                rules=rules,
                findings=findings,
            )
        elif key in {"input", "output"}:
            redacted[key] = _redact_tool_result_output(
                item,
                rules=rules,
                findings=findings,
            )
    return redacted


def _redact_value(
    value: Any,
    *,
    path: tuple[str, ...],
    rules: tuple[RedactionRule, ...],
    findings: dict[str, _FindingCounter] | None = None,
) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                f"[REDACTED:{_secret_key_name(key)}]"
                if _is_secret_key(key)
                else _redact_value(
                    val,
                    path=path + (str(key),),
                    rules=rules,
                    findings=findings,
                )
            )
            for key, val in value.items()
        }

    if isinstance(value, list):
        return [
            _redact_value(item, path=path, rules=rules, findings=findings)
            for item in value
        ]

    if isinstance(value, tuple):
        return tuple(
            _redact_value(item, path=path, rules=rules, findings=findings)
            for item in value
        )

    if isinstance(value, str):
        if path and path[-1] in URL_VALUE_KEYS:
            return _redact_url_query_only(value, rules, findings=findings)
        return _redact_text(value, rules, findings=findings)

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            value_dict = to_dict()
        except Exception:
            value_dict = None
        if isinstance(value_dict, dict):
            redacted_dict = _redact_value(
                value_dict,
                path=path,
                rules=rules,
                findings=findings,
            )
            from_dict = getattr(type(value), "from_dict", None)
            if callable(from_dict):
                try:
                    return from_dict(redacted_dict)
                except Exception:
                    pass
            return redacted_dict

    return copy.deepcopy(value)


def _secret_key_name(key: Any) -> str:
    return str(key or "secret").strip().lower() or "secret"


def _is_secret_key(key: Any) -> bool:
    lowered = _secret_key_name(key)
    return (
        lowered in SECRET_EXACT_VALUE_KEYS
        or lowered.endswith("_api_key")
        or lowered.endswith("-api-key")
        or lowered.endswith("_secret")
        or lowered.endswith("-secret")
        or lowered.endswith("_secret_key")
        or lowered.endswith("-secret-key")
        or lowered.endswith("_token")
        or lowered.endswith("-token")
    )


def _get_protocol_field(value: Any, field: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(field, default)
    return getattr(value, field, default)


def _set_protocol_field(value: Any, field: str, field_value: Any) -> None:
    if isinstance(value, dict):
        value[field] = field_value
        return
    try:
        setattr(value, field, field_value)
    except (AttributeError, ValueError):
        pass


def _protocol_type(value: Any) -> str:
    raw = _get_protocol_field(value, "type", "")
    if hasattr(raw, "value"):
        raw = raw.value
    return str(raw or "")


def _redact_protocol_messages_in_place(
    value: Any,
    *,
    rules: tuple[RedactionRule, ...],
    findings: dict[str, _FindingCounter] | None = None,
) -> None:
    if isinstance(value, list):
        for item in value:
            _redact_protocol_message_in_place(
                item,
                rules=rules,
                findings=findings,
            )
        return
    if isinstance(value, tuple):
        for item in value:
            _redact_protocol_message_in_place(
                item,
                rules=rules,
                findings=findings,
            )
        return
    _redact_protocol_message_in_place(value, rules=rules, findings=findings)


def _redact_protocol_message_in_place(
    message: Any,
    *,
    rules: tuple[RedactionRule, ...],
    findings: dict[str, _FindingCounter] | None = None,
) -> None:
    content = _get_protocol_field(message, "content")
    if isinstance(content, str):
        _set_protocol_field(
            message,
            "content",
            _redact_text(content, rules, findings=findings),
        )
        return
    if isinstance(content, list):
        for block in content:
            _redact_protocol_block_in_place(
                block,
                rules=rules,
                findings=findings,
            )
        return

    content_blocks = _get_protocol_field(message, "content_blocks")
    if isinstance(content_blocks, list):
        for block in content_blocks:
            _redact_protocol_block_in_place(
                block,
                rules=rules,
                findings=findings,
            )


def _redact_protocol_block_in_place(
    block: Any,
    *,
    rules: tuple[RedactionRule, ...],
    findings: dict[str, _FindingCounter] | None = None,
) -> None:
    block_type = _protocol_type(block)
    if block_type == "text":
        text = _get_protocol_field(block, "text")
        if isinstance(text, str):
            _set_protocol_field(
                block,
                "text",
                _redact_text(text, rules, findings=findings),
            )
        return
    if block_type == "thinking":
        thinking = _get_protocol_field(block, "thinking")
        if isinstance(thinking, str):
            _set_protocol_field(
                block,
                "thinking",
                _redact_text(thinking, rules, findings=findings),
            )
        return
    if block_type == "tool_result":
        output = _get_protocol_field(block, "output")
        _set_protocol_field(
            block,
            "output",
            _redact_tool_result_output(output, rules, findings=findings),
        )


def _redact_tool_result_output(
    output: Any,
    rules: tuple[RedactionRule, ...],
    *,
    findings: dict[str, _FindingCounter] | None = None,
) -> Any:
    if isinstance(output, str):
        return _redact_text(output, rules, findings=findings)
    if isinstance(output, list):
        for item in output:
            _redact_protocol_block_in_place(
                item,
                rules=rules,
                findings=findings,
            )
        return output
    if isinstance(output, dict) and _protocol_type(output) in {
        "text",
        "thinking",
    }:
        _redact_protocol_block_in_place(
            output,
            rules=rules,
            findings=findings,
        )
    return output


_AGENT_CALL_TEXT_KEYS = frozenset(
    {
        "input",
        "output",
        "text",
        "content",
        "message",
        "error",
        "summary",
        "result",
        "arguments",
        "raw_text",
        "visible_output",
        "failure_reason",
        "missing_requirements",
        "reason",
        "prompt",
        "system",
        "input_context",
        "user_request",
    },
)


def _redact_agent_calls_in_place(
    value: Any,
    *,
    rules: tuple[RedactionRule, ...],
    findings: dict[str, _FindingCounter] | None = None,
) -> None:
    if isinstance(value, list):
        for item in value:
            _redact_agent_calls_in_place(
                item,
                rules=rules,
                findings=findings,
            )
        return
    if not isinstance(value, dict):
        return
    for key, item in list(value.items()):
        if str(key) not in _AGENT_CALL_TEXT_KEYS:
            continue
        value[key] = _redact_agent_call_text_value(
            item,
            rules=rules,
            findings=findings,
        )


def _redact_agent_call_text_value(
    value: Any,
    *,
    rules: tuple[RedactionRule, ...],
    findings: dict[str, _FindingCounter] | None = None,
) -> Any:
    if isinstance(value, str):
        return _redact_text(value, rules, findings=findings)
    if isinstance(value, list):
        return [
            _redact_agent_call_text_value(
                item,
                rules=rules,
                findings=findings,
            )
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: (
                _redact_agent_call_text_value(
                    item,
                    rules=rules,
                    findings=findings,
                )
                if str(key) in _AGENT_CALL_TEXT_KEYS
                else item
            )
            for key, item in value.items()
        }
    return value


def _redact_delta_url_queries_in_place(
    value: Any,
    *,
    rules: tuple[RedactionRule, ...],
    findings: dict[str, _FindingCounter] | None = None,
) -> None:
    if isinstance(value, list):
        for item in value:
            _redact_delta_url_queries_in_place(
                item,
                rules=rules,
                findings=findings,
            )
        return
    content = _get_protocol_field(value, "content")
    if not isinstance(content, list):
        return
    for block in content:
        for field in URL_VALUE_KEYS:
            url = _get_protocol_field(block, field)
            if isinstance(url, str):
                _set_protocol_field(
                    block,
                    field,
                    _redact_url_query_only(url, rules, findings=findings),
                )


def redact_session_delta_payload(
    payload: dict[str, Any],
    *,
    sensitive_words: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a protocol-aware redacted copy of a session delta payload.

    Langfuse/session-delta uploads should redact the conversation and tool
    summary text, but should not rewrite protocol identifiers such as message
    ids, thinking ids, tool call ids, trace ids, or session ids.
    """

    rules = _load_rules(sensitive_words, use_defaults=True)
    redacted = copy.deepcopy(payload)
    if not isinstance(redacted, dict):
        return {}

    counters: dict[str, _FindingCounter] = {}
    if "delta" in redacted:
        _redact_protocol_messages_in_place(
            redacted["delta"],
            rules=rules,
            findings=counters,
        )
        _redact_delta_url_queries_in_place(
            redacted["delta"],
            rules=rules,
            findings=counters,
        )
    if "agent_calls" in redacted:
        _redact_agent_calls_in_place(
            redacted["agent_calls"],
            rules=rules,
            findings=counters,
        )
    system_prompt = redacted.get("system_prompt")
    if isinstance(system_prompt, str):
        redacted["system_prompt"] = _redact_text(
            system_prompt,
            rules,
            findings=counters,
        )
    if "model_requests" in redacted:
        redacted["model_requests"] = _redact_model_requests(
            redacted["model_requests"],
            rules=rules,
            findings=counters,
        )
    if redacted.get("protocol") == "edge_cloud_trace.v2":
        _redact_edge_cloud_payload(redacted, rules=rules, findings=counters)
    return redacted


def _redact_model_requests(
    value: Any,
    *,
    rules: tuple[RedactionRule, ...],
    findings: dict[str, _FindingCounter] | None = None,
) -> Any:
    if isinstance(value, list):
        return [
            _redact_model_requests(item, rules=rules, findings=findings)
            for item in value
        ]
    if not isinstance(value, dict):
        return copy.deepcopy(value)

    redacted = copy.deepcopy(value)
    for key in ("messages", "input"):
        if key in redacted:
            redacted[key] = _redact_model_protocol_value_with_rules(
                redacted[key],
                rules=rules,
                findings=findings,
            )
    for key in ("params", "model_parameters", "generate_kwargs", "tools"):
        if key in redacted:
            redacted[key] = _redact_value(
                redacted[key],
                path=(key,),
                rules=rules,
                findings=findings,
            )
    return redacted


def _redact_text(
    text: str,
    rules: tuple[RedactionRule, ...],
    *,
    findings: dict[str, _FindingCounter] | None = None,
) -> str:
    redacted = text
    for rule in rules:
        if rule.keyword:
            masked_keyword = _mask_value(rule.keyword, rule)
            if rule.keyword in redacted:
                _record_finding(findings, rule, masked_keyword)
            redacted = redacted.replace(
                rule.keyword,
                masked_keyword,
            )
        pattern = _compile_safe_pattern(
            rule.pattern,
            trusted=rule.trusted_pattern,
        )
        if pattern is None:
            continue
        redacted = pattern.sub(
            lambda match, current_rule=rule: _redact_match(
                match,
                current_rule,
                findings=findings,
            ),
            redacted,
        )
    return redacted


def _redact_match(
    match: re.Match[str],
    rule: RedactionRule,
    *,
    findings: dict[str, _FindingCounter] | None = None,
) -> str:
    value = match.group(0)
    if rule.validator is not None and not rule.validator(value):
        return value
    masked = _mask_value(value, rule)
    if masked != value:
        _record_finding(findings, rule, masked)
    return masked


def _record_finding(
    findings: dict[str, _FindingCounter] | None,
    rule: RedactionRule,
    sample: str,
) -> None:
    if findings is None:
        return
    key = rule.key or rule.name or rule.type or "sensitive"
    item = findings.get(key)
    if item is None:
        item = _FindingCounter(key=key, name=rule.name or key)
        findings[key] = item
    item.count += 1
    if not item.sample:
        item.sample = sample


def _mask_value(value: str, rule: RedactionRule) -> str:
    if not value:
        return value
    keep_head = min(rule.keep_head, len(value))
    keep_tail = min(rule.keep_tail, max(0, len(value) - keep_head))
    if keep_head == 0 and keep_tail == 0:
        return f"[REDACTED:{rule.key or rule.name or 'sensitive'}]"
    head = value[:keep_head]
    tail = value[len(value) - keep_tail :] if keep_tail else ""
    masked_len = max(1, len(value) - keep_head - keep_tail)
    return f"{head}{REDACTION_MARK * masked_len}{tail}"


def _compile_safe_pattern(
    pattern: str,
    *,
    trusted: bool = False,
) -> re.Pattern[str] | None:
    if not pattern or len(pattern) > MAX_PATTERN_LENGTH:
        return None
    if not trusted:
        banned_fragments = (r"\1", r"\2", "(?<=", "(?<!", "(?=", "(?!")
        if any(fragment in pattern for fragment in banned_fragments):
            return None
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _redact_url_query_only(
    value: str,
    rules: tuple[RedactionRule, ...],
    *,
    findings: dict[str, _FindingCounter] | None = None,
) -> str:
    if "?" not in value:
        return value
    base, query = value.split("?", 1)
    if not query:
        return value
    fragment = ""
    if "#" in query:
        query, fragment = query.split("#", 1)
        fragment = f"#{fragment}"
    return f"{base}?{_redact_text(query, rules, findings=findings)}{fragment}"


def _digits_only(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _valid_cn_id_card(value: str) -> bool:
    value = value.strip()
    if len(value) != 18:
        return False
    body = value[:17]
    if not body.isdigit():
        return False
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    checks = "10X98765432"
    total = sum(int(n) * w for n, w in zip(body, weights))
    return checks[total % 11] == value[-1].upper()


def _valid_luhn(value: str) -> bool:
    if not value.isdigit() or not 13 <= len(value) <= 19:
        return False
    checksum = 0
    parity = len(value) % 2
    for index, char in enumerate(value):
        digit = int(char)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


__all__ = [
    "RedactionFinding",
    "RedactionReport",
    "redact_model_protocol_value",
    "redact_sensitive_value",
    "redact_session_delta_payload",
]
