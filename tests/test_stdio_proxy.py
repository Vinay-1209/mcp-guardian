import asyncio
import os
import sys

import pytest

from mcp_guardian.approval.gate import ApprovalGate
from mcp_guardian.demo.client import StdioClient
from mcp_guardian.messages import Decision, PendingRequest


def _wrap_cmd() -> list[str]:
    return [
        sys.executable,
        "-m",
        "mcp_guardian",
        "wrap",
        "--name",
        "demo",
        "--",
        sys.executable,
        "-m",
        "mcp_guardian.demo.server",
    ]


class EnvClient(StdioClient):
    def __init__(self, command: list[str], env: dict[str, str]) -> None:
        super().__init__(command)
        self._env = env

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        assert self._proc.stdout
        self._reader_task = asyncio.create_task(self._read_loop())
        asyncio.create_task(self._stderr_loop())


async def _handshake(client: StdioClient) -> None:
    init = await client.request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    )
    assert "result" in init
    await client.notify("notifications/initialized")


@pytest.mark.asyncio
async def test_allow_forwards_tool_call():
    env = {**os.environ, "MCP_GUARDIAN_AUTO_DECISION": "allow"}
    client = EnvClient(_wrap_cmd(), env)
    await client.start()
    try:
        await _handshake(client)
        tools = await client.request("tools/list")
        assert any(t["name"] == "echo" for t in tools["result"]["tools"])
        result = await client.call_tool("echo", {"message": "ping"})
        assert result["result"]["content"][0]["text"] == "ping"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_block_returns_error():
    env = {**os.environ, "MCP_GUARDIAN_AUTO_DECISION": "block"}
    client = EnvClient(_wrap_cmd(), env)
    await client.start()
    try:
        await _handshake(client)
        result = await client.call_tool("echo", {"message": "nope"})
        assert "error" in result
        assert "Blocked by user" in result["error"]["message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_gate_decisions():
    allow_gate = ApprovalGate(auto_decision=Decision.ALLOW)
    block_gate = ApprovalGate(auto_decision=Decision.BLOCK)
    req = PendingRequest(server_name="s", tool_name="t", arguments={})
    assert await allow_gate.request_approval(req) is Decision.ALLOW
    assert await block_gate.request_approval(req) is Decision.BLOCK
