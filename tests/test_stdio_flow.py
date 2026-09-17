from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from tau_mcp.cache import CacheStore
from tau_mcp.config import McpConfig, ServerConfig
from tau_mcp.proxy import create_proxy_tool
from tau_mcp.registry import ServerRegistry

FAKE_SERVER = Path(__file__).with_name("fake_mcp_server.py")


def _registry(
    tmp_path: Path,
    *,
    mode: str = "normal",
    crash_once: Path | None = None,
    command: str = sys.executable,
) -> tuple[ServerRegistry, Path]:
    pid_file = tmp_path / "pids.txt"
    args = [str(FAKE_SERVER), "--pid-file", str(pid_file), "--mode", mode]
    if crash_once is not None:
        args.extend(("--crash-once", str(crash_once)))
    config = McpConfig(
        servers={
            "fixture": ServerConfig(
                name="fixture",
                command=command,
                args=tuple(args),
                request_timeout_ms=2_000,
            )
        }
    )
    return ServerRegistry(config, CacheStore(tmp_path / "cache")), pid_file


def _pids(path: Path) -> list[int]:
    if not path.exists():
        return []
    return [int(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_reaped(path: Path) -> None:
    pids = _pids(path)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and any(
        Path(f"/proc/{pid}").exists() for pid in pids
    ):
        time.sleep(0.01)
    assert pids
    assert all(not Path(f"/proc/{pid}").exists() for pid in pids)


def test_explicit_connect_refreshes_cache_and_cold_registry_searches_it(
    tmp_path: Path,
) -> None:
    registry, pid_file = _registry(tmp_path)

    async def scenario() -> None:
        proxy = create_proxy_tool(registry)
        try:
            connected = await proxy.execute("connect", {"connect": "fixture"})
            assert "connected" in connected.text
            assert registry.statuses()[0].state == "ready"
            assert registry.statuses()[0].cached_tool_count == 2
            await registry.ping("fixture")
            assert registry.last_activity("fixture") is not None
            await registry.disconnect("fixture")
            await registry.disconnect("fixture")
            assert registry.statuses()[0].state == "cold"
        finally:
            await registry.close()

        cold_registry, _ = _registry(tmp_path)
        try:
            cold_proxy = create_proxy_tool(cold_registry)
            result = await cold_proxy.execute("search", {"search": "echo"})
            assert "fixture__echo" in result.text
            assert len(_pids(pid_file)) == 1
        finally:
            await cold_registry.close()

    asyncio.run(scenario())
    _assert_reaped(pid_file)


def test_lazy_tool_call_accepts_object_and_one_json_string_layer_and_suggests(
    tmp_path: Path,
) -> None:
    registry, pid_file = _registry(tmp_path)

    async def scenario() -> None:
        proxy = create_proxy_tool(registry)
        try:
            first = await proxy.execute(
                "first", {"tool": "fixture__echo", "args": {"value": "object"}}
            )
            assert first.text == "object"
            second = await proxy.execute(
                "second", {"tool": "fixture__echo", "args": '{"value":"json"}'}
            )
            assert second.text == "json"
            nested = await proxy.execute(
                "nested",
                {"tool": "fixture__echo", "args": '"{\\"value\\":\\"nested\\"}"'},
            )
            assert "JSON object" in nested.text
            unknown = await proxy.execute(
                "unknown", {"tool": "fixture__ech", "args": {}}
            )
            assert "fixture__echo" in unknown.text
            assert registry.last_activity("fixture") is not None
            assert len(_pids(pid_file)) == 1
        finally:
            await registry.close()

    asyncio.run(scenario())
    _assert_reaped(pid_file)


def test_two_concurrent_first_calls_share_one_child(tmp_path: Path) -> None:
    registry, pid_file = _registry(tmp_path)

    async def scenario() -> None:
        proxy = create_proxy_tool(registry)
        try:
            first, second = await asyncio.gather(
                proxy.execute(
                    "first", {"tool": "fixture__echo", "args": {"value": "one"}}
                ),
                proxy.execute(
                    "second", {"tool": "fixture__echo", "args": {"value": "two"}}
                ),
            )
            assert {first.text, second.text} == {"one", "two"}
            assert len(_pids(pid_file)) == 1
        finally:
            await registry.close()

    asyncio.run(scenario())
    _assert_reaped(pid_file)


@pytest.mark.parametrize("mode", ["bad-version", "malformed", "mismatch"])
def test_handshake_failures_are_server_named_and_reaped(
    tmp_path: Path, mode: str
) -> None:
    registry, pid_file = _registry(tmp_path, mode=mode)

    async def scenario() -> None:
        proxy = create_proxy_tool(registry)
        try:
            result = await proxy.execute("connect", {"connect": "fixture"})
            assert "fixture" in result.text
            assert registry.statuses()[0].state == "cold"
        finally:
            await registry.close()

    asyncio.run(scenario())
    _assert_reaped(pid_file)


def test_missing_binary_is_server_named_and_stays_cold(tmp_path: Path) -> None:
    registry, pid_file = _registry(tmp_path, command=str(tmp_path / "missing-binary"))

    async def scenario() -> None:
        proxy = create_proxy_tool(registry)
        try:
            result = await proxy.execute("connect", {"connect": "fixture"})
            assert "fixture" in result.text
            assert "missing-binary" in result.text
            assert registry.statuses()[0].state == "cold"
        finally:
            await registry.close()

    asyncio.run(scenario())
    assert not pid_file.exists()


def test_eof_returns_to_cold_and_next_call_respawns(tmp_path: Path) -> None:
    registry, pid_file = _registry(tmp_path, crash_once=tmp_path / "crashed")

    async def scenario() -> None:
        proxy = create_proxy_tool(registry)
        try:
            failed = await proxy.execute(
                "first", {"tool": "fixture__echo", "args": {"value": "first"}}
            )
            assert "fixture" in failed.text
            assert "retry" in failed.text.lower()
            assert registry.statuses()[0].state == "cold"
            recovered = await proxy.execute(
                "second", {"tool": "fixture__echo", "args": {"value": "second"}}
            )
            assert recovered.text == "second"
            assert len(_pids(pid_file)) == 2
        finally:
            await registry.close()

    asyncio.run(scenario())
    _assert_reaped(pid_file)
