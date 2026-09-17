from __future__ import annotations

import asyncio
import json
import statistics
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from tau_coding.commands import CommandContext, CommandSession
from tau_coding.extensions.runtime import ExtensionRuntime
from tau_coding.paths import TauPaths
from tau_coding.resources import TauResourcePaths

from tau_mcp.cache import CacheStore, ToolMetadata, config_digest
from tau_mcp.config import load_config

ROOT = Path(__file__).parents[1]


def _write_config(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_config_parses_expansion_duplicates_and_isolates_invalid_entries(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mcp.json"
    _write_config(
        path,
        """{
          "mcpServers": {
            "duplicate": {"command": "old"},
            "invalid": {"args": ["missing-command"]},
            "remote": {"url": "https://example.test/mcp"},
            "duplicate": {
              "command": "${BIN}/server",
              "args": ["~/data", "${TOKEN}"],
              "env": {"TOKEN": "${TOKEN}", "HOME_COPY": "~"},
              "cwd": "~/work",
              "lifecycle": "eager",
              "requestTimeoutMs": 1234
            }
          },
          "settings": {"idleTimeout": 4, "requestTimeoutMs": 5678}
        }""",
    )

    config = load_config(
        path,
        environment={"BIN": "/opt/bin", "TOKEN": "secret", "HOME": "/home/test"},
    )

    assert tuple(config.servers) == ("duplicate",)
    server = config.servers["duplicate"]
    assert server.command == "/opt/bin/server"
    assert server.args == ("/home/test/data", "secret")
    assert dict(server.env) == {"TOKEN": "secret", "HOME_COPY": "/home/test"}
    assert server.cwd == "/home/test/work"
    assert server.lifecycle == "eager"
    assert server.idle_timeout == 4
    assert server.request_timeout_ms == 1234
    messages = [diagnostic.message for diagnostic in config.diagnostics]
    assert any("duplicate server name `duplicate`" in message for message in messages)
    assert any("invalid" in message and "command" in message for message in messages)
    assert any(
        "remote" in message and "HTTP transport not supported yet" in message
        for message in messages
    )


def test_missing_config_is_empty_and_not_created(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "mcp.json"

    config = load_config(path, environment={"HOME": str(tmp_path)})

    assert not config.servers
    assert not config.diagnostics
    assert not path.exists()


def test_cache_digest_invalidation_and_deterministic_ranking(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp.json"
    _write_config(
        config_path,
        json.dumps(
            {
                "mcpServers": {
                    "alpha": {"command": "python", "args": ["one"]},
                    "beta": {"command": "python", "args": ["two"]},
                }
            }
        ),
    )
    config = load_config(config_path, environment={"HOME": str(tmp_path)})
    cache = CacheStore(tmp_path / "cache")
    alpha = config.servers["alpha"]
    beta = config.servers["beta"]
    cache.write(
        alpha,
        (
            ToolMetadata(
                "query_docs", "Query project documentation", {"type": "object"}
            ),
            ToolMetadata("screenshot", "Capture a browser image", {"type": "object"}),
        ),
    )
    cache.write(
        beta,
        (ToolMetadata("query", "Search remote code", {"type": "object"}),),
    )

    assert config_digest(alpha) != config_digest(beta)
    assert [
        result.qualified_name
        for result in cache.search(config.servers.values(), "query")
    ] == [
        "beta__query",
        "alpha__query_docs",
    ]
    assert cache.read(alpha) is not None

    changed_path = tmp_path / "changed.json"
    _write_config(
        changed_path,
        json.dumps(
            {"mcpServers": {"alpha": {"command": "python", "args": ["changed"]}}}
        ),
    )
    changed = load_config(changed_path, environment={"HOME": str(tmp_path)}).servers[
        "alpha"
    ]
    assert cache.read(changed) is None


def test_cached_search_benchmark_stays_below_fifty_milliseconds(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp.json"
    servers = {
        f"server-{index}": {"command": "server", "args": [str(index)]}
        for index in range(20)
    }
    _write_config(config_path, json.dumps({"mcpServers": servers}))
    config = load_config(config_path, environment={"HOME": str(tmp_path)})
    cache = CacheStore(tmp_path / "cache")
    for server in config.servers.values():
        cache.write(
            server,
            tuple(
                ToolMetadata(
                    f"tool_{index}",
                    f"Search representative catalog item {index}",
                    {"type": "object", "properties": {"query": {"type": "string"}}},
                )
                for index in range(50)
            ),
        )

    durations = []
    results = ()
    for _ in range(15):
        started = time.perf_counter()
        results = cache.search(config.servers.values(), "representative search")
        durations.append(time.perf_counter() - started)
    assert len(results) == 1000
    assert statistics.median(durations) < 0.050


def test_runtime_load_registers_only_proxy_and_status_command_without_spawning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tau_home = tmp_path / ".tau"
    config_path = tau_home / "mcp.json"
    _write_config(
        config_path,
        json.dumps({"mcpServers": {"alpha": {"command": "must-not-run"}}}),
    )
    config = load_config(config_path, environment={"HOME": str(tmp_path)})
    CacheStore(tau_home / "mcp-cache").write(
        config.servers["alpha"],
        (ToolMetadata("query", "Query local metadata", {"type": "object"}),),
    )

    def fail_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("offline extension surface must not spawn")

    monkeypatch.setattr(subprocess, "Popen", fail_spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)

    paths = TauPaths(home=tau_home, agents_home=tmp_path / ".agents")
    runtime = ExtensionRuntime(built_in_extensions=(), paths=paths)
    runtime.load(
        TauResourcePaths(root=tau_home, agents_root=paths.agents_home, paths=paths),
        extra_paths=(ROOT,),
        include_resource_dirs=False,
    )

    assert not runtime.diagnostics
    assert [tool.name for tool in runtime.extension_tools] == ["mcp"]
    tool = runtime.extension_tools[0]
    properties = cast(Mapping[str, object], tool.parameters["properties"])
    assert set(properties) == {
        "search",
        "tool",
        "args",
        "connect",
        "disconnect",
    }
    command_registry = runtime.build_command_registry()
    command = command_registry.get("mcp")
    assert command is not None
    status = command.handler(
        CommandContext(
            session=cast(CommandSession, object()),
            registry=command_registry,
            text="/mcp",
            name="mcp",
            args="",
        )
    )
    assert status.message is not None
    assert "alpha" in status.message
    assert "cold" in status.message
    assert "1 cached tools" in status.message

    search_result = asyncio.run(tool.execute("call-1", {"search": "query"}))
    assert search_result.text.startswith('Found 1 tool matching "query":')
    assert "alpha__query\n  Query local metadata\n  Parameters:" in search_result.text
    assert '"type": "object"' in search_result.text
