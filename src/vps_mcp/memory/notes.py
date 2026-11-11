"""Note model, name/tag rules and links (data-model.md §2). Pure."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.memory import frontmatter

NOTE_TYPES = ("decision", "lesson", "issue", "convention", "next_step", "preference", "fact")
OPEN_BY_DEFAULT = ("issue", "next_step")
STATUSES = ("open", "done")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
LINK_RE = re.compile(r"\[\[([^\[\]\n]{1,100})\]\]")
MAX_TAGS = 10
MAX_TITLE = 120
_KNOWN = {"name", "type", "title", "tags", "status", "created", "updated", "source"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def invalid(msg: str, **extra: object) -> VpsMcpError:
    return VpsMcpError(ErrorCategory.INVALID_INPUT, msg, extra=extra or None)


def validate_name(name: object) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise invalid(
            "note name must be 1-64 chars of a-z, 0-9 and '-', starting with a letter or digit"
        )
    return name


def validate_type(t: object) -> str:
    if t not in NOTE_TYPES:
        raise invalid(f"type must be one of {', '.join(NOTE_TYPES)}")
    return str(t)


def validate_title(title: object) -> str:
    if not isinstance(title, str) or not title.strip():
        raise invalid("title is required")
    title = title.strip()
    if "\n" in title or "\r" in title or len(title) > MAX_TITLE:
        raise invalid(f"title must be a single line of at most {MAX_TITLE} characters")
    return title


def validate_tags(tags: object) -> list[str]:
    if tags is None:
        return []
    if not isinstance(tags, list) or len(tags) > MAX_TAGS:
        raise invalid(f"tags must be a list of at most {MAX_TAGS} items")
    out: list[str] = []
    for t in tags:
        if not isinstance(t, str) or not TAG_RE.fullmatch(t):
            raise invalid("each tag must be 1-32 chars of a-z, 0-9, '_' and '-'")
        if t not in out:
            out.append(t)
    return out


def validate_status(status: object) -> str | None:
    if status is None:
        return None
    if status not in STATUSES:
        raise invalid("status must be 'open' or 'done'")
    return str(status)


def slugify(title: str) -> str:
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:64].rstrip("-")
    return s or "note"


def links(body: str) -> list[tuple[str, bool]]:
    """``[[target]]`` links in order of first appearance, with whether the target is valid."""
    seen: dict[str, bool] = {}
    for m in LINK_RE.finditer(body):
        target = m.group(1).strip()
        if target not in seen:
            seen[target] = bool(NAME_RE.fullmatch(target))
    return list(seen.items())


@dataclass
class Note:
    name: str
    type: str
    title: str
    body: str = ""
    tags: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    source: str = "operator"
    status: str | None = None
    extra: dict[str, str | list[str]] = field(default_factory=dict)

    @property
    def effective_status(self) -> str | None:
        if self.status:
            return self.status
        return "open" if self.type in OPEN_BY_DEFAULT else None

    @property
    def summary(self) -> str:
        for line in self.body.splitlines():
            line = line.strip().lstrip("#>-* ").strip()
            if line:
                return line[:160]
        return ""

    @classmethod
    def from_text(cls, file_stem: str, text: str, mtime: float) -> Note:
        """Parse a note file. Lenient on hand-written defaults; raises on a broken header."""
        header, body = frontmatter.parse(text)
        stamp = datetime.fromtimestamp(mtime, UTC).isoformat(timespec="seconds")

        def s(key: str, default: str = "") -> str:
            v = header.get(key)
            return v if isinstance(v, str) and v else default

        raw_tags = header.get("tags", [])
        tags = [t for t in (raw_tags if isinstance(raw_tags, list) else [raw_tags]) if t]
        ntype = s("type", "fact")
        if ntype not in NOTE_TYPES:
            raise frontmatter.FrontMatterError(f"unknown type '{ntype}'")
        status = s("status") or None
        return cls(
            name=s("name", file_stem),
            type=ntype,
            title=s("title", file_stem.replace("-", " ")),
            body=body.rstrip("\n"),
            tags=tags,
            created=s("created", stamp),
            updated=s("updated", stamp),
            source=s("source", "operator"),
            status=status if status in STATUSES else None,
            extra={k: v for k, v in header.items() if k not in _KNOWN},
        )

    def to_text(self) -> str:
        header: frontmatter.Header = {
            "name": self.name,
            "type": self.type,
            "title": self.title,
            "tags": list(self.tags),
            "created": self.created,
            "updated": self.updated,
            "source": self.source,
        }
        if self.status:
            header["status"] = self.status
        header.update(self.extra)
        return frontmatter.render(header, self.body)

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "kind": "recorded_memory",
            "name": self.name,
            "type": self.type,
            "title": self.title,
            "tags": self.tags,
            "created": self.created,
            "updated": self.updated,
            "source": self.source,
            "body": self.body,
        }
        if self.effective_status:
            d["status"] = self.effective_status
        return d
