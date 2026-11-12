"""`agent-memory serve`: the standalone MCP server (research A10)."""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent

from agent_memory import mcp_tools
from agent_memory.errors import AgentMemoryError
from agent_memory.store import Memory

log = logging.getLogger("agent_memory")


def _result(payload: dict[str, Any], is_error: bool) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(
                text=json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)
            )
        ],
        structured_content=payload,
        is_error=is_error,
    )


def wrap(fn: Callable[..., Awaitable[dict[str, Any]]]) -> Callable[..., Awaitable[CallToolResult]]:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> CallToolResult:
        try:
            payload = await fn(*args, **kwargs)
        except AgentMemoryError as err:
            return _result(err.to_envelope(), True)
        except Exception as exc:  # noqa: BLE001
            log.exception("unhandled error in %s", fn.__name__)
            return _result(
                {
                    "ok": False,
                    "error": {"category": "failed", "message": f"internal error: {exc!r}"[:300]},
                },
                True,
            )
        return _result({"ok": True, **payload}, False)

    return wrapper


def session_id(ctx: Context | None) -> str | None:
    try:
        return f"session-{id(ctx.session):x}" if ctx is not None else None
    except Exception:  # noqa: BLE001
        return None


def create_server(mem: Memory) -> MCPServer:
    try:
        instructions = mem.pinned_core()
    except Exception as exc:  # noqa: BLE001 - the server must still start
        instructions = f"Memory unavailable: {getattr(exc, 'message', exc.__class__.__name__)}."
    mcp = MCPServer(name="agent-memory", instructions=instructions, version="0.1.0")
    mcp_tools.register(mcp, mem, wrap, session_id)
    return mcp
