"""Helpers shared by the backends."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.models.views import ServiceState, ServiceStatus
from vps_mcp.remote.executor import CommandResult

# Patched to 0 in unit tests.
POLL_INTERVAL = 1.0
SETTLE_DELAY = 1.0

_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([KMGTP]?i?B|B)?\s*$", re.IGNORECASE)
_SIZE_MULT = {
    "": 1,
    "B": 1,
    "KB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "TB": 10**12,
    "PB": 10**15,
    "KIB": 2**10,
    "MIB": 2**20,
    "GIB": 2**30,
    "TIB": 2**40,
    "PIB": 2**50,
}


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_size(text: str) -> int | None:
    m = _SIZE_RE.match(text)
    if not m:
        return None
    unit = (m[2] or "").upper()
    return int(float(m[1]) * _SIZE_MULT.get(unit, 1))


def parse_rfc3339(value: str | None) -> datetime | None:
    if not value or value.startswith("0001-01-01"):
        return None
    v = value.strip().replace("Z", "+00:00")
    # Trim nanoseconds to microseconds for fromisoformat.
    v = re.sub(r"(\.\d{6})\d+", r"\1", v)
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def tail_text(text: str, n: int = 20) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


def command_failed(what: str, result: CommandResult, **extra: Any) -> VpsMcpError:
    detail = tail_text(result.stderr or result.stdout, 10) or f"exit status {result.exit_status}"
    return VpsMcpError(ErrorCategory.FAILED, f"{what} failed: {detail}", extra=extra or None)


async def poll_status(
    fetch: Callable[[], Awaitable[ServiceStatus]],
    *,
    deadline_s: float,
    done: Callable[[ServiceStatus], bool] = lambda s: s.state is not ServiceState.TRANSITIONING,
) -> ServiceStatus:
    """Poll until ``done`` or the deadline; returns the last status seen."""
    end = time.monotonic() + deadline_s
    status = await fetch()
    while not done(status) and time.monotonic() < end:
        await asyncio.sleep(POLL_INTERVAL)
        status = await fetch()
    return status


async def settle_running(
    fetch: Callable[[], Awaitable[ServiceStatus]], *, deadline_s: float
) -> ServiceStatus:
    """Wait for a start/restart to finish, then re-check once so crash-on-start is caught."""
    status = await poll_status(fetch, deadline_s=deadline_s)
    if status.state is ServiceState.RUNNING and SETTLE_DELAY:
        await asyncio.sleep(SETTLE_DELAY)
        status = await poll_status(fetch, deadline_s=deadline_s)
    return status
