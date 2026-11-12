from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_memory import Memory


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: Any) -> None:
        self.t += timedelta(**kw)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "mem"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def mem(store: Path, clock: Clock) -> Memory:
    return Memory.open(store, project="shop", environment="prod", client="test", now=clock)


def fact(**kw: Any) -> dict[str, Any]:
    base = {
        "type": "semantic",
        "subject": "postgres.version",
        "title": "Production runs PG 16",
        "content": "The production database runs PostgreSQL 16.",
    }
    base.update(kw)
    return base
