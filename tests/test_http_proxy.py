import sys

import pytest
from aiohttp.test_utils import TestClient, TestServer

from mcp_guardian.approval.gate import ApprovalGate
from mcp_guardian.messages import Decision
from mcp_guardian.proxy.http import HttpStdioBridge, create_app


@pytest.mark.asyncio
async def test_http_allow_tool_call():
    bridge = HttpStdioBridge(
        [sys.executable, "-m", "mcp_guardian.demo.server"],
        server_name="demo",
        gate=ApprovalGate(auto_decision=Decision.ALLOW),
    )
    await bridge.start()
    app = create_app(bridge)
    try:
        async with TestClient(TestServer(app)) as client:
            health = await client.get("/health")
            assert health.status == 200

            init = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                },
            )
            assert init.status == 200
            body = await init.json()
            assert "result" in body

            call = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"message": "http"}},
                },
            )
            assert call.status == 200
            result = await call.json()
            assert result["result"]["content"][0]["text"] == "http"
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_http_block_tool_call():
    bridge = HttpStdioBridge(
        [sys.executable, "-m", "mcp_guardian.demo.server"],
        server_name="demo",
        gate=ApprovalGate(auto_decision=Decision.BLOCK),
    )
    await bridge.start()
    app = create_app(bridge)
    try:
        async with TestClient(TestServer(app)) as client:
            call = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"message": "no"}},
                },
            )
            result = await call.json()
            assert "error" in result
            assert "Blocked by user" in result["error"]["message"]
    finally:
        await bridge.stop()
