"""Note search: scoring and snippets (data-model.md §6). Pure."""

from __future__ import annotations

import re
from typing import Any

from vps_mcp.memory.notes import Note

SNIPPET = 160


def _terms(query: str) -> list[str]:
    return [t for t in re.split(r"\s+", query.lower().strip()) if t][:20]


def snippet(body: str, terms: list[str]) -> str:
    flat = " ".join(body.split())
    low = flat.lower()
    pos = min((p for p in (low.find(t) for t in terms) if p >= 0), default=-1)
    if pos < 0:
        return flat[:SNIPPET]
    start = max(0, pos - SNIPPET // 3)
    out = flat[start : start + SNIPPET]
    return ("…" if start else "") + out


def score(note: Note, query: str, terms: list[str]) -> int:
    s = 0
    if query and note.name == query.strip().lower():
        s += 10
    title = note.title.lower()
    tags = [t.lower() for t in note.tags]
    body = note.body.lower()
    for t in terms:
        if t in title:
            s += 4
        if any(t in tag for tag in tags):
            s += 3
        s += min(body.count(t), 5)
    return s


def search(
    notes: list[Note],
    query: str = "",
    type: str | None = None,
    tag: str | None = None,
    limit: int = 10,
) -> tuple[list[dict[str, Any]], int]:
    terms = _terms(query)
    hits: list[tuple[int, Note]] = []
    for n in notes:
        if type and n.type != type:
            continue
        if tag and tag not in n.tags:
            continue
        sc = score(n, query, terms) if terms else 1
        if terms and sc == 0:
            continue
        hits.append((sc, n))
    hits.sort(key=lambda h: h[1].updated, reverse=True)  # stable: newest first among ties
    hits.sort(key=lambda h: -h[0])
    items = [
        {
            "name": n.name,
            "type": n.type,
            "title": n.title,
            "tags": n.tags,
            "updated": n.updated,
            "snippet": snippet(n.body, terms),
            "score": sc,
        }
        for sc, n in hits[:limit]
    ]
    return items, len(hits)
