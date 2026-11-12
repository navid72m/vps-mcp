"""Hybrid ranking, flags and grouping (research A5). Pure."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from agent_memory.model import GROUP_OF, MemoryRecord, parse_time

DEFAULT_WEIGHTS = {
    "keyword": 0.50,
    "recency": 0.15,
    "importance": 0.15,
    "confidence": 0.10,
    "scope": 0.10,
}
CONFIDENCE_SCORE = {"verified": 1.0, "inferred": 0.6, "unverified": 0.3}
SCOPE_SCORE = {"task": 1.0, "environment": 0.8, "project": 0.6, "user": 0.4}


def scope_kind(scope: str) -> str:
    return scope.split(":", 1)[0]


def age_days(rec: MemoryRecord, now: datetime) -> float:
    t = parse_time(rec.last_confirmed or rec.updated or rec.created)
    return max(0.0, (now - t).total_seconds() / 86400) if t else 365.0


def score(
    rec: MemoryRecord, kw: float, now: datetime, weights: dict[str, float], half_life: float = 90.0
) -> float:
    w = {**DEFAULT_WEIGHTS, **weights}
    return (
        w["keyword"] * kw
        + w["recency"] * math.exp(-age_days(rec, now) / half_life)
        + w["importance"] * rec.importance / 5
        + w["confidence"] * CONFIDENCE_SCORE.get(rec.confidence, 0.3)
        + w["scope"] * SCOPE_SCORE.get(scope_kind(rec.scope), 0.4)
    )


def normalise(raw: dict[str, float]) -> dict[str, float]:
    if not raw:
        return {}
    hi, lo = max(raw.values()), min(raw.values())
    if hi == lo:
        return {k: 1.0 for k in raw}
    return {k: 0.2 + 0.8 * (v - lo) / (hi - lo) for k, v in raw.items()}


def human_age(days: float) -> str:
    if days < 1:
        return "today"
    if days < 60:
        return f"{int(days)}d"
    return f"{int(days // 30)}mo"


def flags(rec: MemoryRecord, now: datetime, default_fresh: int) -> list[str]:
    out = []
    if rec.live_state:
        out.append("live_state: verify")
        stale = rec.stale_days(now, default_fresh)
        if stale is not None:
            out.append(f"possibly stale ({stale}d)")
    if rec.needs_verification or rec.conflicts_with:
        out.append("conflict")
    if rec.confidence != "verified":
        out.append(rec.confidence)
    if rec.state == "open":
        out.append("open")
    status = rec.effective_status(now)
    if status != "active":
        out.append(status)
    return out


def item(rec: MemoryRecord, now: datetime, default_fresh: int) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": rec.id,
        "subject": rec.subject,
        "title": rec.title,
        "content": rec.content,
        "confidence": rec.confidence,
        "age": human_age(age_days(rec, now)),
        "source": rec.source_kind,
        "scope": rec.scope,
    }
    f = flags(rec, now, default_fresh)
    if f:
        d["flags"] = f
    if rec.type == "decision" and rec.alternatives:
        d["rejected_alternatives"] = rec.alternatives
    return d


def group(items: list[tuple[MemoryRecord, dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for rec, it in items:
        out.setdefault(GROUP_OF[rec.type], []).append(it)
    return out
