"""Progress-file parsing (research J4). Pure functions.

Grammar: ``[YYYY-MM-DD HH:MM:SS ]<text>[ | free <N>]``; outcome rules are applied in order.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

_TS_RE = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)\s+(?P<rest>.*)$")
_FREE_RE = re.compile(r"\s*\|\s*free\s+(?P<free>\S+)\s*$")
_EXIT_RE = re.compile(r"\b(?:EXIT|exit)\s+(-?\d+)\b|\brc=(-?\d+)\b")
_FAILED_RE = re.compile(r"\bFAILED\b")
_OK_RE = re.compile(r"\bok\b")
_FAIL_COUNT_RE = re.compile(r"\b(\d+)\s+FAIL\b")


@dataclass
class ProgressLine:
    ts: str | None
    text: str
    step: str | None = None
    outcome: str | None = None  # ok | failed | done | None
    disk_free: str | None = None
    reported_failures: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _step_before(text: str, pos: int) -> str | None:
    head = text[:pos].strip().rstrip(":,").strip()
    return head or None


def parse_line(
    raw: str, marker: str = "ALL DONE", patterns: list[re.Pattern[str]] | None = None
) -> ProgressLine:
    line = raw.rstrip("\r\n")
    ts = None
    m = _TS_RE.match(line)
    if m:
        ts, line = m["ts"], m["rest"]
    free = None
    f = _FREE_RE.search(line)
    if f:
        free, line = f["free"], line[: f.start()]
    text = line.strip()
    pl = ProgressLine(ts=ts, text=text, disk_free=free)
    fc = _FAIL_COUNT_RE.search(text)
    if fc and int(fc.group(1)) > 0:
        pl.reported_failures = int(fc.group(1))
    for rx in patterns or []:
        pm = rx.search(text)
        if pm:
            gd = pm.groupdict()
            pl.step = gd.get("step") or pl.step
            status = gd.get("status")
            if status in ("ok", "failed", "done"):
                pl.outcome = status
            return pl
    if marker and marker in text:
        pl.outcome = "done"
        return pl
    if text.startswith("STOP"):
        pl.outcome = "failed"
        pl.step = "STOP"
        return pl
    em = _EXIT_RE.search(text)
    if em:
        code = int(em.group(1) if em.group(1) is not None else em.group(2))
        pl.outcome = "ok" if code == 0 else "failed"
        pl.step = _step_before(text, em.start())
        return pl
    fm = _FAILED_RE.search(text)
    if fm:
        pl.outcome = "failed"
        pl.step = _step_before(text, fm.start())
        return pl
    om = _OK_RE.search(text)
    if om:
        pl.outcome = "ok"
        pl.step = _step_before(text, om.start())
    return pl


def parse(
    text: str, marker: str = "ALL DONE", patterns: list[str] | None = None
) -> list[ProgressLine]:
    compiled = [re.compile(p) for p in patterns or []]
    return [parse_line(ln, marker, compiled) for ln in text.splitlines() if ln.strip()]
