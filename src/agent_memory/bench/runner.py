"""Benchmark runner: recall, precision, stale hits, conflict flags, token cost (A13)."""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_memory.bench import fixtures
from agent_memory.index import row_to_record
from agent_memory.model import iso
from agent_memory.store import Memory

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
CONFIGS = ("none", "naive", "ranked")


def tokens(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False)) // 4


def load(root: Path, data: dict[str, Any]) -> dict[str, str]:
    """Write the fixture memories into a fresh store; returns key -> memory id."""
    keys: dict[str, str] = {}
    projects = sorted(
        {m["scope"].split(":")[1] for m in data["memories"] if m["scope"].startswith("project:")}
    )
    for m in data["memories"]:
        kind, _, name = m["scope"].partition(":")
        mem = Memory(
            root,
            project=name if kind == "project" else projects[0],
            environment=name if kind == "environment" else None,
            client="bench",
            now=lambda: NOW - timedelta(days=10),
            allow_scopes=["*"],
        )
        raw = {k: m[k] for k in ("type", "scope", "subject", "title", "content")}
        raw.update(
            {
                k: m[k]
                for k in ("importance", "pinned", "alternatives", "state", "tags", "live_state")
                if k in m
            }
        )
        raw["source_kind"] = m.get("source_kind", "task")
        if m.get("expires") == "past":
            raw["expires"] = iso(NOW - timedelta(days=1))
        res = mem.save(raw)
        keys[m["key"]] = res["id"]
    # lifecycle states after the fact
    mem = Memory(root, client="bench", now=lambda: NOW - timedelta(days=9), allow_scopes=["*"])
    for m in data["memories"]:
        if m.get("superseded_by"):
            mem.update(
                keys[m["superseded_by"]],
                "resolve_conflict",
                keep=keys[m["superseded_by"]],
                reason="bench: newer fact",
            )
        if m.get("status") == "invalidated":
            mem.update(keys[m["key"]], "invalidate", reason="bench")
        if m.get("stale"):
            rec = mem._load(keys[m["key"]])
            rec.last_confirmed = iso(NOW - timedelta(days=30))
            mem._write(rec)
    return keys


def naive(mem: Memory, query: str, k: int) -> list[str]:
    words = [w for w in query.lower().split() if len(w) > 3]
    rows = [row_to_record(r) for r in mem.index.all_rows()]
    hits = [r for r in rows if any(w in (r.title + " " + r.content).lower() for w in words)]
    hits.sort(key=lambda r: r.updated, reverse=True)
    return [r.id for r in hits[:k]]


def run(k: int = 8, data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data or fixtures.build()
    tmp = Path(tempfile.mkdtemp(prefix="agent-memory-bench-"))
    try:
        keys = load(tmp, data)
        inv = {v: k_ for k_, v in keys.items()}
        conflict_groups: dict[str, set[str]] = {}
        for m in data["memories"]:
            if m.get("conflict_group"):
                conflict_groups.setdefault(m["conflict_group"], set()).add(m["key"])
        results: dict[str, Any] = {}
        for cfg in CONFIGS:
            rec_sum = prec_sum = 0.0
            prec_n = 0
            stale_hits = stale_total = 0
            conflict_ok = conflict_total = 0
            tok = 0
            for t in data["tasks"]:
                scope = t["scope"]
                mem = Memory(
                    tmp,
                    project=scope["project"],
                    environment=scope.get("environment"),
                    client="bench",
                    now=lambda: NOW,
                    read_only=True,
                )
                flagged: dict[str, list[str]] = {}
                if cfg == "none":
                    got: list[str] = []
                    payload: Any = {}
                elif cfg == "naive":
                    got = naive(mem, t["text"], k)
                    payload = got
                else:
                    res = mem.search(t["text"], limit=k)
                    payload = res
                    got = []
                    for items in res["groups"].values():
                        for it in items:
                            got.append(it["id"])
                            flagged[it["id"]] = it.get("flags", [])
                tok += tokens(payload) if cfg != "none" else 0
                got_keys = [inv.get(i, "?") for i in got]
                exp, forb = set(t["expected"]), set(t["forbidden"])
                if exp:
                    rec_sum += len(exp & set(got_keys)) / len(exp)
                if got_keys:
                    prec_sum += len(exp & set(got_keys)) / len(got_keys)
                    prec_n += 1
                stale_total += len(forb)
                stale_hits += len(forb & set(got_keys))
                for grp in conflict_groups.values():
                    if grp <= exp:
                        for key in grp:
                            conflict_total += 1
                            if key in got_keys and "conflict" in flagged.get(keys[key], []):
                                conflict_ok += 1
            n_exp = sum(1 for t in data["tasks"] if t["expected"])
            results[cfg] = {
                "recall_at_k": round(rec_sum / n_exp, 3),
                "precision_at_k": round(prec_sum / prec_n, 3) if prec_n else 0.0,
                "stale_hit_rate": round(stale_hits / stale_total, 3) if stale_total else 0.0,
                "contradiction_flag_rate": round(conflict_ok / conflict_total, 3)
                if conflict_total
                else 0.0,
                "avg_result_tokens": round(tok / len(data["tasks"]), 1),
            }
        core = Memory(
            tmp, project="shop", environment="prod", client="bench", now=lambda: NOW, read_only=True
        ).pinned_core()
        return {
            "k": k,
            "tasks": len(data["tasks"]),
            "memories": len(data["memories"]),
            "configs": results,
            "pinned_core_tokens": len(core) // 4,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def table(report: dict[str, Any]) -> str:
    cols = (
        "recall_at_k",
        "precision_at_k",
        "stale_hit_rate",
        "contradiction_flag_rate",
        "avg_result_tokens",
    )
    head = f"{'config':8} " + " ".join(f"{c:>24}" for c in cols)
    lines = [
        f"agent-memory benchmark: {report['memories']} memories, {report['tasks']} tasks, "
        f"k={report['k']}",
        head,
    ]
    for cfg, r in report["configs"].items():
        lines.append(f"{cfg:8} " + " ".join(f"{r[c]:>24}" for c in cols))
    lines.append(f"pinned core: ~{report['pinned_core_tokens']} tokens")
    return "\n".join(lines)
