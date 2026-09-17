"""Opt-in native Tau tools backed by the shared MCP registry."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase

from tau_agent.messages import TextContent
from tau_agent.tools import (
    AgentTool,
    AgentToolResult,
    ToolCancellationToken,
    ToolUpdateCallback,
)
from tau_agent.types import JSONValue
from tau_coding.extensions.api import ExtensionAPI

from .cache import ToolMetadata, sanitize_server_name
from .client import McpError, ToolCallResult
from .config import ServerConfig
from .registry import ServerRegistry

_OUTPUT_LIMIT = 256 * 1024


def tool_is_selected(server: ServerConfig, tool_name: str) -> bool:
    """Apply directTools, then includeTools, then excludeTools."""
    selected = server.direct_tools is True or (
        isinstance(server.direct_tools, tuple) and tool_name in server.direct_tools
    )
    if not selected:
        return False
    if server.include_tools and not any(
        fnmatchcase(tool_name, pattern) for pattern in server.include_tools
    ):
        return False
    return not any(fnmatchcase(tool_name, pattern) for pattern in server.exclude_tools)


class DirectToolRegistrar:
    """Register each selected direct tool once for one Tau generation."""

    def __init__(self, tau: ExtensionAPI, registry: ServerRegistry) -> None:
        self._tau = tau
        self._registry = registry
        self._registered: set[str] = set()

    def register_cached(self) -> tuple[str, ...]:
        added: list[str] = []
        for server in self._registry.servers:
            entry = self._registry.cache.read(server)
            if entry is not None:
                added.extend(self.register_discovered(server, entry.tools))
        return tuple(added)

    def register_discovered(
        self,
        server: ServerConfig,
        tools: Iterable[ToolMetadata],
    ) -> tuple[str, ...]:
        if not self._registry.namespace_available(server.name):
            return ()
        added: list[str] = []
        for metadata in tools:
            if not tool_is_selected(server, metadata.name):
                continue
            name = f"{sanitize_server_name(server.name)}__{metadata.name}"
            if name in self._registered:
                continue
            self._tau.register_tool(self._create_tool(name, metadata))
            self._registered.add(name)
            added.append(name)
        return tuple(added)

    def _create_tool(self, name: str, metadata: ToolMetadata) -> AgentTool:
        async def execute(
            tool_call_id: str,
            arguments: Mapping[str, JSONValue],
            signal: ToolCancellationToken | None = None,
            on_update: ToolUpdateCallback | None = None,
        ) -> AgentToolResult:
            del tool_call_id, signal, on_update
            try:
                result, added = await self._registry.call_tool_with_added(
                    name, arguments
                )
            except McpError as exc:
                return text_result(str(exc))
            return tool_call_result(result, added)

        return AgentTool(
            name=name,
            label=name,
            description=metadata.description or f"MCP tool {metadata.name}",
            parameters=metadata.parameters,
            execute_fn=execute,
        )


def text_result(message: str, added: Iterable[str] = ()) -> AgentToolResult:
    names = list(added)
    return AgentToolResult(
        content=[TextContent(text=message)],
        added_tool_names=names or None,
    )


def tool_call_result(
    result: ToolCallResult,
    added: Iterable[str] = (),
) -> AgentToolResult:
    texts: list[str] = []
    for item in result.content:
        value = item.get("text")
        if item.get("type") == "text" and isinstance(value, str):
            texts.append(value)
    text = "\n".join(texts) or "MCP tool returned no text content."
    return text_result(_truncate_output(text), added)


def _truncate_output(value: str) -> str:
    encoded = value.encode()
    if len(encoded) <= _OUTPUT_LIMIT:
        return value
    note = (
        f"\n\n[truncated MCP output: original {len(encoded)} bytes; "
        f"limit {_OUTPUT_LIMIT} bytes]"
    )
    budget = _OUTPUT_LIMIT - len(note.encode())
    prefix = encoded[:budget].decode(errors="ignore")
    return prefix + note
