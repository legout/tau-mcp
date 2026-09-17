"""Hermetic newline-delimited MCP fixture server."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _send(message: object) -> None:
    print(json.dumps(message, separators=(",", ":")), flush=True)


def _response(request_id: object, result: object) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=("normal", "bad-version", "malformed", "mismatch"),
        default="normal",
    )
    parser.add_argument("--crash-once", type=Path)
    args = parser.parse_args()

    args.pid_file.parent.mkdir(parents=True, exist_ok=True)
    with args.pid_file.open("a", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n")
        handle.flush()

    initialized = False
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
        elif not initialized:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32002, "message": "not initialized"},
                }
            )
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
            if args.crash_once is not None and not args.crash_once.exists():
                args.crash_once.write_text("crashed", encoding="utf-8")
                return 17
            params = message.get("params", {})
            name = params.get("name") if isinstance(params, dict) else None
            arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
            if name == "echo":
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
