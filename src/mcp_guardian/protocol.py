"""JSON-RPC / MCP message helpers for newline-delimited stdio transport."""

from __future__ import annotations

import json
from typing import Any


BLOCKED_ERROR_CODE = -32000
BLOCKED_ERROR_MESSAGE = "Blocked by user via MCP Guardian"
PARSE_ERROR_CODE = -32700
INTERNAL_ERROR_CODE = -32603


def encode_message(obj: dict[str, Any]) -> bytes:
    """Encode a JSON-RPC object as a single NDJSON line."""
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def decode_line(line: bytes | str) -> dict[str, Any] | None:
    """Parse one NDJSON line into a dict. Empty lines yield None."""
    if isinstance(line, bytes):
        text = line.decode("utf-8", errors="replace").strip()
    else:
        text = line.strip()
    if not text:
        return None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("JSON-RPC message must be an object")
    return data


def is_request(msg: dict[str, Any]) -> bool:
    return "method" in msg and "id" in msg


def is_notification(msg: dict[str, Any]) -> bool:
    return "method" in msg and "id" not in msg


def is_response(msg: dict[str, Any]) -> bool:
    return "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg


def is_tools_call(msg: dict[str, Any]) -> bool:
    return is_request(msg) and msg.get("method") == "tools/call"


def tool_call_parts(msg: dict[str, Any]) -> tuple[str, dict[str, Any] | list[Any] | None]:
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return "", None
    name = params.get("name") or ""
    arguments = params.get("arguments")
    return str(name), arguments


def make_error_response(
    request_id: Any,
    code: int,
    message: str,
    data: Any | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def blocked_response(request_id: Any, tool_name: str | None = None) -> dict[str, Any]:
    data = {"tool": tool_name} if tool_name else None
    return make_error_response(
        request_id, BLOCKED_ERROR_CODE, BLOCKED_ERROR_MESSAGE, data=data
    )


def timeout_response(request_id: Any, tool_name: str | None = None) -> dict[str, Any]:
    data = {"tool": tool_name} if tool_name else None
    return make_error_response(
        request_id,
        BLOCKED_ERROR_CODE,
        "MCP Guardian approval timed out; request blocked",
        data=data,
    )


def should_gate(msg: dict[str, Any], gated_methods: frozenset[str] | None = None) -> bool:
    """Return True if this request must wait for user approval."""
    methods = gated_methods or frozenset({"tools/call"})
    return is_request(msg) and msg.get("method") in methods
