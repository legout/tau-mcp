"""MCP server registry, lifecycle state, and cache refresh."""

from __future__ import annotations

import asyncio
import difflib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from tau_coding.extensions.api import ExtensionError

from .cache import CacheStore, ToolMetadata, sanitize_server_name
from .client import JsonValue, McpClient, McpError, ToolCallResult
from .config import ConfigDiagnostic, McpConfig, ServerConfig

_STALE_MESSAGE = "MCP registry is stale after session shutdown"
type ToolDiscoveryCallback = Callable[
    [ServerConfig, tuple[ToolMetadata, ...]], tuple[str, ...]
]


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
    idle_task: asyncio.Task[None] | None = None
    in_flight: int = 0
    last_activity: float | None = None


class ServerRegistry:
    """Own one generation of lazy/eager servers and their idle tasks."""

    def __init__(
        self,
        config: McpConfig,
        cache: CacheStore,
        *,
        handshake_timeout: float = 10,
    ) -> None:
        self._managed = {
            server.name: _ManagedServer(server)
            for server in config.servers.values()
            if not server.disabled
        }
        self._handshake_timeout = handshake_timeout
        self._active = True
        self._tool_discovery_callback: ToolDiscoveryCallback | None = None
        namespace_owners: dict[str, list[str]] = {}
        for server in self._managed.values():
            namespace_owners.setdefault(
                sanitize_server_name(server.config.name), []
            ).append(server.config.name)
        self._namespace_owners = {
            namespace: tuple(owners) for namespace, owners in namespace_owners.items()
        }
        self._reserved_namespaces = tuple(
            sorted(
                (
                    namespace
                    for namespace in self._namespace_owners
                    if "__" in namespace
                ),
                key=len,
                reverse=True,
            )
        )
        collision_diagnostics = tuple(
            ConfigDiagnostic(
                f"sanitized server namespace collision `{namespace}`: "
                f"{', '.join(owners)}",
            )
            for namespace, owners in self._namespace_owners.items()
            if len(owners) > 1
        )
        reserved_diagnostics = tuple(
            ConfigDiagnostic(
                f"sanitized server namespace `{namespace}` contains reserved "
                "delimiter `__` and is unavailable for qualified MCP tool addressing"
            )
            for namespace in self._reserved_namespaces
        )
        self.cache = cache
        self.diagnostics: tuple[ConfigDiagnostic, ...] = (
            *config.diagnostics,
            *collision_diagnostics,
            *reserved_diagnostics,
        )

    @property
    def servers(self) -> tuple[ServerConfig, ...]:
        self._assert_active()
        return tuple(managed.config for managed in self._managed.values())

    async def start(self) -> None:
        """Connect all eager servers for this session generation."""
        self._assert_active()
        eager = [
            self.connect(managed.config.name)
            for managed in self._managed.values()
            if managed.config.lifecycle == "eager"
        ]
        if not eager:
            return
        results = await asyncio.gather(*eager, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def connect(self, name: str) -> McpClient:
        """Connect a server and refresh its metadata cache."""
        client, _ = await self.connect_with_added(name)
        return client

    async def connect_with_added(self, name: str) -> tuple[McpClient, tuple[str, ...]]:
        self._assert_active()
        client, _, added = await self._ensure_connected(
            name, refresh=True, allow_spawn=True, lease=False
        )
        return client, added

    async def disconnect(self, name: str) -> None:
        self._assert_active()
        await self._disconnect_managed(self._get(name), force=False, stopped=False)

    async def call_tool(
        self,
        qualified_name: str,
        arguments: Mapping[str, JsonValue],
    ) -> ToolCallResult:
        result, _ = await self.call_tool_with_added(qualified_name, arguments)
        return result

    async def call_tool_with_added(
        self,
        qualified_name: str,
        arguments: Mapping[str, JsonValue],
    ) -> tuple[ToolCallResult, tuple[str, ...]]:
        self._assert_active()
        managed, tool_name = self._resolve_server(qualified_name)
        client, managed, added = await self._ensure_connected(
            managed.config.name,
            refresh=False,
            allow_spawn=managed.config.lifecycle != "eager",
            lease=True,
        )
        call_succeeded = False
        try:
            entry = self.cache.read(managed.config)
            available = (
                {tool.name for tool in entry.tools} if entry is not None else set()
            )
            if tool_name not in available:
                raise McpError(self._unknown_tool_message(qualified_name))
            result = await client.call_tool(tool_name, arguments)
            call_succeeded = True
            return result, added
        finally:
            self._release_use(managed, succeeded=call_succeeded)

    async def ping(self, name: str) -> None:
        self._assert_active()
        managed = self._get(name)
        client, managed, _ = await self._ensure_connected(
            name,
            refresh=False,
            allow_spawn=managed.config.lifecycle != "eager",
            lease=True,
        )
        ping_succeeded = False
        try:
            await client.ping()
            ping_succeeded = True
        finally:
            self._release_use(managed, succeeded=ping_succeeded)

    async def close(self) -> None:
        """Close children while retaining an active, reusable generation."""
        self._assert_active()
        await asyncio.gather(
            *(
                self._disconnect_managed(managed, force=False, stopped=False)
                for managed in self._managed.values()
            )
        )

    async def shutdown(self) -> None:
        """Invalidate this generation, cancel timers/requests, and reap children."""
        if not self._active:
            return
        self._active = False
        clients: list[McpClient] = []
        idle_tasks: list[asyncio.Task[None]] = []
        for managed in self._managed.values():
            if managed.idle_task is not None and not managed.idle_task.done():
                idle_tasks.append(managed.idle_task)
            self._cancel_idle(managed)
            if managed.client is not None:
                clients.append(managed.client)
            managed.client = None
            managed.state = "stopped"
        await asyncio.gather(
            *(client.close(force=True) for client in clients),
            *idle_tasks,
            return_exceptions=True,
        )
        for managed in self._managed.values():
            managed.state = "stopped"

    def set_tool_discovery_callback(
        self, callback: ToolDiscoveryCallback | None
    ) -> None:
        self._assert_active()
        self._tool_discovery_callback = callback

    def namespace_available(self, server_name: str) -> bool:
        self._assert_active()
        namespace = sanitize_server_name(server_name)
        owners = self._namespace_owners.get(namespace, ())
        return "__" not in namespace and owners == (server_name,)

    def last_activity(self, name: str) -> float | None:
        self._assert_active()
        return self._get(name).last_activity

    def statuses(self) -> tuple[ServerStatus, ...]:
        self._assert_active()
        statuses: list[ServerStatus] = []
        for managed in self._managed.values():
            if managed.state == "ready" and (
                managed.client is None or not managed.client.connected
            ):
                self._mark_cold(managed, managed.client)
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

    async def _ensure_connected(
        self,
        name: str,
        *,
        refresh: bool,
        allow_spawn: bool,
        lease: bool,
    ) -> tuple[McpClient, _ManagedServer, tuple[str, ...]]:
        managed = self._get(name)
        async with managed.lock:
            self._assert_active()
            if managed.client is not None and managed.client.connected:
                managed.state = "ready"
                added = (
                    await self._refresh_cache(managed, managed.client)
                    if refresh
                    else ()
                )
                if lease:
                    managed.in_flight += 1
                self._record_activity(managed)
                return managed.client, managed, added
            if not allow_spawn:
                raise McpError(
                    f"MCP server `{name}` is disconnected; call connect to restart it"
                )
            if managed.client is not None:
                await managed.client.close(force=True)
                managed.client = None
            managed.state = "connecting"
            client: McpClient
            client = McpClient(
                managed.config,
                on_disconnect=lambda: self._mark_cold(managed, client),
                handshake_timeout=self._handshake_timeout,
            )
            managed.client = client
            try:
                await client.connect()
                self._assert_active()
                added = await self._refresh_cache(managed, client)
            except BaseException:
                managed.state = "cold" if self._active else "stopped"
                if managed.client is client:
                    managed.client = None
                await client.close(force=not self._active)
                raise
            managed.state = "ready"
            if lease:
                managed.in_flight += 1
            self._record_activity(managed)
            return client, managed, added

    async def _refresh_cache(
        self, managed: _ManagedServer, client: McpClient
    ) -> tuple[str, ...]:
        tools = tuple(
            ToolMetadata(tool.name, tool.description, tool.input_schema)
            for tool in await client.list_tools()
        )
        self.cache.write(managed.config, tools)
        if self._tool_discovery_callback is None:
            return ()
        return self._tool_discovery_callback(managed.config, tools)

    async def _disconnect_managed(
        self,
        managed: _ManagedServer,
        *,
        force: bool,
        stopped: bool,
    ) -> None:
        self._cancel_idle(managed)
        async with managed.lock:
            client = managed.client
            managed.client = None
            managed.state = "stopped" if stopped else "cold"
            if client is not None:
                await client.close(force=force)

    def _record_activity(self, managed: _ManagedServer) -> None:
        if not self._active:
            return
        managed.last_activity = time.monotonic()
        if managed.config.idle_timeout <= 0:
            self._cancel_idle(managed)
            return
        if managed.idle_task is not None and not managed.idle_task.done():
            return
        managed.idle_task = asyncio.create_task(
            self._idle_disconnect(managed),
            name=f"mcp-idle-{managed.config.name}",
        )

    def _release_use(self, managed: _ManagedServer, *, succeeded: bool) -> None:
        managed.in_flight = max(0, managed.in_flight - 1)
        if succeeded:
            self._record_activity(managed)

    async def _idle_disconnect(self, managed: _ManagedServer) -> None:
        current = asyncio.current_task()
        delay = managed.config.idle_timeout * 60
        try:
            while self._active:
                await asyncio.sleep(delay)
                async with managed.lock:
                    if managed.idle_task is not current or not self._active:
                        return
                    if managed.in_flight:
                        delay = managed.config.idle_timeout * 60
                        continue
                    last_activity = managed.last_activity or 0
                    remaining = managed.config.idle_timeout * 60 - (
                        time.monotonic() - last_activity
                    )
                    if remaining > 0:
                        delay = remaining
                        continue
                    client = managed.client
                    managed.state = "cold"
                if client is not None:
                    await client.close()
                    async with managed.lock:
                        if managed.client is client:
                            managed.client = None
                return
        except asyncio.CancelledError:
            return
        finally:
            if managed.idle_task is current:
                managed.idle_task = None

    def _cancel_idle(self, managed: _ManagedServer) -> None:
        task = managed.idle_task
        managed.idle_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _get(self, name: str) -> _ManagedServer:
        try:
            return self._managed[name]
        except KeyError as exc:
            raise McpError(f"MCP server `{name}` is not configured") from exc

    def _resolve_server(self, qualified_name: str) -> tuple[_ManagedServer, str]:
        for namespace in self._reserved_namespaces:
            if qualified_name.startswith(f"{namespace}__"):
                raise McpError(
                    f"MCP server namespace `{namespace}` is unavailable because it "
                    "contains reserved delimiter `__`"
                )
        if "__" not in qualified_name:
            raise McpError(self._unknown_tool_message(qualified_name))
        prefix, tool_name = qualified_name.split("__", 1)
        owners = self._namespace_owners.get(prefix, ())
        if len(owners) > 1:
            raise McpError(
                f"Ambiguous MCP server namespace `{prefix}`: {', '.join(owners)}"
            )
        if len(owners) == 1 and tool_name:
            return self._managed[owners[0]], tool_name
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

    def _mark_cold(self, managed: _ManagedServer, client: McpClient | None) -> None:
        if managed.client is client:
            if managed.state not in {"cold", "stopped"}:
                self._cancel_idle(managed)
            managed.state = "cold" if self._active else "stopped"

    def _assert_active(self) -> None:
        if not self._active:
            raise ExtensionError(_STALE_MESSAGE)
