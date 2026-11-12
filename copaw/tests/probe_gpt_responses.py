#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""Call OpenAI.responses.create(stream=True) and print each SDK event.

Run manually; pytest does not collect this live diagnostic script.

Defaults: gpt-5.6-sol and probe_gpt_responses.local.json beside this script.
The API key is read locally at runtime, never copied into this script or logs.
    python tests/probe_gpt_responses.py

Use the configured provider/model (including its credentials and generation settings):
    python tests/probe_gpt_responses.py --current
    python tests/probe_gpt_responses.py --current --provider fw-agent-online --model YOUR_GPT_MODEL

Or supply OPENAI_BASE_URL, OPENAI_API_KEY and OPENAI_MODEL, or the matching CLI flags:
    python tests/probe_gpt_responses.py --base-url https://gateway.example/v1 --model YOUR_GPT_MODEL

The default input uses the official SDK's EasyInputMessage format: an explicit
message type and string content, including assistant history. It retains system,
compressed-memory, tool-call and tool-output examples. All text is synthetic.
--request-file supplies a JSON object of official responses.create arguments.
The truncated query-error dump cannot be passed directly as --request-file.

    python tests/probe_gpt_responses.py --current --request-file request.json

stream=True is always sent. The SDK builds the /responses request and decodes
SSE; this script only loops over the stream and calls print(event). It does not
build endpoint URLs, send raw POST bodies, or extract/concatenate delta text.
HTTP errors print their original body. Diagnostics go to stderr and the adjacent
probe_gpt_responses.log, without prompt text or credentials. Use --verbose for
per-event type logs, or --log-file to choose another log. There are no automatic
retries. Exit status reflects HTTP/SDK errors; inspect printed events for model
failures reported within an otherwise successful HTTP response.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any

import httpx
from openai import APIStatusError, OpenAI, __version__ as OPENAI_VERSION


DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_PROVIDER_FILE = Path(__file__).with_name("probe_gpt_responses.local.json")
DEFAULT_LOG_FILE = Path(__file__).with_suffix(".log")
LOGGER = logging.getLogger("probe_gpt_responses")


def configure_logging(log_file: Path, verbose: bool) -> None:
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOGGER.propagate = False
    for handler in LOGGER.handlers[:]:
        LOGGER.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(sys.stderr), logging.FileHandler(log_file, encoding="utf-8")):
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)
    LOGGER.info("=== probe start | openai=%s | log=%s ===", OPENAI_VERSION, log_file)


def decrypt_saved_key(value: str, provider_file: Path) -> str:
    """Read the install's existing master key; never create/migrate secrets."""
    if not value.startswith("ENC:"):
        return value
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError as error:
        raise ValueError("Use the project Python environment to decrypt the saved API key") from error

    def decode(master_hex: str) -> str | None:
        try:
            master = bytes.fromhex(master_hex)
            if len(master) != 32:
                return None
            return Fernet(base64.urlsafe_b64encode(master)).decrypt(
                value[4:].encode("ascii"),
            ).decode("utf-8")
        except (ValueError, InvalidToken, UnicodeError):
            return None

    secret_dir = provider_file.resolve().parents[2]
    master_file = secret_dir / ".master_key"
    if master_file.is_file():
        plain = decode(master_file.read_text(encoding="utf-8").strip())
        if plain:
            return plain

    # Same service/account conventions as qwenpaw.security.secret_store, using
    # read-only getters. The installed DevCo configuration uses the OS keyring.
    try:
        import keyring
    except ImportError as error:
        raise ValueError("The saved API key needs keyring; run with the project .venv Python") from error
    accounts = [
        os.environ.get("QWENPAW_KEYRING_ACCOUNT")
        or os.environ.get("COPAW_KEYRING_ACCOUNT"),
        "master_key:" + hashlib.sha256(str(secret_dir).encode("utf-8")).hexdigest()[:16],
        "master_key",
    ]
    for service in ("qwenpaw", "copaw"):
        for account in dict.fromkeys(account for account in accounts if account):
            try:
                master_hex = keyring.get_password(service, account)
            except Exception:
                continue
            if master_hex:
                plain = decode(master_hex)
                if plain:
                    return plain
    raise ValueError(
        "Cannot decrypt the saved API key from the install's master-key file/OS keyring; "
        "run under DevCo's Windows account or provide OPENAI_API_KEY",
    )


def saved_provider_settings(provider_file: Path, *, load_key: bool) -> tuple[str, str, dict]:
    """Read connection defaults without importing/migrating ProviderManager."""
    data = json.loads(provider_file.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Provider configuration must be a JSON object")
    key = decrypt_saved_key(data.get("api_key") or "", provider_file) if load_key else ""
    return data.get("base_url") or "", key, data.get("custom_headers") or {}


def example_request(model: str, prompt: str) -> dict[str, Any]:
    """Use official EasyInputMessage and function-call argument structures."""
    def message(role: str, text: str) -> dict[str, Any]:
        return {
            "type": "message",
            "role": role,
            "content": text,
        }

    return {
        "model": model,
        "stream": True,
        "max_output_tokens": 256,
        "input": [
            message("system", "你是一个助手，请根据已有工具结果简短回答用户。"),
            message("user", "<system-info>\n[context compressed] 已完成环境检查；详细结果见后续工具记录。\n</system-info>"),
            message("user", "请检查当前环境是否可用。"),
            message("assistant", "我先检查环境。"),
            {
                "type": "function_call",
                "call_id": "call_probe_environment",
                "name": "probe_environment",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_probe_environment",
                "output": '{"status":"ok","details":"环境检查通过"}',
            },
            message("user", prompt),
        ],
        "tools": [{
            "type": "function",
            "name": "probe_environment",
            "description": "检查环境是否可用。",
            "strict": True,
            "parameters": {
                "type": "object", "properties": {},
                "required": [], "additionalProperties": False,
            },
        }],
        "tool_choice": "none",
    }


def current_settings(args: argparse.Namespace) -> tuple[str, str, str, dict, dict]:
    """Resolve the app's provider without calling the model or changing selection."""
    from qwenpaw.providers.provider_manager import ProviderManager

    manager = ProviderManager.get_instance()
    slot = manager.get_active_model()
    if args.agent_id:
        from qwenpaw.config.config import load_agent_config
        slot = load_agent_config(args.agent_id).active_model or slot
    provider_id = args.provider or (slot.provider_id if slot else None)
    # Selecting another provider requires an explicit model, not a model from
    # the previously active provider.
    model = args.model or (
        slot.model if slot and slot.provider_id == provider_id else None
    )
    if not provider_id or not model:
        raise ValueError("No active model; specify --provider and --model with --current")
    provider = manager.get_provider(provider_id)
    if provider is None:
        raise ValueError(f"Provider not found: {provider_id}")
    if not hasattr(provider, "_responses_provider"):
        raise ValueError("The selected provider is not OpenAI compatible")
    # Reuse the production Responses reasoning alias/extra_body normalization.
    compat = provider._responses_provider()
    generation = compat.get_effective_generate_kwargs(model)
    generation.pop("disable_thinking", None)
    max_tokens = generation.pop("max_tokens", None)
    if max_tokens is not None:
        generation.setdefault("max_output_tokens", max_tokens)
    extra_body = generation.pop("extra_body", {})
    if extra_body:
        generation.update(extra_body)
    return (
        provider.base_url, provider.api_key, model,
        provider._build_default_headers(), generation,
    )


def send_request(
    base_url: str, key: str, headers: dict, body: dict, timeout: float,
    *, transport: httpx.BaseTransport | None = None,
) -> int:
    """Use the real SDK boundary; optional transport is for offline verification."""
    started = time.perf_counter()

    def log_request(request: httpx.Request) -> None:
        # HTTPX hooks run after the SDK has serialized the actual wire body.
        wire = request.content
        payload = json.loads(wire)
        LOGGER.info(
            "request method=%s url=%s query_keys=%s model=%s stream=%s bytes=%d sha256=%s top_keys=%s",
            request.method, request.url.copy_with(query=None),
            list(request.url.params.keys()), payload.get("model"),
            payload.get("stream"), len(wire), hashlib.sha256(wire).hexdigest(),
            sorted(payload),
        )
        for index, item in enumerate(payload.get("input", [])):
            if not isinstance(item, dict):
                LOGGER.info("input[%d] python_type=%s", index, type(item).__name__)
                continue
            content = item.get("content")
            summary = {
                "type_present": "type" in item,
                "type": item.get("type"),
                "role": item.get("role"),
                "keys": sorted(item),
                "content_types": [part.get("type") for part in content if isinstance(part, dict)]
                if isinstance(content, list) else type(content).__name__,
            }
            LOGGER.info("input[%d] %s", index, json.dumps(summary, ensure_ascii=False))

    def log_response(response: httpx.Response) -> None:
        identifiers = {name: response.headers[name] for name in (
            "x-request-id", "request-id", "x-trace-id", "x-correlation-id", "traceparent",
        ) if name in response.headers}
        content_type = response.headers.get("content-type", "")
        LOGGER.info(
            "response_headers status=%d content_type=%s ids=%s elapsed_ms=%.1f",
            response.status_code, content_type, json.dumps(identifiers),
            (time.perf_counter() - started) * 1000,
        )
        if response.is_success and "text/event-stream" not in content_type.lower():
            LOGGER.warning("Requested streaming but received non-SSE content")

    # HTTPX is only the SDK's transport and diagnostic hook; the SDK owns
    # request construction and streaming protocol handling.
    http_client = httpx.Client(
        transport=transport, follow_redirects=False, timeout=timeout,
        event_hooks={"request": [log_request], "response": [log_response]},
    )
    with OpenAI(
        api_key=key, base_url=base_url, timeout=timeout, max_retries=0,
        default_headers={"Accept": "text/event-stream", **headers},
        http_client=http_client,
    ) as client:
        try:
            options = {name: value for name, value in body.items() if name not in ("model", "input", "stream")}
            stream = client.responses.create(
                model=body["model"], input=body["input"], stream=True,
                **options,
            )
            event_count = 0
            try:
                with stream:
                    for event in stream:
                        event_count += 1
                        if event_count == 1:
                            LOGGER.info("first_event elapsed_ms=%.1f", (time.perf_counter() - started) * 1000)
                        LOGGER.debug("sdk_event index=%d type=%s", event_count, getattr(event, "type", None))
                        print(event, flush=True)
                return stream.response.status_code
            finally:
                LOGGER.info("stream_end events=%d elapsed_ms=%.1f", event_count, (time.perf_counter() - started) * 1000)
        except APIStatusError as error:
            LOGGER.error(
                "api_error exception=%s status=%d param=%s code=%s request_id=%s",
                type(error).__name__, error.status_code, error.param, error.code, error.request_id,
            )
            # SDK status errors expose the original HTTP response, including
            # the JSON 400 body returned before any SSE stream could start.
            try:
                print(error.response.text, end="", flush=True)
            finally:
                error.response.close()
            return error.status_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--current", action="store_true", help="Use the app's saved provider settings")
    parser.add_argument("--provider", help="Provider ID; requires --current")
    parser.add_argument("--agent-id", help="Read this agent's active model; requires --current")
    parser.add_argument("--base-url", help="SDK API base URL, e.g. http://host:port/v1; fallback: OPENAI_BASE_URL")
    parser.add_argument("--api-key", help="Fallback: OPENAI_API_KEY; never printed")
    parser.add_argument("--model", help=f"Exact GPT model ID; fallback: OPENAI_MODEL, then {DEFAULT_MODEL}")
    parser.add_argument("--provider-file", type=Path, default=DEFAULT_PROVIDER_FILE, help="Connection defaults (probe_gpt_responses.local.json by default)")
    parser.add_argument("--request-file", type=Path, help="JSON object of official responses.create arguments")
    parser.add_argument("--prompt", default="可以使用了吗？请根据以上工具结果简短回答。")
    parser.add_argument("--max-output-tokens", type=int, help="Optional override; sample default: 256")
    parser.add_argument("--header", action="append", default=[], metavar="NAME:VALUE", help="Additional gateway header; never printed")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG_FILE)
    parser.add_argument("--verbose", action="store_true", help="Log each SDK event's type")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        configure_logging(args.log_file, args.verbose)
    except OSError as error:
        parser.error(f"Cannot open diagnostic log: {error}")
    key = args.api_key or os.environ.get("OPENAI_API_KEY", "")
    try:
        if args.timeout <= 0 or (args.max_output_tokens is not None and args.max_output_tokens <= 0):
            raise ValueError("--timeout and --max-output-tokens must be positive")
        if (args.provider or args.agent_id) and not args.current:
            raise ValueError("--provider and --agent-id require --current")
        if args.current:
            if args.base_url or args.api_key:
                raise ValueError("Use --current OR explicit connection arguments")
            base_url, key, model, headers, generation = current_settings(args)
            LOGGER.info("settings_source=current provider=%s agent=%s", args.provider or "active", args.agent_id or "global")
        else:
            base_url = args.base_url or os.environ.get("OPENAI_BASE_URL", "")
            model = args.model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
            headers, generation = {}, {}
            if not base_url or not key:
                saved_url, saved_key, headers = saved_provider_settings(
                    args.provider_file, load_key=not bool(key),
                )
                base_url = base_url or saved_url
                key = key or saved_key
                LOGGER.info("settings_source=provider_file path=%s", args.provider_file)
            else:
                LOGGER.info("settings_source=explicit_arguments_or_environment")
        parsed_url = httpx.URL(base_url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.host:
            raise ValueError("--base-url must be an http(s) API base URL")
        if parsed_url.username or parsed_url.password:
            raise ValueError("Pass credentials using --api-key, not in --base-url")
        if parsed_url.path.rstrip("/").endswith("/responses"):
            raise ValueError("Pass the API base URL (e.g. /v1); the OpenAI SDK adds /responses")
        if args.request_file:
            body = json.loads(args.request_file.read_text(encoding="utf-8-sig"))
            if not isinstance(body, dict) or not isinstance(body.get("input"), list):
                raise ValueError("--request-file must contain responses.create arguments with an input array, not a query-error dump")
            # Preserve every file parameter unless explicitly overridden.
            if args.model:
                body["model"] = args.model
            elif not body.get("model"):
                body["model"] = model
        else:
            body = {**example_request(model, args.prompt), **generation}
            body["model"] = model
        if not body.get("model"):
            raise ValueError("Specify --model / OPENAI_MODEL, a request-file model, or --current")
        if not isinstance(body.get("input"), list):
            raise ValueError("input must be an array")
        if args.max_output_tokens is not None:
            body["max_output_tokens"] = args.max_output_tokens
        body["stream"] = True
        LOGGER.info("input_source=%s", args.request_file or "built-in official SDK message example")
        for header in args.header:
            name, sep, value = header.partition(":")
            if not sep or not name.strip():
                raise ValueError("--header must have the form NAME:VALUE")
            headers[name.strip()] = value.strip()
        if not key:
            raise ValueError("Specify OPENAI_API_KEY / --api-key, or use --current")
    except Exception as error:
        LOGGER.error("configuration_error exception=%s", type(error).__name__)
        parser.error(str(error).replace(key, "***") if key else str(error))

    try:
        status = send_request(base_url, key, headers, body, args.timeout)
        LOGGER.info("probe_end status=%d", status)
        return int(not 200 <= status < 300)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        detail = str(error).replace(key, "***") if key else str(error)
        LOGGER.error("transport_or_stream_error exception=%s detail=%s", type(error).__name__, detail)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
