# Plan 0001: MCP client extension for Tau

Status: **Approved and ticketized** — owner approved the execution map on
2026-09-16. GitHub issues #1-#5 own the canonical task bodies.

## Goal and authority

Implement the proxy-first, stdlib-only MCP extension defined by
[`docs/specs/0001-mcp-client-extension.md`](../specs/0001-mcp-client-extension.md),
revision approved by the owner on 2026-09-16. Architectural rationale is owned
by accepted [ADR 0001](../adr/0001-proxy-first-stdlib-client.md).

Capture checkpoint:

- **Vocabulary:** no new glossary terms are required. MCP and Tau terms retain
  their upstream meanings; extension-specific config names are defined by the
  approved specification.
- **Decisions:** ADR 0001 records the durable proxy-first and stdlib-only choice.
- **Behavior:** Spec 0001 owns scope, failures, and the four acceptance examples.
- **Uncertainty:** no material owner decision remains. Planning verified Tau
  0.4.4 exposes synchronous extension commands, so `/mcp` is status-only, and
  exposes no post-decision trust state, so v1 reads only `~/.tau/mcp.json`.

Planning contract: version 1. Installed from `legout/skills`; exact catalog
revision unknown.

## Constraints and sequence

- Runtime target: Tau 0.4.4 on Python >=3.12. Runtime code uses only the Python
  standard library; `tau-ai`, pytest, Ruff, and ty are development-only.
- MCP transport is newline-delimited stdio JSON-RPC. Offer protocol version
  `2025-11-25` and accept the compatible legacy versions used by the current MCP
  SDK (`2025-06-18`, `2025-03-26`, `2024-11-05`, `2024-10-07`) for this common
  initialize/tools subset.
- One writer executes the tickets in order. Issues #2-#4 share `registry.py`,
  `proxy.py`, and `extension.py`; parallel writers would create avoidable
  integration work.
- Security boundary: MCP server stdout and tool results are external input. The
  host process and context window are the assets; malformed frames and oversized
  results are the reachable attack path. Validate JSON-RPC shapes, bound retained
  stderr and returned output, and terminate failed children. User-owned global
  config is trusted local input; project config is not read.
- No HTTP transport, OAuth, project config, interactive UI, MCP scripting,
  resources, prompts, sampling, or live stderr streaming.

## Tickets and requirement coverage

Tickets are sequential; each linked GitHub issue is the canonical editable task
body, including owned files, interfaces, validation obligation, and completion
evidence.

| Ticket | Scope and approved requirements | Dependency |
| --- | --- | --- |
| [#1 — Ship the offline extension surface](https://github.com/legout/tau-mcp/issues/1) | Global config parsing/expansion/diagnostics, unsupported HTTP rejection, no auto-create, cache digest/ranked search under 50 ms, one proxy tool, status-only `/mcp` | None |
| [#2 — Add one lazy stdio call path](https://github.com/legout/tau-mcp/issues/2) | Lazy initialize/list/call, explicit connect/disconnect, JSON-string args recovery, spawn/handshake/EOF errors, Acceptance 1 | Blocked by #1 |
| [#3 — Harden lifecycle and failure bounds](https://github.com/legout/tau-mcp/issues/3) | Eager start, idle timeout, cancellation, bounded output/stderr, crash recovery, shutdown/reload cleanup, Acceptance 3 | Blocked by #2 |
| [#4 — Register opt-in direct tools](https://github.com/legout/tau-mcp/issues/4) | `directTools`, include/exclude filters, real schemas, proxy retention, Acceptance 4 | Blocked by #3 |
| [#5 — Document, install-smoke, and verify](https://github.com/legout/tau-mcp/issues/5) | Installation/migration docs, temporary install smoke, complete static/test checks, acceptance reconciliation | Blocked by #4 |

Issues #3 and #4 retain their immediate high-risk review boundaries. Issue #5
owns candidate review, the one permitted fix pass and delta recheck, and the
complete verification commands.

## Residual manual check

The hermetic fake server proves protocol behavior but not the user's ariadne
command, environment, or database path. After implementation, and only with
explicit authorization to edit/use `~/.tau/mcp.json`, run one real ariadne
connect, cached search, and read-only query; then confirm a fresh Tau session can
search the populated cache without spawning ariadne. This is the only planned
manual acceptance check.
