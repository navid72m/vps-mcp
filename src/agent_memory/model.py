"""Memory records and field rules (data-model.md §1). Pure."""

from __future__ import annotations

import hashlib
import re
import secrets as _secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_memory import frontmatter
from agent_memory.errors import invalid

TYPES = ("episodic", "semantic", "procedural", "decision")
STATUSES = ("active", "superseded", "expired", "invalidated", "archived")
CONFIDENCE = ("verified", "inferred", "unverified")
TRUSTED_SOURCES = ("client", "task", "command", "operator", "migration", "tool_generated")
UNTRUSTED_SOURCES = ("file", "web", "issue", "tool_output", "email", "unknown")
SOURCE_KINDS = TRUSTED_SOURCES + UNTRUSTED_SOURCES
SCOPE_KINDS = ("project", "environment", "task")
GROUP_OF = {
    "semantic": "facts",
    "decision": "decisions",
    "episodic": "experiences",
    "procedural": "procedures",
}

SUBJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
SCOPE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
ID_RE = re.compile(r"^m_\d{8}_[0-9a-f]{8}$")
MAX_CONTENT = 2048
MAX_TITLE = 120

_LIST = ("conflicts_with", "related", "tags", "alternatives")
_BOOL = ("pinned", "live_state", "needs_verification")
_INT = ("importance", "confirmations", "fresh_days")
ORDER = (
    "id",
    "type",
    "scope",
    "subject",
    "title",
    "confidence",
    "importance",
    "status",
    "pinned",
    "live_state",
    "state",
    "source_kind",
    "source_ref",
    "source_client",
    "created",
    "updated",
    "last_confirmed",
    "confirmations",
    "expires",
    "fresh_days",
    "needs_verification",
    "supersedes",
    "superseded_by",
    "conflicts_with",
    "related",
    "tags",
    "generated_key",
    "alternatives",
)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = datetime.fromisoformat(value + "T00:00:00")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_scope(scope: str) -> tuple[str, str | None]:
    if scope == "user":
        return "user", None
    kind, _, name = scope.partition(":")
    if kind not in SCOPE_KINDS or not SCOPE_NAME_RE.fullmatch(name):
        raise invalid(
            "scope must be 'user' or 'project:<name>', 'environment:<name>', 'task:<name>' "
            "with a lower-case name"
        )
    return kind, name


def scope_dir(scope: str) -> str:
    kind, name = parse_scope(scope)
    return "user" if name is None else f"{kind}/{name}"


def normalize_subject(subject: str) -> str:
    if ".." in subject or subject.strip().startswith(("/", ".")) or "\x00" in subject:
        raise invalid("subject must be a short dotted key like 'postgres.version'")
    s = unicodedata.normalize("NFKD", subject).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[\s/:]+", ".", s.strip())
    s = re.sub(r"[^a-z0-9._-]", "", s).strip(".-_")
    s = re.sub(r"\.{2,}", ".", s)
    if not SUBJECT_RE.fullmatch(s):
        raise invalid("subject must be a short dotted key like 'postgres.version'")
    return s


def slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].rstrip("-")
    return s or "memory"


def new_id(now: datetime) -> str:
    return f"m_{now.strftime('%Y%m%d')}_{_secrets.token_hex(4)}"


def file_name(subject: str, mem_id: str) -> str:
    return f"{slug(subject)}.{mem_id[-8:]}.md"


@dataclass
class MemoryRecord:
    id: str
    type: str
    scope: str
    subject: str
    title: str
    content: str
    confidence: str = "verified"
    importance: int = 3
    status: str = "active"
    pinned: bool = False
    live_state: bool = False
    state: str | None = None
    source_kind: str = "client"
    source_ref: str | None = None
    source_client: str | None = None
    created: str = ""
    updated: str = ""
    last_confirmed: str = ""
    confirmations: int = 1
    expires: str | None = None
    fresh_days: int | None = None
    needs_verification: bool = False
    supersedes: str | None = None
    superseded_by: str | None = None
    conflicts_with: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    generated_key: str | None = None
    alternatives: list[str] = field(default_factory=list)
    path: str = ""  # relative to the store root
    extra: dict[str, Any] = field(default_factory=dict)

    # --------------------------------------------------------------- derived

    def effective_status(self, now: datetime) -> str:
        if self.status == "active":
            exp = parse_time(self.expires)
            if exp is not None and exp <= now:
                return "expired"
        return self.status

    def stale_days(self, now: datetime, default_fresh: int) -> int | None:
        if not self.live_state:
            return None
        seen = parse_time(self.last_confirmed or self.updated)
        if seen is None:
            return None
        age = (now - seen).days
        return age if age > (self.fresh_days or default_fresh) else None

    def summary(self, limit: int = 160) -> str:
        first = self.content.strip().split("\n", 1)[0]
        return first[:limit]

    # ------------------------------------------------------------ serialise

    def to_text(self) -> str:
        header: frontmatter.Header = {}
        for k in ORDER:
            v = getattr(self, k)
            if k in _LIST:
                header[k] = list(v)
            elif k in _BOOL:
                header[k] = "true" if v else "false"
            elif v is None:
                header[k] = ""
            else:
                header[k] = str(v)
        header.update(self.extra)
        return frontmatter.render(header, self.content)

    @classmethod
    def from_text(cls, rel_path: str, text: str, mtime: float) -> MemoryRecord:
        header, body = frontmatter.parse(text)
        stamp = iso(datetime.fromtimestamp(mtime, UTC))

        def s(key: str, default: str | None = None) -> str | None:
            v = header.get(key)
            return v if isinstance(v, str) and v != "" else default

        def lst(key: str) -> list[str]:
            v = header.get(key)
            return [x for x in v if x] if isinstance(v, list) else ([v] if v else [])

        mem_id = s("id") or "m_00000000_" + hashlib.sha256(rel_path.encode()).hexdigest()[:8]
        mtype = s("type", "semantic") or "semantic"
        if mtype not in TYPES:
            raise frontmatter.FrontMatterError(f"unknown type '{mtype}'")
        status = s("status", "active") or "active"
        if status not in STATUSES:
            raise frontmatter.FrontMatterError(f"unknown status '{status}'")
        scope = s("scope", "user") or "user"
        parse_scope(scope)
        subject = s("subject") or rel_path.rsplit("/", 1)[-1].split(".", 1)[0]
        known = set(ORDER)
        return cls(
            id=mem_id,
            type=mtype,
            scope=scope,
            subject=normalize_subject(subject),
            title=(s("title") or subject)[:MAX_TITLE],
            content=body.strip("\n"),
            confidence=str(s("confidence")) if s("confidence") in CONFIDENCE else "unverified",
            importance=_int(s("importance"), 3, 1, 5),
            status=status,
            pinned=s("pinned") == "true",
            live_state=s("live_state") == "true",
            state=s("state"),
            source_kind=str(s("source_kind")) if s("source_kind") in SOURCE_KINDS else "operator",
            source_ref=s("source_ref"),
            source_client=s("source_client"),
            created=s("created", stamp) or stamp,
            updated=s("updated", stamp) or stamp,
            last_confirmed=s("last_confirmed") or s("updated", stamp) or stamp,
            confirmations=_int(s("confirmations"), 1, 1, 10**6),
            expires=s("expires"),
            fresh_days=_int(s("fresh_days"), 0, 1, 365) or None,
            needs_verification=s("needs_verification") == "true",
            supersedes=s("supersedes"),
            superseded_by=s("superseded_by"),
            conflicts_with=lst("conflicts_with"),
            related=lst("related"),
            tags=lst("tags"),
            generated_key=s("generated_key"),
            alternatives=lst("alternatives"),
            path=rel_path,
            extra={k: v for k, v in header.items() if k not in known},
        )

    def public(self, now: datetime | None = None) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in ORDER}
        d["content"] = self.content
        d["path_hint"] = self.path
        if now is not None:
            d["status"] = self.effective_status(now)
        return {k: v for k, v in d.items() if v not in (None, "", [])}


def _int(value: str | None, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value) if value is not None else default
    except ValueError:
        return default
    return max(lo, min(hi, n))
