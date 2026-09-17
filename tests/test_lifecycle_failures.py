from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from tau_coding.extensions.api import ExtensionError
from tau_coding.extensions.runtime import ExtensionRuntime
from tau_coding.paths import TauPaths
from tau_coding.resources import TauResourcePaths

from tau_mcp.cache import CacheStore, ToolMetadata
from tau_mcp.client import McpError
from tau_mcp.config import McpConfig, ServerConfig
from tau_mcp.proxy import create_proxy_tool
from tau_mcp.registry import ServerRegistry

ROOT = Path(__file__).parents[1]
FAKE_SERVER = Path(__file__).with_name("fake_mcp_server.py")


def _server(
    tmp_path: Path,
    *,
    mode: str = "normal",
    lifecycle: str = "lazy",
    idle_timeout: float = 0,
    request_timeout_ms: int = 2_000,
    crash_once: Path | None = None,
    name: str = "fixture",
) -> tuple[ServerConfig, Path, Path]:
    pid_file = tmp_path / f"{name}-pids.txt"
    event_file = tmp_path / f"{name}-events.txt"
    args = [
        str(FAKE_SERVER),
        "--pid-file",
        str(pid_file),
        "--event-file",
        str(event_file),
        "--mode",
        mode,
    ]
    if crash_once is not None:
        args.extend(("--crash-once", str(crash_once)))
    return (
        ServerConfig(
            name=name,
            command=sys.executable,
            args=tuple(args),
            lifecycle=lifecycle,
            idle_timeout=idle_timeout,
            request_timeout_ms=request_timeout_ms,
        ),
        pid_file,
        event_file,
    )


def _registry(
    tmp_path: Path,
    server: ServerConfig,
    *,
    handshake_timeout: float = 10.0,
) -> ServerRegistry:
    return ServerRegistry(
        McpConfig(servers={server.name: server}),
        CacheStore(tmp_path / "cache"),
        handshake_timeout=handshake_timeout,
    )


def _pids(path: Path) -> list[int]:
    if not path.exists():
        return []
    return [int(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_reaped(*paths: Path) -> None:
    pids = [pid for path in paths for pid in _pids(path)]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and any(
        Path(f"/proc/{pid}").exists() for pid in pids
    ):
        time.sleep(0.01)
    assert pids
    assert all(not Path(f"/proc/{pid}").exists() for pid in pids)


def _tasks_named(name: str) -> list[asyncio.Task[object]]:
    return [task for task in asyncio.all_tasks() if task.get_name() == name]


async def _wait_until(predicate: Callable[[], bool], timeout: float = 2) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.01)


def test_eager_session_start_reload_shutdown_invalidates_and_reaps(
    tmp_path: Path,
) -> None:
    server, pid_file, _ = _server(tmp_path, lifecycle="eager")
    tau_home = tmp_path / ".tau"
    tau_home.mkdir()
    (tau_home / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    server.name: {
                        "command": server.command,
                        "args": list(server.args),
                        "lifecycle": "eager",
                        "idleTimeout": 0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    paths = TauPaths(home=tau_home, agents_home=tmp_path / ".agents")
    runtime = ExtensionRuntime(built_in_extensions=(), paths=paths)
    runtime.load(
        TauResourcePaths(root=tau_home, agents_root=paths.agents_home, paths=paths),
        extra_paths=(ROOT,),
        include_resource_dirs=False,
    )
    tool = runtime.extension_tools[0]

    async def scenario() -> None:
        await runtime.emit_session_start("startup")
        await _wait_until(lambda: len(_pids(pid_file)) == 1)
        result = await tool.execute(
            "call", {"tool": "fixture__echo", "args": {"value": "eager"}}
        )
        assert result.text == "eager"
        await runtime.emit_session_shutdown("reload")
        with pytest.raises(ExtensionError, match="stale"):
            await tool.execute("stale", {"search": "echo"})
        await runtime.aclose()

    asyncio.run(scenario())
    assert not runtime.diagnostics
    _assert_reaped(pid_file)


def test_idle_expiry_resets_and_zero_disables_idle_close(tmp_path: Path) -> None:
    expiring, expiring_pids, _ = _server(tmp_path, idle_timeout=0.003, name="expiring")
    disabled, disabled_pids, _ = _server(tmp_path, idle_timeout=0, name="disabled")
    expiring_registry = _registry(tmp_path / "expiring", expiring)
    disabled_registry = _registry(tmp_path / "disabled", disabled)

    async def scenario() -> None:
        try:
            await expiring_registry.connect("expiring")
            assert len(_tasks_named("mcp-idle-expiring")) == 1
            await asyncio.sleep(0.10)
            await expiring_registry.ping("expiring")
            assert len(_tasks_named("mcp-idle-expiring")) == 1
            await asyncio.sleep(0.10)
            assert expiring_registry.statuses()[0].state == "ready"
            await _wait_until(lambda: expiring_registry.statuses()[0].state == "cold")

            await disabled_registry.connect("disabled")
            await asyncio.sleep(0.25)
            assert disabled_registry.statuses()[0].state == "ready"
            assert not _tasks_named("mcp-idle-disabled")
        finally:
            await expiring_registry.shutdown()
            await disabled_registry.shutdown()

    asyncio.run(scenario())
    _assert_reaped(expiring_pids, disabled_pids)


def test_handshake_timeout_reaps_child_and_preserves_cache(tmp_path: Path) -> None:
    server, pid_file, _ = _server(tmp_path, mode="slow-handshake")
    registry = _registry(tmp_path, server, handshake_timeout=0.5)
    registry.cache.write(
        server,
        (ToolMetadata("old_tool", "old metadata", {"type": "object"}),),
    )

    async def scenario() -> None:
        try:
            with pytest.raises(McpError, match="initialize.*timed out"):
                await registry.connect("fixture")
            cached = registry.cache.read(server)
            assert cached is not None
            assert [tool.name for tool in cached.tools] == ["old_tool"]
            assert registry.statuses()[0].state == "cold"
        finally:
            await registry.shutdown()

    asyncio.run(scenario())
    _assert_reaped(pid_file)


def test_call_timeout_sends_cancel_and_server_stays_connected(tmp_path: Path) -> None:
    server, pid_file, event_file = _server(
        tmp_path, mode="slow-call", request_timeout_ms=50
    )
    registry = _registry(tmp_path, server)
    proxy = create_proxy_tool(registry)

    async def scenario() -> None:
        try:
            result = await proxy.execute(
                "slow", {"tool": "fixture__echo", "args": {"value": "slow"}}
            )
            assert "timed out" in result.text
            await _wait_until(
                lambda: (
                    event_file.exists()
                    and "cancelled:" in event_file.read_text(encoding="utf-8")
                )
            )
            await registry.ping("fixture")
            assert registry.statuses()[0].state == "ready"
            assert len(_pids(pid_file)) == 1
        finally:
            await registry.shutdown()

    asyncio.run(scenario())
    _assert_reaped(pid_file)


def test_crash_respawns_and_malformed_frame_returns_to_cold(tmp_path: Path) -> None:
    crashing, crash_pids, _ = _server(
        tmp_path / "crash", crash_once=tmp_path / "crash" / "once"
    )
    malformed, malformed_pids, _ = _server(
        tmp_path / "malformed", mode="malformed-call"
    )
    crash_registry = _registry(tmp_path / "crash", crashing)
    malformed_registry = _registry(tmp_path / "malformed", malformed)

    async def scenario() -> None:
        try:
            crash_proxy = create_proxy_tool(crash_registry)
            failed = await crash_proxy.execute(
                "crash", {"tool": "fixture__echo", "args": {"value": "first"}}
            )
            assert "retry" in failed.text.lower()
            recovered = await crash_proxy.execute(
                "retry", {"tool": "fixture__echo", "args": {"value": "second"}}
            )
            assert recovered.text == "second"
            assert len(_pids(crash_pids)) == 2

            malformed_proxy = create_proxy_tool(malformed_registry)
            malformed_result = await malformed_proxy.execute(
                "malformed", {"tool": "fixture__echo", "args": {"value": "x"}}
            )
            assert "non-object JSON-RPC frame" in malformed_result.text
            assert malformed_registry.statuses()[0].state == "cold"
        finally:
            await crash_registry.shutdown()
            await malformed_registry.shutdown()

    asyncio.run(scenario())
    _assert_reaped(crash_pids, malformed_pids)


def test_stderr_is_bounded_and_never_leaks_into_success(tmp_path: Path) -> None:
    crashing, crash_pids, _ = _server(tmp_path / "crash", mode="stderr-crash")
    successful, success_pids, _ = _server(tmp_path / "success", mode="stderr-success")
    crash_registry = _registry(tmp_path / "crash", crashing)
    success_registry = _registry(tmp_path / "success", successful)

    async def scenario() -> None:
        try:
            crash_result = await create_proxy_tool(crash_registry).execute(
                "crash", {"tool": "fixture__echo", "args": {"value": "x"}}
            )
            assert "STDERR_TAIL" in crash_result.text
            assert "STDERR_BEGIN" not in crash_result.text
            assert len(crash_result.text.encode()) < 17 * 1024

            success_result = await create_proxy_tool(success_registry).execute(
                "success", {"tool": "fixture__echo", "args": {"value": "safe"}}
            )
            assert success_result.text == "safe"
            assert "STDERR" not in success_result.text
        finally:
            await crash_registry.shutdown()
            await success_registry.shutdown()

    asyncio.run(scenario())
    _assert_reaped(crash_pids, success_pids)


def test_large_text_result_is_bounded_with_original_byte_count(tmp_path: Path) -> None:
    server, pid_file, _ = _server(
        tmp_path, mode="large-result", request_timeout_ms=10_000
    )
    registry = _registry(tmp_path, server)
    proxy = create_proxy_tool(registry)

    async def scenario() -> None:
        try:
            result = await proxy.execute(
                "large", {"tool": "fixture__echo", "args": {"value": "ignored"}}
            )
            assert len(result.text.encode()) <= 256 * 1024
            assert "truncated" in result.text.lower()
            assert "307200 bytes" in result.text
        finally:
            await registry.shutdown()

    asyncio.run(scenario())
    _assert_reaped(pid_file)


def test_shutdown_cancels_pending_call_and_all_stale_use_raises(tmp_path: Path) -> None:
    server, pid_file, event_file = _server(
        tmp_path, mode="slow-call", request_timeout_ms=10_000
    )
    registry = _registry(tmp_path, server)
    proxy = create_proxy_tool(registry)

    async def scenario() -> None:
        call = asyncio.create_task(
            proxy.execute(
                "pending", {"tool": "fixture__echo", "args": {"value": "pending"}}
            )
        )
        await _wait_until(
            lambda: (
                event_file.exists()
                and "call:" in event_file.read_text(encoding="utf-8")
            )
        )
        await registry.shutdown()
        result = await asyncio.wait_for(call, timeout=1)
        assert "connection closed" in result.text
        with pytest.raises(ExtensionError, match="stale"):
            await registry.connect("fixture")
        with pytest.raises(ExtensionError, match="stale"):
            registry.statuses()

    asyncio.run(scenario())
    _assert_reaped(pid_file)
