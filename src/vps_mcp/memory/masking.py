"""Secret masking for memory text (research M8). Pure."""

from __future__ import annotations

import re

from vps_mcp.remote.redact import MASK, MIN_SECRET_LEN, mask_inline

_URL_CRED = re.compile(r"(?P<pre>\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)(?P<pw>[^\s@/]+)(?P<at>@)", re.I)
_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S
)
_BEARER = re.compile(r"(?P<pre>\bBearer\s+)(?P<tok>[A-Za-z0-9._~+/=-]{8,})")
_TOKENS = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-[A-Za-z0-9_-]{16,}"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|glpat-[A-Za-z0-9_-]{20,})"
)


def mask_note(text: str, globs: list[str], secrets: list[str]) -> tuple[str, int]:
    """Return ``(masked_text, number_of_values_masked)``."""
    if not text:
        return text, 0
    count = 0

    def sub(rx: re.Pattern[str], repl: str, s: str) -> str:
        nonlocal count
        s, n = rx.subn(repl, s)
        count += n
        return s

    text = sub(_PEM, MASK, text)
    text = sub(_URL_CRED, rf"\g<pre>{MASK}\g<at>", text)
    text = sub(_BEARER, rf"\g<pre>{MASK}", text)
    text = sub(_TOKENS, MASK, text)
    before = text.count(MASK)
    text = mask_inline(text, globs)
    count += text.count(MASK) - before
    for secret in secrets:
        if secret and len(secret) >= MIN_SECRET_LEN and secret in text:
            count += text.count(secret)
            text = text.replace(secret, MASK)
    return text, count
