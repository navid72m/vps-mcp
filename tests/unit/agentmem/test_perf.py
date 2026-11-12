"""SC-007: search over 5,000 memories <= 300 ms; rebuild <= 10 s; no-op sync <= 50 ms."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_memory import Memory
from agent_memory.model import MemoryRecord

WORDS = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike "
    "november oscar papa quebec romeo sierra tango"
).split()


@pytest.mark.slow
def test_5000_memories(tmp_path: Path) -> None:
    store = tmp_path / "mem"
    mem = Memory.open(store, project="shop")
    d = store / "memories" / "project" / "shop"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(5000):
        w = [WORDS[(i * k) % len(WORDS)] for k in (1, 3, 7, 11)]
        rec = MemoryRecord(
            id=f"m_20261112_{i:08x}",
            type="semantic",
            scope="project:shop",
            subject=f"topic.{w[0]}.{i}",
            title=f"{w[0]} {w[1]} note {i}",
            content=f"Memory {i} about {' '.join(w)} and item{i}.",
            created="2026-11-01T00:00:00+00:00",
            updated="2026-11-01T00:00:00+00:00",
            last_confirmed="2026-11-01T00:00:00+00:00",
        )
        (d / f"t{i}.md").write_text(rec.to_text())
    t = time.perf_counter()
    mem.index.rebuild()
    assert time.perf_counter() - t <= 10
    t = time.perf_counter()
    mem.index.sync()
    assert time.perf_counter() - t <= 0.05
    t = time.perf_counter()
    res = mem.search("charlie hotel item42")
    assert time.perf_counter() - t <= 0.3
    assert res["total"] > 0
