"""Wall + monotonic clock, injectable for tests."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def wall(self) -> datetime: ...
    def mono(self) -> float: ...


class RealClock:
    def wall(self) -> datetime:
        return datetime.now(UTC)

    def mono(self) -> float:
        return time.monotonic()
