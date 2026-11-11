"""US4: summarise the operator's own pipeline runs (FR-024, research M7)."""

from __future__ import annotations

import fnmatch
import posixpath
import re
from collections import Counter
from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.migration.config import PipelineDefinition
from vps_mcp.migration.runtime import MigrationRuntime

MAX_BYTES = 2 * 1024 * 1024
GREP_PATTERN = " (ERROR|CRITICAL|WARNING) "
ODOO_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \d+ (?P<level>ERROR|CRITICAL|WARNING) "
    r"(?P<db>\S+) (?P<logger>[\w.\-]+): (?P<msg>.*)$"
)
DEFAULT_PROGRESS = [
    r"^(?P<step>START)\b\s*(?P<detail>.*)$",
    r"^(?P<step>SERVER STARTED)\b\s*(?P<detail>.*)$",
    r"^(?P<step>STEP\S*(?: [^\s=]+)*?)\s+(?P<detail>(?:ok|OK|FAILED|failed|done|START|EXIT -?\d+|rc=-?\d+)\b.*)$",
    r"^(?P<step>STEP\S*)\s*(?P<detail>.*)$",
]
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_NUM_RE = re.compile(r"\b0x[0-9a-fA-F]+\b|\b\d+(?:\.\d+)?\b")


def normalise(msg: str) -> str:
    return _NUM_RE.sub("#", _QUOTED_RE.sub("'…'", msg)).strip()[:300]


def step_status(detail: str) -> str:
    d = detail.strip()
    if re.search(r"\b(FAILED|failed)\b", d):
        return "failed"
    m = re.search(r"\b(?:rc=|EXIT )(-?\d+)", d)
    if m:
        return "ok" if m.group(1) == "0" else "failed"
    if re.match(r"(ok|OK|done)\b", d):
        return "ok"
    if d.startswith("START"):
        return "running"
    return "info"


def parse_progress(text: str, patterns: list[str] | None = None) -> dict[str, Any]:
    regexes = [re.compile(p) for p in (patterns or DEFAULT_PROGRESS)]
    started = None
    steps: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        for rx in regexes:
            m = rx.match(line)
            if not m:
                continue
            gd = m.groupdict()
            step = (gd.get("step") or "").strip()
            detail = (gd.get("detail") or "").strip()
            if step == "START":
                started = detail
            else:
                status = gd.get("status") or step_status(detail)
                if step == "SERVER STARTED":
                    status = "ok"
                steps.append({"name": step, "status": status, "raw": line[:300]})
            break
    return {"started": started, "steps": steps}


def summarise_log_lines(lines: list[str]) -> dict[str, Any]:
    by_level: Counter[str] = Counter()
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for line in lines:
        m = ODOO_LINE_RE.match(line)
        if not m:
            continue
        level = m["level"]
        by_level[level] += 1
        key = (level, f"{m['logger']}: {normalise(m['msg'])}")
        g = groups.setdefault(
            key,
            {
                "level": level,
                "message": key[1],
                "count": 0,
                "first_seen": m["ts"],
                "last_seen": m["ts"],
            },
        )
        g["count"] += 1
        g["first_seen"] = min(g["first_seen"], m["ts"])
        g["last_seen"] = max(g["last_seen"], m["ts"])
    rank = {"CRITICAL": 0, "ERROR": 1, "WARNING": 2}
    top = sorted(groups.values(), key=lambda g: (rank[g["level"]], -g["count"], g["message"]))
    return {"count_by_level": dict(sorted(by_level.items())), "top": top[:10]}


async def _expand(rt: MigrationRuntime, globs: list[str]) -> tuple[list[str], list[str]]:
    files: list[str] = []
    notes: list[str] = []
    for g in globs:
        d, pattern = posixpath.split(g)
        if any(ch in d for ch in "*?["):
            notes.append(f"{g}: wildcards are only supported in the file name")
            continue
        r = await rt.ex.run(["ls", "-1", "--", d], sudo=True, timeout=30.0)
        if not r.ok:
            notes.append(f"{d}: not readable")
            continue
        files += [
            posixpath.join(d, n)
            for n in sorted(r.stdout.split())
            if fnmatch.fnmatchcase(n, pattern)
        ]
    return sorted(set(files)), notes


async def last_run(rt: MigrationRuntime, pipeline: str) -> dict[str, Any]:
    pipe: PipelineDefinition | None = (
        rt.settings.get_pipeline(pipeline) if isinstance(pipeline, str) else None
    )
    if pipe is None:
        raise VpsMcpError(
            ErrorCategory.NOT_FOUND, f"pipeline '{str(pipeline)[:60]}' is not declared"
        )
    out: dict[str, Any] = {
        "pipeline": pipe.name,
        "progress_file": pipe.progress_file,
        "truncated": False,
        "notes": [],
    }
    if pipe.progress_file:
        r = await rt.ex.run(["cat", "--", pipe.progress_file], sudo=True, timeout=30.0)
        if r.ok:
            out.update(parse_progress(rt.mask(r.stdout[:MAX_BYTES]), pipe.progress_patterns))
        else:
            out["notes"].append(f"{pipe.progress_file}: not readable")
    files, notes = await _expand(rt, pipe.log_globs)
    out["notes"] += notes
    out["log_files"] = files
    if files:
        r = await rt.ex.run(
            ["grep", "-E", "-h", "-m", "20000", "--", GREP_PATTERN, *files],
            sudo=True,
            timeout=float(rt.settings.analysis_timeout_s),
        )
        text = r.stdout
        if len(text) > MAX_BYTES:
            text = text[:MAX_BYTES]
            out["truncated"] = True
        out["errors"] = summarise_log_lines(rt.mask(text).splitlines())
        for g in out["errors"]["top"]:
            g["message"] = rt.mask(g["message"])
    else:
        out["errors"] = {"count_by_level": {}, "top": []}
    return out
