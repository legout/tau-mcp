"""Disk-backed MCP tool metadata cache."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .config import ServerConfig

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    name: str
    description: str
    parameters: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class CacheEntry:
    tools: tuple[ToolMetadata, ...]
    age_seconds: float


@dataclass(frozen=True, slots=True)
class SearchResult:
    qualified_name: str
    description: str
    parameters: Mapping[str, JsonValue]
    score: int


def sanitize_server_name(name: str) -> str:
    """Return the server-name component used by cache files and proxy tools."""
    sanitized = re.sub(r"[^a-z0-9_]", "_", name.lower())
    return sanitized or "server"


def config_digest(server: ServerConfig) -> str:
    """Hash the command-bearing fields that determine a server's metadata."""
    payload = {
        "args": list(server.args),
        "command": server.command,
        "cwd": server.cwd,
        "env": dict(server.env),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return sha256(encoded.encode()).hexdigest()


class CacheStore:
    """Read and write per-server cache records below one injected root."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, server_name: str) -> Path:
        return self.root / f"{sanitize_server_name(server_name)}.json"

    def write(self, server: ServerConfig, tools: Iterable[ToolMetadata]) -> None:
        """Atomically replace a server's metadata after a successful listing."""
        payload = {
            "configDigest": config_digest(server),
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": dict(tool.parameters),
                }
                for tool in tools
            ],
        }
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.path_for(server.name)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.root,
                prefix=f".{destination.name}.",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                json.dump(payload, temporary, ensure_ascii=False, separators=(",", ":"))
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, destination)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    def read(self, server: ServerConfig) -> CacheEntry | None:
        """Read valid metadata, treating malformed or stale files as cold."""
        path = self.path_for(server.name)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            modified = path.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("configDigest") != config_digest(
            server
        ):
            return None
        raw_tools = raw.get("tools")
        if not isinstance(raw_tools, list):
            return None
        tools: list[ToolMetadata] = []
        for raw_tool in raw_tools:
            if not isinstance(raw_tool, dict):
                return None
            name = raw_tool.get("name")
            description = raw_tool.get("description", "")
            parameters = raw_tool.get("inputSchema", {})
            if (
                not isinstance(name, str)
                or not isinstance(description, str)
                or not isinstance(parameters, dict)
            ):
                return None
            tools.append(ToolMetadata(name, description, parameters))
        return CacheEntry(tuple(tools), max(0.0, time.time() - modified))

    def search(
        self,
        servers: Iterable[ServerConfig],
        query: str,
    ) -> tuple[SearchResult, ...]:
        """Rank matching cached metadata without connecting to a server."""
        terms = tuple(dict.fromkeys(query.lower().split()))
        results: list[SearchResult] = []
        for server in servers:
            if server.disabled:
                continue
            entry = self.read(server)
            if entry is None:
                continue
            prefix = sanitize_server_name(server.name)
            for tool in entry.tools:
                score = _score(tool, query.lower().strip(), terms)
                if terms and score == 0:
                    continue
                results.append(
                    SearchResult(
                        qualified_name=f"{prefix}__{tool.name}",
                        description=tool.description,
                        parameters=tool.parameters,
                        score=score,
                    )
                )
        return tuple(
            sorted(results, key=lambda result: (-result.score, result.qualified_name))
        )


def _score(tool: ToolMetadata, query: str, terms: tuple[str, ...]) -> int:
    name = tool.name.lower()
    description = tool.description.lower()
    score = 1000 if query and name == query else 0
    if query and query in name:
        score += 100
    for term in terms:
        if term in name:
            score += 20
        if term in description:
            score += 10
    return score
