"""Minimal stdlib MCP client for newline-delimited stdio JSON-RPC."""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import cast

from .config import ServerConfig

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)

OFFERED_PROTOCOL_VERSION = "2025-11-25"
COMPATIBLE_PROTOCOL_VERSIONS = frozenset(
    {
        OFFERED_PROTOCOL_VERSION,
        "2025-06-18",
        "2025-03-26",
        "2024-11-05",
        "2024-10-07",
    }
)
_STDERR_LIMIT = 16 * 1024


class McpError(RuntimeError):
    """A server-named MCP transport or protocol failure."""


@dataclass(frozen=True, slots=True)
class McpTool:
    name: str
    description: str
    input_schema: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    content: tuple[Mapping[str, JsonValue], ...]
    is_error: bool


class McpClient:
    """Own one MCP child and multiplex JSON-RPC responses by request ID."""

    def __init__(
        self,
        server: ServerConfig,
        *,
        on_disconnect: Callable[[], None] | None = None,
        handshake_timeout: float = 10,
    ) -> None:
        self.server = server
        self._on_disconnect = on_disconnect
        self._handshake_timeout = handshake_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[JsonValue]] = {}
        self._next_id = 1
        self._connect_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._stderr_chunks: deque[bytes] = deque()
        self._stderr_size = 0
        self._ready = False
        self._closing = False

    @property
    def connected(self) -> bool:
        process = self._process
        return self._ready and process is not None and process.returncode is None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def stderr_text(self) -> str:
        return b"".join(self._stderr_chunks).decode(errors="replace")

    async def connect(self) -> None:
        async with self._connect_lock:
            if self.connected:
                return
            if self._process is not None:
                await self.close()
            self._closing = False
            self._stderr_chunks.clear()
            self._stderr_size = 0
            try:
                self._process = await asyncio.create_subprocess_exec(
                    self.server.command,
                    *self.server.args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.server.cwd,
                    env={**os.environ, **self.server.env},
                )
            except OSError as exc:
                raise self._error(
                    f"could not start command `{self.server.command}`: {exc}"
                ) from exc
            self._reader_task = asyncio.create_task(
                self._read_stdout(), name=f"mcp-stdout-{self.server.name}"
            )
            self._stderr_task = asyncio.create_task(
                self._read_stderr(), name=f"mcp-stderr-{self.server.name}"
            )
            try:
                result = await self._request(
                    "initialize",
                    {
                        "protocolVersion": OFFERED_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "tau-mcp", "version": "0.1.0"},
                    },
                    timeout=self._handshake_timeout,
                    allow_unready=True,
                )
                if not isinstance(result, dict):
                    raise self._error("initialize result must be an object")
                version = result.get("protocolVersion")
                if version not in COMPATIBLE_PROTOCOL_VERSIONS:
                    raise self._error(f"unsupported protocol version: {version!r}")
                await self._notify("notifications/initialized", {})
                self._ready = True
            except BaseException:
                await self.close()
                raise

    async def list_tools(self) -> tuple[McpTool, ...]:
        result = await self._request("tools/list", {})
        if not isinstance(result, dict):
            raise self._error("tools/list result must be an object")
        raw_tools = result.get("tools")
        if not isinstance(raw_tools, list):
            raise self._error("tools/list result must contain a tools array")
        tools: list[McpTool] = []
        for item in raw_tools:
            if not isinstance(item, dict):
                raise self._error("tools/list returned a non-object tool")
            name = item.get("name")
            description = item.get("description", "")
            input_schema = item.get("inputSchema", {})
            if (
                not isinstance(name, str)
                or not isinstance(description, str)
                or not isinstance(input_schema, dict)
            ):
                raise self._error("tools/list returned malformed tool metadata")
            tools.append(McpTool(name, description, input_schema))
        return tuple(tools)

    async def call_tool(
        self, name: str, arguments: Mapping[str, JsonValue]
    ) -> ToolCallResult:
        result = await self._request(
            "tools/call", {"name": name, "arguments": dict(arguments)}
        )
        if not isinstance(result, dict):
            raise self._error("tools/call result must be an object")
        raw_content = result.get("content")
        if not isinstance(raw_content, list):
            raise self._error("tools/call result must contain a content array")
        content: list[Mapping[str, JsonValue]] = []
        for item in raw_content:
            if not isinstance(item, dict):
                raise self._error("tools/call returned non-object content")
            content.append(item)
        is_error = result.get("isError", False)
        if not isinstance(is_error, bool):
            raise self._error("tools/call isError must be a boolean")
        return ToolCallResult(tuple(content), is_error)

    async def ping(self) -> None:
        result = await self._request("ping", {})
        if not isinstance(result, dict):
            raise self._error("ping result must be an object")

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._ready = False
        self._fail_pending(self._error("connection closed"))
        process = self._process
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=1)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=1)
                    except TimeoutError:
                        with suppress(ProcessLookupError):
                            process.kill()
                        await process.wait()
            else:
                await process.wait()
        current = asyncio.current_task()
        tasks = [
            task
            for task in (self._reader_task, self._stderr_task)
            if task is not None and task is not current
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._process = None
        self._reader_task = None
        self._stderr_task = None
        self._closing = False

    async def _request(
        self,
        method: str,
        params: Mapping[str, JsonValue],
        *,
        timeout: float | None = None,
        allow_unready: bool = False,
    ) -> JsonValue:
        if not allow_unready and not self.connected:
            raise self._error("server is not connected")
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise self._error("server process is not running")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[JsonValue] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                }
            )
            deadline = (
                timeout
                if timeout is not None
                else self.server.request_timeout_ms / 1000
            )
            return await asyncio.wait_for(asyncio.shield(future), timeout=deadline)
        except TimeoutError as exc:
            raise self._error(f"request `{method}` timed out") from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def _notify(self, method: str, params: Mapping[str, JsonValue]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    async def _write(self, message: Mapping[str, JsonValue]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise self._error("server process is not running")
        encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            process.stdin.write(encoded)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise self._error("server stdin closed; retry the request") from exc

    async def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        failure: McpError | None = None
        try:
            while line := await process.stdout.readline():
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise self._error("received malformed JSON-RPC frame") from exc
                self._handle_message(message)
            if not self._closing:
                failure = self._error("server closed stdout; retry the request")
        except asyncio.CancelledError:
            raise
        except McpError as exc:
            failure = exc
        finally:
            self._ready = False
            if failure is not None:
                self._fail_pending(failure)
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.terminate()
            if self._on_disconnect is not None:
                self._on_disconnect()

    def _handle_message(self, message: object) -> None:
        if not isinstance(message, dict):
            raise self._error("received non-object JSON-RPC frame")
        if message.get("jsonrpc") != "2.0":
            raise self._error("received frame with invalid JSON-RPC version")
        if "id" not in message:
            if (
                isinstance(message.get("method"), str)
                and "result" not in message
                and "error" not in message
            ):
                return
            raise self._error("received malformed JSON-RPC notification")
        if "method" in message:
            raise self._error("received unsupported JSON-RPC server request")
        response_id = message["id"]
        if not isinstance(response_id, int) or isinstance(response_id, bool):
            raise self._error("received response with invalid request ID")
        future = self._pending.get(response_id)
        if future is None:
            raise self._error(f"received mismatched response ID {response_id}")
        has_result = "result" in message
        has_error = "error" in message
        if has_result == has_error:
            raise self._error("received malformed JSON-RPC response")
        if has_error:
            error = message["error"]
            if (
                not isinstance(error, dict)
                or not isinstance(error.get("code"), int)
                or isinstance(error.get("code"), bool)
                or not isinstance(error.get("message"), str)
            ):
                raise self._error("received malformed JSON-RPC error")
            future.set_exception(self._error(f"server error: {error['message']}"))
            return
        future.set_result(cast(JsonValue, message["result"]))

    async def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        while chunk := await process.stderr.read(4096):
            self._stderr_chunks.append(chunk)
            self._stderr_size += len(chunk)
            while self._stderr_size > _STDERR_LIMIT and self._stderr_chunks:
                removed = self._stderr_chunks.popleft()
                self._stderr_size -= len(removed)

    def _fail_pending(self, error: McpError) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    def _error(self, message: str) -> McpError:
        stderr = self.stderr_text.strip()
        suffix = f"; stderr: {stderr}" if stderr else ""
        return McpError(f"MCP server `{self.server.name}`: {message}{suffix}")
