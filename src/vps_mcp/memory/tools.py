"""The memory tools in vps-mcp are agent-memory's own (specs/006-agent-memory-service)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver import MCPServer

from agent_memory import mcp_tools
from vps_mcp.memory.service import MemoryAdapter

Wrap = Callable[[Callable[..., Awaitable[dict[str, Any]]]], Callable[..., Awaitable[Any]]]


def register(
    mcp: MCPServer, adapter: MemoryAdapter, wrap: Wrap, session_id: Callable[[Any], str | None]
) -> None:
    if adapter.mem is not None:
        mcp_tools.register(mcp, adapter.mem, wrap, session_id, writable=adapter.writable)
