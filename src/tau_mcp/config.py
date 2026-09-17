"""Load and validate Tau's global MCP configuration."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import cast

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_UNSUPPORTED_FIELDS = {"url", "headers", "auth", "socket"}


class _ObjectPairs(list[tuple[str, object]]):
    pass


@dataclass(frozen=True, slots=True)
class ConfigDiagnostic:
    message: str
    server: str | None = None


@dataclass(frozen=True, slots=True)
class ServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = MappingProxyType({})
    cwd: str | None = None
    lifecycle: str = "lazy"
    idle_timeout: float = 10
    request_timeout_ms: int = 30_000
    direct_tools: bool | tuple[str, ...] | None = None
    include_tools: tuple[str, ...] = ()
    exclude_tools: tuple[str, ...] = ()
    disabled: bool = False


@dataclass(frozen=True, slots=True)
class McpConfig:
    servers: Mapping[str, ServerConfig]
    diagnostics: tuple[ConfigDiagnostic, ...] = ()
    idle_timeout: float = 10
    request_timeout_ms: int = 30_000


def load_config(
    path: Path,
    *,
    environment: Mapping[str, str],
) -> McpConfig:
    """Load one config path without creating or mutating any files."""
    if not path.is_file():
        return McpConfig(servers=MappingProxyType({}))

    diagnostics: list[ConfigDiagnostic] = []
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_ObjectPairs
        )
    except (OSError, json.JSONDecodeError) as exc:
        return McpConfig(
            servers=MappingProxyType({}),
            diagnostics=(ConfigDiagnostic(f"could not read config: {exc}"),),
        )
    if not isinstance(raw, _ObjectPairs):
        return McpConfig(
            servers=MappingProxyType({}),
            diagnostics=(ConfigDiagnostic("config root must be an object"),),
        )

    root = _last_wins(raw)
    idle_timeout, request_timeout_ms = _parse_settings(
        root.get("settings"), diagnostics
    )
    server_pairs = root.get("mcpServers", _ObjectPairs())
    if not isinstance(server_pairs, _ObjectPairs):
        diagnostics.append(ConfigDiagnostic("`mcpServers` must be an object"))
        return McpConfig(
            servers=MappingProxyType({}),
            diagnostics=tuple(diagnostics),
            idle_timeout=idle_timeout,
            request_timeout_ms=request_timeout_ms,
        )

    raw_servers: dict[str, object] = {}
    for name, entry in server_pairs:
        if name in raw_servers:
            diagnostics.append(
                ConfigDiagnostic(
                    f"duplicate server name `{name}`; last entry wins",
                    server=name,
                )
            )
            del raw_servers[name]
        raw_servers[name] = entry

    servers: dict[str, ServerConfig] = {}
    for name, entry in raw_servers.items():
        server = _parse_server(
            name,
            entry,
            environment,
            idle_timeout,
            request_timeout_ms,
            diagnostics,
        )
        if server is not None:
            servers[name] = server
    return McpConfig(
        servers=MappingProxyType(servers),
        diagnostics=tuple(diagnostics),
        idle_timeout=idle_timeout,
        request_timeout_ms=request_timeout_ms,
    )


def _parse_settings(
    raw: object,
    diagnostics: list[ConfigDiagnostic],
) -> tuple[float, int]:
    idle_timeout: float = 10
    request_timeout_ms = 30_000
    if raw is None:
        return idle_timeout, request_timeout_ms
    if not isinstance(raw, _ObjectPairs):
        diagnostics.append(ConfigDiagnostic("`settings` must be an object"))
        return idle_timeout, request_timeout_ms
    settings = _last_wins(raw)
    candidate_idle = settings.get("idleTimeout", idle_timeout)
    if _is_nonnegative_number(candidate_idle):
        idle_timeout = float(cast(int | float, candidate_idle))
    else:
        diagnostics.append(
            ConfigDiagnostic("settings.idleTimeout must be a non-negative number")
        )
    candidate_request = settings.get("requestTimeoutMs", request_timeout_ms)
    if _is_positive_int(candidate_request):
        request_timeout_ms = cast(int, candidate_request)
    else:
        diagnostics.append(
            ConfigDiagnostic("settings.requestTimeoutMs must be a positive integer")
        )
    return idle_timeout, request_timeout_ms


def _parse_server(
    name: str,
    raw: object,
    environment: Mapping[str, str],
    default_idle_timeout: float,
    default_request_timeout_ms: int,
    diagnostics: list[ConfigDiagnostic],
) -> ServerConfig | None:
    if not isinstance(raw, _ObjectPairs):
        diagnostics.append(
            ConfigDiagnostic(f"server `{name}` must be an object", server=name)
        )
        return None
    entry = _last_wins(raw)
    if any(
        key in _UNSUPPORTED_FIELDS or key.startswith("bearerToken") for key in entry
    ):
        diagnostics.append(
            ConfigDiagnostic(
                f"server `{name}`: HTTP transport not supported yet",
                server=name,
            )
        )
        return None

    command = entry.get("command")
    if not isinstance(command, str) or not command:
        diagnostics.append(
            ConfigDiagnostic(
                f"server `{name}` requires a non-empty command", server=name
            )
        )
        return None
    args = _string_list(entry.get("args", _ObjectPairs()), "args", name, diagnostics)
    if args is None:
        return None
    env = _string_mapping(entry.get("env", _ObjectPairs()), name, diagnostics)
    if env is None:
        return None
    cwd = entry.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        diagnostics.append(
            ConfigDiagnostic(f"server `{name}` cwd must be a string", server=name)
        )
        return None
    lifecycle = entry.get("lifecycle", "lazy")
    if not isinstance(lifecycle, str) or lifecycle not in ("lazy", "eager"):
        diagnostics.append(
            ConfigDiagnostic(
                f"server `{name}` lifecycle must be `lazy` or `eager`", server=name
            )
        )
        return None
    idle_timeout = entry.get("idleTimeout", default_idle_timeout)
    if not _is_nonnegative_number(idle_timeout):
        diagnostics.append(
            ConfigDiagnostic(
                f"server `{name}` idleTimeout must be a non-negative number",
                server=name,
            )
        )
        return None
    request_timeout_ms = entry.get("requestTimeoutMs", default_request_timeout_ms)
    if not _is_positive_int(request_timeout_ms):
        diagnostics.append(
            ConfigDiagnostic(
                f"server `{name}` requestTimeoutMs must be a positive integer",
                server=name,
            )
        )
        return None
    direct_tools = entry.get("directTools")
    if direct_tools is not None and not isinstance(direct_tools, bool):
        direct_tools = _string_list(direct_tools, "directTools", name, diagnostics)
        if direct_tools is None:
            return None
    include_tools = _string_list(
        entry.get("includeTools", _ObjectPairs()), "includeTools", name, diagnostics
    )
    exclude_tools = _string_list(
        entry.get("excludeTools", _ObjectPairs()), "excludeTools", name, diagnostics
    )
    if include_tools is None or exclude_tools is None:
        return None
    disabled = entry.get("disabled", False)
    if not isinstance(disabled, bool):
        diagnostics.append(
            ConfigDiagnostic(f"server `{name}` disabled must be a boolean", server=name)
        )
        return None

    return ServerConfig(
        name=name,
        command=_expand(command, environment),
        args=tuple(_expand(value, environment) for value in args),
        env=MappingProxyType(
            {key: _expand(value, environment) for key, value in env.items()}
        ),
        cwd=_expand(cwd, environment) if cwd is not None else None,
        lifecycle=lifecycle,
        idle_timeout=float(cast(int | float, idle_timeout)),
        request_timeout_ms=cast(int, request_timeout_ms),
        direct_tools=direct_tools,
        include_tools=include_tools,
        exclude_tools=exclude_tools,
        disabled=disabled,
    )


def _last_wins(pairs: _ObjectPairs) -> dict[str, object]:
    return dict(pairs)


def _string_list(
    raw: object,
    field: str,
    server: str,
    diagnostics: list[ConfigDiagnostic],
) -> tuple[str, ...] | None:
    if isinstance(raw, _ObjectPairs) and not raw:
        return ()
    if (
        not isinstance(raw, list)
        or isinstance(raw, _ObjectPairs)
        or not all(isinstance(value, str) for value in raw)
    ):
        diagnostics.append(
            ConfigDiagnostic(
                f"server `{server}` {field} must be a string array", server=server
            )
        )
        return None
    return tuple(raw)


def _string_mapping(
    raw: object,
    server: str,
    diagnostics: list[ConfigDiagnostic],
) -> dict[str, str] | None:
    if not isinstance(raw, _ObjectPairs) or not all(
        isinstance(value, str) for _, value in raw
    ):
        diagnostics.append(
            ConfigDiagnostic(
                f"server `{server}` env must map strings to strings", server=server
            )
        )
        return None
    return {key: value for key, value in raw if isinstance(value, str)}


def _expand(value: str, environment: Mapping[str, str]) -> str:
    expanded = _VARIABLE.sub(
        lambda match: environment.get(match.group(1), match.group(0)), value
    )
    home = environment.get("HOME")
    if home is not None and (expanded == "~" or expanded.startswith("~/")):
        return home + expanded[1:]
    return expanded


def _is_nonnegative_number(value: object) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    )


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
