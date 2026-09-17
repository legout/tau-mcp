"""The single proxy tool, limited to offline cache search in this slice."""

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
        for operation in ("tool", "connect", "disconnect"):
            if operation in arguments:
                return _text(
                    f"`{operation}` is not yet connected in the offline extension surface."
                )
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
