"""Knows me: the operator's profile, instant remembering, recaps, interview (feature 009).

Facts are agent-memory records under a fixed subject scheme (research R1):
`me.<category>.<key>` for the operator, `people.<person>.<key>` for others, tagged `profile`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from agent_memory.similarity import STOPWORDS, stem

CATEGORIES = ("identity", "work", "people", "interests", "routines", "other")
CARD_CHARS = 1500
RECAP_CHARS = 1000
RECAP_COUNT = 3
RECAP_DAYS = 7
UNDO_HOURS = 24
FACT_CHARS = 300
SENSITIVE_TAG = "sensitive"
PROFILE_TAG = "profile"
RECAP_TAG = "recap"
SOURCE_SHARE = 0.6  # share of a fact's words that must come from the operator's message

# an explicit request to remember, in the operator's languages (R2)
TRIGGERS = re.compile(
    r"\b(remember|note that|make a note|keep in mind|don'?t forget|save (?:this|that)|"
    r"memorize|merk dir|merke dir|notier)\b|یادت باشه|یادت بمونه|به خاطر بسپار|یادداشت کن",
    re.IGNORECASE,
)
# "my password is X": plain phrasings agent-memory's secret patterns don't catch (FR-004)
SECRET_RE = re.compile(
    r"\b(password|passwort|passphrase|passcode|pin(?: code)?|api[ -]?key|access[ -]?key|"
    r"secret[ -]?key|token|private key|kennwort|security code|cvv|tan)\b"
    r"[^.\n]{0,40}?\b(is|ist|=|:|lautet)\s*\S{3,}|رمز\s*(عبور)?\s*\S*\s*(است|:)",
    re.IGNORECASE,
)
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PERSON_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
LEGACY_MAP = {  # operator.<first>.* (saved before 009) -> category
    "identity": "identity",
    "name": "identity",
    "career": "work",
    "work": "work",
    "education": "work",
    "projects": "work",
    "assistant": "routines",
    "preferences": "routines",
    "memory": "routines",
    "interests": "interests",
    "hobbies": "interests",
}
CATEGORY_LABEL = {
    "identity": "Identity",
    "work": "Work",
    "people": "People",
    "interests": "Interests",
    "routines": "Routines & preferences",
    "other": "Other",
}


class KnowMeError(ValueError):
    pass


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].strip("-")


def fact_subject(category: str, key: str, person: str | None = None) -> str:
    """`me.<category>.<key>` or `people.<person>.<key>` (data-model: key is a slug of at most
    40 characters, person a slug of at most 31, category one of CATEGORIES)."""
    if category not in CATEGORIES:
        raise KnowMeError(f"category must be one of {', '.join(CATEGORIES)}")
    k = slug(key)
    if not KEY_RE.fullmatch(k):
        raise KnowMeError("key must be a short slug like 'city' or 'employer'")
    if person or category == "people":
        p = slug(person or "", 31)
        if not PERSON_RE.fullmatch(p):
            raise KnowMeError("people facts need the person's name")
        return f"people.{p}.{k}"
    return f"me.{category}.{k}"


def parse_subject(subject: str) -> tuple[str, str | None, str]:
    """(category, person, key). Legacy `operator.*` subjects map by their first part."""
    parts = subject.split(".")
    if parts[0] == "me" and len(parts) >= 3:
        cat = parts[1] if parts[1] in CATEGORIES else "other"
        return cat, None, ".".join(parts[2:])
    if parts[0] == "people" and len(parts) >= 3:
        return "people", parts[1], ".".join(parts[2:])
    if parts[0] == "operator" and len(parts) >= 2:
        return LEGACY_MAP.get(parts[1], "other"), None, ".".join(parts[1:])
    return "other", None, subject


def is_profile_subject(subject: str) -> bool:
    return subject.split(".", 1)[0] in ("me", "people", "operator")


# ------------------------------------------------------------------ the gate (R2)


def words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[^\W_]+", text.lower()):
        if len(w) > 1 and w not in STOPWORDS:
            out.add(stem(w))
    return out


def looks_secret(text: str) -> bool:
    return bool(SECRET_RE.search(text))


def may_save_now(
    fact: str,
    operator_text: str,
    *,
    origin: str,
    correcting: bool,
    interview_active: bool,
    sensitive: bool,
) -> tuple[bool, str]:
    """Whether `me__remember` may save immediately. Only the operator's own current message can
    authorise it; text from tools, files, web pages or memory never can (FR-003)."""
    if origin != "chat":
        return False, "not asked in a message from the operator"
    fw = words(fact)
    if not fw:
        return False, "nothing to remember"
    share = len(fw & words(operator_text)) / len(fw)
    if share < SOURCE_SHARE:
        return False, "this fact is not in the operator's message"
    asked = bool(TRIGGERS.search(operator_text))
    if sensitive and not asked:
        return False, "sensitive facts are kept only when the operator explicitly asks"
    if asked or correcting or interview_active:
        return True, ""
    return False, "the operator did not ask to remember it"


# ------------------------------------------------------------------- facts / card


@dataclass
class Fact:
    id: str
    subject: str
    title: str
    content: str
    importance: int = 3
    tags: list[str] = field(default_factory=list)
    confirmed: str = ""
    created: str = ""

    @property
    def category(self) -> str:
        return parse_subject(self.subject)[0]

    @property
    def person(self) -> str | None:
        return parse_subject(self.subject)[1]

    @property
    def sensitive(self) -> bool:
        return SENSITIVE_TAG in self.tags


def by_priority(facts: list[Fact]) -> list[Fact]:
    """Identity first, then importance, then most recently confirmed."""
    newest = sorted(facts, key=lambda f: f.confirmed, reverse=True)
    return sorted(newest, key=lambda f: (f.category != "identity", -f.importance))


def render_card(facts: list[Fact], budget: int = CARD_CHARS) -> str:
    """The profile in every conversation (R3): never sensitive facts, identity first, then
    importance and recency, grouped by category and person, within `budget` characters."""
    shown = by_priority([f for f in facts if not f.sensitive])
    if not shown:
        return ""
    head = "## About the operator\n"
    lines: list[str] = []
    used = len(head)
    left = 0
    for i, f in enumerate(shown):
        label = f"{f.person.title()}: " if f.person else ""
        line = f"- [{CATEGORY_LABEL[f.category]}] {label}{f.content.strip()}"
        if used + len(line) + 1 > budget - 60:
            left = len(shown) - i
            break
        lines.append(line)
        used += len(line) + 1
    lines.sort(key=lambda s: list(CATEGORY_LABEL.values()).index(s[3 : s.index("]")]))
    tail = f"\n(+{left} more facts — memory__search finds them)" if left else ""
    return head + "\n".join(lines) + tail


def card_for_phone(facts: list[Fact], include_sensitive: bool) -> list[tuple[str, list[Fact]]]:
    """Facts grouped for /me: [(heading, facts)], in category order."""
    groups: dict[str, list[Fact]] = {}
    for f in by_priority(facts):
        if f.sensitive and not include_sensitive:
            continue
        head = "Sensitive" if f.sensitive else CATEGORY_LABEL[f.category]
        groups.setdefault(head, []).append(f)
    order = [*CATEGORY_LABEL.values(), "Sensitive"]
    return sorted(groups.items(), key=lambda kv: order.index(kv[0]))


# ----------------------------------------------------------------------- recaps


@dataclass
class Recap:
    day: str  # yyyy-mm-dd
    title: str
    content: str


def recap_text(recap: dict[str, Any]) -> tuple[str, str] | None:
    """(title, content) from the extraction's recap object; None if it says nothing."""
    if not isinstance(recap, dict):
        return None

    def items(key: str) -> list[str]:
        v = recap.get(key) or []
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []

    topics, outcomes, loops = items("topics"), items("outcomes"), items("open_loops")
    if not topics:
        return None
    lines = [f"Topics: {'; '.join(topics)}"]
    if outcomes:
        lines.append(f"Outcomes: {'; '.join(outcomes)}")
    if loops:
        lines.append(f"Open loops: {'; '.join(loops)}")
    return "; ".join(topics)[:120], "\n".join(lines)[:600]


def render_recaps(recaps: list[Recap], today: datetime) -> str:
    """The last RECAP_COUNT recaps or RECAP_DAYS days, whichever is fewer, newest first."""
    since = (today - timedelta(days=RECAP_DAYS)).date().isoformat()
    recent = [r for r in sorted(recaps, key=lambda r: r.day, reverse=True) if r.day >= since]
    if not recent:
        return ""
    out = "## Recent conversations (your notes; older ones: memory__search 'recap')"
    for r in recent[:RECAP_COUNT]:
        block = f"\n- {r.day}: " + r.content.replace("\n", " · ")
        if len(out) + len(block) > RECAP_CHARS:
            break
        out += block
    return out


# -------------------------------------------------------------------- interview (R7)

TOPICS = (
    ("name", "what to call them, and how they like to be addressed"),
    ("place", "where they live and which languages they speak"),
    ("work", "their work: role, employer or clients, current projects"),
    ("people", "the people they will mention most, and how they relate to them"),
    ("interests", "what they do outside work: hobbies and interests"),
    ("style", "their routines, and how they like answers: short or detailed, tone"),
)
SENSITIVE_TOPIC = ("sensitive", "anything sensitive they want you to know (health, finances)")
OFFER_DAYS = 7


def interview_default() -> dict[str, Any]:
    return {
        "done": [],
        "skipped": [],
        "sensitive_opt_in": False,
        "last_offered": "",
        "active": False,
        "current": "",
    }


def interview_next(st: dict[str, Any]) -> tuple[str, str] | None:
    topics = list(TOPICS) + ([SENSITIVE_TOPIC] if st.get("sensitive_opt_in") else [])
    seen = set(st.get("done", [])) | set(st.get("skipped", []))
    for name, hint in topics:
        if name not in seen:
            return name, hint
    return None


def interview_prompt(st: dict[str, Any]) -> str:
    if not st.get("active") or not st.get("current"):
        return ""
    hint = dict([*TOPICS, SENSITIVE_TOPIC]).get(st["current"], "")
    return (
        "## Interview mode\nYou are getting to know the operator. Ask ONE short, friendly "
        f"question about: {hint}. Save each answer with me__remember (it is allowed during the "
        "interview), then call me__interview with action 'next'. If the operator says skip, "
        "call me__interview 'skip'; if they want to stop or talk about something else, call "
        "me__interview 'stop' and help them normally. Never ask about health, finances, "
        "relationships, religion or politics unless the topic above is the sensitive one."
    )


def should_offer(st: dict[str, Any], fact_count: int, now: datetime) -> bool:
    if fact_count >= 3 or st.get("active"):
        return False
    last = st.get("last_offered") or ""
    return not last or last < (now - timedelta(days=OFFER_DAYS)).isoformat()


# ----------------------------------------------------------------------- export


def export_markdown(facts: list[Fact], recaps: list[Recap], now: datetime) -> str:
    out = [f"# What your assistant knows about you\n\nExported {now:%Y-%m-%d %H:%M}.\n"]
    for heading, group in card_for_phone(facts, include_sensitive=True):
        out.append(f"## {heading}\n")
        for f in group:
            who = f" ({f.person})" if f.person else ""
            src = ", ".join(t for t in f.tags if t not in (PROFILE_TAG, f.category)) or "-"
            out.append(
                f"- {f.title}{who}: {f.content}  \n  _{f.subject} · {f.confirmed[:10]} · {src}_"
            )
        out.append("")
    if recaps:
        out.append("## Recent conversations\n")
        for r in sorted(recaps, key=lambda r: r.day, reverse=True)[:30]:
            out.append(f"- **{r.day}**: {r.content.replace(chr(10), ' · ')}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------- tool specs


def _specs() -> list[Any]:
    from assistant.providers import ToolSpec

    return [
        ToolSpec(
            "me__remember",
            "Save a fact the operator told you about themselves or someone in their life. Use it "
            "when the operator asks you to remember something, corrects a fact you know, or "
            "answers an interview question. The runtime decides whether it is saved now or kept "
            "as a suggestion, and confirms it to the operator itself; tell the operator exactly "
            "the outcome it returns.",
            {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "key": {
                        "type": "string",
                        "description": "short slug for what this is, e.g. name, city, employer",
                    },
                    "person": {
                        "type": "string",
                        "description": "for a fact about someone else: their first name",
                    },
                    "fact": {
                        "type": "string",
                        "description": "the fact as one sentence, e.g. 'Sara lives in Hamburg'",
                    },
                    "sensitive": {
                        "type": "boolean",
                        "description": "health, money, relationships, religion or politics",
                    },
                },
                "required": ["category", "key", "fact"],
            },
        ),
        ToolSpec(
            "me__forget",
            "When the operator asks you to forget something about them: list matching facts so "
            "the operator can confirm on their phone. Nothing is removed without their tap.",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        ToolSpec(
            "me__interview",
            "During the getting-to-know-you interview: 'next' after saving an answer, 'skip' if "
            "the operator skips the question, 'stop' to end the interview.",
            {
                "type": "object",
                "properties": {"action": {"type": "string", "enum": ["next", "skip", "stop"]}},
                "required": ["action"],
            },
        ),
    ]


SPECS = _specs()
