# Changelog

All notable changes to tau-mcp are documented here.

## [Unreleased](https://github.com/legout/tau-mcp/compare/v0.1.0...HEAD)

## [0.1.0](https://github.com/legout/tau-mcp/releases/tag/v0.1.0) - 2026-09-17

### Added

- A proxy-first `mcp` tool with global config parsing, offline metadata search,
  atomic cache updates, and a status-only `/mcp` command.
- A stdlib-only stdio MCP client with lazy and eager lifecycle handling,
  protocol-version negotiation, request cancellation, bounded stderr and output,
  crash recovery, and deterministic child-process cleanup.
- Opt-in direct Tau tools with exact MCP input schemas, include/exclude filters,
  dynamic registration, and shared registry routing.
- Hermetic install, protocol, lifecycle, direct-tool, and offline-surface tests,
  plus migration and usage documentation.

### Fixed

- Reject ambiguous or reserved sanitized server namespaces before they can
  register an uncallable tool or route a call to the wrong MCP server.
