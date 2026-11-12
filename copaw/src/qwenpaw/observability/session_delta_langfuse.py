# -*- coding: utf-8 -*-
"""Upload per-turn session deltas to Langfuse.

This path is intentionally separate from QwenPaw's normal Langfuse tracing.
It only reads ``QWENPAW_SESSION_DELTA_LANGFUSE_*`` environment variables so
operators can send session deltas without enabling the built-in trace hooks.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import sqlite3
from contextlib import closing, nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_DEFAULT_ENABLED = "0"
_DEFAULT_HOST = ""
_DEFAULT_PUBLIC_KEY = ""
_DEFAULT_SECRET_KEY = ""
_DEFAULT_TIMEOUT = "60"
_upload_tails: dict[tuple[Any, str, str], asyncio.Future[None]] = {}
# Fallback de-duplication when the workspace has no history.db.
_sent_config_keys: set[tuple[str, str, str]] = set()


def _first_config_db(payload: dict[str, Any]) -> Path | None:
    workspace_dir = payload.get("workspace_dir")
    session_id = payload.get("session_id")
    if not isinstance(workspace_dir, str) or not workspace_dir:
        return None
    if not isinstance(session_id, str) or not session_id:
        return None
    workspace = Path(workspace_dir)
    return workspace / "history.db" if workspace.is_absolute() else None


def _first_config_exists(db_path: Path, session_id: str) -> bool:
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS session_delta_first_config "
                "(session_id TEXT PRIMARY KEY)",
            )
        return conn.execute(
            "SELECT 1 FROM session_delta_first_config WHERE session_id = ?",
            (session_id,),
        ).fetchone() is not None


def _record_first_config(db_path: Path, session_id: str) -> None:
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO session_delta_first_config "
                "(session_id) VALUES (?)",
                (session_id,),
            )


def _omit_first_config(payload: dict[str, Any]) -> None:
    payload.pop("system_prompt", None)
    requests = payload.get("model_requests")
    if isinstance(requests, list):
        payload["model_requests"] = [
            {key: value for key, value in request.items() if key != "tools"}
            if isinstance(request, dict) else request
            for request in requests
        ]


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE_VALUES


def _langfuse_available() -> bool:
    return importlib.util.find_spec("langfuse") is not None


def is_session_delta_langfuse_enabled() -> bool:
    """Return True when session-delta uploads are explicitly configured."""

    public_key = os.environ.get(
        "QWENPAW_SESSION_DELTA_LANGFUSE_PUBLIC_KEY",
        _DEFAULT_PUBLIC_KEY,
    )
    secret_key = os.environ.get(
        "QWENPAW_SESSION_DELTA_LANGFUSE_SECRET_KEY",
        _DEFAULT_SECRET_KEY,
    )
    return (
        _truthy(
            os.environ.get(
                "QWENPAW_SESSION_DELTA_LANGFUSE_ENABLED",
                _DEFAULT_ENABLED,
            )
        )
        and bool(public_key)
        and bool(secret_key)
        and _langfuse_available()
    )


def _stable_trace_id(payload: dict[str, Any]) -> str:
    """Build a stable trace id so duplicate uploads are de-duplicatable."""

    raw = "|".join(
        str(payload.get(k, ""))
        for k in (
            "agent_id",
            "channel",
            "user_id",
            "session_id",
            "delta_start",
            "delta_end",
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _initial_config_trace_id(payload: dict[str, Any]) -> str:
    raw = "|".join(
        (
            "qwenpaw.session_delta.initial_config",
            str(payload.get("agent_id", "")),
            str(payload.get("channel", "")),
            str(payload.get("user_id", "")),
            str(payload.get("session_id", "")),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _tools_trace_id(payload: dict[str, Any]) -> str:
    raw = "|".join(
        (
            "qwenpaw.session_delta.tools",
            str(payload.get("agent_id", "")),
            str(payload.get("channel", "")),
            str(payload.get("user_id", "")),
            str(payload.get("session_id", "")),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _config_exists(
    db_path: Path,
    session_id: str,
    config_key: str,
    record_type: str,
) -> bool:
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS session_delta_configs "
                "(session_id TEXT NOT NULL, config_key TEXT NOT NULL, "
                "record_type TEXT NOT NULL, content_hash TEXT, "
                "uploaded_at TEXT NOT NULL, "
                "PRIMARY KEY (session_id, config_key, record_type))",
            )
        return conn.execute(
            "SELECT 1 FROM session_delta_configs "
            "WHERE session_id = ? AND config_key = ? AND record_type = ?",
            (session_id, config_key, record_type),
        ).fetchone() is not None


def _record_config(
    db_path: Path,
    session_id: str,
    config_key: str,
    record_type: str,
    content_hash: str,
) -> None:
    from datetime import datetime, timezone

    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS session_delta_configs "
                "(session_id TEXT NOT NULL, config_key TEXT NOT NULL, "
                "record_type TEXT NOT NULL, content_hash TEXT, "
                "uploaded_at TEXT NOT NULL, "
                "PRIMARY KEY (session_id, config_key, record_type))",
            )
            conn.execute(
                "INSERT OR IGNORE INTO session_delta_configs "
                "(session_id, config_key, record_type, content_hash, "
                "uploaded_at) VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    config_key,
                    record_type,
                    content_hash,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )


def _edge_cloud_config_trace_id(
    payload: dict[str, Any],
    config: dict[str, Any],
) -> str:
    raw = "|".join(
        (
            "qwenpaw.edge_cloud.config",
            str(payload.get("agent_id", "")),
            str(payload.get("session_id", "")),
            str(config.get("config_key", "")),
            str(config.get("record_type", "")),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _edge_cloud_delta_trace_id(payload: dict[str, Any]) -> str:
    edge_cloud = payload.get("edge_cloud")
    edge_cloud = edge_cloud if isinstance(edge_cloud, dict) else {}
    raw = "|".join(
        (
            "qwenpaw.edge_cloud.delta",
            str(payload.get("agent_id", "")),
            str(payload.get("session_id", "")),
            str(edge_cloud.get("flow_id") or payload.get("turn_id", "")),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        "agent_id": payload.get("agent_id"),
        "root_agent_id": payload.get("root_agent_id"),
        "session_id": payload.get("session_id"),
        "root_session_id": payload.get("root_session_id"),
        "channel": payload.get("channel"),
        "user_id": payload.get("user_id"),
        "delta_start": payload.get("delta_start"),
        "delta_end": payload.get("delta_end"),
        "delta_count": payload.get("delta_count"),
        "agent_call_count": len(payload.get("agent_calls", []) or []),
        "model_request_count": len(payload.get("model_requests", []) or []),
        "session_skill_count": len(payload.get("session_skills", []) or []),
        "source": "qwenpaw.session_delta",
    }
    edge_cloud = payload.get("edge_cloud")
    if isinstance(edge_cloud, dict):
        metadata.update(
            {
                "edge_cloud_enabled": edge_cloud.get("enabled", True),
                "edge_cloud_flow_id": edge_cloud.get("flow_id"),
                "edge_cloud_final_agent": edge_cloud.get("final_agent"),
                "edge_cloud_fallback": edge_cloud.get("fallback"),
                "edge_cloud_fallback_reason": edge_cloud.get(
                    "fallback_reason",
                ),
                "edge_cloud_base_agent_id": edge_cloud.get("base_agent_id"),
                "edge_cloud_stage_count": len(
                    edge_cloud.get("stages", []) or []
                ),
            },
        )
        router = edge_cloud.get("router")
        result = router.get("result") if isinstance(router, dict) else None
        parsed = result.get("parsed") if isinstance(result, dict) else None
        if isinstance(parsed, dict):
            metadata["edge_cloud_router_task_type"] = parsed.get(
                "task_type",
            )
    return metadata


def _ascii_id(value: Any) -> str | None:
    """Coerce an id to the US-ASCII, <=200-char shape Langfuse requires.

    Langfuse drops trace-level attribute values that are not strings or
    exceed 200 chars, and documents user_id / session_id as US-ASCII.
    Non-ASCII ids are percent-encoded and over-long ids are hashed so the
    user/session dimension is never silently lost.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if not text.isascii():
        text = quote(text, safe="")
    if len(text) > 200:
        text = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    return text


def _propagate_context(user_id: str | None, session_id: str | None) -> Any:
    """Return an active-trace-attribute context for one upload.

    ``start_observation`` takes no user_id and Langfuse spans expose no
    trace-attribute setter in 4.x; ``propagate_attributes`` is the
    supported way to put ``user.id`` / ``session.id`` on the span created
    inside the context. Degrades to a no-op on older SDKs.
    """
    try:
        from langfuse import propagate_attributes
    except ImportError:
        return nullcontext()
    return propagate_attributes(user_id=user_id, session_id=session_id)


def _sdk_warning_or_error(record: logging.LogRecord) -> bool:
    return record.levelno >= logging.WARNING


def _client() -> Any:
    from langfuse import Langfuse  # type: ignore[import]

    sdk_logger = logging.getLogger("langfuse")
    sdk_logger.setLevel(logging.WARNING)
    # The SDK can re-enable DEBUG via LANGFUSE_DEBUG or another client.
    # addFilter is idempotent for the same callable; never print span payloads.
    sdk_logger.addFilter(_sdk_warning_or_error)

    return Langfuse(
        public_key=os.environ.get(
            "QWENPAW_SESSION_DELTA_LANGFUSE_PUBLIC_KEY",
            _DEFAULT_PUBLIC_KEY,
        ),
        secret_key=os.environ.get(
            "QWENPAW_SESSION_DELTA_LANGFUSE_SECRET_KEY",
            _DEFAULT_SECRET_KEY,
        ),
        host=os.environ.get("QWENPAW_SESSION_DELTA_LANGFUSE_HOST")
        or _DEFAULT_HOST,
        timeout=int(
            os.environ.get(
                "QWENPAW_SESSION_DELTA_LANGFUSE_TIMEOUT", _DEFAULT_TIMEOUT,
            ),
        ),
        debug=False,
        # Real-time processing of OTLP-ingested spans on the v4 data model;
        # without it the server may delay visibility by up to 10 minutes.
        additional_headers={"x-langfuse-ingestion-version": "4"},
    )


def _flush_client(client: Any) -> None:
    flush = getattr(client, "flush", None)
    if callable(flush):
        flush()


def _send_initial_config_span(
    client: Any,
    payload: dict[str, Any],
    trace_id: str,
    initial_config: dict[str, Any],
    system_prompt_chars: int,
) -> None:
    """Upload the initial_config (system_prompt) span, flushed alone."""

    try:
        observation = client.start_observation(
            as_type="span",
            name="qwenpaw.session_delta",
            input=initial_config,
            metadata={
                "source": "qwenpaw.session_delta",
                "session_id": payload.get("session_id"),
                "root_session_id": payload.get("root_session_id"),
                "system_prompt_chars": system_prompt_chars,
            },
            trace_context={"trace_id": trace_id},
        )
        observation.end()
    finally:
        _flush_client(client)


def _send_tools_span(
    client: Any,
    payload: dict[str, Any],
    trace_id: str,
    tools: Any,
) -> None:
    """Upload the tools snapshot span and flush it on its own."""

    tools_input = {
        "session_id": payload.get("session_id"),
        "tools": tools,
    }
    try:
        observation = client.start_observation(
            as_type="span",
            name="qwenpaw.session_delta",
            input=tools_input,
            metadata={
                "source": "qwenpaw.session_delta",
                "session_id": payload.get("session_id"),
                "root_session_id": payload.get("root_session_id"),
                "tool_count": len(tools) if isinstance(tools, list) else 0,
            },
            trace_context={"trace_id": trace_id},
        )
        observation.end()
    finally:
        _flush_client(client)


def _strip_thinking_blocks(delta: Any) -> Any:
    """Return delta messages without thinking content blocks."""

    if not isinstance(delta, list):
        return delta
    stripped: list[Any] = []
    for message in delta:
        if not isinstance(message, dict):
            stripped.append(message)
            continue
        updated = message
        for key in ("content", "content_blocks"):
            blocks = message.get(key)
            if not isinstance(blocks, list):
                continue
            kept = [
                block
                for block in blocks
                if not (
                    isinstance(block, dict)
                    and block.get("type") == "thinking"
                )
            ]
            if len(kept) != len(blocks):
                if updated is message:
                    updated = dict(message)
                updated[key] = kept
        stripped.append(updated)
    return stripped


def _send_delta_span(
    client: Any,
    payload: dict[str, Any],
    trace_id: str,
    delta_requests: list[Any],
) -> None:
    """Upload the delta span and flush it on its own."""

    delta_input = {
        "session_id": payload.get("session_id"),
        "agent_calls": payload.get("agent_calls", []),
        "delta": _strip_thinking_blocks(payload.get("delta", [])),
        "session_skills": payload.get("session_skills", []),
        "model_requests": delta_requests,
        "edge_cloud": payload.get("edge_cloud"),
    }
    try:
        observation = client.start_observation(
            as_type="span",
            name="qwenpaw.session_delta",
            input=delta_input,
            metadata=_metadata(payload),
            trace_context={"trace_id": trace_id},
        )
        try:
            observation.update(
                output={
                    "status": "uploaded",
                    "delta_count": payload.get("delta_count", 0),
                    "agent_call_count": len(
                        payload.get("agent_calls", []) or []
                    ),
                },
            )
        finally:
            observation.end()
    finally:
        _flush_client(client)


def _send_edge_cloud_config_span(
    client: Any,
    payload: dict[str, Any],
    config: dict[str, Any],
) -> None:
    record_type = str(config.get("record_type") or "config.prompt")
    name = (
        "qwenpaw.session_delta.config.prompt"
        if record_type == "config.prompt"
        else "qwenpaw.session_delta.config.tools"
    )
    trace_id = _edge_cloud_config_trace_id(payload, config)
    config_input = {
        key: value
        for key, value in config.items()
        if key != "protocol"
    }
    try:
        observation = client.start_observation(
            as_type="span",
            name=name,
            input=config_input,
            metadata={
                "source": "qwenpaw.session_delta",
                "protocol": "edge_cloud_trace.v2",
                "session_id": payload.get("session_id"),
                "root_session_id": payload.get("root_session_id"),
                "config_key": config.get("config_key"),
                "record_type": record_type,
                "role": config.get("role"),
            },
            trace_context={"trace_id": trace_id},
        )
        observation.end()
    finally:
        _flush_client(client)


def _send_edge_cloud_delta_span(
    client: Any,
    payload: dict[str, Any],
) -> None:
    trace_id = _edge_cloud_delta_trace_id(payload)
    delta_input = {
        "session_id": payload.get("session_id"),
        "agent_calls": payload.get("agent_calls", []),
        "delta": payload.get("delta", []),
        "session_skills": payload.get("session_skills", []),
        "model_requests": payload.get("model_requests", []),
        "edge_cloud": payload.get("edge_cloud"),
    }
    edge_cloud = payload.get("edge_cloud")
    edge_cloud = edge_cloud if isinstance(edge_cloud, dict) else {}
    metadata = _metadata(payload)
    metadata.pop("edge_cloud_stage_count", None)
    metadata.update(
        {
            "protocol": "edge_cloud_trace.v2",
            "record_type": "delta",
            "content_format": payload.get("content_format", "session_messages.v1"),
            "message_count": len(delta_input["delta"]),
            "edge_cloud_router_task_type": edge_cloud.get("router_task_type"),
            "edge_cloud_router_target": edge_cloud.get("router_target"),
        }
    )
    try:
        observation = client.start_observation(
            as_type="span",
            name="qwenpaw.session_delta",
            input=delta_input,
            metadata=metadata,
            trace_context={"trace_id": trace_id},
        )
        try:
            observation.update(
                output={
                    "status": "uploaded",
                    "delta_count": len(delta_input["delta"]),
                    "agent_call_count": len(delta_input["agent_calls"]),
                },
            )
        finally:
            observation.end()
    finally:
        _flush_client(client)


def _upload_edge_cloud_sync(payload: dict[str, Any]) -> None:
    client = _client()
    db_path = _first_config_db(payload)
    session_id = str(payload.get("session_id") or "")
    configs = payload.get("configs")
    if not isinstance(configs, list):
        configs = []

    pending: list[dict[str, Any]] = []
    for config in configs:
        if not isinstance(config, dict):
            continue
        config_key = str(config.get("config_key") or "")
        record_type = str(config.get("record_type") or "")
        if not config_key or not record_type:
            continue
        if db_path is not None:
            try:
                exists = _config_exists(
                    db_path, session_id, config_key, record_type
                )
            except Exception:
                logger.debug(
                    "edge_cloud config dedupe read failed", exc_info=True
                )
                exists = False
            if exists:
                continue
        elif (session_id, config_key, record_type) in _sent_config_keys:
            continue
        pending.append(config)

    session_key = _ascii_id(payload.get("session_id"))
    with _propagate_context(_ascii_id(payload.get("user_id")), session_key):
        errors: list[Exception] = []
        for config in pending:
            try:
                _send_edge_cloud_config_span(client, payload, config)
            except Exception as exc:
                errors.append(exc)
                continue
            config_key = str(config.get("config_key") or "")
            record_type = str(config.get("record_type") or "")
            content_hash = hashlib.sha256(
                json.dumps(config, ensure_ascii=False, default=str).encode(
                    "utf-8"
                )
            ).hexdigest()
            if db_path is not None:
                try:
                    _record_config(
                        db_path,
                        session_id,
                        config_key,
                        record_type,
                        content_hash,
                    )
                except Exception as exc:
                    logger.debug(
                        "edge_cloud config dedupe write failed", exc_info=True
                    )
                    errors.append(exc)
            else:
                _sent_config_keys.add(
                    (session_id, config_key, record_type)
                )
        try:
            _send_edge_cloud_delta_span(client, payload)
        except Exception as exc:
            errors.append(exc)
        if errors:
            raise errors[0]


async def _upload_edge_cloud(payload: dict[str, Any]) -> None:
    if not is_session_delta_langfuse_enabled():
        return
    try:
        from .session_delta_redactor import redact_session_delta_payload

        raw_sensitive_words = payload.get("_sensitive_words")
        upload_payload = redact_session_delta_payload(
            payload,
            sensitive_words=(
                raw_sensitive_words
                if isinstance(raw_sensitive_words, dict)
                else None
            ),
        )
        upload_payload.pop("_sensitive_words", None)
    except Exception:
        logger.warning(
            "session_delta_langfuse: edge-cloud redaction failed; "
            "upload skipped",
            exc_info=True,
        )
        return

    try:
        await asyncio.to_thread(_upload_edge_cloud_sync, upload_payload)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug(
            "session_delta_langfuse: edge-cloud upload failed",
            exc_info=True,
        )


def _upload_sync(payload: dict[str, Any]) -> None:
    client = _client()
    delta_trace_id = str(payload.get("trace_id") or _stable_trace_id(payload))
    initial_config_trace_id = _initial_config_trace_id(payload)
    system_prompt = payload.get("system_prompt")
    system_prompt_chars = (
        len(system_prompt) if isinstance(system_prompt, str) else 0
    )
    model_requests = payload.get("model_requests", [])
    delta_requests = model_requests
    initial_config: dict[str, Any] = {}
    if isinstance(system_prompt, str) and system_prompt:
        initial_config["system_prompt"] = system_prompt
    if isinstance(model_requests, list):
        delta_requests = []
        for request in model_requests:
            if isinstance(request, dict):
                if "tools" in request and "tools" not in initial_config:
                    initial_config["tools"] = request["tools"]
                delta_requests.append(
                    {
                        key: value
                        for key, value in request.items()
                        if key != "tools"
                    }
                )
            else:
                delta_requests.append(request)
    tools = initial_config.pop("tools", None)
    tools_trace_id = _tools_trace_id(payload)
    session_id = _ascii_id(payload.get("session_id"))
    with _propagate_context(_ascii_id(payload.get("user_id")), session_id):
        # Each span is sent and flushed on its own so a failed export of
        # one span cannot drop the others from the same batch, and an
        # error in one span no longer skips the others.
        errors: list[Exception] = []
        if "system_prompt" in initial_config:
            try:
                _send_initial_config_span(
                    client,
                    payload,
                    initial_config_trace_id,
                    initial_config,
                    system_prompt_chars,
                )
            except Exception as exc:
                errors.append(exc)
        if tools is not None:
            try:
                _send_tools_span(
                    client, payload, tools_trace_id, tools,
                )
            except Exception as exc:
                errors.append(exc)
        try:
            _send_delta_span(client, payload, delta_trace_id, delta_requests)
        except Exception as exc:
            errors.append(exc)
        if errors:
            if len(errors) > 1:
                logger.debug(
                    "session_delta_langfuse: second span upload error",
                    exc_info=errors[1],
                )
            raise errors[0]


async def upload_session_delta(payload: dict[str, Any]) -> None:
    """Best-effort upload of one session delta payload."""

    if payload.get("protocol") == "edge_cloud_trace.v2":
        await _upload_edge_cloud(payload)
        return

    if not is_session_delta_langfuse_enabled():
        return
    try:
        from .session_delta_redactor import redact_session_delta_payload

        # Keep only the final model configuration in the uploaded delta.
        # Leave the caller's payload unchanged.
        upload_source = payload
        model_requests = payload.get("model_requests")
        if isinstance(model_requests, list) and model_requests:
            latest = model_requests[-1]
            fields = (
                "tools", "tool_choice", "model", "stream",
                "model_parameters", "params", "generate_kwargs",
            )
            upload_source = {
                **payload,
                "model_requests": (
                    [{field: latest.get(field) for field in fields}]
                    if isinstance(latest, dict)
                    else []
                ),
            }

        raw_sensitive_words = payload.get("_sensitive_words")
        upload_payload = redact_session_delta_payload(
            upload_source,
            sensitive_words=(
                raw_sensitive_words
                if isinstance(raw_sensitive_words, dict)
                else None
            ),
        )
        upload_payload.pop("_sensitive_words", None)
    except Exception:
        logger.warning(
            "session_delta_langfuse: redaction failed; upload skipped",
            exc_info=True,
        )
        return

    db_path = _first_config_db(payload)
    record_first_config = False
    if db_path is not None:
        try:
            already_uploaded = await asyncio.to_thread(
                _first_config_exists, db_path, payload["session_id"],
            )
        except Exception:
            _omit_first_config(upload_payload)
            logger.debug(
                "session_delta_langfuse: first config lookup failed",
                exc_info=True,
            )
        else:
            if already_uploaded:
                _omit_first_config(upload_payload)
            else:
                requests = upload_payload.get("model_requests")
                record_first_config = (
                    isinstance(upload_payload.get("system_prompt"), str)
                    and bool(upload_payload["system_prompt"])
                    and isinstance(requests, list)
                    and bool(requests)
                    and isinstance(requests[0], dict)
                    and "tools" in requests[0]
                )
                if not record_first_config:
                    _omit_first_config(upload_payload)
    try:
        await asyncio.to_thread(_upload_sync, upload_payload)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug(
            "session_delta_langfuse: upload failed",
            exc_info=True,
        )
    else:
        if record_first_config and db_path is not None:
            try:
                await asyncio.to_thread(
                    _record_first_config, db_path, payload["session_id"],
                )
            except Exception:
                logger.debug(
                    "session_delta_langfuse: first config record failed",
                    exc_info=True,
                )


def schedule_session_delta_upload(payload: dict[str, Any]) -> None:
    """Start a non-blocking upload task when enabled."""

    if not is_session_delta_langfuse_enabled():
        return
    try:
        loop = asyncio.get_running_loop()
        db_path = _first_config_db(payload)
        if db_path is None:
            loop.create_task(upload_session_delta(payload))
        else:
            key = (loop, str(db_path), payload["session_id"])
            previous = _upload_tails.get(key)
            done = loop.create_future()
            _upload_tails[key] = done

            async def run_in_order() -> None:
                if previous is not None:
                    await previous
                await upload_session_delta(payload)

            task = loop.create_task(run_in_order())

            def release(_task: asyncio.Task[None]) -> None:
                if not done.done():
                    done.set_result(None)
                if _upload_tails.get(key) is done:
                    _upload_tails.pop(key, None)

            task.add_done_callback(release)
    except RuntimeError:
        logger.debug(
            "session_delta_langfuse: no running loop; upload skipped",
            exc_info=True,
        )


__all__ = [
    "is_session_delta_langfuse_enabled",
    "schedule_session_delta_upload",
    "upload_session_delta",
]
