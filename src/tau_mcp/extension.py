"""Tau extension registration entry point."""

from __future__ import annotations

import os

from tau_coding.extensions.api import (
    ExtensionAPI,
    ExtensionCommandContext,
    ExtensionContext,
)

from .cache import CacheStore  # ty: ignore[unresolved-import]
from .config import load_config  # ty: ignore[unresolved-import]
from .direct import DirectToolRegistrar  # ty: ignore[unresolved-import]
from .proxy import create_proxy_tool  # ty: ignore[unresolved-import]
from .registry import ServerRegistry  # ty: ignore[unresolved-import]


def setup(tau: ExtensionAPI) -> None:
    """Register the MCP proxy, status command, and session lifecycle."""
    home = tau.context.paths.home
    config = load_config(home / "mcp.json", environment=os.environ)
    registry = ServerRegistry(config, CacheStore(home / "mcp-cache"))
    tau.register_tool(create_proxy_tool(registry))
    direct_tools = DirectToolRegistrar(tau, registry)
    registry.set_tool_discovery_callback(direct_tools.register_discovered)
    direct_tools.register_cached()
    for diagnostic in registry.diagnostics:
        tau.context.ui.notify(diagnostic.message, "warning")

    async def session_start(_event: object, _context: ExtensionContext) -> None:
        await registry.start()

    async def session_shutdown(_event: object, _context: ExtensionContext) -> None:
        await registry.shutdown()

    tau.on("session_start", session_start)
    tau.on("session_shutdown", session_shutdown)

    def status(_args: str, _context: ExtensionCommandContext) -> str:
        return registry.format_status()

    tau.register_command(
        "mcp",
        status,
        description="Show MCP server and metadata cache status.",
        usage="/mcp",
    )
