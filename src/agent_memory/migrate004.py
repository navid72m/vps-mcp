"""Migrate a vps-mcp feature-004 memory folder into agent-memory (research A12)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from agent_memory import frontmatter
from agent_memory.errors import AgentMemoryError
from agent_memory.files import Files
from agent_memory.model import normalize_subject, slug
from agent_memory.store import Memory

TYPE_MAP = {
    "decision": ("decision", None, None),
    "lesson": ("episodic", None, None),
    "fact": ("semantic", None, None),
    "convention": ("procedural", None, None),
    "preference": ("semantic", "user", None),
    "issue": ("episodic", None, "open"),
    "next_step": ("episodic", None, "open"),
}
PART_BYTES = 1900
LINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9-]{0,63})\]\]")


def plan(folder: Path, project: str, environment: str | None) -> list[dict[str, Any]]:
    notes = folder / "notes"
    if not notes.is_dir():
        raise AgentMemoryError(
            "not_found", f"{folder} has no notes/ folder (not a feature-004 memory folder?)"
        )
    out: list[dict[str, Any]] = []
    for path in sorted(notes.glob("*.md")):
        if path.is_symlink():
            continue
        try:
            header, body = frontmatter.parse(Files.read(path))
        except (OSError, UnicodeDecodeError, frontmatter.FrontMatterError) as exc:
            out.append({"note": path.name, "error": str(exc)})
            continue

        def s(k: str, default: str = "", header: frontmatter.Header = header) -> str:
            v = header.get(k)
            return v if isinstance(v, str) and v else default

        old_type = s("type", "fact")
        new_type, scope_override, state = TYPE_MAP.get(old_type, ("semantic", None, None))
        if state == "open" and s("status") == "done":
            state = "done"
        name = path.stem
        scope = scope_override or f"project:{project}"
        tags = header.get("tags") or []
        source = s("source", "operator")
        out.append(
            {
                "note": path.name,
                "name": name,
                "old_type": old_type,
                "type": new_type,
                "scope": scope,
                "subject": normalize_subject(name.replace("-", ".")),
                "state": state,
                "title": s("title", name.replace("-", " "))[:120],
                "content": body.strip() or s("title", name),
                "tags": [t for t in tags if isinstance(t, str)][:10]
                if isinstance(tags, list)
                else [],
                "created": s("created"),
                "updated": s("updated"),
                "confidence": "verified" if source == "operator" else "unverified",
                "old_source": source,
                "links": LINK_RE.findall(body),
            }
        )
    return out


def split(content: str, limit: int = PART_BYTES) -> list[str]:
    """Split long content on paragraph boundaries into parts of at most ``limit`` bytes."""
    if len(content.encode()) <= limit:
        return [content]
    parts: list[str] = []
    cur = ""
    for para in re.split(r"\n\s*\n", content):
        para = para.strip()
        while len(para.encode()) > limit:  # a single huge paragraph: cut at line ends
            cut = para.encode()[:limit].decode("utf-8", "ignore")
            nl = cut.rfind("\n")
            cut = cut[:nl] if nl > limit // 2 else cut
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(cut.strip())
            para = para[len(cut) :].strip()
        candidate = f"{cur}\n\n{para}" if cur else para
        if len(candidate.encode()) > limit:
            parts.append(cur)
            cur = para
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    return [x for x in parts if x]


def apply(mem: Memory, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name: dict[str, str] = {}
    results = []
    for it in items:
        if "error" in it:
            results.append({"note": it["note"], "outcome": "skipped", "reason": it["error"]})
            continue
        chunks = split(it["content"])
        ids: list[str] = []
        for n, chunk in enumerate(chunks, start=1):
            multi = len(chunks) > 1
            res = mem.save(
                {
                    "type": it["type"],
                    "scope": it["scope"],
                    "subject": f"{it['subject']}.part{n}" if multi else it["subject"],
                    "title": (f"{it['title'][:108]} ({n}/{len(chunks)})" if multi else it["title"]),
                    "content": chunk,
                    "tags": it["tags"],
                    "state": it["state"],
                    "source_kind": "migration",
                    "source_ref": f"004:{it['name']}",
                    "confidence": it["confidence"],
                }
            )
            rec = mem._load(res["id"])
            if res["outcome"] == "created":
                rec.created = it["created"] or rec.created
                rec.updated = it["updated"] or rec.updated
                rec.last_confirmed = rec.updated
                rec.source_client = it["old_source"]
                mem._write(rec)
            ids.append(res["id"])
            results.append(
                {
                    "note": it["note"] + (f" part {n}/{len(chunks)}" if multi else ""),
                    "outcome": res["outcome"],
                    "id": res["id"],
                }
            )
        if len(ids) > 1:  # parts of one note know each other
            for i in ids:
                rec = mem._load(i)
                rec.related = list(dict.fromkeys([*rec.related, *(x for x in ids if x != i)]))
                mem._write(rec)
        by_name[it["name"]] = ids[0]
    for it in items:
        if "error" in it or not it.get("links"):
            continue
        rec = mem._load(by_name[it["name"]])
        targets = [by_name[n] for n in it["links"] if n in by_name and by_name[n] != rec.id]
        if targets:
            rec.related = list(dict.fromkeys([*rec.related, *targets]))
            mem._write(rec)
    mem._event("migrate", None, reason=f"{len(results)} note(s) from feature 004")
    return results


def describe(items: list[dict[str, Any]]) -> list[str]:
    lines = []
    for it in items:
        if "error" in it:
            lines.append(f"SKIP {it['note']}: {it['error']}")
            continue
        state = f" [{it['state']}]" if it["state"] else ""
        n = len(split(it["content"]))
        parts = f" (split into {n} parts)" if n > 1 else ""
        lines.append(
            f"{it['note']}: {it['old_type']} -> {it['type']}{state} {it['scope']} {it['subject']}"
            f"{parts}"
        )
    return lines


def unchanged_snapshot(folder: Path) -> dict[str, float]:
    return {str(p): os.stat(p).st_mtime for p in folder.rglob("*") if p.is_file()}


__all__ = ["apply", "describe", "plan", "slug"]
