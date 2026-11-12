"""Recurring prompts with sleep detection (research R11)."""

from __future__ import annotations

import re
import zoneinfo
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from assistant.clock import Clock
from assistant.config import CADENCE_RE

SLEEP_GAP_S = 90.0


@dataclass(frozen=True)
class Cadence:
    kind: str  # daily | weekdays | every
    hour: int = 0
    minute: int = 0
    hours: int = 0

    @classmethod
    def parse(cls, text: str) -> Cadence:
        m = CADENCE_RE.fullmatch(text.strip())
        if not m:
            raise ValueError("cadence must be 'daily HH:MM', 'weekdays HH:MM' or 'every Nh'")
        if m.group(4):
            return cls("every", hours=int(m.group(4)))
        return cls(m.group(1), int(m.group(2)), int(m.group(3)))

    def next_after(self, after: datetime, tz: zoneinfo.ZoneInfo) -> datetime:
        """Next run strictly after ``after``, computed on the local wall clock, returned in UTC
        (stored timestamps are compared as UTC ISO strings)."""
        local = after.astimezone(tz)
        if self.kind == "every":
            base = local.replace(minute=0, second=0, microsecond=0)
            nxt = base
            while nxt <= local:
                nxt = nxt + timedelta(hours=self.hours)
            return nxt.astimezone(UTC)
        day = local.date()
        for _ in range(9):
            cand = datetime(day.year, day.month, day.day, self.hour, self.minute, tzinfo=tz)
            cand = cand.astimezone(tz)  # normalise across DST
            if cand > local and (self.kind == "daily" or cand.weekday() < 5):
                return cand.astimezone(UTC)
            day = day + timedelta(days=1)
        raise RuntimeError("no next run found")


def describe(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class SleepDetector:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.last_wall = clock.wall()
        self.last_mono = clock.mono()

    def check(self) -> tuple[datetime, datetime] | None:
        """Return (gap_start, gap_end) if the machine slept since the last check."""
        wall, mono = self.clock.wall(), self.clock.mono()
        wall_d = (wall - self.last_wall).total_seconds()
        mono_d = mono - self.last_mono
        gap = None
        if wall_d - mono_d > SLEEP_GAP_S:
            gap = (self.last_wall, wall)
        self.last_wall, self.last_mono = wall, mono
        return gap
