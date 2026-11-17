from mcp_guardian.protocol import (
    BLOCKED_ERROR_CODE,
    blocked_response,
    decode_line,
    encode_message,
    is_request,
    is_tools_call,
    should_gate,
    tool_call_parts,
)


def test_encode_decode_roundtrip():
    msg = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    raw = encode_message(msg)
    assert raw.endswith(b"\n")
    assert decode_line(raw) == msg


def test_decode_empty_line():
    assert decode_line(b"\n") is None
    assert decode_line("   ") is None


def test_tools_call_detection():
    msg = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"message": "hi"}},
    }
    assert is_request(msg)
    assert is_tools_call(msg)
    assert should_gate(msg)
    name, args = tool_call_parts(msg)
    assert name == "echo"
    assert args == {"message": "hi"}


def test_initialize_not_gated():
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    assert not should_gate(msg)


def test_blocked_response():
    resp = blocked_response(3, "write_note")
    assert resp["id"] == 3
    assert resp["error"]["code"] == BLOCKED_ERROR_CODE
    assert "Blocked by user" in resp["error"]["message"]
