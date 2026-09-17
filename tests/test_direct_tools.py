from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

from tau_coding.extensions.api import NotifyLevel, NullUiBridge
from tau_coding.extensions.runtime import ExtensionRuntime
from tau_coding.paths import TauPaths
from tau_coding.resources import TauResourcePaths

from tau_mcp.cache import CacheStore, JsonValue, ToolMetadata
from tau_mcp.config import McpConfig, ServerConfig, load_config
from tau_mcp.direct import tool_is_selected
from tau_mcp.proxy import create_proxy_tool
from tau_mcp.registry import ServerRegistry

ROOT = Path(__file__).parents[1]
FAKE_SERVER = Path(__file__).with_name("fake_mcp_server.py")
ECHO_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
}
SUM_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
    "required": ["a", "b"],
}


class RecordingUi(NullUiBridge):
    def __init__(self) -> None:
        self.messages: list[str] = []

    def notify(self, message: str, level: NotifyLevel = "info") -> None:
        self.messages.append(f"{level}: {message}")


def _configured_runtime(
    tmp_path: Path,
    *,
    direct_tools: bool | list[str] | None,
    include_tools: list[str] | None = None,
    exclude_tools: list[str] | None = None,
    populate_cache: bool,
) -> tuple[ExtensionRuntime, Path, Path]:
    tau_home = tmp_path / ".tau"
    tau_home.mkdir()
    pid_file = tmp_path / "pids.txt"
    event_file = tmp_path / "events.txt"
    entry: dict[str, object] = {
        "command": sys.executable,
        "args": [
            str(FAKE_SERVER),
            "--pid-file",
            str(pid_file),
            "--event-file",
            str(event_file),
        ],
        "idleTimeout": 0,
    }
    if direct_tools is not None:
        entry["directTools"] = direct_tools
    if include_tools is not None:
        entry["includeTools"] = include_tools
    if exclude_tools is not None:
        entry["excludeTools"] = exclude_tools
    config_path = tau_home / "mcp.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"fixture": entry}}), encoding="utf-8"
    )
    config = load_config(config_path, environment={"HOME": str(tmp_path)})
    if populate_cache:
        CacheStore(tau_home / "mcp-cache").write(
            config.servers["fixture"],
            (
                ToolMetadata("echo", "Echo one value", ECHO_SCHEMA),
                ToolMetadata("sum", "Add two numbers", SUM_SCHEMA),
            ),
        )
    paths = TauPaths(home=tau_home, agents_home=tmp_path / ".agents")
    runtime = ExtensionRuntime(built_in_extensions=(), paths=paths)
    runtime.load(
        TauResourcePaths(root=tau_home, agents_root=paths.agents_home, paths=paths),
        extra_paths=(ROOT,),
        include_resource_dirs=False,
    )
    return runtime, pid_file, event_file


def _pids(path: Path) -> list[int]:
    if not path.exists():
        return []
    return [int(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_reaped(path: Path) -> None:
    pids = _pids(path)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and any(
        Path(f"/proc/{pid}").exists() for pid in pids
    ):
        time.sleep(0.01)
    assert pids
    assert all(not Path(f"/proc/{pid}").exists() for pid in pids)


def test_direct_selection_absent_true_list_and_filter_precedence() -> None:
    base = ServerConfig(name="server", command="command")
    assert not tool_is_selected(base, "echo")
    assert tool_is_selected(replace(base, direct_tools=True), "echo")
    assert tool_is_selected(replace(base, direct_tools=("echo",)), "echo")
    assert not tool_is_selected(replace(base, direct_tools=("sum",)), "echo")

    filtered = replace(
        base,
        direct_tools=True,
        include_tools=("echo*", "sum"),
        exclude_tools=("*_private", "sum"),
    )
    assert tool_is_selected(filtered, "echo")
    assert not tool_is_selected(filtered, "echo_private")
    assert not tool_is_selected(filtered, "sum")
    assert not tool_is_selected(filtered, "other")


def test_proxy_only_is_default_even_with_populated_cache(tmp_path: Path) -> None:
    runtime, _, _ = _configured_runtime(
        tmp_path, direct_tools=None, populate_cache=True
    )

    async def scenario() -> None:
        assert [tool.name for tool in runtime.extension_tools] == ["mcp"]
        await runtime.emit_session_shutdown("quit")
        await runtime.aclose()

    asyncio.run(scenario())


def test_cached_direct_tool_keeps_exact_schema_and_routes_live_through_registry(
    tmp_path: Path,
) -> None:
    runtime, pid_file, _ = _configured_runtime(
        tmp_path,
        direct_tools=["echo"],
        populate_cache=True,
    )
    tools = {tool.name: tool for tool in runtime.extension_tools}
    assert set(tools) == {"mcp", "fixture__echo"}
    assert tools["fixture__echo"].parameters == ECHO_SCHEMA
    assert "fixture__sum" not in tools

    async def scenario() -> None:
        direct = await tools["fixture__echo"].execute("direct", {"value": "direct"})
        assert direct.text == "direct"
        proxy = await tools["mcp"].execute(
            "proxy", {"tool": "fixture__echo", "args": {"value": "proxy"}}
        )
        assert proxy.text == "proxy"
        assert len(_pids(pid_file)) == 1
        await runtime.emit_session_shutdown("quit")
        await runtime.aclose()

    asyncio.run(scenario())
    _assert_reaped(pid_file)


def test_first_discovery_registers_selected_tool_once_and_returns_added_names(
    tmp_path: Path,
) -> None:
    runtime, pid_file, _ = _configured_runtime(
        tmp_path,
        direct_tools=True,
        include_tools=["e*", "sum"],
        exclude_tools=["sum"],
        populate_cache=False,
    )
    assert [tool.name for tool in runtime.extension_tools] == ["mcp"]

    async def scenario() -> None:
        proxy = runtime.extension_tools[0]
        first, second = await asyncio.gather(
            proxy.execute("connect", {"connect": "fixture"}),
            proxy.execute("connect-again", {"connect": "fixture"}),
        )
        assert [first.added_tool_names, second.added_tool_names].count(
            ["fixture__echo"]
        ) == 1
        assert [tool.name for tool in runtime.extension_tools] == [
            "mcp",
            "fixture__echo",
        ]
        assert [tool.name for tool in runtime.extension_tools].count(
            "fixture__echo"
        ) == 1
        direct = runtime.extension_tools[1]
        assert direct.parameters == ECHO_SCHEMA
        result = await direct.execute("direct", {"value": "discovered"})
        assert result.text == "discovered"
        assert not result.added_tool_names
        await runtime.emit_session_shutdown("quit")
        await runtime.aclose()

    asyncio.run(scenario())
    _assert_reaped(pid_file)


def test_sanitized_namespace_collision_is_diagnostic_and_registers_nothing(
    tmp_path: Path,
) -> None:
    first = ServerConfig(name="Foo-Bar", command="same", direct_tools=True)
    second = ServerConfig(name="foo_bar", command="same", direct_tools=True)
    cache = CacheStore(tmp_path / "cache")
    cache.write(first, (ToolMetadata("echo", "Echo", ECHO_SCHEMA),))
    registry = ServerRegistry(
        McpConfig(servers={first.name: first, second.name: second}), cache
    )
    assert any("namespace collision" in item.message for item in registry.diagnostics)

    tau_home = tmp_path / ".tau"
    tau_home.mkdir()
    (tau_home / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    first.name: {"command": "same", "directTools": True},
                    second.name: {"command": "same", "directTools": True},
                }
            }
        ),
        encoding="utf-8",
    )
    CacheStore(tau_home / "mcp-cache").write(
        first, (ToolMetadata("echo", "Echo", ECHO_SCHEMA),)
    )
    paths = TauPaths(home=tau_home, agents_home=tmp_path / ".agents")
    recording_ui = RecordingUi()
    runtime = ExtensionRuntime(built_in_extensions=(), paths=paths, ui=recording_ui)
    runtime.load(
        TauResourcePaths(root=tau_home, agents_root=paths.agents_home, paths=paths),
        extra_paths=(ROOT,),
        include_resource_dirs=False,
    )
    assert [tool.name for tool in runtime.extension_tools] == ["mcp"]
    assert any("namespace collision" in message for message in recording_ui.messages)

    async def scenario() -> None:
        collision = await create_proxy_tool(registry).execute(
            "collision", {"tool": "foo_bar__echo", "args": {}}
        )
        assert "Ambiguous MCP server namespace" in collision.text
        await registry.shutdown()
        await runtime.emit_session_shutdown("quit")
        await runtime.aclose()

    asyncio.run(scenario())
