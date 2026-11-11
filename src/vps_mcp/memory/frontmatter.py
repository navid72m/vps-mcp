"""Restricted front matter: flat ``key: value`` scalars and ``[a, b]`` flow lists (research M4).

Deliberately not YAML: no nesting, no anchors, no type coercion. Everything is a string or a
list of strings, so hand-edited files can never be interpreted as anything else.
"""

from __future__ import annotations

import re

Header = dict[str, str | list[str]]

ORDER = ("name", "type", "title", "tags", "status", "created", "updated", "source")
_KEY_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(?P<val>.*)$")
_NEEDS_QUOTE = re.compile(r"[:#\[\]{},\"'\\]|^\s|\s$|^$")


class FrontMatterError(ValueError):
    pass


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        return v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def _split_list(inner: str) -> list[str]:
    items, buf, quote = [], "", ""
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf += ch
        elif ch == ",":
            items.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        items.append(buf)
    return [s for s in (_unquote(i) for i in items) if s]


def parse(text: str) -> tuple[Header, str]:
    """Split ``---`` front matter from the body. Raises :class:`FrontMatterError`."""
    text = text.replace("\r\n", "\n").lstrip("\ufeff")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise FrontMatterError("missing opening '---'")
    header: Header = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body = "\n".join(lines[i + 1 :])
            return header, body.lstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _KEY_RE.match(line)
        if not m:
            raise FrontMatterError(f"line {i + 1}: expected 'key: value'")
        val = m["val"].strip()
        if val.startswith("[") and val.endswith("]"):
            header[m["key"]] = _split_list(val[1:-1])
        else:
            header[m["key"]] = _unquote(val)
    raise FrontMatterError("missing closing '---'")


def _scalar(v: str) -> str:
    if _NEEDS_QUOTE.search(v):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return v


def render(header: Header, body: str) -> str:
    keys = [k for k in ORDER if k in header] + sorted(k for k in header if k not in ORDER)
    out = ["---"]
    for k in keys:
        v = header[k]
        if isinstance(v, list):
            out.append(f"{k}: [{', '.join(_scalar(x) for x in v)}]")
        else:
            out.append(f"{k}: {_scalar(v.replace(chr(10), ' '))}")
    out.append("---")
    body = body.replace("\r\n", "\n").strip("\n")
    return "\n".join(out) + "\n" + (body + "\n" if body else "")
