"""Hermetic newline-delimited MCP fixture server."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def _send(message: object) -> None:
    print(json.dumps(message, separators=(",", ":")), flush=True)


def _response(request_id: object, result: object) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _record(path: Path | None, event: str) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{event}\n")
        handle.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=(
            "normal",
            "bad-version",
            "malformed",
            "mismatch",
            "slow-handshake",
            "slow-call",
            "malformed-call",
            "large-result",
            "stderr-crash",
            "stderr-success",
        ),
        default="normal",
    )
    parser.add_argument("--crash-once", type=Path)
    parser.add_argument("--event-file", type=Path)
    args = parser.parse_args()

    args.pid_file.parent.mkdir(parents=True, exist_ok=True)
    with args.pid_file.open("a", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n")
        handle.flush()
    if args.mode in {"stderr-crash", "stderr-success"}:
        print(
            "STDERR_BEGIN" + "x" * (64 * 1024) + "STDERR_TAIL",
            file=sys.stderr,
            flush=True,
        )

    initialized = False
    slow_request_id: object | None = None
    for line in sys.stdin:
        try:
            message: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            params = message.get("params")
            if (
                not isinstance(params, dict)
                or params.get("protocolVersion") != "2025-11-25"
            ):
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32602,
                            "message": "wrong offered protocol version",
                        },
                    }
                )
                continue
            if args.mode == "slow-handshake":
                time.sleep(30)
                continue
            if args.mode == "malformed":
                _send([])
                continue
            if args.mode == "mismatch" and isinstance(request_id, int):
                request_id += 1
            version = "1900-01-01" if args.mode == "bad-version" else "2025-06-18"
            _send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {"progress": 1},
                }
            )
            _response(
                request_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-mcp", "version": "1"},
                },
            )
        elif method == "notifications/initialized":
            initialized = True
            _record(args.event_file, "initialized")
        elif not initialized:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32002, "message": "not initialized"},
                }
            )
        elif method == "notifications/cancelled":
            _record(
                args.event_file,
                f"cancelled:{message.get('params', {}).get('requestId')}",
            )
            if slow_request_id is not None:
                _response(
                    slow_request_id,
                    {"content": [{"type": "text", "text": "late"}], "isError": False},
                )
                slow_request_id = None
        elif method == "ping":
            _response(request_id, {})
        elif method == "tools/list":
            _response(
                request_id,
                {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo one value",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"value": {"type": "string"}},
                                "required": ["value"],
                            },
                        },
                        {
                            "name": "sum",
                            "description": "Add two numbers",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "a": {"type": "number"},
                                    "b": {"type": "number"},
                                },
                                "required": ["a", "b"],
                            },
                        },
                    ]
                },
            )
        elif method == "tools/call":
            _record(args.event_file, f"call:{request_id}")
            if args.crash_once is not None and not args.crash_once.exists():
                args.crash_once.write_text("crashed", encoding="utf-8")
                return 17
            if args.mode == "stderr-crash":
                return 18
            if args.mode == "malformed-call":
                _send([])
                continue
            if args.mode == "slow-call":
                slow_request_id = request_id
                continue
            params = message.get("params", {})
            name = params.get("name") if isinstance(params, dict) else None
            arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
            if args.mode == "large-result":
                text = "x" * (300 * 1024)
            elif name == "echo":
                text = str(arguments.get("value", ""))
            elif name == "sum":
                text = str(arguments.get("a", 0) + arguments.get("b", 0))
            else:
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": f"unknown tool: {name}"},
                    }
                )
                continue
            _response(
                request_id,
                {"content": [{"type": "text", "text": text}], "isError": False},
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
