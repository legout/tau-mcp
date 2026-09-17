"""The single MCP proxy tool for discovery, lifecycle, and calls."""

from __future__ import annotations

import json
from collections.abc import Mapping

from tau_agent.messages import TextContent
from tau_agent.tools import (
    AgentTool,
    AgentToolResult,
    ToolCancellationToken,
    ToolUpdateCallback,
)
from tau_agent.types import JSONValue

from .client import JsonValue as McpJsonValue
from .client import McpError
from .registry import ServerRegistry  # ty: ignore[unresolved-import]

_PARAMETERS: Mapping[str, JSONValue] = {
    "type": "object",
    "properties": {
        "search": {"type": "string", "description": "Search cached MCP tool metadata."},
        "tool": {"type": "string", "description": "Call server__tool."},
        "args": {
            "description": "Tool arguments as an object or JSON string.",
            "anyOf": [{"type": "object"}, {"type": "string"}],
        },
        "connect": {"type": "string", "description": "Connect and refresh one server."},
        "disconnect": {"type": "string", "description": "Disconnect one server."},
    },
    "additionalProperties": False,
}


def create_proxy_tool(registry: ServerRegistry) -> AgentTool:
    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        del tool_call_id, signal, on_update
        if "search" in arguments:
            query = arguments["search"]
            if not isinstance(query, str):
                return _text("`search` must be a string.")
            results = registry.cache.search(registry.servers, query)
            lines: list[str] = []
            if results:
                noun = "tool" if len(results) == 1 else "tools"
                lines.append(f'Found {len(results)} {noun} matching "{query}":')
                for result in results:
                    lines.append(
                        f"{result.qualified_name}\n"
                        f"  {_one_line(result.description) or '(no description)'}\n"
                        f"  Parameters:\n"
                        f"    {json.dumps(dict(result.parameters), sort_keys=True)}"
                    )
            cold = [
                server.name
                for server in registry.servers
                if registry.cache.read(server) is None
            ]
            lines.extend(
                f'{name}: no metadata yet — call connect("{name}")' for name in cold
            )
            if not lines:
                return _text(f'No tools matching "{query}"')
            return _text("\n\n".join(lines))
        if "connect" in arguments:
            name = arguments["connect"]
            if not isinstance(name, str):
                return _text("`connect` must be a server name string.")
            try:
                await registry.connect(name)
            except McpError as exc:
                return _text(str(exc))
            return _text(f'MCP server "{name}" connected and metadata refreshed.')
        if "disconnect" in arguments:
            name = arguments["disconnect"]
            if not isinstance(name, str):
                return _text("`disconnect` must be a server name string.")
            try:
                await registry.disconnect(name)
            except McpError as exc:
                return _text(str(exc))
            return _text(f'MCP server "{name}" disconnected.')
        if "tool" in arguments:
            target = arguments["tool"]
            if not isinstance(target, str):
                return _text("`tool` must be a server__tool name string.")
            parsed_args = _tool_arguments(arguments.get("args", {}))
            if isinstance(parsed_args, str):
                return _text(parsed_args)
            try:
                result = await registry.call_tool(target, parsed_args)
            except McpError as exc:
                return _text(str(exc))
            texts: list[str] = []
            for item in result.content:
                value = item.get("text")
                if item.get("type") == "text" and isinstance(value, str):
                    texts.append(value)
            return _text("\n".join(texts) or "MCP tool returned no text content.")
        return _text("Provide one of: search, tool, connect, or disconnect.")

    return AgentTool(
        name="mcp",
        label="MCP",
        description=(
            "Search cached MCP tools or route a call by server__tool name. "
            "Servers stay offline until connect or a tool call requires one."
        ),
        parameters=_PARAMETERS,
        execute_fn=execute,
        execution_mode="sequential",
    )


def _text(message: str) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=message)])


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _tool_arguments(value: JSONValue) -> Mapping[str, McpJsonValue] | str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return (
                "`args` must be a JSON object or a string containing one JSON object."
            )
    if not isinstance(value, dict):
        return "`args` must be a JSON object or a string containing one JSON object."
    return value
