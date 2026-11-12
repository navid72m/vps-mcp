"""Model, files, events, index (T003-T006)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agent_memory import AgentMemoryError, Memory
from agent_memory.files import Files
from agent_memory.model import MemoryRecord, normalize_subject, parse_scope, scope_dir
from tests.unit.agentmem.conftest import fact


def test_scope_and_subject_rules() -> None:
    assert parse_scope("user") == ("user", None)
    assert scope_dir("project:shop") == "project/shop"
    for bad in ("project:", "project:Shop", "org:x", "project:../x", "environment:a/b"):
        with pytest.raises(AgentMemoryError):
            parse_scope(bad)
    assert normalize_subject("Postgres Version") == "postgres.version"
    assert normalize_subject("deploy/order") == "deploy.order"
    with pytest.raises(AgentMemoryError):
        normalize_subject("../..")


def test_record_round_trip() -> None:
    rec = MemoryRecord(
        id="m_20261112_abcdef12",
        type="decision",
        scope="project:shop",
        subject="upgrade.strategy",
        title="Stage it",
        content="Upgrade in stages.\n\nRationale: safer.",
        alternatives=["jump, straight"],
        tags=["db"],
        pinned=True,
    )
    again = MemoryRecord.from_text("memories/project/shop/x.md", rec.to_text(), 0)
    assert again.alternatives == ["jump, straight"] and again.pinned is True
    assert again.content == rec.content and again.extra == {}


def test_hand_written_file_defaults(store: Path) -> None:
    d = store / "memories" / "user"
    d.mkdir(parents=True)
    (d / "note.md").write_text("---\ntitle: Hand note\n---\nWritten by hand.\n")
    m = Memory.open(store, project="shop")
    res = m.search("hand note")
    item = res["groups"]["facts"][0]
    assert item["title"] == "Hand note" and item["source"] == "operator"


def test_store_layout_modes_and_files(mem: Memory, store: Path) -> None:
    res = mem.save(fact())
    for d in ("", "memories", "archive", "events"):
        assert stat.S_IMODE((store / d).stat().st_mode) == 0o700
    path = store / res["path_hint"]
    assert res["path_hint"].startswith("memories/project/shop/postgres-version.")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(next((store / "events").glob("*.jsonl")).stat().st_mode) == 0o600
    events = next((store / "events").glob("*.jsonl")).read_text()
    assert "PostgreSQL 16" not in events  # events never hold content


def test_symlinks_are_ignored(mem: Memory, store: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("---\ntitle: evil\nscope: user\n---\nsecret stuff\n")
    mem.files.ensure()
    (store / "memories" / "user").mkdir(parents=True, exist_ok=True)
    (store / "memories" / "user" / "evil.md").symlink_to(outside)
    assert mem.search("evil secret stuff")["total"] == 0
    assert mem.doctor()["warnings"]


def test_index_incremental_and_rebuild(mem: Memory, store: Path) -> None:
    res = mem.save(fact())
    path = store / res["path_hint"]
    path.write_text(path.read_text().replace("PostgreSQL 16", "PostgreSQL 16 with a twist"))
    assert "twist" in mem.search("twist")["groups"]["facts"][0]["content"]
    path.unlink()
    assert mem.search("postgres")["total"] == 0
    (store / "index.sqlite").write_bytes(b"garbage")
    for suffix in ("-wal", "-shm"):
        p = Path(str(store / "index.sqlite") + suffix)
        if p.exists():
            p.unlink()
    m2 = Memory.open(store, project="shop")
    m2.save(fact(subject="redis.version", title="Redis 7", content="Redis runs version 7."))
    assert m2.search("redis")["total"] == 1


def test_malformed_file_reported(mem: Memory, store: Path) -> None:
    mem.save(fact())
    bad = store / "memories" / "project" / "shop" / "broken.md"
    bad.write_text("---\ntype: rumour\n---\nx\n")
    assert mem.search("postgres")["total"] == 1
    assert any("broken.md" in m["path"] for m in mem.doctor()["malformed"])


def test_like_fallback_without_fts(mem: Memory) -> None:
    mem.save(fact())
    mem.index.fts = False
    assert mem.search("postgres")["total"] == 1


def test_files_path_escape(store: Path) -> None:
    f = Files(store)
    f.ensure()
    with pytest.raises(AgentMemoryError):
        f.abs("../../etc/passwd")
    assert os.access(store, os.W_OK)
