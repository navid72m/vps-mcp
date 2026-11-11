"""Per-service locks so state-changing actions on one service run one at a time (FR-012)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class ServiceLocks:
    def __init__(self) -> None:
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    @asynccontextmanager
    async def acquire(self, kind: str, name: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault((kind, name), asyncio.Lock())
        async with lock:
            yield
