import asyncio

import pytest

from mcp_guardian.approval.gate import ApprovalGate, ApprovalTimeout
from mcp_guardian.messages import Decision, PendingRequest


def _req(tool: str = "echo") -> PendingRequest:
    return PendingRequest(
        server_name="test",
        tool_name=tool,
        arguments={"x": 1},
    )


@pytest.mark.asyncio
async def test_auto_allow():
    gate = ApprovalGate(auto_decision=Decision.ALLOW)
    assert await gate.request_approval(_req()) is Decision.ALLOW


@pytest.mark.asyncio
async def test_auto_block():
    gate = ApprovalGate(auto_decision=Decision.BLOCK)
    assert await gate.request_approval(_req()) is Decision.BLOCK


@pytest.mark.asyncio
async def test_custom_presenter():
    async def present(req: PendingRequest) -> Decision:
        assert req.tool_name == "echo"
        return Decision.ALLOW

    gate = ApprovalGate(presenter=present, auto_decision=None)
    assert await gate.request_approval(_req()) is Decision.ALLOW


@pytest.mark.asyncio
async def test_timeout_raises():
    async def slow(_req: PendingRequest) -> Decision:
        await asyncio.sleep(1.0)
        return Decision.ALLOW

    gate = ApprovalGate(presenter=slow, timeout_seconds=0.05, auto_decision=None)
    with pytest.raises(ApprovalTimeout):
        await gate.request_approval(_req())


@pytest.mark.asyncio
async def test_concurrent_approvals():
    decisions = {"echo": Decision.ALLOW, "write_note": Decision.BLOCK}

    async def present(req: PendingRequest) -> Decision:
        await asyncio.sleep(0.01)
        return decisions[req.tool_name]

    gate = ApprovalGate(presenter=present, auto_decision=None)
    results = await asyncio.gather(
        gate.request_approval(_req("echo")),
        gate.request_approval(_req("write_note")),
    )
    assert results == [Decision.ALLOW, Decision.BLOCK]
