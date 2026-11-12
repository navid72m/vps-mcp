"""Append-only events: history + audit, never memory content (research A14)."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

ACTIONS = (
    "save",
    "confirm",
    "conflict",
    "update",
    "supersede",
    "invalidate",
    "expire",
    "close",
    "resolve_conflict",
    "archive",
    "refuse",
    "migrate",
    "generated",
)


def append(events_dir: Path, now: datetime, **event: Any) -> None:
    assert event.get("action") in ACTIONS, event.get("action")
    events_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = events_dir / f"{now.strftime('%Y-%m')}.jsonl"
    line = json.dumps({"ts": now.isoformat(timespec="seconds"), **event}, sort_keys=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)


def history(events_dir: Path, mem_id: str, limit: int = 50) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not events_dir.is_dir():
        return out
    for path in sorted(events_dir.glob("*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("id") == mem_id or ev.get("related") == mem_id:
                    out.append(ev)
    return out[-limit:]
