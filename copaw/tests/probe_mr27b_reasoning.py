r"""Print raw OpenAI API responses and inspect reasoning fields.

Run directly; this is a live diagnostic script, not an automatic pytest test.
It bypasses AgentScope and QwenPaw's model wrappers. No thinking switch is
sent unless explicitly supplied using --extra-body.

PowerShell example (the API key is read from the environment):
    .venv\Scripts\python.exe tests\probe_mr27b_reasoning.py --mode both

Environment: QWENPAW_EDGE_CLOUD_EDGE_BASE_URL / _API_KEY / _MODEL, with
OPENAI_BASE_URL / OPENAI_API_KEY as fallbacks. The key is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

from openai import OpenAI


@dataclass
class Observations:
    choice_objects: int = 0
    fields: dict[str, dict[str, int]] = field(default_factory=lambda: {
        name: {state: 0 for state in ("absent", "null", "empty", "nonempty")}
        for name in ("reasoning_content", "reasoning")
    })
    content: list[str] = field(default_factory=list)
    finish_reasons: list[str] = field(default_factory=list)

    def observe(self, payload: dict[str, Any]) -> None:
        for choice in payload.get("choices") or []:
            reason = choice.get("finish_reason")
            if reason is not None:
                self.finish_reasons.append(reason)
            message = choice.get("delta", choice.get("message"))
            if not isinstance(message, dict):
                continue
            self.choice_objects += 1
            for name, counts in self.fields.items():
                if name not in message:
                    state = "absent"
                elif message[name] is None:
                    state = "null"
                elif message[name] in ("", [], {}):
                    state = "empty"
                else:
                    state = "nonempty"
                counts[state] += 1
            if isinstance(message.get("content"), str):
                self.content.append(message["content"])

    def summary(self, *, mode: str, completed: bool) -> dict[str, Any]:
        text = "".join(self.content)
        if self.fields["reasoning_content"]["nonempty"]:
            result = "Observed non-empty reasoning_content in this request."
        elif self.choice_objects:
            result = (
                "No non-empty reasoning_content observed in this request; "
                "check absent/null/empty counts below."
            )
        else:
            result = "No choice data received; cannot assess reasoning_content."
        return {
            "mode": mode,
            "completed": completed,
            "result": result,
            "choice_objects": self.choice_objects,
            "fields": self.fields,
            "content_characters": len(text),
            "content_think_open_tags": text.count("<think>"),
            "content_think_close_tags": text.count("</think>"),
            "finish_reasons": self.finish_reasons,
            "output_truncated": "length" in self.finish_reasons,
            "scope": "This observation applies only to this request and its parameters.",
        }


def sse_events(lines: Iterable[str]) -> Iterable[str]:
    """Keep raw SSE data, including fields the SDK might not know about."""
    data: list[str] = []
    for line in lines:
        if not line:
            if data:
                yield "\n".join(data)
                data.clear()
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
        elif not line.startswith(":"):
            print(f"[SSE metadata] {line}", flush=True)
    if data:
        yield "\n".join(data)


def probe(client: OpenAI, request: dict[str, Any], *, stream: bool) -> None:
    observations = Observations()
    completed = False
    mode = "stream" if stream else "non-stream"
    body = {**request, "stream": stream}
    if stream:
        # Match the existing Edge OpenAI client's usage request.
        body["stream_options"] = {"include_usage": True}
    print(f"\n=== {mode}: request (without credentials) ===", flush=True)
    print(json.dumps(body, ensure_ascii=False, indent=2), flush=True)
    try:
        # Read the raw HTTP body through the OpenAI SDK. Do not use model_dump:
        # SDK defaults could turn an absent field into a printed null field.
        with client.chat.completions.with_streaming_response.create(**body) as response:
            print(f"[HTTP] {response.status_code}", flush=True)
            if stream:
                for number, data in enumerate(sse_events(response.iter_lines()), 1):
                    print(f"[event {number:04d}] {data}", flush=True)
                    if data == "[DONE]":
                        completed = True
                        continue
                    payload = json.loads(data)
                    if payload.get("error"):
                        raise RuntimeError("API returned an error event; see raw event above.")
                    observations.observe(payload)
            else:
                raw = response.read().decode("utf-8")
                print(f"[response] {raw}", flush=True)
                observations.observe(json.loads(raw))
                completed = True
    finally:
        print(f"\n=== {mode}: field observations ===", flush=True)
        print(json.dumps(observations.summary(mode=mode, completed=completed),
                         ensure_ascii=False, indent=2), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=(
        os.environ.get("QWENPAW_EDGE_CLOUD_EDGE_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "http://10.199.230.180:30061/v1"
    ))
    parser.add_argument("--api-key", default=(
        os.environ.get("QWENPAW_EDGE_CLOUD_EDGE_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    ))
    parser.add_argument("--model", default=(
        os.environ.get("QWENPAW_EDGE_CLOUD_EDGE_MODEL") or "Mr-27B-0919"
    ))
    parser.add_argument("--mode", choices=("stream", "non-stream", "both"), default="stream")
    parser.add_argument("--prompt", default="请计算 37 × 48 + 125，并简要说明计算方法。")
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--extra-body", type=json.loads, help="Optional JSON object; no default thinking overrides.")
    args = parser.parse_args(argv)
    if not args.api_key:
        parser.error("Set QWENPAW_EDGE_CLOUD_EDGE_API_KEY / OPENAI_API_KEY, or pass --api-key.")
    if args.max_tokens < 1 or args.timeout <= 0:
        parser.error("--max-tokens and --timeout must be positive.")
    if args.extra_body is not None and not isinstance(args.extra_body, dict):
        parser.error("--extra-body must be a JSON object.")
    if args.extra_body and {"model", "messages", "stream", "stream_options"} & args.extra_body.keys():
        parser.error("Use the dedicated arguments for model, messages and streaming mode.")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    request = {
        "model": args.model,
        "messages": [{"role": "user", "content": args.prompt}],
        "max_tokens": args.max_tokens,
    }
    if args.extra_body is not None:
        request["extra_body"] = args.extra_body
    try:
        with OpenAI(api_key=args.api_key, base_url=args.base_url,
                    timeout=args.timeout, max_retries=0) as client:
            for stream in ((True, False) if args.mode == "both" else (args.mode == "stream",)):
                probe(client, request, stream=stream)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"[ERROR] {str(exc).replace(args.api_key, '***')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
