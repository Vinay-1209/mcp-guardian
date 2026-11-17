import socket
import threading
from pathlib import Path

import pytest

from mcp_guardian.approval.ipc import (
    _recv,
    _send,
    is_ui_alive,
    request_decision,
    socket_path,
)
from mcp_guardian.messages import Decision, PendingRequest


@pytest.fixture()
def fake_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sock_file = tmp_path / "approve.sock"
    monkeypatch.setenv("MCP_GUARDIAN_UI_SOCK", str(sock_file))

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_file))
    server.listen(4)
    server.settimeout(2.0)
    stopping = threading.Event()

    def run() -> None:
        while not stopping.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            with conn:
                msg = _recv(conn)
                if not msg:
                    continue
                if msg.get("op") == "ping":
                    _send(conn, {"ok": True})
                elif msg.get("op") == "approve":
                    _send(conn, {"ok": True, "decision": "allow"})

    t = threading.Thread(target=run, daemon=True)
    t.start()
    yield sock_file
    stopping.set()
    server.close()
    if sock_file.exists():
        sock_file.unlink()


def test_ipc_ping_and_approve(fake_ui: Path):
    assert is_ui_alive(fake_ui)
    decision = request_decision(
        PendingRequest(server_name="s", tool_name="echo", arguments={"m": "1"}),
        timeout=2.0,
    )
    assert decision is Decision.ALLOW


def test_default_socket_path_under_cache():
    assert "mcp-guardian" in str(socket_path())
