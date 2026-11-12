"""Recurring prompts, reminders, and cron jobs with sleep detection (research R11)."""

from __future__ import annotations

import re
import zoneinfo
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from assistant.clock import Clock
from assistant.config import CADENCE_RE

SLEEP_GAP_S = 90.0


def _parse_cron_field(field: str, min_val: int, max_val: int, is_dow: bool = False) -> set[int]:
    """Parse a single cron field: '*', '*/N', '1,2,3', '1-5', '1-5/2'."""
    res: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            res.update(range(min_val, max_val + 1))
        elif part.startswith("*/"):
            step = int(part[2:])
            if step <= 0:
                raise ValueError(f"invalid step in {field}")
            res.update(range(min_val, max_val + 1, step))
        elif "-" in part:
            sub = part.split("/")
            rng = sub[0].split("-")
            start, end = int(rng[0]), int(rng[1])
            step = int(sub[1]) if len(sub) > 1 else 1
            if start > end or step <= 0:
                raise ValueError(f"invalid range in {field}")
            res.update(range(start, end + 1, step))
        else:
            val = int(part)
            if is_dow and val == 7:
                val = 0
            if val < min_val or val > max_val:
                raise ValueError(f"value {val} out of range [{min_val}, {max_val}] in {field}")
            res.add(val)
    if not res:
        raise ValueError(f"empty cron field: {field}")
    return res


def next_after_cron(cron_expr: str, after: datetime, tz: zoneinfo.ZoneInfo) -> datetime:
    """Find the next datetime matching a 5-field cron pattern strictly after ``after``."""
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError("cron expression must have 5 fields: min hour dom month dow")
    mins = _parse_cron_field(parts[0], 0, 59)
    hours = _parse_cron_field(parts[1], 0, 23)
    doms = _parse_cron_field(parts[2], 1, 31)
    months = _parse_cron_field(parts[3], 1, 12)
    dows = _parse_cron_field(parts[4], 0, 7, is_dow=True)

    local = after.astimezone(tz)
    cand = (local + timedelta(minutes=1)).replace(second=0, microsecond=0)
    limit = local + timedelta(days=366)
    while cand <= limit:
        if cand.month not in months:
            cand = (cand + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        cron_dow = (cand.weekday() + 1) % 7
        if cand.day not in doms or cron_dow not in dows:
            cand = (cand + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if cand.hour not in hours:
            cand = (cand + timedelta(hours=1)).replace(minute=0)
            continue
        if cand.minute not in mins:
            cand = cand + timedelta(minutes=1)
            continue
        return cand.astimezone(UTC)
    raise RuntimeError(f"no next run found for cron '{cron_expr}' within 1 year")


@dataclass(frozen=True)
class Cadence:
    kind: str  # daily | weekdays | every | once | cron
    hour: int = 0
    minute: int = 0
    hours: int = 0
    minutes: int = 0
    target: datetime | None = None
    cron_expr: str = ""

    @classmethod
    def parse(
        cls,
        text: str,
        now: datetime | None = None,
        tz: zoneinfo.ZoneInfo | None = None,
    ) -> Cadence:
        clean = text.strip()
        m = CADENCE_RE.fullmatch(clean)
        if not m:
            raise ValueError(
                "cadence must be 'daily HH:MM', 'weekdays HH:MM', 'every Nh|Nm', "
                "'in Ns|Nm|Nh|Nd', 'at HH:MM', 'at YYYY-MM-DD HH:MM', or 'cron min hour dom month dow'"
            )

        # 1. daily / weekdays
        if clean.startswith(("daily ", "weekdays ")):
            kind, time_str = clean.split(maxsplit=1)
            hh, mm = map(int, time_str.split(":"))
            return cls(kind, hour=hh, minute=mm)

        # 2. every Nh / every Nm
        if clean.startswith("every "):
            unit = clean[6:].strip()
            if unit.endswith("h"):
                return cls("every", hours=int(unit[:-1]))
            if unit.endswith("m"):
                return cls("every", minutes=int(unit[:-1]))

        # 3. cron
        if clean.startswith("cron "):
            expr = clean[5:].strip()
            # Validate expression
            parts = expr.split()
            if len(parts) != 5:
                raise ValueError("cron must have exactly 5 fields")
            _parse_cron_field(parts[0], 0, 59)
            _parse_cron_field(parts[1], 0, 23)
            _parse_cron_field(parts[2], 1, 31)
            _parse_cron_field(parts[3], 1, 12)
            _parse_cron_field(parts[4], 0, 7, is_dow=True)
            return cls("cron", cron_expr=expr)

        # 4. in <N>s|m|h|d (one-off reminder)
        if clean.startswith("in "):
            ref = now or datetime.now(UTC)
            qty_unit = clean[3:].strip()
            unit = qty_unit[-1]
            qty = int(qty_unit[:-1])
            delta = {
                "s": timedelta(seconds=qty),
                "m": timedelta(minutes=qty),
                "h": timedelta(hours=qty),
                "d": timedelta(days=qty),
            }[unit]
            return cls("once", target=(ref + delta).astimezone(UTC))

        # 5. at HH:MM (one-off today/tomorrow)
        parts = clean.split()
        if clean.startswith("at ") and len(parts) == 2 and ":" in parts[1] and len(parts[1]) == 5:
            ref = now or datetime.now(UTC)
            zone = tz or (ref.tzinfo if isinstance(ref.tzinfo, zoneinfo.ZoneInfo) else UTC)
            local = ref.astimezone(zone)
            hh, mm = map(int, parts[1].split(":"))
            cand = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand <= local:
                cand = cand + timedelta(days=1)
            return cls("once", target=cand.astimezone(UTC))

        # 6. at/once YYYY-MM-DD ...
        if clean.startswith(("at ", "once ")):
            dt_str = clean.split(maxsplit=1)[1]
            dt = datetime.fromisoformat(dt_str.replace(" ", "T"))
            if dt.tzinfo is None:
                zone = tz or UTC
                dt = dt.replace(tzinfo=zone).astimezone(UTC)
            else:
                dt = dt.astimezone(UTC)
            return cls("once", target=dt)

        raise ValueError(f"unrecognized cadence: {text}")

    def next_after(self, after: datetime, tz: zoneinfo.ZoneInfo) -> datetime | None:
        """Next run strictly after ``after``, computed on the local wall clock, returned in UTC
        (stored timestamps are compared as UTC ISO strings). Returns None for completed one-off tasks."""
        local = after.astimezone(tz)
        if self.kind == "once":
            if self.target is not None and self.target > after:
                return self.target
            return None

        if self.kind == "cron":
            return next_after_cron(self.cron_expr, after, tz)

        if self.kind == "every":
            if self.minutes > 0:
                base = local.replace(second=0, microsecond=0)
                base_min = (base.minute // self.minutes) * self.minutes
                nxt = base.replace(minute=base_min)
                while nxt <= local:
                    nxt = nxt + timedelta(minutes=self.minutes)
                return nxt.astimezone(UTC)
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
