"""Secret detection and masking (research A7). Self-contained: no vps_mcp import. Pure."""

from __future__ import annotations

import fnmatch
import re

MASK = "***"
DEFAULT_SECRET_KEYS = [
    "*PASSWORD*",
    "*PASSWD*",
    "*SECRET*",
    "*TOKEN*",
    "*API_KEY*",
    "*APIKEY*",
    "*PRIVATE_KEY*",
    "*ACCESS_KEY*",
]

_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
            re.S,
        ),
        MASK,
    ),
    (
        "credentials in a URL",
        re.compile(r"(?P<pre>\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)(?P<pw>[^\s@/]+)(?P<at>@)", re.I),
        rf"\g<pre>{MASK}\g<at>",
    ),
    (
        "bearer token",
        re.compile(r"(?P<pre>\bBearer\s+)(?P<tok>[A-Za-z0-9._~+/=-]{8,})"),
        rf"\g<pre>{MASK}",
    ),
    (
        "access token",
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}"
            r"|xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|glpat-[A-Za-z0-9_-]{20,})"
        ),
        MASK,
    ),
]
_KEY_SEP_RE = re.compile(r"\b(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)(?P<sep>\s*[=:]\s*)")
_VALUE_RE = re.compile(r"\"[^\"]*\"|'[^']*'|[^\s,;)]+")


def _secret_key(key: str, globs: list[str]) -> bool:
    up = key.upper()
    return any(fnmatch.fnmatchcase(up, g.upper()) for g in globs)


def _mask_assignments(text: str, globs: list[str]) -> tuple[str, int]:
    out: list[str] = []
    pos = n = 0
    for m in _KEY_SEP_RE.finditer(text):
        if m.start() < pos or not _secret_key(m["key"], globs):
            continue
        v = _VALUE_RE.match(text, m.end())
        if not v or v.group(0) == MASK:
            continue
        out.append(text[pos : m.end()])
        out.append(MASK)
        pos = v.end()
        n += 1
    out.append(text[pos:])
    return "".join(out), n


def scan(text: str, globs: list[str] | None = None) -> tuple[str, list[str]]:
    """Return ``(masked_text, kinds_found)``; one entry in ``kinds_found`` per value masked."""
    kinds: list[str] = []
    if not text:
        return text, kinds
    for kind, rx, repl in _PATTERNS:
        text, n = rx.subn(repl, text)
        kinds += [kind] * n
    text, n = _mask_assignments(text, globs or DEFAULT_SECRET_KEYS)
    kinds += ["secret assignment"] * n
    return text, kinds
