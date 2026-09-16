# ADR 0001: Proxy-first architecture on a stdlib stdio client

Status: Proposed (design approved in direction on 2026-09-15; spec review pending)

## Context

Tau needs generic MCP support (the ariadne project server, grep.app, codegraph,
and other stdio servers; all servers currently in scope are stdio). Two
obvious designs exist: register every MCP tool as a native Tau tool (10k+
system-prompt tokens for a single busy server), or one generic dispatcher.
A third, proven design is pi-mcp-adapter: a single ~200-token `mcp` proxy tool
with on-demand search, a metadata cache for offline discovery, lazy server
spawn, and opt-in per-server direct tool registration.

The official `mcp` Python SDK is protocol-complete but drags dependencies that
Tau's installer never manages — `uv tool upgrade tau-ai` would silently break
the extension.

## Decision

Port pi-mcp-adapter's architecture, not its feature surface: one `mcp` proxy
tool backed by a hand-rolled, stdlib-only stdio JSON-RPC client. Servers are
lazy by default; tool metadata is disk-cached so search works without spawning
anything. Direct tool registration is per-server opt-in (`directTools`), never
the default. Streamable HTTP transport, OAuth, approvals, elicitation,
sampling, and the interactive panel are explicitly out of v1 scope.

## Consequences

- Context cost stays ~constant regardless of how many servers are configured.
- The MCP stdio client protocol subset (initialize, tools/list, tools/call,
  ping, notifications) is small and testable with a fixture server script;
  protocol edge cases the SDK would absorb are ours to handle, and the scope
  fence (stdio only) keeps that bounded.
- No dependency-management fragility: the extension runs on any Tau install
  with Python ≥3.11 stdlib.
- Servers needing HTTP transport or OAuth stay unreachable until a later
  phase; the config schema reserves those fields so configs are forward-
  compatible.
