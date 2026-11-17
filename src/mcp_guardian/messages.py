"""Shared request models for the approval gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class Decision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"


@dataclass(slots=True)
class PendingRequest:
    """A gated MCP tools/call awaiting a user decision."""

    server_name: str
    tool_name: str
    arguments: dict[str, Any] | list[Any] | str | None
    method: str = "tools/call"
    request_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_params: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        return f"{self.server_name} / {self.tool_name}"
