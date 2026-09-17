"""Offline MCP server registry and observable cold-state status."""

from __future__ import annotations

from dataclasses import dataclass

from .cache import CacheStore
from .config import ConfigDiagnostic, McpConfig, ServerConfig


@dataclass(frozen=True, slots=True)
class ServerStatus:
    name: str
    state: str
    cached_tool_count: int
    cache_age_seconds: float | None


class ServerRegistry:
    """Hold configured servers without creating clients or processes."""

    def __init__(self, config: McpConfig, cache: CacheStore) -> None:
        self._servers: tuple[ServerConfig, ...] = tuple(
            server for server in config.servers.values() if not server.disabled
        )
        self.cache = cache
        self.diagnostics: tuple[ConfigDiagnostic, ...] = config.diagnostics

    @property
    def servers(self) -> tuple[ServerConfig, ...]:
        return self._servers

    def statuses(self) -> tuple[ServerStatus, ...]:
        statuses: list[ServerStatus] = []
        for server in self._servers:
            entry = self.cache.read(server)
            statuses.append(
                ServerStatus(
                    name=server.name,
                    state="cold",
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
