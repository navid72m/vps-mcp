"""Deterministic write policy: schema, secrets, origin/instruction, confidence (A7, A8)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from agent_memory import secrets
from agent_memory.errors import invalid, not_permitted
from agent_memory.model import (
    CONFIDENCE,
    MAX_CONTENT,
    MAX_TITLE,
    SOURCE_KINDS,
    TAG_RE,
    TRUSTED_SOURCES,
    TYPES,
    iso,
    normalize_subject,
    parse_scope,
)

INSTRUCTION_RE = re.compile(
    r"ignore\s+(all|any|previous|prior|the\s+above)"
    r"|disregard\s"
    r"|you\s+(must|should)\s+(now\s+)?(always|never|ignore|run|execute|obey)"
    r"|^\s*system\s*:"
    r"|always\s+(run|execute|use|send|save|store)\b"
    r"|never\s+(ask|confirm|check|tell)\b"
    r"|<\s*/?\s*(system|instructions?)\b"
    r"|(^|\s)(curl|wget|rm\s+-rf|sudo|chmod\s+777|bash\s+-c|sh\s+-c)\s",
    re.I | re.M,
)
_DURATION = re.compile(r"^(\d{1,4})\s*([dhw])$")


@dataclass
class Settings:
    secrets: str = "refuse"  # refuse | mask
    pinned_chars: int = 2000
    max_results: int = 8
    fresh_days: int = 7
    weights: dict[str, float] = field(default_factory=dict)
    half_life_days: float = 90.0


@dataclass
class Clean:
    fields: dict[str, Any]
    masked: int = 0
    notes: list[str] = field(default_factory=list)


def trusted(source_kind: str) -> bool:
    return source_kind in TRUSTED_SOURCES


def parse_expires(value: str | None, now: datetime) -> str | None:
    if value in (None, ""):
        return None
    assert value is not None
    m = _DURATION.match(value.strip().lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "w": timedelta(weeks=n)}[unit]
        return iso(now + delta)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise invalid("expires must be an ISO date/time or a duration like '30d'") from None
    return iso(dt if dt.tzinfo else dt.replace(tzinfo=now.tzinfo))


def check_text(name: str, value: Any, limit: int, *, single_line: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise invalid(f"{name} is required")
    value = value.strip()
    if single_line and ("\n" in value or "\r" in value):
        raise invalid(f"{name} must be a single line")
    if len(value.encode("utf-8")) > limit:
        raise invalid(
            f"{name} is longer than {limit} bytes; split it into several concise memories"
        )
    return str(value)


def scrub(fields: dict[str, Any], settings: Settings) -> int:
    """Detect secrets in every text field; refuse or mask per policy. Returns count masked."""
    total = 0
    kinds: list[str] = []
    for key in ("title", "content"):
        if isinstance(fields.get(key), str):
            fields[key], found = secrets.scan(fields[key])
            kinds += found
    for key in ("tags", "alternatives"):
        cleaned = []
        for item in fields.get(key) or []:
            masked, found = secrets.scan(item)
            kinds += found
            if key == "tags" and found:
                continue
            cleaned.append(masked)
        fields[key] = cleaned
    total = len(kinds)
    if total and settings.secrets == "refuse":
        raise invalid(
            f"refusing to store a secret ({', '.join(sorted(set(kinds)))}); memory never holds "
            "credentials - store a reference to where the secret lives instead",
            secret_kinds=sorted(set(kinds)),
        )
    return total


def validate(raw: dict[str, Any], settings: Settings, now: datetime) -> Clean:
    mtype = raw.get("type")
    if mtype not in TYPES:
        raise invalid(f"type must be one of {', '.join(TYPES)}")
    scope = raw.get("scope") or "user"
    parse_scope(scope)
    subject = normalize_subject(str(raw.get("subject") or ""))
    title = check_text("title", raw.get("title"), MAX_TITLE * 4, single_line=True)
    if len(title) > MAX_TITLE:
        raise invalid(f"title must be at most {MAX_TITLE} characters")
    content = check_text("content", raw.get("content"), MAX_CONTENT)
    source_kind = raw.get("source_kind") or "client"
    if source_kind not in SOURCE_KINDS:
        raise invalid(f"source_kind must be one of {', '.join(SOURCE_KINDS)}")
    tags = raw.get("tags") or []
    if not isinstance(tags, list) or len(tags) > 10:
        raise invalid("tags must be a list of at most 10 items")
    tags = [str(t).lower() for t in tags]
    if any(not TAG_RE.fullmatch(t) for t in tags):
        raise invalid("each tag must be 1-32 chars of a-z, 0-9, '_' and '-'")
    alternatives = raw.get("alternatives") or []
    if alternatives and mtype != "decision":
        raise invalid("alternatives are only for decisions")
    if len(alternatives) > 10 or any(len(str(a)) > 200 for a in alternatives):
        raise invalid("at most 10 alternatives of 200 characters each")
    state = raw.get("state")
    if state not in (None, "", "open", "done"):
        raise invalid("state must be 'open' or 'done'")
    if state and mtype != "episodic":
        raise invalid("state (open/done) is only for episodic memories")
    importance = raw.get("importance", 3)
    if not isinstance(importance, int) or not 1 <= importance <= 5:
        raise invalid("importance must be an integer from 1 to 5")
    fresh = raw.get("fresh_days")
    if fresh is not None and (not isinstance(fresh, int) or not 1 <= fresh <= 365):
        raise invalid("fresh_days must be 1-365")

    fields: dict[str, Any] = {
        "type": mtype,
        "scope": scope,
        "subject": subject,
        "title": title,
        "content": content,
        "tags": tags,
        "alternatives": [str(a) for a in alternatives],
        "state": state or None,
        "importance": importance,
        "fresh_days": fresh,
        "source_kind": source_kind,
        "source_ref": (str(raw["source_ref"])[:200] if raw.get("source_ref") else None),
        "pinned": bool(raw.get("pinned")),
        "live_state": bool(raw.get("live_state")),
        "expires": parse_expires(raw.get("expires"), now),
        "related": [str(r) for r in raw.get("related") or []][:20],
    }
    masked = scrub(fields, settings)
    notes: list[str] = []

    # origin and instruction check (A8)
    if not trusted(source_kind):
        instructiony = INSTRUCTION_RE.search(fields["content"] + "\n" + fields["title"])
        if mtype in ("procedural", "decision") and instructiony:
            raise not_permitted(
                f"instruction-like {mtype} content from an untrusted source ({source_kind}) is "
                "not stored; if the operator wants it, they can save it with "
                "`agent-memory save --operator`"
            )
    # confidence (A8 cap, FR-011)
    wanted = raw.get("confidence")
    if wanted not in (None, *CONFIDENCE):
        raise invalid("confidence must be verified, inferred or unverified")
    if not trusted(source_kind):
        confidence = "unverified"
        if wanted in ("verified", "inferred"):
            notes.append(f"stored as unverified: the source ({source_kind}) is untrusted")
    else:
        confidence = wanted or "verified"
    fields["confidence"] = confidence
    if masked:
        notes.append(f"{masked} secret value(s) masked")
    return Clean(fields, masked, notes)
