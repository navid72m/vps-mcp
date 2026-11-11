"""Compact shapes, caps, masking and the external-content envelope (research G5, G6)."""

from __future__ import annotations

import hashlib
from typing import Any

from vps_mcp.memory.masking import mask_note

FILE_MAX_BYTES = 100 * 1024
LIST_MAX = 200
COMMITS_MAX = 50
COMPARE_FILES_MAX = 50
DIFF_LINES_MAX = 2000
NOTES_MAX = 50
NOTE_MAX_BYTES = 4096
LOG_LINES_DEFAULT = 200
LOG_LINES_MAX = 2000
TEXT_MAX = 16 * 1024  # issue / MR descriptions


class Masker:
    def __init__(self, globs: list[str], secrets: list[str]) -> None:
        self.globs = globs
        self.secrets = secrets

    def __call__(self, text: str) -> str:
        return mask_note(text, self.globs, self.secrets)[0]


def cap_text(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    return raw[:max_bytes].decode("utf-8", errors="ignore"), True


def external(
    mask: Masker,
    text: str | None,
    *,
    author: str | None = None,
    created_at: str | None = None,
    max_bytes: int = TEXT_MAX,
) -> dict[str, Any]:
    body, truncated = cap_text(mask(text or ""), max_bytes)
    out: dict[str, Any] = {"kind": "external_content", "text": body}
    if author:
        out["author"] = author
    if created_at:
        out["created_at"] = created_at
    if truncated:
        out["truncated"] = True
    return out


def is_binary(data: bytes) -> bool:
    if b"\x00" in data[:8192]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def binary_summary(data: bytes) -> dict[str, Any]:
    return {"binary": True, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def day(ts: str | None) -> str:
    return (ts or "")[:16].replace("T", " ")


def commit_line(c: dict[str, Any]) -> str:
    return (
        f"{str(c.get('id', ''))[:12]} {day(c.get('committed_date') or c.get('created_at'))} "
        f"{c.get('author_name', '?')}: {str(c.get('title', '')).strip()[:120]}"
    )


def duration(seconds: Any) -> str:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "-"
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def pipeline_line(p: dict[str, Any], failed: list[str] | None = None) -> str:
    line = (
        f"{p.get('id')} {p.get('status')} {p.get('ref')} {str(p.get('sha', ''))[:12]} "
        f"{duration(p.get('duration'))}"
    )
    return line + (f" failed:[{', '.join(failed)}]" if failed else "")


def item_line(it: dict[str, Any]) -> str:
    labels = ",".join(it.get("labels") or [])
    author = (it.get("author") or {}).get("username", "?")
    return (
        f"#{it.get('iid')} [{it.get('state')}] {str(it.get('title', ''))[:120]} "
        f"({author}, {day(it.get('updated_at'))}" + (f", {labels}" if labels else "") + ")"
    )
