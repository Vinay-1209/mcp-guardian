"""Stdio MCP proxy: sits between a client and an upstream MCP server process."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from typing import Any

from mcp_guardian.approval.gate import ApprovalGate, ApprovalTimeout
from mcp_guardian.messages import Decision, PendingRequest
from mcp_guardian.protocol import (
    PARSE_ERROR_CODE,
    blocked_response,
    decode_line,
    encode_message,
    is_response,
    make_error_response,
    should_gate,
    timeout_response,
    tool_call_parts,
)

logger = logging.getLogger(__name__)


class StdioProxy:
    """
    Bidirectional NDJSON proxy.

    Client <-> (stdin/stdout of this process, or injected streams)
          <-> approval gate <-> upstream child process
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        server_name: str = "mcp-server",
        gate: ApprovalGate | None = None,
        client_stdin: asyncio.StreamReader | None = None,
        client_stdout: asyncio.StreamWriter | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        if not command:
            raise ValueError("upstream command is required")
        self.command = list(command)
        self.server_name = server_name
        self.gate = gate or ApprovalGate()
        self._client_stdin = client_stdin
        self._client_stdout = client_stdout
        self._env = env
        self._cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._pending_upstream: dict[Any, asyncio.Future[dict[str, Any]]] = {}
        self._stopping = False
        self._timed_out_ids: set[str] = set()

    async def run(self) -> int:
        self._install_signal_handlers()
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            cwd=self._cwd,
        )
        assert self._proc.stdin and self._proc.stdout and self._proc.stderr
        logger.info(
            "guardian wrapping %s as %r (pid=%s) [ipc-ui-gate]",
            self.command,
            self.server_name,
            self._proc.pid,
        )

        own_stdio = self._client_stdin is None
        if self._client_stdin is not None and self._client_stdout is not None:
            reader, writer = self._client_stdin, self._client_stdout
        else:
            reader = await _stdin_reader()
            writer = await _stdout_writer()

        tasks = [
            asyncio.create_task(self._pump_client(reader, writer), name="c2u"),
            asyncio.create_task(
                self._pump_upstream(self._proc.stdout, writer), name="u2c"
            ),
            asyncio.create_task(self._forward_stderr(self._proc.stderr), name="err"),
            asyncio.create_task(self._proc.wait(), name="wait"),
        ]
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                if t.cancelled():
                    continue
                exc = t.exception()
                if exc:
                    logger.error("proxy task failed: %s", exc)
        finally:
            await self._shutdown(writer if own_stdio else None)

        return self._proc.returncode if self._proc.returncode is not None else 1

    async def handle_client_message(self, raw_line: bytes) -> dict[str, Any] | None:
        """
        Process one client NDJSON line.

        Returns an immediate client response (block/parse error), or None when
        the message was forwarded upstream.
        """
        try:
            msg = decode_line(raw_line)
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            logger.warning("malformed client message: %s", exc)
            return make_error_response(None, PARSE_ERROR_CODE, f"Parse error: {exc}")

        if msg is None:
            return None

        if not should_gate(msg):
            await self._write_upstream(msg)
            return None

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
            raw_params=msg.get("params") if isinstance(msg.get("params"), dict) else {},
        )
        logger.info("gating %s", pending.summary())

        try:
            decision = await self.gate.request_approval(pending)
        except ApprovalTimeout:
            return timeout_response(msg.get("id"), tool_name)

        if decision is Decision.BLOCK:
            return blocked_response(msg.get("id"), tool_name)

        await self._write_upstream(msg)
        return None

    async def _pump_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        while not self._stopping:
            line = await reader.readline()
            if not line:
                logger.info("client stdin closed")
                break
            response = await self.handle_client_message(line)
            if response is not None:
                writer.write(encode_message(response))
                await writer.drain()

    async def _pump_upstream(
        self, upstream_stdout: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        while not self._stopping:
            line = await upstream_stdout.readline()
            if not line:
                logger.info("upstream stdout closed")
                break
            try:
                msg = decode_line(line)
            except (UnicodeDecodeError, ValueError, TypeError) as exc:
                logger.warning("malformed upstream message: %s", exc)
                continue
            if msg is None:
                continue
            if is_response(msg):
                fut = self._pending_upstream.pop(msg.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(msg)
            writer.write(encode_message(msg))
            await writer.drain()

    async def _forward_stderr(self, stderr: asyncio.StreamReader) -> None:
        while True:
            line = await stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.info("[upstream] %s", text)

    async def _write_upstream(self, msg: dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("upstream process is not running")
        self._proc.stdin.write(encode_message(msg))
        await self._proc.stdin.drain()

    async def request_and_wait(
        self, msg: dict[str, Any], timeout: float = 30.0
    ) -> dict[str, Any]:
        """Send a request upstream and wait for the matching response."""
        req_id = msg.get("id")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_upstream[req_id] = fut
        await self._write_upstream(msg)
        return await asyncio.wait_for(fut, timeout=timeout)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig, lambda s=sig: asyncio.create_task(self._on_signal(s))
                )
            except NotImplementedError:
                pass

    async def _on_signal(self, sig: signal.Signals) -> None:
        logger.info("received %s; shutting down", sig.name)
        self._stopping = True
        await self.gate.shutdown()
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()

    async def _shutdown(self, writer: asyncio.StreamWriter | None) -> None:
        self._stopping = True
        await self.gate.shutdown()
        if self._proc:
            if self._proc.stdin and not self._proc.stdin.is_closing():
                self._proc.stdin.close()
                try:
                    await self._proc.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            if self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
        for fut in self._pending_upstream.values():
            if not fut.done():
                fut.set_exception(RuntimeError("proxy shutting down"))
        self._pending_upstream.clear()
        if writer is not None and not writer.is_closing():
            try:
                writer.close()
                # connect_write_pipe FlowControlMixin may not implement wait_closed
                wait = getattr(writer, "wait_closed", None)
                if wait is not None:
                    try:
                        await wait()
                    except NotImplementedError:
                        pass
            except (BrokenPipeError, ConnectionResetError):
                pass


async def _stdin_reader() -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    return reader


async def _stdout_writer() -> asyncio.StreamWriter:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    return asyncio.StreamWriter(transport, protocol, None, loop)


async def run_stdio_proxy(
    command: Sequence[str],
    *,
    server_name: str = "mcp-server",
    gate: ApprovalGate | None = None,
) -> int:
    return await StdioProxy(command, server_name=server_name, gate=gate).run()
