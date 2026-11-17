"""Demo MCP client that drives allow/block scenarios through a stdio proxy."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from mcp_guardian.protocol import decode_line, encode_message

logger = logging.getLogger(__name__)


class StdioClient:
    def __init__(self, command: list[str]) -> None:
        self.command = command
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[Any, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._reader_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._proc.stdout
        self._reader_task = asyncio.create_task(self._read_loop())
        asyncio.create_task(self._stderr_loop())

    async def close(self) -> None:
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

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = fut
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(encode_message(msg))
        await self._proc.stdin.drain()
        return await asyncio.wait_for(fut, timeout=180)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(encode_message(msg))
        await self._proc.stdin.drain()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.request("tools/call", {"name": name, "arguments": arguments})

    async def _read_loop(self) -> None:
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

    async def _stderr_loop(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                print(f"[guardian] {text}", file=sys.stderr)


async def run_interactive_demo(guardian_command: list[str]) -> int:
    """
    Drive a short scripted demo:

    1. initialize + tools/list (pass-through)
    2. echo tool call — user should Allow
    3. write_note tool call — user should Block
    4. two concurrent list_dir + echo calls
    """
    client = StdioClient(guardian_command)
    await client.start()
    try:
        print("\n=== MCP Guardian Demo ===\n")
        init = await client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-guardian-demo", "version": "0.1.0"},
            },
        )
        print("initialize:", json.dumps(init.get("result", {}), indent=2))
        await client.notify("notifications/initialized")

        tools = await client.request("tools/list")
        names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
        print("tools:", ", ".join(names))

        print("\n--- Scenario 1: Allow echo ---")
        print("A popup should appear. Click ALLOW.")
        echo = await client.call_tool("echo", {"message": "Hello from MCP Guardian"})
        print("result:", json.dumps(echo, indent=2))

        print("\n--- Scenario 2: Block write_note ---")
        print("A popup should appear. Click BLOCK.")
        blocked = await client.call_tool(
            "write_note", {"filename": "secret.txt", "content": "should not be written"}
        )
        print("result:", json.dumps(blocked, indent=2))

        print("\n--- Scenario 3: Concurrent requests ---")
        print("Two popups will appear one after another. Allow the first, Block the second.")
        t1 = asyncio.create_task(
            client.call_tool("list_dir", {"path": "/tmp"}), name="list_dir"
        )
        await asyncio.sleep(0.2)
        t2 = asyncio.create_task(
            client.call_tool("echo", {"message": "concurrent"}), name="echo"
        )
        r1, r2 = await asyncio.gather(t1, t2)
        print("list_dir:", json.dumps(r1, indent=2))
        print("echo:", json.dumps(r2, indent=2))

        print("\n=== Demo complete ===\n")
        return 0
    finally:
        await client.close()
