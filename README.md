# tau-mcp

A [Tau](https://github.com/huggingface/tau) extension for stdio MCP servers.
It keeps Tau's prompt small by exposing one `mcp` proxy tool, caching tool
metadata for offline search, and starting servers only when they are needed.
Native per-server tools are available as an explicit opt-in.

- Design: [`docs/specs/0001-mcp-client-extension.md`](docs/specs/0001-mcp-client-extension.md)
- Decision record: [`docs/adr/0001-proxy-first-stdlib-client.md`](docs/adr/0001-proxy-first-stdlib-client.md)
- Runtime: Tau 0.4.4, Python 3.12 or newer, and the Python standard library

## Install

Install the repository through Tau's extension installer:

```console
tau install <source>
```

`<source>` may be a local checkout or a supported Git source. For example:

```console
tau install .
tau install git:github.com/legout/tau-mcp
```

The repository declares `src/tau_mcp/extension.py` in `[tool.tau]`, so Tau
loads it from the normal user extension directory on the next run.

## Configure servers

The only v1 config source is the global file `~/.tau/mcp.json`. The extension
does not create this file and does not read project or ancestor MCP config.
The top-level shape is Claude-compatible:

```json
{
  "mcpServers": {
    "ariadne": {
      "command": "/home/me/coding/ariadne/.venv/bin/python",
      "args": ["-m", "ariadne.serve", "--db", "data/demo.db"],
      "env": {"ARIADNE_LOG": "warning"},
      "cwd": "/home/me/coding/ariadne",
      "lifecycle": "lazy",
      "idleTimeout": 10,
      "requestTimeoutMs": 120000,
      "directTools": ["run_query"],
      "includeTools": ["run_*"],
      "excludeTools": ["*_unsafe"],
      "disabled": false
    }
  },
  "settings": {
    "idleTimeout": 10,
    "requestTimeoutMs": 30000
  }
}
```

Per-server fields are:

- `command` (required), `args`, `env`, and `cwd`;
- `lifecycle`: `"lazy"` (default) or `"eager"`;
- `idleTimeout` in minutes (`0` disables idle disconnect) and
  `requestTimeoutMs`;
- `directTools`: `true`, a list of original MCP tool names, or absent for the
  default proxy-only mode;
- `includeTools` and `excludeTools`: glob filters for direct tools, applied in
  that order;
- `disabled`: skip the server when `true`.

`idleTimeout` and `requestTimeoutMs` under `settings` provide global defaults.
`${VAR}` and `~` expand in `command`, `args`, `env` values, and `cwd`. Invalid
server entries are skipped with a diagnostic rather than blocking the session.
HTTP-oriented fields such as `url`, `headers`, and `auth` are diagnosed as
unsupported in v1.

### Migrating ariadne from `.mcp.json`

Copy the existing `ariadne` entry from the project's `.mcp.json` into the
`mcpServers` object in `~/.tau/mcp.json`. Keep its `command`, `args`, and `cwd`;
set `"lifecycle": "lazy"` and, for long database queries,
`"requestTimeoutMs": 120000`. Merge with any existing global servers instead
of replacing them. The project `.mcp.json` can remain for other hosts—tau-mcp
does not read or modify it.

## Use the proxy

The `mcp` tool accepts one operation at a time. Representative payloads are:

```json
{"search": "query"}
{"connect": "ariadne"}
{"tool": "ariadne__run_query", "args": {"sql": "select 1"}}
{"tool": "ariadne__run_query", "args": "{\"sql\":\"select 1\"}"}
{"disconnect": "ariadne"}
```

`search` reads cached metadata and does not start a server. A new server has no
cache until `connect` or the first tool call successfully lists its tools.
Tool calls use `<sanitized-server>__<original-tool-name>`, start lazy servers
as needed, and return text content. `connect` prewarms a server and refreshes
its metadata; `disconnect` closes it.

The `/mcp` command is status-only. It reports each server's state, cached tool
count, and cache age. Connect and disconnect through the `mcp` tool, not the
command.

## Opt in to direct tools

By default only the proxy is registered. Set `directTools` to `true` for every
selected server tool or to an original-name list such as `["run_query"]`.
Selected tools appear as native Tau tools such as `ariadne__run_query`, retain
the MCP server's exact JSON input schema, and still execute through the shared
MCP connection registry. The `mcp` proxy always remains available.

`includeTools` narrows the opt-in set with standard glob patterns, then
`excludeTools` removes matches. Cached definitions are registered at startup;
newly discovered definitions become available after the first successful
connect.

## Cache and diagnostics

Metadata is stored at:

```text
~/.tau/mcp-cache/<sanitized-server>.json
```

The cache contains a digest of `command`, `args`, `env`, and `cwd`; changing
those fields invalidates it. It contains schemas and descriptions, not live
MCP results.

Server namespaces are lowercased and characters outside `[a-z0-9_]` become
`_`. If two configured names sanitize to the same namespace, or a sanitized
namespace contains the reserved `__` delimiter, Tau emits a diagnostic and
does not register direct tools for that namespace. Ambiguous qualified proxy
calls are rejected instead of being routed to another server. Rename the
server key to a unique namespace without `__` to make it addressable.

## v1 scope

Version 1 intentionally supports newline-delimited stdio MCP only. It does not
support HTTP/SSE transport, OAuth, approvals or consent UI, elicitation,
sampling, resources-as-tools, prompts, MCP scripting, an interactive panel,
live stderr streaming, search-activated direct tools, automatic import of host
configs, or project/ancestor config discovery. `/mcp` remains a text status
command. The proxy retains bounded stderr, frame, timeout, and output handling
for the supported stdio path.

## Develop and verify

```console
uv sync
uv run ruff format --check .
uv run ruff check .
uv run ty check src
uv run pytest
```

Tests install into temporary Tau directories and use a local fake MCP server;
they never read or write `~/.tau/mcp.json`. A real ariadne query is deliberately
a separate, explicitly authorized manual check.
