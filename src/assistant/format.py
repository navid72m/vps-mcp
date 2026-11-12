"""Model Markdown -> Telegram HTML (bold, code, links, headings; tables fitted to a phone)."""

from __future__ import annotations

import html
import re

PHONE_COLS = 34  # monospace characters that fit a phone-width chat bubble without wrapping

FENCE_RE = re.compile(r"^\s*```")
SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
HEAD_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
BULLET_RE = re.compile(r"^(\s*)[*+-]\s+")
RULE_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
INLINE_RE = re.compile(
    r"`([^`\n]+)`"  # code
    r"|\[([^\]\n]+)\]\((https?://[^)\s]+)\)"  # link
    r"|\*\*(.+?)\*\*|__(.+?)__"  # bold
    r"|~~(.+?)~~"  # strike
    r"|(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])"  # italic (not snake_case _x_)
)
STRIP_RE = re.compile(r"\*\*|__|~~|`")


def plain(text: str) -> str:
    """Markdown markers removed: the fallback when Telegram rejects the HTML."""
    out = []
    for line in text.splitlines():
        if FENCE_RE.match(line) or SEP_RE.match(line):
            continue
        m = HEAD_RE.match(line)
        line = m.group(1) if m else BULLET_RE.sub(lambda b: f"{b.group(1)}• ", line)
        out.append(STRIP_RE.sub("", line))
    return "\n".join(out)


def inline(text: str) -> str:
    out, pos = [], 0
    for m in INLINE_RE.finditer(text):
        out.append(html.escape(text[pos : m.start()], quote=False))
        code, label, url, b1, b2, strike, ital = m.groups()
        if code is not None:
            out.append(f"<code>{html.escape(code, quote=False)}</code>")
        elif label is not None:
            out.append(f'<a href="{html.escape(url)}">{inline(label)}</a>')
        elif b1 is not None or b2 is not None:
            out.append(f"<b>{inline(b1 if b1 is not None else b2)}</b>")
        elif strike is not None:
            out.append(f"<s>{inline(strike)}</s>")
        else:
            out.append(f"<i>{inline(ital)}</i>")
        pos = m.end()
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out)


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def table(lines: list[str]) -> str:
    head = _cells(lines[0])
    rows = [_cells(r) for r in lines[2:]]
    n = len(head)
    rows = [(r + [""] * n)[:n] for r in rows]
    bare = [[STRIP_RE.sub("", c) for c in r] for r in [head, *rows]]
    widths = [max(len(r[i]) for r in bare) for i in range(n)]
    if sum(widths) + 2 * (n - 1) <= PHONE_COLS:
        fmt = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in bare]
        fmt.insert(1, "  ".join("─" * w for w in widths))
        return "<pre>" + html.escape("\n".join(fmt), quote=False) + "</pre>"
    # too wide for a phone: one bullet per row, the first column as its title
    out = []
    for r in rows:
        first = f"• <b>{inline(r[0])}</b>" if r[0] else "•"
        rest = [
            (inline(c) if n == 2 else f"{inline(head[i])}: {inline(c)}")
            for i, c in enumerate(r)
            if i and c
        ]
        out.append(first + (": " if n == 2 else "\n   ") + "\n   ".join(rest) if rest else first)
    return "\n".join(out)


def to_html(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if FENCE_RE.match(line):
            j = i + 1
            while j < len(lines) and not FENCE_RE.match(lines[j]):
                j += 1
            code = "\n".join(lines[i + 1 : j])
            out.append(f"<pre>{html.escape(code, quote=False)}</pre>")
            i = j + 1
            continue
        if "|" in line and i + 1 < len(lines) and SEP_RE.match(lines[i + 1]):
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                j += 1
            out.append(table(lines[i:j]))
            i = j
            continue
        m = HEAD_RE.match(line)
        if m:
            out.append(f"<b>{inline(m.group(1))}</b>")
        elif RULE_RE.match(line):
            out.append("")
        elif line.lstrip().startswith(">"):
            quote = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(inline(lines[i].lstrip()[1:].strip()))
                i += 1
            out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            continue
        else:
            b = BULLET_RE.match(line)
            if b:
                line = f"{b.group(1)}• {line[b.end() :]}"
            out.append(inline(line))
        i += 1
    return "\n".join(out).strip()
