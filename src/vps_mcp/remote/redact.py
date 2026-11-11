"""Credential redaction and secret masking (FR-017, FR-033)."""

from __future__ import annotations

import fnmatch
import logging
import re
from typing import Any

MASK = "***"
# Shorter strings are not scrubbed: replacing e.g. a 1-character "secret" everywhere would
# mangle unrelated output while protecting nothing.
MIN_SECRET_LEN = 6

_LINE_RE = re.compile(
    r"^(?P<lead>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)(?P<sep>\s*[=:]\s*|\s+)(?P<val>.*)$"
)


def is_secret_key(key: str, globs: list[str]) -> bool:
    upper = key.upper()
    return any(fnmatch.fnmatchcase(upper, g.upper()) for g in globs)


def mask_mapping(d: dict[str, Any], globs: list[str]) -> dict[str, Any]:
    return {
        k: (MASK if is_secret_key(k, globs) and v not in (None, "") else v) for k, v in d.items()
    }


def mask_text_lines(text: str, globs: list[str]) -> str:
    """Mask values of ``KEY=value``, ``key = value``, ``key: value`` and ``key value`` lines."""
    out = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        m = _LINE_RE.match(body)
        if m and m["val"].strip() and is_secret_key(m["key"], globs):
            body = f"{m['lead']}{m['key']}{m['sep']}{MASK}"
        out.append(body + ending)
    return "".join(out)


_KEY_SEP_RE = re.compile(r"\b(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)(?P<sep>\s*[=:]\s*)")
_VALUE_RE = re.compile(r"\"[^\"]*\"|'[^']*'|[^\s,;)]+")


def mask_inline(text: str, globs: list[str]) -> str:
    """Mask ``key=value`` / ``key: value`` pairs anywhere in a line (e.g. inside log messages).

    Keys are found without consuming values, so ``query: password=x`` still masks ``x``.
    """
    out: list[str] = []
    pos = 0
    for m in _KEY_SEP_RE.finditer(text):
        if m.start() < pos or not is_secret_key(m["key"], globs):
            continue
        v = _VALUE_RE.match(text, m.end())
        if not v:
            continue
        out.append(text[pos : m.end()])
        out.append(MASK)
        pos = v.end()
    out.append(text[pos:])
    return "".join(out)


def scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s and len(s) >= MIN_SECRET_LEN:
            text = text.replace(s, MASK)
    return text


def scrub_obj(obj: Any, secrets: list[str]) -> Any:
    if isinstance(obj, str):
        return scrub(obj, secrets)
    if isinstance(obj, dict):
        return {k: scrub_obj(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_obj(v, secrets) for v in obj]
    return obj


class RedactionFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= MIN_SECRET_LEN]

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            record.msg = scrub(record.getMessage(), self.secrets)
            record.args = None
        return True


def install_redaction(logger: logging.Logger, secrets: list[str]) -> RedactionFilter:
    flt = RedactionFilter(secrets)
    logger.addFilter(flt)
    for handler in logger.handlers:
        handler.addFilter(flt)
    return flt
