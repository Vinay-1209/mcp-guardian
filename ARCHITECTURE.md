# Architecture

## Goal

Give users interactive control over MCP tool execution: every `tools/call` is held until they **Allow** or **Block** it, with enough context (server, tool, arguments) to decide safely.

## Why a protocol proxy (not traffic sniffing)

MCP defines clear transports (stdio NDJSON and Streamable HTTP). Intercepting on those boundaries is more reliable than trying to sniff host process traffic:

| Concern | Protocol proxy | OS / network sniffing |
|--------|----------------|------------------------|
| Tool name + arguments | Native JSON-RPC fields | Often opaque or encrypted |
| Hold until user decides | Await before forward | Racy / incomplete |
| Clean denial to the client | JSON-RPC error | Client may hang or retry oddly |
| Claude Desktop | Replace server `command` with `mcp-guardian wrap` | N/A |
| Claude Web | Public HTTPS `/mcp` → HTTP mode | Cloud egress, not the browser |

```
MCP client (Claude Desktop, demo, …)
        |  stdio JSON-RPC
        v
  mcp-guardian wrap
        |  gate tools/call → UI (popup / UI daemon)
        |  Allow → forward / Block → JSON-RPC error (-32000)
        v
  Upstream MCP server

Claude Web (custom connector)
        |  HTTPS Streamable HTTP
        v
  Tunnel → mcp-guardian http → same gate → upstream
```

## Components

1. **`protocol`** — NDJSON encode/decode, `tools/call` detection, block/timeout helpers.
2. **`approval.gate`** — Concurrent `asyncio.Future`s per pending request; optional auto-decision for tests (`MCP_GUARDIAN_AUTO_DECISION`).
3. **`approval.popup`** — Tk thread; serial always-on-top dialogs (`-topmost`); Escape/close = Block, Return = Allow.
4. **`approval.ipc` + `mcp-guardian ui`** — Some hosts (notably Claude Desktop) strip `$DISPLAY` from MCP child processes. The wrap proxy talks to a UI daemon over a Unix socket (`~/.cache/mcp-guardian/approve.sock`) so popups still appear in the user’s graphical session.
5. **`proxy.stdio`** — Spawns upstream, pumps client↔upstream, gates `tools/call` only (`initialize`, `tools/list`, notifications pass through).
6. **`proxy.http`** — aiohttp `/mcp` for Streamable HTTP clients; shared stdio upstream bridge; same gate.
7. **`demo`** — Minimal MCP server + client for local verification without an external host.

## Concurrency

Multiple `tools/call` requests may be in flight. Each gets its own future. The UI shows **one dialog at a time** (queue) while the proxy correlates each decision to the correct request id.

## Failure modes (fail closed)

| Event | Behavior |
|-------|----------|
| User Blocks / closes dialog | JSON-RPC error; upstream never sees the call |
| Approval timeout | Timeout error; treated as block |
| Display / Tk unavailable | Fail closed (Block), preferably via UI daemon recovery |
| Malformed JSON | Parse error; no forward |
| Upstream crash | Client sees closed stream / error; guardian cleans up |
| SIGINT / SIGTERM | Outstanding approvals resolved as Block; upstream terminated |

## Security notes

- Guardian is a local trust boundary: the host must be configured to launch it.
- Do not enable auto-decision outside automated tests.
- Public HTTP mode should use HTTPS and authentication for anything beyond short demos.
- Closing the popup defaults to **Block**, never Allow.
