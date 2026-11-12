"""Smoke-test the QwenPaw session list API (GET /api/chats)."""

import argparse
import http.client
import json
import math
import os
import sys
import time
from urllib.parse import urlencode, urlsplit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host", "--ip", default="10.159.221.147",
        help="Server IP or hostname (default: %(default)s).",
    )
    parser.add_argument(
        "--port", type=int, default=8088, help="Server port (default: %(default)s).",
    )
    parser.add_argument(
        "--base-url",
        help="HTTP(S) server URL with optional API prefix, overriding host and port.",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0,
        help="Connect and read timeout in seconds (default: %(default)s).",
    )
    parser.add_argument(
        "--agent-id", help="Agent ID (defaults to the server's active agent).",
    )
    parser.add_argument("--user-id", help="Filter sessions by user ID.")
    parser.add_argument("--channel", help="Filter sessions by channel.")
    parser.add_argument(
        "--token", default=os.environ.get("QWENPAW_API_TOKEN"),
        help="Bearer token (defaults to QWENPAW_API_TOKEN).",
    )
    args = parser.parse_args()

    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite number greater than zero")

    base_url = args.base_url
    if base_url is None:
        if not 1 <= args.port <= 65535:
            parser.error("--port must be between 1 and 65535")
        host = args.host
        if any(char in host for char in "/\\@?#") or any(char.isspace() for char in host):
            parser.error("--host must be an IP address or hostname")
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        base_url = f"http://{host}:{args.port}"

    try:
        base = urlsplit(base_url)
        port = base.port
    except ValueError as exc:
        parser.error(f"Invalid server address: {exc}")
    if (
        base.scheme not in {"http", "https"}
        or not base.hostname
        or port == 0
        or base.username is not None
        or base.query
        or base.fragment
    ):
        parser.error("Use an HTTP(S) URL without credentials, query or fragment")

    prefix = base.path.rstrip("/")
    if not prefix.endswith("/api"):
        prefix += "/api"
    path = f"{prefix}/chats" 
    filters = {
        key: value
        for key, value in (("user_id", args.user_id), ("channel", args.channel))
        if value is not None
    }
    if filters:
        path += f"?{urlencode(filters)}"

    headers = {"Accept": "application/json"}
    if args.agent_id:
        headers["X-Agent-Id"] = args.agent_id
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    connection_type = (
        http.client.HTTPSConnection
        if base.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_type(base.hostname, port, timeout=args.timeout)
    start = time.perf_counter()
    try:
        connection.request("GET", path, headers=headers)
        with connection.getresponse() as response:
            if response.status != 200:
                raise ValueError(f"HTTP {response.status} {response.reason}")
            sessions = json.loads(response.read())
        if not isinstance(sessions, list):
            raise ValueError("expected a JSON array of sessions")
        for index, session in enumerate(sessions):
            if not isinstance(session, dict) or not all(
                isinstance(session.get(key), str)
                for key in ("id", "session_id", "user_id", "channel")
            ):
                raise ValueError(
                    f"session at index {index} must have string fields "
                    "id, session_id, user_id and channel",
                )
            for key, value in filters.items():
                if session[key] != value:
                    raise ValueError(
                        f"session at index {index} does not match the {key} filter",
                    )
    except TimeoutError:
        print(f"FAIL: socket timed out after {args.timeout:g}s", file=sys.stderr)
        return 1
    except (OSError, http.client.HTTPException, ValueError, OverflowError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    elapsed = time.perf_counter() - start
    print(f"PASS: HTTP 200, {len(sessions)} session(s), {elapsed:.3f}s", file=sys.stderr)
    print(json.dumps(sessions, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
