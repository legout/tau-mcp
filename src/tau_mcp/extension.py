"""Tau extension registration entry point."""

from __future__ import annotations

import os

from tau_coding.extensions.api import ExtensionAPI, ExtensionCommandContext

from .cache import CacheStore  # ty: ignore[unresolved-import]
from .config import load_config  # ty: ignore[unresolved-import]
from .proxy import create_proxy_tool  # ty: ignore[unresolved-import]
from .registry import ServerRegistry  # ty: ignore[unresolved-import]


def setup(tau: ExtensionAPI) -> None:
    """Register the offline MCP proxy and synchronous status command."""
    home = tau.context.paths.home
    config = load_config(home / "mcp.json", environment=os.environ)
    registry = ServerRegistry(config, CacheStore(home / "mcp-cache"))
    tau.register_tool(create_proxy_tool(registry))

    def status(_args: str, _context: ExtensionCommandContext) -> str:
        return registry.format_status()

    tau.register_command(
        "mcp",
        status,
        description="Show MCP server and metadata cache status.",
        usage="/mcp",
    )
