# Spec 0001: MCP client extension for Tau (proxy-first)

Status: **Proposed** — approaches and scope approved by the owner on
2026-09-15 (pi-mcp-adapter architecture; Tau-native global config at
`~/.tau/mcp.json`; stdlib-only, stdio v1; repository root). Pending the owner's
review of this revised specification before implementation.

## Goal

Use MCP servers from Tau through a single `mcp` proxy tool with on-demand
discovery, so tool definitions never flood the system prompt. The ariadne
project MCP server and the user's other stdio servers become callable when
configured in the global Tau MCP config.

## Non-goals (v1)

- Streamable HTTP / SSE transports (config fields `url`, `headers`, `auth`
  are parsed and reported as unsupported, so configs are forward-compatible).
- OAuth, approvals/consent UI, elicitation, sampling, resources-as-tools,
  prompts, MCP scripting, interactive panel widgets.
- Search-activated direct tools (`directTools: "search"`), host-config
  auto-discovery/import, project or ancestor config roots. Tau 0.4.4 exposes
  no post-decision project-trust state to extensions, so v1 does not read
  `<cwd>/.tau/mcp.json`.
- A package manager; this extension installs like any Tau extension.

## Architecture

```
src/tau_mcp/
  extension.py      setup(tau): merge configs, start lifecycle, register tool + /mcp
  config.py         load + merge + validate config sources; ${VAR} expansion
  cache.py          metadata cache at ~/.tau/mcp-cache/<server>.json
  client.py         stdio JSON-RPC 2.0 MCP client (spawn, handshake, call, close)
  registry.py       per-server state machine: cold -> connecting -> ready -> stopped
  proxy.py          the `mcp` AgentTool: search / tool / connect / disconnect
  direct.py         directTools registration (opt-in)
tests/
  fake_mcp_server.py  minimal stdio MCP server fixture (initialize/list/call)
  test_*.py
```

Stdlib-only (`subprocess`, `asyncio`, `json`, `urllib.parse`). Packaging:
`[tool.tau] extensions = ["src/tau_mcp/extension.py"]`.

## Interfaces

### Config source

`~/.tau/mcp.json` is the only v1 config source. Project config is deferred
until Tau exposes a public post-decision project-trust state to extensions;
the existing `project_trust` hook is a pre-decision override hook and cannot
safely authorize reading command-bearing project config.

Shape (Claude-compatible `mcpServers`):

```json
{
  "mcpServers": {
    "ariadne": {
      "command": "/…/.venv/bin/python",
      "args": ["-m", "ariadne.serve", "--db", "data/demo.db"],
      "cwd": "/home/volker/coding/ariadne",
      "lifecycle": "lazy",
      "requestTimeoutMs": 120000
    }
  },
  "settings": { "idleTimeout": 10, "requestTimeoutMs": 30000 }
}
```

Per-server fields (v1): `command`, `args`, `env`, `cwd`, `lifecycle`
(`lazy` default | `eager`), `idleTimeout` (minutes), `requestTimeoutMs`,
`directTools` (`true` | `string[]` | absent=proxy-only), `includeTools` /
`excludeTools` (globs, applied to direct tools), `disabled` (bool).
Unsupported-but-parsed fields: `url`, `headers`, `auth`, `bearerToken*`,
`socket`. `${VAR}` and `~` expand in `command`, `args`, `env` values, `cwd`.
Duplicate server names within the file use the last entry with a diagnostic.
Invalid server entries are skipped with a diagnostic and never block the
session. **No file is auto-created.**

### The `mcp` proxy tool

One registered tool, ~200 tokens of prompt:

```json
{ "search": "screenshot" }
{ "tool": "ariadne__run_query", "args": { "sql": "…" } }
{ "connect": "ariadne" }        // prewarm + refresh cache
{ "disconnect": "ariadne" }
```

- **search** ranks cached tool metadata (name + description keywords) across
  all servers and prints name, one-line description, parameters — exactly the
  pi-mcp-adapter output shape. Works fully offline from cache.
- **tool** resolves `<server>__<name>` (unknown name → best-match list),
  ensures the server is connected (lazy spawn), calls `tools/call`, returns
  the text content. `args` may be an object or a JSON string (one recovery
  layer).
- Tool names are `<server>__<original>` (server name sanitized to
  `[a-z0-9_]`); collision with a builtin name is impossible (builtins are
  `read`/`write`/`edit`/`bash`, no `__`).

### Metadata cache

`~/.tau/mcp-cache/<sanitized-server>.json`: `{ configDigest, tools: [...] }`.
`configDigest` hashes the merged server entry (command/args/env/cwd). Populate
on first successful `tools/list` (via `connect` or first call); invalidate
when the digest changes. Search prefers fresh cache; a cold cache makes
`search` report `no metadata yet — call connect("<server>")`.

### Lifecycle

- `lazy` (default): spawn on first tool call for that server; idle disconnect
  after `idleTimeout` (default 10 min, 0 disables).
- `eager`: spawn at `session_start`, no auto-reconnect.
- All children are killed on `session_shutdown` (including reload) — server
  handles are generation-scoped; a stale handle raises `ExtensionError` on
  use, mirroring Tau's generation contract.

### `/mcp` command (text)

`/mcp` prints per-server: name, state, cached tool count, and cache age. `/mcp connect <server>` and `/mcp disconnect <server>` wrap
the proxy operations. Interactive panel UI is a later phase.

## Data flow

`mcp {tool}` → registry lookup → (cold?) spawn subprocess → newline-delimited
JSON-RPC `initialize` → `notifications/initialized` → `tools/call` →
content array joined to text → idle timer reset. Handshake timeout 10s; call
timeout from `requestTimeoutMs`. Server stderr is captured to a ring buffer
and shown only in error text; live stderr streaming is out of v1 scope.

## Failures

| Failure | Behavior |
| --- | --- |
| Spawn fails / binary missing | Error names the server and the command; server stays cold. |
| Handshake timeout | Process killed; actionable error; cache untouched. |
| Call timeout | `notifications/cancelled` sent, error returned; server stays connected. |
| Server crashes mid-call / EOF | Error suggests retry (next call respawns). |
| HTTP-server fields configured | Config error at load: `HTTP transport not supported yet`. |
| Oversized result (>256 KB) | Truncated with byte count and note (output guard, fixed v1 bounds). |

## Migration

ariadne's existing `.mcp.json` entry is copied into `~/.tau/mcp.json`
(`command`, `args`, `cwd`, `lifecycle: "lazy"`, `requestTimeoutMs: 120000`
already match the schema). Nothing reads `.mcp.json` directly; the file stays
for other hosts.

agentmemory coexistence (A2): configuring agentmemory's MCP server here
exposes `mcp__<name>__memory_*` proxy targets alongside tau-agentmemory's
native `memory_*` tools; no name conflicts.

## Testing

Hermetic pytest: `fake_mcp_server.py` (a Python stdio script speaking
initialize/tools-list/tools/call, with fault injection: slow handshake, crash
on call, huge result). Unit tests: config merge + precedence + expansion;
digest invalidation; name sanitization; idle timer; cancellation path;
generation teardown. Extension loading through the real `ExtensionRuntime.load`
with `include_resource_dirs=False`.

## Acceptance examples

1. With `~/.tau/mcp.json` configuring the ariadne server,
   `mcp {search: "query"}` lists its tools offline after one connect, and
   `mcp {tool: "ariadne__…", args: …}` returns live results from `data/demo.db`.
2. A cold session with populated cache answers `search` in <50 ms without
   spawning any process.
3. `session_shutdown` leaves no orphaned MCP child processes
   (`pgrep -f fake_mcp_server` empty after the session ends).
4. `directTools: ["run_query"]` on ariadne registers `ariadne__run_query` as
   a native tool with its real JSON schema; the proxy tool remains.

## Decision log

- 2026-09-15, owner: adopt pi-mcp-adapter architecture (proxy + cache + lazy,
  directTools opt-in); stdlib-only, no MCP SDK; stdio-only v1; repository root.
- 2026-09-16, owner: v1 uses only `~/.tau/mcp.json`; trusted project config is
  deferred until Tau exposes post-decision trust state. Keep stderr in the
  bounded error buffer and defer live debug streaming.
