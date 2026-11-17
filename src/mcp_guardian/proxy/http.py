"""Streamable HTTP MCP proxy for Claude Web custom connectors."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any

from aiohttp import web

from mcp_guardian.approval.gate import ApprovalGate, ApprovalTimeout
from mcp_guardian.messages import Decision, PendingRequest
from mcp_guardian.protocol import (
    PARSE_ERROR_CODE,
    blocked_response,
    decode_line,
    encode_message,
    make_error_response,
    should_gate,
    timeout_response,
    tool_call_parts,
)

logger = logging.getLogger(__name__)


class HttpStdioBridge:
    """
    One long-lived stdio upstream shared by HTTP requests.

    Claude Web posts JSON-RPC to /mcp; we gate tools/call, then forward to
    the upstream stdio MCP server and return the JSON-RPC response.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        server_name: str = "mcp-server",
        gate: ApprovalGate | None = None,
    ) -> None:
        self.command = list(command)
        self.server_name = server_name
        self.gate = gate or ApprovalGate()
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._pending: dict[Any, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._proc.stdout and self._proc.stderr
        self._reader_task = asyncio.create_task(self._read_upstream())
        asyncio.create_task(self._log_stderr())
        logger.info("HTTP bridge started upstream %s", self.command)

    async def stop(self) -> None:
        await self.gate.shutdown()
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._proc and self._proc.returncode is None:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()

    async def handle_rpc(self, msg: dict[str, Any]) -> dict[str, Any] | list[Any] | None:
        """Handle a single JSON-RPC message; notifications return None."""
        if "method" in msg and "id" not in msg:
            await self._write(msg)
            return None

        if should_gate(msg):
            tool_name, arguments = tool_call_parts(msg)
            pending = PendingRequest(
                server_name=self.server_name,
                tool_name=tool_name or "(unknown)",
                arguments=(
                    arguments
                    if isinstance(arguments, (dict, list, str)) or arguments is None
                    else str(arguments)
                ),
                method=str(msg.get("method")),
                raw_params=(
                    msg.get("params") if isinstance(msg.get("params"), dict) else {}
                ),
            )
            try:
                decision = await self.gate.request_approval(pending)
            except ApprovalTimeout:
                return timeout_response(msg.get("id"), tool_name)
            if decision is Decision.BLOCK:
                return blocked_response(msg.get("id"), tool_name)

        return await self._roundtrip(msg)

    async def _roundtrip(self, msg: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        req_id = msg.get("id")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        async with self._lock:
            self._pending[req_id] = fut
            await self._write(msg)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return make_error_response(
                req_id, PARSE_ERROR_CODE, "Upstream MCP server timed out"
            )

    async def _write(self, msg: dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("upstream not running")
        self._proc.stdin.write(encode_message(msg))
        await self._proc.stdin.drain()

    async def _read_upstream(self) -> None:
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            try:
                msg = decode_line(line)
            except (UnicodeDecodeError, ValueError, TypeError):
                continue
            if msg is None:
                continue
            req_id = msg.get("id")
            fut = self._pending.pop(req_id, None)
            if fut and not fut.done():
                fut.set_result(msg)

    async def _log_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.info("[upstream] %s", text)


def create_app(bridge: HttpStdioBridge) -> web.Application:
    app = web.Application()
    app.router.add_route("POST", "/mcp", _make_mcp_handler(bridge))
    app.router.add_route("GET", "/mcp", _make_get_handler())
    app.router.add_route("GET", "/health", _health)
    return app


def _make_get_handler():
    async def get_mcp(_request: web.Request) -> web.Response:
        # Streamable HTTP may open GET for SSE streams; acknowledge readiness.
        return web.Response(
            status=405,
            text="Use POST for MCP JSON-RPC",
            headers={"Allow": "POST"},
        )

    return get_mcp


async def _health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "mcp-guardian"})


def _make_mcp_handler(bridge: HttpStdioBridge):
    async def mcp_handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            return web.json_response(
                make_error_response(None, PARSE_ERROR_CODE, f"Parse error: {exc}"),
                status=400,
            )

        accept = request.headers.get("Accept", "")

        async def handle_one(msg: dict[str, Any]) -> dict[str, Any] | None:
            return await bridge.handle_rpc(msg)  # type: ignore[return-value]

        if isinstance(body, list):
            results = []
            for item in body:
                if not isinstance(item, dict):
                    results.append(
                        make_error_response(None, PARSE_ERROR_CODE, "Invalid request")
                    )
                    continue
                result = await handle_one(item)
                if result is not None:
                    results.append(result)
            payload: Any = results
        elif isinstance(body, dict):
            payload = await handle_one(body)
            if payload is None:
                return web.Response(status=202)
        else:
            return web.json_response(
                make_error_response(None, PARSE_ERROR_CODE, "Invalid request"),
                status=400,
            )

        # Prefer JSON when client accepts it; otherwise wrap as a single SSE event.
        if "text/event-stream" in accept and "application/json" not in accept:
            data = json.dumps(payload, ensure_ascii=False)
            sse = f"event: message\ndata: {data}\n\n"
            return web.Response(
                text=sse,
                content_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )
        return web.json_response(payload)

    return mcp_handler


async def run_http_server(
    command: Sequence[str],
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    server_name: str = "mcp-server",
    gate: ApprovalGate | None = None,
) -> None:
    bridge = HttpStdioBridge(command, server_name=server_name, gate=gate)
    await bridge.start()
    app = create_app(bridge)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("MCP Guardian HTTP listening on http://%s:%s/mcp", host, port)
    try:
        await asyncio.Event().wait()
    finally:
        await bridge.stop()
        await runner.cleanup()
