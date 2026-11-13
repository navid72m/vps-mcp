"""Lazy tool loading: core tools, toolsets, catalog and keyword prefetch (feature 008, R1-R8)."""

from __future__ import annotations

import fnmatch
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from agent_memory.similarity import STOPWORDS, stem
from assistant.config import Config, ToolsetSection
from assistant.providers import ToolSpec

BUILTIN = "builtin"
BUILTIN_PREFIXES = (
    "memory__",
    "schedule__",
    "files__",
    "assistant__",
    "tools__",
    "me__",
    "web__",
    "calendar__",
    "email__",
    "tg_user__",
)
BUILTIN_CORE = (
    "memory__search",
    "memory__get",
    "schedule__add",
    "schedule__list",
    "assistant__switch_model",
    "tools__load",
    "me__remember",
    "me__forget",
    "me__interview",
)
BUILTIN_SETS: dict[str, ToolsetSection] = {
    "files": ToolsetSection(
        tools=["files__*"],
        description="read, write, edit, search, list, delete and send files on this laptop",
        keywords=[
            "file",
            "folder",
            "directory",
            "document",
            "pdf",
            "path",
            "disk",
            "laptop",
            "فایل",
            "پوشه",
            "اسناد",
        ],
    ),
    "web": ToolsetSection(
        tools=["web__*"],
        description="search the web and read web pages or PDF links",
        keywords=[
            "search",
            "google",
            "look up",
            "web",
            "internet",
            "website",
            "online",
            "news",
            "latest",
            "price",
            "review",
            "link",
            "http",
            "https",
            "www",
            "wikipedia",
            "opening hours",
            "جستجو",
            "سرچ",
            "اینترنت",
            "وب",
        ],  # fmt: skip
    ),
    "schedules": ToolsetSection(
        tools=["schedule__remove", "schedule__pause", "schedule__resume"],
        description="cancel, pause or resume reminders, timers and schedules",
        keywords=["cancel", "pause", "resume", "stop reminder", "delete reminder"],
    ),
    "calendar": ToolsetSection(
        tools=["calendar__*"],
        description="Google Calendar: view upcoming events and schedule meetings",
        keywords=[
            "calendar",
            "event",
            "meeting",
            "agenda",
            "appointment",
            "schedule",
            "busy",
            "free time",
            "call",
            "sync",
            "booking",
            "تقویم",
            "جلسه",
            "جلسات",
            "قرار",
            "رویداد",
            "برنامه",
        ],
    ),
    "email": ToolsetSection(
        tools=["email__*"],
        description="Gmail: check inbox, search emails, read messages, create drafts and send emails",
        keywords=[
            "email",
            "mail",
            "gmail",
            "inbox",
            "unread",
            "send email",
            "draft",
            "reply to",
            "message from",
            "ایمیل",
            "ایمیلام",
            "ایمیلم",
            "ایمیل‌ها",
            "ایمیل‌هایم",
            "جیمیل",
            "نامه",
            "نامه‌ها",
            "پیام",
            "پیام‌ها",
        ],
    ),
    "telegram_user": ToolsetSection(
        tools=["tg_user__*"],
        description="Personal Telegram account: check chats, unread messages, search history, read and send messages",
        keywords=[
            "telegram",
            "chat",
            "channel",
            "dm",
            "message",
            "group",
            "dialog",
            "saved messages",
            "تلگرام",
            "چت",
            "پیام",
            "کانال",
            "گروه",
            "پیام‌ها",
            "چت‌ها",
        ],
    ),
}
LINE_CAP = 160
EXTRA_STOP = frozenset(
    "please tell show give let want need like know check look last find time today yesterday "
    # generic words in tool names: they say nothing about which toolset is meant
    "status list info overview item items detail details service services".split()
)
_TOKEN = re.compile(r"[\w]+", re.UNICODE)

LOAD_SPEC = ToolSpec(
    "tools__load",
    "Load one or more toolsets from the catalog so their tools become available from your next "
    "step. Use it when the operator's request needs tools you don't have yet.",
    {
        "type": "object",
        "properties": {
            "toolsets": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4}
        },
        "required": ["toolsets"],
    },
)


def tokens(text: str) -> list[str]:
    """Stemmed words, short technical ones (ci, db, mr) kept, stopwords dropped."""
    out = []
    for w in _TOKEN.findall(text.lower()):
        if w in STOPWORDS or w in EXTRA_STOP or (len(w) < 2):
            continue
        out.append(stem(w))
    return out


def _related(a: str, b: str) -> bool:
    """Same stem, or one a prefix of the other (deploy/deployment, rehears/rehearsal)."""
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 4 and long_.startswith(short)


@dataclass
class Toolset:
    name: str
    server: str
    description: str
    tools: list[str] = field(default_factory=list)  # model-facing names, fixed order
    keywords: dict[str, float] = field(default_factory=dict)  # stem -> weight
    available: bool = True

    def line(self) -> str:
        text = f"- {self.name} ({len(self.tools)}): {self.description}"
        if not self.available:
            text += " [unavailable]"
        return text if len(text) <= LINE_CAP else text[: LINE_CAP - 1] + "…"


@dataclass
class Catalog:
    core: list[str]
    sets: dict[str, Toolset]  # sorted by name
    specs: dict[str, ToolSpec]  # every offerable spec by model-facing name
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ lookup

    def toolset_of(self, tool: str) -> str | None:
        for ts in self.sets.values():
            if tool in ts.tools:
                return ts.name
        return None

    def render(self) -> str:
        if not self.sets:
            return ""
        head = (
            "Tools: you always have the core tools. More tools are grouped into toolsets; load "
            "one with tools__load when you need it (some are loaded automatically from the "
            "operator's words):"
        )
        return head + "\n" + "\n".join(ts.line() for ts in self.sets.values())

    def tool_specs(self, loaded: Iterable[str]) -> list[ToolSpec]:
        """Core first, then loaded toolsets in catalog order: a stable, cache-friendly list."""
        want = set(loaded)
        names = list(self.core)
        for ts in self.sets.values():
            if ts.name in want and ts.available:
                names.extend(ts.tools)
        return [self.specs[n] for n in names if n in self.specs]

    # ----------------------------------------------------------------- scoring

    def score(
        self, current: str, earlier: Iterable[str] = ()
    ) -> list[tuple[str, float, list[str]]]:
        """Toolsets ranked by keyword match with the message (and, at half weight, earlier
        messages). Returns (name, score, matched words), best first, available sets only."""
        query: dict[str, float] = {}
        for text, weight in [(current, 1.0)] + [(t, 0.5) for t in earlier]:
            for tok in tokens(text):
                query[tok] = max(query.get(tok, 0.0), weight)
        n = max(1, len(self.sets))
        df: dict[str, int] = {}  # rarity from the strong words only (names, configured keywords)
        for ts in self.sets.values():
            for kw, w in ts.keywords.items():
                if w >= 2.0:
                    df[kw] = df.get(kw, 0) + 1
        out = []
        for ts in self.sets.values():
            if not ts.available or not ts.tools:
                continue
            total, matched = 0.0, []
            for q, qw in query.items():
                best, best_kw = 0.0, ""
                for kw, kww in ts.keywords.items():
                    if kww > best and _related(q, kw):
                        best, best_kw = kww, kw
                if best:
                    idf = math.log(1 + n / df.get(best_kw, n))
                    total += best * qw * idf
                    matched.append(q)
            if total > 0:
                out.append((ts.name, round(total, 2), matched))
        return sorted(out, key=lambda x: -x[1])

    def prefetch(
        self, current: str, earlier: Iterable[str], threshold: float, limit: int
    ) -> list[tuple[str, float, list[str]]]:
        """The toolsets worth loading before the model call: above the threshold and within
        60% of the best match (a clear winner doesn't drag in weak runners-up)."""
        ranked = self.score(current, earlier)
        if not ranked:
            return []
        best = ranked[0][1]
        return [r for r in ranked if r[1] >= threshold and r[1] >= 0.6 * best][:limit]

    def size(self, loaded: Iterable[str] = ()) -> int:
        return spec_chars(self.tool_specs(loaded))

    def full_size(self) -> int:
        return spec_chars(self.specs.values())


def spec_chars(specs: Iterable[ToolSpec]) -> int:
    return sum(
        len(
            json.dumps({"name": s.name, "description": s.description, "parameters": s.input_schema})
        )
        for s in specs
    )


def _server_of(name: str) -> tuple[str, str]:
    if name.startswith(BUILTIN_PREFIXES):
        return BUILTIN, name
    server, _, tool = name.partition("__")
    return server, tool


def _keywords(names: list[str], descs: list[str], extra: list[str]) -> dict[str, float]:
    kw: dict[str, float] = {}

    def add(words: Iterable[str], weight: float) -> None:
        for w in words:
            kw[w] = max(kw.get(w, 0.0), weight)

    for n in names:
        add(tokens(n.split("__", 1)[-1].replace("_", " ")), 2.0)
    for d in descs:
        add(tokens(d), 0.5)
    for e in extra:
        add(tokens(e), 3.0)
    return kw


def build(config: Config, specs: list[ToolSpec], up: dict[str, bool]) -> Catalog:
    """Group the offerable specs (policy `never` already removed) into core + toolsets (R3)."""
    by_name = {s.name: s for s in specs}
    servers = {s.name: s for s in config.server}
    assigned: dict[str, str] = {}  # tool -> toolset name, or "" for core
    warnings: list[str] = []

    # 1. core (wins over any toolset): built-in core, [tools] core (model-facing globs), per-server core (tool globs)
    core: list[str] = []
    for name in by_name:
        if name in assigned:
            continue
        server, tool = _server_of(name)
        sec = servers.get(server)
        if (
            name in BUILTIN_CORE
            or any(fnmatch.fnmatch(name, p) for p in config.tools.core)
            or (sec is not None and any(fnmatch.fnmatch(tool, p) for p in sec.core))
        ):
            core.append(name)
            assigned[name] = ""
    # 2. configured toolsets (per server, in config order)
    groups: dict[str, tuple[str, ToolsetSection]] = {}
    for srv in config.server:
        for tsname, section in srv.toolsets.items():
            groups[tsname] = (srv.name, section)
            hit = False
            for name in by_name:
                server, tool = _server_of(name)
                if server == srv.name and name not in assigned:
                    if any(fnmatch.fnmatch(tool, p) for p in section.tools):
                        assigned[name] = tsname
                        hit = True
            if not hit and up.get(srv.name, False):
                warnings.append(f"toolset '{tsname}' matches no tool on server '{srv.name}'")

    for srv in config.server:
        for p in srv.core:
            if up.get(srv.name, False) and not any(
                fnmatch.fnmatch(_server_of(n)[1], p) and _server_of(n)[0] == srv.name for n in core
            ):
                warnings.append(f"core pattern '{p}' matches no tool on server '{srv.name}'")

    # 3. built-in default toolsets
    for tsname, section in BUILTIN_SETS.items():
        groups[tsname] = (BUILTIN, section)
        for name in by_name:
            if name not in assigned and _server_of(name)[0] == BUILTIN:
                if any(fnmatch.fnmatch(name, p) for p in section.tools):
                    assigned[name] = tsname

    # 4. leftovers: one toolset per server ("<server>", or "<server>-more" if it has groups)
    for name in by_name:
        if name in assigned:
            continue
        server, _ = _server_of(name)
        grouped = server == BUILTIN or bool(servers[server].toolsets if server in servers else [])
        tsname = f"{server}-more" if grouped else server
        if tsname not in groups:
            groups[tsname] = (server, ToolsetSection(tools=["*"], description=""))
        assigned[name] = tsname

    sets: dict[str, Toolset] = {}
    for tsname in sorted(groups):
        server, section = groups[tsname]
        tools = [n for n in by_name if assigned.get(n) == tsname]
        bare = [_server_of(n)[1].removeprefix(f"{server}__") for n in tools]
        desc = section.description or ("tools: " + ", ".join(bare))
        available = True if server == BUILTIN else up.get(server, False)
        if not tools and available:
            continue
        sets[tsname] = Toolset(
            name=tsname,
            server=server,
            description=desc,
            tools=tools,
            keywords=_keywords(tools, [by_name[n].description for n in tools], section.keywords),
            available=available,
        )
    _drop_common(sets)
    first = [n for n in BUILTIN_CORE if n in core]
    core = first + [n for n in core if n not in BUILTIN_CORE]
    return Catalog(core=core, sets=sets, specs=by_name, warnings=warnings)


def _drop_common(sets: dict[str, Toolset]) -> None:
    """Description words found in more than half of the toolsets carry no signal (R4)."""
    if len(sets) < 3:
        return
    df: dict[str, int] = {}
    for ts in sets.values():
        for kw in ts.keywords:
            df[kw] = df.get(kw, 0) + 1
    common = {kw for kw, c in df.items() if c > len(sets) / 2}
    for ts in sets.values():
        ts.keywords = {k: w for k, w in ts.keywords.items() if k not in common or w >= 3.0}
