# MCP Guardian

**Human-in-the-loop authorization for Model Context Protocol (MCP) tool calls.**

MCP Guardian sits between an MCP client (Claude Desktop, Claude.ai custom connectors, or any stdio/HTTP MCP host) and your real MCP servers. Every `tools/call` is paused until you **Allow** or **Block** it in an always-on-top confirmation dialog.

```
MCP client  →  mcp-guardian  →  Allow?  →  upstream MCP server
                                  ↓ Block
                            JSON-RPC error (tool never runs)
```

## Features

- Intercepts MCP `tools/call` requests (stdio and Streamable HTTP)
- Always-on-top Allow / Block popup with server name, tool, and arguments
- Holds requests until you decide; supports concurrent calls
- Fail-closed: timeouts, UI failures, and dialog close default to **Block**
- Works with Claude Desktop (local wrap) and Claude Web (HTTP + HTTPS tunnel)
- Bundled demo server/client for local testing without a Claude account
- UI daemon for environments (like Claude Desktop) that strip `$DISPLAY`

## Requirements

- Python 3.11+
- A display for popups (Linux/macOS; Windows via WSL/X may vary)
  - Debian/Ubuntu: `sudo apt install python3-tk`
  - Fedora: `sudo dnf install python3-tkinter`
  - macOS: Tk usually ships with python.org installers
- Optional: Claude Desktop, Claude.ai, `cloudflared` or `ngrok` for remote connectors

## Install

```bash
git clone https://github.com/Vinay-1209/mcp-guardian.git
cd mcp-guardian
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Quick start (local demo)

```bash
# Terminal A — UI daemon (recommended; keep running)
mcp-guardian ui

# Terminal B — interactive demo
mcp-guardian demo
```

You will be prompted to Allow / Block sample tool calls (`echo`, `write_note`, concurrent calls).

Headless / CI:

```bash
MCP_GUARDIAN_AUTO_DECISION=allow pytest -q
```

## Claude Desktop

1. Install MCP Guardian and note absolute paths to the venv binaries.
2. Edit the Claude Desktop config:

| OS | Config path |
|----|-------------|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

3. Wrap each MCP server with Guardian:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "/ABS/PATH/TO/.venv/bin/mcp-guardian",
      "args": [
        "wrap",
        "--name", "filesystem",
        "--",
        "npx", "-y", "@modelcontextprotocol/server-filesystem",
        "/home/you/Documents"
      ],
      "env": {
        "DISPLAY": ":0"
      }
    }
  }
}
```

4. Start the UI daemon (`mcp-guardian ui`), restart Claude Desktop, then invoke a tool in chat. A confirmation popup should appear before the tool runs.

`DISPLAY` in `env` helps on Linux; the UI daemon covers cases where Claude strips the display from child processes.

## Claude Web (custom connector)

Claude.ai reaches your connector from Anthropic’s cloud over public HTTPS:

```bash
# Terminal 1
mcp-guardian ui

# Terminal 2
mcp-guardian http --listen 127.0.0.1:8765 --name demo -- \
  python -m mcp_guardian.demo.server

# Terminal 3 — expose HTTPS
cloudflared tunnel --url http://127.0.0.1:8765
```

In Claude.ai: **Customize → Connectors → Add custom connector** → `https://<tunnel-host>/mcp`.

## CLI

```bash
mcp-guardian ui                                      # approval UI daemon
mcp-guardian wrap --name <label> -- <upstream...>    # stdio proxy
mcp-guardian http --listen 127.0.0.1:8765 -- <cmd>   # Streamable HTTP proxy
mcp-guardian demo                                    # local interactive demo
```

Flags: `--timeout 120`, `-v`.

| Environment variable | Meaning |
|----------------------|---------|
| `MCP_GUARDIAN_AUTO_DECISION=allow\|block` | Skip UI (tests / automation only) |
| `MCP_GUARDIAN_UI_SOCK` | Override UI Unix socket path |

## Project layout

```
src/mcp_guardian/
  approval/     # gate, popup, UI daemon IPC
  proxy/        # stdio + HTTP interceptors
  demo/         # sample MCP server + client
  cli.py        # wrap | http | demo | ui
tests/
ARCHITECTURE.md
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for transport design, concurrency, and failure modes.

## Security notes

- Point Claude only at Guardian binaries you trust and control.
- Never set `MCP_GUARDIAN_AUTO_DECISION` in production configs.
- Prefer ephemeral tunnels for HTTP demos; add auth before long-lived public exposure.
- Closing the popup blocks the request (never silently allows).

## License

MIT — see [LICENSE](LICENSE).
