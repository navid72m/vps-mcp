"""Refuse SQL step files that could escape the rehearsal copy (research M5).

SQL steps run as the PostgreSQL superuser against the copy, so anything that can reach
another database, the cluster, the filesystem or a shell is refused up front.
"""

from __future__ import annotations

import re

# psql meta-commands that are harmless; every other backslash command is refused
# (e.g. \c, \connect, \!, \o |cmd, \g |cmd, \copy … program, \i other-file, \setenv).
SAFE_META = {"echo", "qecho", "warn", "set", "unset", "timing", "pset", "x", "a", "t"}

FORBIDDEN: list[tuple[str, re.Pattern[str]]] = [
    ("dblink", re.compile(r"\bdblink\w*", re.I)),
    (
        "postgres_fdw / foreign servers",
        re.compile(
            r"\b(postgres_fdw|create\s+server|"
            r"create\s+foreign\s+table|"
            r"import\s+foreign\s+schema)\b",
            re.I,
        ),
    ),
    ("COPY … PROGRAM", re.compile(r"\bcopy\b[^;]*\bprogram\b", re.I | re.S)),
    ("COPY to/from a server file", re.compile(r"\bcopy\b[^;]*\b(from|to)\s+'", re.I | re.S)),
    ("ALTER SYSTEM", re.compile(r"\balter\s+system\b", re.I)),
    ("cluster-level DATABASE statement", re.compile(r"\b(create|drop|alter)\s+database\b", re.I)),
    (
        "role/privilege statement",
        re.compile(
            r"\b((create|drop|alter)\s+(role|user|group)|"
            r"grant|revoke|reassign\s+owned|"
            r"set\s+(session\s+)?(role|session\s+authorization))"
            r"\b",
            re.I,
        ),
    ),
    ("CREATE EXTENSION", re.compile(r"\bcreate\s+extension\b", re.I)),
    (
        "untrusted procedural language",
        re.compile(
            r"\blanguage\s+'?(c|internal|plpython\w*|"
            r"plperlu|pltclu|plsh)\b",
            re.I,
        ),
    ),
    (
        "server file / program access",
        re.compile(
            r"\b(pg_read_file|pg_read_binary_file|"
            r"pg_ls_dir|pg_stat_file|lo_import|lo_export|"
            r"pg_execute_server_program|pg_file_write)\b",
            re.I,
        ),
    ),
    ("tablespace statement", re.compile(r"\b(create|drop|alter)\s+tablespace\b", re.I)),
    (
        "subscription/publication",
        re.compile(r"\b(create|alter)\s+(subscription|publication)\b", re.I),
    ),
    ("pg_terminate/cancel_backend", re.compile(r"\bpg_(terminate|cancel)_backend\b", re.I)),
]

_DOLLAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def strip_comments_and_literals(sql: str, keep_literals: bool = False) -> tuple[str, list[str]]:
    """Return SQL with comments removed and literal contents blanked, plus meta-command lines.

    Inside dollar-quoted code bodies (DO blocks, functions) literal contents are kept, since
    they may be executed as dynamic SQL (EXECUTE '...').
    """
    out: list[str] = []
    meta: list[str] = []
    i, n = 0, len(sql)
    at_line_start = True
    while i < n:
        c = sql[i]
        if at_line_start:
            j = i
            while j < n and sql[j] in " \t":
                j += 1
            if j < n and sql[j] == "\\":
                end = sql.find("\n", j)
                end = n if end == -1 else end
                meta.append(sql[j:end].strip())
                i = end
                continue
        at_line_start = False
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end
            continue
        if sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            out.append(" ")
            continue
        if c == "'":
            start = i
            i += 1
            while i < n:
                if sql[i] == "'" and i + 1 < n and sql[i + 1] == "'":
                    i += 2
                elif sql[i] == "'":
                    i += 1
                    break
                else:
                    i += 1
            out.append(sql[start:i] if keep_literals else "''")
            continue
        if c == "$":
            m = _DOLLAR_RE.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, m.end())
                body = sql[m.end() : n if end == -1 else end]
                # Function bodies are code: keep them scannable (minus their own literals).
                inner, inner_meta = strip_comments_and_literals(body, keep_literals=True)
                out.append(f" {inner} ")
                meta.extend(inner_meta)
                i = n if end == -1 else end + len(tag)
                continue
        if c == "\\" and sql.startswith(("\\g", "\\gx", "\\gexec", "\\gset", "\\o", "\\!"), i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            meta.append(sql[i:end].strip())
            i = end
            continue
        if c == "\n":
            at_line_start = True
        out.append(c)
        i += 1
    return "".join(out), meta


def scan(sql: str) -> list[str]:
    """Return a sorted list of forbidden constructs found (empty list means allowed)."""
    code, meta = strip_comments_and_literals(sql)
    issues: set[str] = set()
    for line in meta:
        cmd = re.match(r"\\([A-Za-z!]+)", line)
        name = cmd.group(1).lower() if cmd else "?"
        if name not in SAFE_META:
            issues.add(f"psql meta-command \\{name}")
    for label, pattern in FORBIDDEN:
        if pattern.search(code):
            issues.add(label)
    return sorted(issues)
