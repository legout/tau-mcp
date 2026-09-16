# tau-mcp

A [Tau](https://github.com/huggingface/tau) extension that brings MCP server
support to Tau without burning the context window — a port of the proven
[pi-mcp-adapter](https://github.com/nicobailon/pi-mcp-adapter) architecture
(proxy tool + metadata cache + lazy servers) to Tau's Python extension API.

- Design: `docs/specs/0001-mcp-client-extension.md`
- Decision record: `docs/adr/0001-proxy-first-stdlib-client.md`

Status: specification phase — not yet implemented.
