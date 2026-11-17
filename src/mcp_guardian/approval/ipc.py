"""Unix-socket IPC between the MCP proxy and a graphical UI daemon."""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from mcp_guardian.messages import Decision, PendingRequest

logger = logging.getLogger(__name__)

DEFAULT_SOCK = Path.home() / ".cache" / "mcp-guardian" / "approve.sock"


def socket_path() -> Path:
    raw = os.environ.get("MCP_GUARDIAN_UI_SOCK")
    return Path(raw) if raw else DEFAULT_SOCK


def is_ui_alive(path: Path | None = None, timeout: float = 0.4) -> bool:
    path = path or socket_path()
    if not path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            _send(sock, {"op": "ping"})
            reply = _recv(sock)
            return bool(reply and reply.get("ok"))
    except OSError:
        return False


def ensure_ui_daemon() -> bool:
    """Start the UI daemon in the background if it is not already running."""
    if is_ui_alive():
        return True

    from mcp_guardian.approval.display import ensure_display_env

    ensure_display_env()
    path = socket_path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass

    env = os.environ.copy()
    log_path = Path.home() / ".cache" / "mcp-guardian" / "ui.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 — kept for daemon life
    cmd = [sys.executable, "-m", "mcp_guardian", "ui"]
    subprocess.Popen(
        cmd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(40):
        time.sleep(0.1)
        if is_ui_alive():
            logger.info("UI daemon ready (%s)", socket_path())
            return True
    logger.error("UI daemon failed to start; see %s", log_path)
    return False


def request_decision(request: PendingRequest, timeout: float = 120.0) -> Decision:
    path = socket_path()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        _send(
            sock,
            {
                "op": "approve",
                "request": {
                    "request_id": request.request_id,
                    "server_name": request.server_name,
                    "tool_name": request.tool_name,
                    "method": request.method,
                    "arguments": request.arguments,
                    "created_at": request.created_at.isoformat(),
                },
            },
        )
        reply = _recv(sock)
    if not reply:
        raise ConnectionError("empty reply from UI daemon")
    decision = str(reply.get("decision", "block")).lower()
    return Decision.ALLOW if decision == "allow" else Decision.BLOCK


def _send(sock: socket.socket, obj: dict[str, Any]) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    sock.sendall(data)


def _recv(sock: socket.socket) -> dict[str, Any] | None:
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    if not buf.strip():
        return None
    line = buf.split(b"\n", 1)[0]
    return json.loads(line.decode("utf-8"))


class ApprovalUIServer:
    """Accept approval requests and show popups on the graphical session."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or socket_path()
        self._sock: socket.socket | None = None
        self._stopping = threading.Event()

    def serve_forever(self) -> None:
        from mcp_guardian.approval.display import ensure_display_env
        from mcp_guardian.approval.popup import PopupPresenter

        ensure_display_env()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass

        presenter = PopupPresenter()
        presenter.start()

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        self._sock.listen(32)
        self._sock.settimeout(1.0)
        logger.info("MCP Guardian UI listening on %s", self.path)

        try:
            while not self._stopping.is_set():
                try:
                    conn, _ = self._sock.accept()
                except socket.timeout:
                    continue
                threading.Thread(
                    target=self._handle, args=(conn, presenter), daemon=True
                ).start()
        finally:
            presenter.stop()
            try:
                if self._sock:
                    self._sock.close()
            except OSError:
                pass
            if self.path.exists():
                try:
                    self.path.unlink()
                except OSError:
                    pass

    def stop(self) -> None:
        self._stopping.set()

    def _handle(self, conn: socket.socket, presenter: Any) -> None:
        try:
            with conn:
                msg = _recv(conn)
                if not msg:
                    return
                op = msg.get("op")
                if op == "ping":
                    _send(conn, {"ok": True})
                    return
                if op != "approve":
                    _send(conn, {"ok": False, "decision": "block"})
                    return
                payload = msg.get("request") or {}
                request = PendingRequest(
                    server_name=str(payload.get("server_name", "mcp-server")),
                    tool_name=str(payload.get("tool_name", "(unknown)")),
                    arguments=payload.get("arguments"),
                    method=str(payload.get("method", "tools/call")),
                    request_id=str(
                        payload.get("request_id")
                        or PendingRequest(
                            server_name="x", tool_name="y", arguments=None
                        ).request_id
                    ),
                )

                done = threading.Event()
                holder: dict[str, Decision] = {"d": Decision.BLOCK}

                def on_decision(decision: Decision) -> None:
                    holder["d"] = decision
                    done.set()

                presenter.ask(request, on_decision)
                if not done.wait(timeout=300):
                    holder["d"] = Decision.BLOCK
                _send(conn, {"ok": True, "decision": holder["d"].value})
        except Exception as exc:  # pragma: no cover
            logger.exception("UI handler failed: %s", exc)
