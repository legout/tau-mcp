"""MCP server registry, lazy connection state, and cache refresh."""

from __future__ import annotations

import asyncio
import difflib
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

from .cache import CacheStore, ToolMetadata, sanitize_server_name
from .client import JsonValue, McpClient, McpError, ToolCallResult
from .config import ConfigDiagnostic, McpConfig, ServerConfig


@dataclass(frozen=True, slots=True)
class ServerStatus:
    name: str
    state: str
    cached_tool_count: int
    cache_age_seconds: float | None


@dataclass(slots=True)
class _ManagedServer:
    config: ServerConfig
    state: str = "cold"
    client: McpClient | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_activity: float | None = None


class ServerRegistry:
    """Serialize lazy startup and own one client per configured server."""

    def __init__(self, config: McpConfig, cache: CacheStore) -> None:
        self._managed = {
            server.name: _ManagedServer(server)
            for server in config.servers.values()
            if not server.disabled
        }
        self.cache = cache
        self.diagnostics: tuple[ConfigDiagnostic, ...] = config.diagnostics

    @property
    def servers(self) -> tuple[ServerConfig, ...]:
        return tuple(managed.config for managed in self._managed.values())

    async def connect(self, name: str) -> McpClient:
        """Connect a server and refresh its metadata cache."""
        return await self._ensure_connected(name, refresh=True)

    async def disconnect(self, name: str) -> None:
        managed = self._get(name)
        async with managed.lock:
            client = managed.client
            managed.client = None
            managed.state = "cold"
            if client is not None:
                await client.close()

    async def call_tool(
        self,
        qualified_name: str,
        arguments: Mapping[str, JsonValue],
    ) -> ToolCallResult:
        managed, tool_name = self._resolve_server(qualified_name)
        client = await self._ensure_connected(managed.config.name, refresh=False)
        entry = self.cache.read(managed.config)
        available = {tool.name for tool in entry.tools} if entry is not None else set()
        if tool_name not in available:
            raise McpError(self._unknown_tool_message(qualified_name))
        result = await client.call_tool(tool_name, arguments)
        managed.last_activity = time.monotonic()
        return result

    async def ping(self, name: str) -> None:
        managed = self._get(name)
        client = await self._ensure_connected(name, refresh=False)
        await client.ping()
        managed.last_activity = time.monotonic()

    async def close(self) -> None:
        for name in tuple(self._managed):
            await self.disconnect(name)

    def last_activity(self, name: str) -> float | None:
        return self._get(name).last_activity

    def statuses(self) -> tuple[ServerStatus, ...]:
        statuses: list[ServerStatus] = []
        for managed in self._managed.values():
            if managed.state == "ready" and (
                managed.client is None or not managed.client.connected
            ):
                managed.state = "cold"
            entry = self.cache.read(managed.config)
            statuses.append(
                ServerStatus(
                    name=managed.config.name,
                    state=managed.state,
                    cached_tool_count=len(entry.tools) if entry is not None else 0,
                    cache_age_seconds=entry.age_seconds if entry is not None else None,
                )
            )
        return tuple(statuses)

    def format_status(self) -> str:
        statuses = self.statuses()
        if not statuses:
            return "No MCP servers configured."
        lines = []
        for status in statuses:
            age = (
                "never"
                if status.cache_age_seconds is None
                else f"{status.cache_age_seconds:.0f}s"
            )
            lines.append(
                f"{status.name}: {status.state}, {status.cached_tool_count} cached tools, cache age {age}"
            )
        return "\n".join(lines)

    async def _ensure_connected(self, name: str, *, refresh: bool) -> McpClient:
        managed = self._get(name)
        async with managed.lock:
            if managed.client is not None and managed.client.connected:
                managed.state = "ready"
                if refresh:
                    await self._refresh_cache(managed, managed.client)
                return managed.client
            if managed.client is not None:
                await managed.client.close()
                managed.client = None
            managed.state = "connecting"
            client: McpClient
            client = McpClient(
                managed.config,
                on_disconnect=lambda: self._mark_cold(managed, client),
            )
            managed.client = client
            try:
                await client.connect()
                await self._refresh_cache(managed, client)
            except BaseException:
                managed.state = "cold"
                managed.client = None
                await client.close()
                raise
            managed.state = "ready"
            return client

    async def _refresh_cache(self, managed: _ManagedServer, client: McpClient) -> None:
        tools = await client.list_tools()
        self.cache.write(
            managed.config,
            (
                ToolMetadata(tool.name, tool.description, tool.input_schema)
                for tool in tools
            ),
        )

    def _get(self, name: str) -> _ManagedServer:
        try:
            return self._managed[name]
        except KeyError as exc:
            raise McpError(f"MCP server `{name}` is not configured") from exc

    def _resolve_server(self, qualified_name: str) -> tuple[_ManagedServer, str]:
        if "__" not in qualified_name:
            raise McpError(self._unknown_tool_message(qualified_name))
        prefix, tool_name = qualified_name.split("__", 1)
        for managed in self._managed.values():
            if sanitize_server_name(managed.config.name) == prefix and tool_name:
                return managed, tool_name
        raise McpError(self._unknown_tool_message(qualified_name))

    def _unknown_tool_message(self, target: str) -> str:
        known = []
        for managed in self._managed.values():
            entry = self.cache.read(managed.config)
            if entry is None:
                continue
            prefix = sanitize_server_name(managed.config.name)
            known.extend(f"{prefix}__{tool.name}" for tool in entry.tools)
        suggestions = difflib.get_close_matches(target, known, n=3, cutoff=0.3)
        suffix = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        return f"Unknown MCP tool `{target}`.{suffix}"

    @staticmethod
    def _mark_cold(managed: _ManagedServer, client: McpClient) -> None:
        if managed.client is client:
            managed.state = "cold"
