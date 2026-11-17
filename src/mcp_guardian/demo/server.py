"""Minimal stdio MCP server used for demos and local testing."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


SERVER_INFO = {
    "name": "mcp-guardian-demo",
    "version": "0.1.0",
}

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back a message",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    {
        "name": "list_dir",
        "description": "List files in a directory (demo)",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_note",
        "description": "Write a short note to a file under /tmp (demo)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["filename", "content"],
        },
    },
]


def _read() -> dict[str, Any] | None:
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    text = line.decode("utf-8").strip()
    if not text:
        return _read()
    return json.loads(text)


def _write(msg: dict[str, Any]) -> None:
    sys.stdout.buffer.write(
        (json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
    )
    sys.stdout.buffer.flush()


def _result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    args = arguments or {}
    if name == "echo":
        text = str(args.get("message", ""))
        return {"content": [{"type": "text", "text": text}]}
    if name == "list_dir":
        path = Path(str(args.get("path", "."))).expanduser()
        if not path.exists():
            return {
                "content": [{"type": "text", "text": f"Path not found: {path}"}],
                "isError": True,
            }
        entries = sorted(p.name for p in path.iterdir())
        return {"content": [{"type": "text", "text": "\n".join(entries) or "(empty)"}]}
    if name == "write_note":
        filename = Path(str(args.get("filename", "note.txt"))).name
        content = str(args.get("content", ""))
        target = Path("/tmp") / "mcp-guardian-demo" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {
            "content": [{"type": "text", "text": f"Wrote {len(content)} bytes to {target}"}]
        }
    return {
        "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
        "isError": True,
    }


def handle(msg: dict[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method")
    req_id = msg.get("id")

    if method == "initialize":
        return _result(
            req_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        return _result(req_id, _call_tool(str(name), arguments if isinstance(arguments, dict) else {}))
    if req_id is not None:
        return _error(req_id, -32601, f"Method not found: {method}")
    return None


def main() -> None:
    while True:
        try:
            msg = _read()
        except json.JSONDecodeError as exc:
            _write(_error(None, -32700, f"Parse error: {exc}"))
            continue
        if msg is None:
            break
        response = handle(msg)
        if response is not None:
            _write(response)


if __name__ == "__main__":
    main()
