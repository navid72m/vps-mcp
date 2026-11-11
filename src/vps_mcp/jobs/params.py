"""Declared job parameters (research J7). Pure functions."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from vps_mcp.errors import invalid
from vps_mcp.migration.config import DATE_TEMPLATE_RE, JobDeclaration

MAX_VALUE_LEN = 256


def render_default(template: str, now: datetime) -> str:
    return DATE_TEMPLATE_RE.sub(lambda m: now.strftime(m.group(1)), template)


def resolve(job: JobDeclaration, supplied: Any, now: datetime) -> dict[str, str]:
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, dict):
        raise invalid("params must be an object of NAME: value")
    declared = {p.name: p for p in job.params}
    unknown = sorted(str(k)[:40] for k in supplied if k not in declared)
    if unknown:
        raise invalid(f"undeclared parameter(s) for job '{job.name}': {', '.join(unknown)}")
    out: dict[str, str] = {}
    for name, decl in declared.items():
        if name in supplied:
            value = supplied[name]
        elif decl.default is not None:
            value = render_default(decl.default, now)
        else:
            continue
        if not isinstance(value, str):
            raise invalid(f"parameter {name}: value must be a string")
        if len(value) > MAX_VALUE_LEN or "\x00" in value or "\n" in value or "\r" in value:
            raise invalid(f"parameter {name}: value too long or contains control characters")
        if not re.fullmatch(decl.pattern, value):
            raise invalid(f"parameter {name}: value does not match {decl.pattern}")
        out[name] = value
    return out
