"""Finding classification and verdict (FR-012, FR-013, data-model §3). Pure functions."""

from __future__ import annotations

import re
from typing import Any

from vps_mcp.migration.config import RiskThresholds
from vps_mcp.migration.store import new_id
from vps_mcp.store.db import utcnow

SEVERITY_ORDER = {"blocking": 0, "warning": 1, "info": 2}
VERDICTS = ("do_not_promote", "review_warnings", "safe_to_promote")

_INT_RANK = {"smallint": 1, "integer": 2, "bigint": 3}
_FLOAT_RANK = {"real": 1, "double precision": 2}
_PARAM_RE = re.compile(r"^(?P<base>[a-z ]+?)(?:\((?P<args>[0-9, ]+)\))?(?P<arr>(\[\])*)$")


def parse_type(t: str) -> tuple[str, tuple[int, ...] | None, bool]:
    """'character varying(64)' -> ('character varying', (64,), False)."""
    m = _PARAM_RE.match((t or "").strip())
    if not m:
        return (t or "").strip(), None, False
    args = tuple(int(x) for x in m["args"].split(",")) if m["args"] else None
    return m["base"].strip(), args, bool(m["arr"])


def type_change(old: str, new: str) -> str:
    """Classify a column type change as 'narrowed', 'widened' or 'changed'."""
    ob, oa, o_arr = parse_type(old)
    nb, na, n_arr = parse_type(new)
    if o_arr != n_arr:
        return "narrowed"
    text_like = {"text", "character varying"}
    if ob in text_like and nb in text_like:
        o_lim = oa[0] if ob == "character varying" and oa else None
        n_lim = na[0] if nb == "character varying" and na else None
        if o_lim == n_lim:
            return "changed"
        if n_lim is None:
            return "widened"
        if o_lim is None or n_lim < o_lim:
            return "narrowed"
        return "widened"
    if ob == "character" and nb == "character":
        return "narrowed" if (na or (1,))[0] < (oa or (1,))[0] else "widened"
    if ob == "character" and nb in text_like:
        return "widened"
    if ob in _INT_RANK and nb in _INT_RANK:
        return "narrowed" if _INT_RANK[nb] < _INT_RANK[ob] else "widened"
    if ob in _INT_RANK and nb == "numeric":
        return "widened"
    if ob == "numeric" and nb == "numeric":
        if oa == na:
            return "changed"
        if na is None:
            return "widened"
        if oa is None:
            return "narrowed"
        op, os_ = oa[0], (oa[1] if len(oa) > 1 else 0)
        np_, ns = na[0], (na[1] if len(na) > 1 else 0)
        if np_ < op or ns < os_:
            return "narrowed"
        return "widened"
    if ob in _FLOAT_RANK and nb in _FLOAT_RANK:
        return "narrowed" if _FLOAT_RANK[nb] < _FLOAT_RANK[ob] else "widened"
    if ob == "timestamp with time zone" and nb == "timestamp without time zone":
        return "narrowed"
    if ob == "timestamp without time zone" and nb == "timestamp with time zone":
        return "changed"
    if ob == "date" and nb.startswith("timestamp"):
        return "widened"
    if ob in ("json",) and nb == "jsonb":
        return "changed"
    return "narrowed"  # change between type families (research M3)


def _f(
    severity: str, category: str, obj: str, message: str, values: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "severity": severity,
        "category": category,
        "object": obj,
        "message": message,
        "values": values or {},
    }


def _fmt(n: int | None) -> str:
    return "unknown" if n is None else f"{n:,}"


def classify(raw: list[dict[str, Any]], t: RiskThresholds) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in raw:
        k, obj, v = d["kind"], d["object"], d["values"]
        if k == "table_removed":
            rows = v.get("rows_before")
            if rows:
                out.append(
                    _f(
                        "blocking",
                        "table_removed",
                        obj,
                        f"Table {obj} was removed; it held {_fmt(rows)} rows"
                        + ("" if v.get("exact") else " (estimated)")
                        + ".",
                        v,
                    )
                )
            else:
                out.append(_f("info", "table_removed", obj, f"Empty table {obj} was removed.", v))
        elif k == "column_removed":
            rows = v.get("table_rows")
            if rows:
                out.append(
                    _f(
                        "blocking",
                        "column_removed",
                        obj,
                        f"Column {obj} ({v.get('type')}) was removed from a table with "
                        f"{_fmt(rows)} rows; its data is lost.",
                        v,
                    )
                )
            else:
                out.append(
                    _f(
                        "info",
                        "column_removed",
                        obj,
                        f"Column {obj} was removed from an empty table.",
                        v,
                    )
                )
        elif k == "table_added":
            out.append(_f("info", "table_added", obj, f"Table {obj} was added."))
        elif k == "column_added":
            out.append(
                _f("info", "column_added", obj, f"Column {obj} ({v.get('type')}) was added.", v)
            )
        elif k == "type_change":
            kind = type_change(v["from"], v["to"])
            if kind == "narrowed":
                out.append(
                    _f(
                        "warning",
                        "type_narrowed",
                        obj,
                        f"Column {obj} type narrowed from {v['from']} to {v['to']}; values "
                        "may have been truncated or rejected.",
                        v,
                    )
                )
            else:
                out.append(
                    _f(
                        "info",
                        f"type_{kind}",
                        obj,
                        f"Column {obj} type {kind} from {v['from']} to {v['to']}.",
                        v,
                    )
                )
        elif k in ("index_added", "index_removed", "constraint_added"):
            verb = "added" if k.endswith("added") else "removed"
            what = "Index" if k.startswith("index") else "Constraint"
            out.append(_f("info", k, obj, f"{what} {obj} was {verb}.", v))
        elif k == "constraint_removed":
            sev = "warning" if v.get("type") in ("f", "u", "c", "p", "x") else "info"
            label = {
                "f": "foreign key",
                "u": "unique",
                "c": "check",
                "p": "primary key",
                "x": "exclusion",
                "n": "not-null",
            }.get(v.get("type", ""), "constraint")
            out.append(
                _f(sev, "constraint_removed", obj, f"The {label} constraint {obj} was removed.", v)
            )
        elif k == "row_change":
            b, a = v["before"], v["after"]
            if b and a < b:
                pct = round((b - a) * 100 / b, 2)
                vals = {**v, "drop_pct": pct}
                msg = (
                    f"Table {obj} lost {pct}% of its rows ({_fmt(b)} → {_fmt(a)})"
                    + ("" if v["exact"] else ", estimated from statistics")
                    + "."
                )
                if pct > t.row_drop_block_pct and v["exact"]:
                    out.append(_f("blocking", "row_drop", obj, msg, vals))
                elif pct > t.row_drop_warn_pct:
                    out.append(_f("warning", "row_drop", obj, msg, vals))
                else:
                    out.append(_f("info", "row_drop", obj, msg, vals))
            else:
                out.append(
                    _f(
                        "info",
                        "row_change",
                        obj,
                        f"Table {obj} rows changed {_fmt(b)} → {_fmt(a)}.",
                        v,
                    )
                )
        elif k == "module_not_upgraded":
            out.append(
                _f(
                    "blocking",
                    "module_not_upgraded",
                    obj,
                    f"{obj} is installed but still at version {v['version']} "
                    f"(expected {v['expected_major']}.x).",
                    v,
                )
            )
        elif k == "module_transitional":
            out.append(
                _f(
                    "blocking",
                    "module_transitional",
                    obj,
                    f"{obj} is left in state '{v['state']}'.",
                    v,
                )
            )
        elif k == "module_removed":
            out.append(
                _f(
                    "warning",
                    "module_removed",
                    obj,
                    f"{obj} was installed before and is '{v.get('state_after') or 'absent'}' "
                    "after.",
                    v,
                )
            )
        elif k == "module_code_missing":
            out.append(
                _f(
                    "warning",
                    "module_code_missing",
                    obj,
                    f"{obj} is installed but its code is not in the addons paths.",
                    v,
                )
            )
        elif k in ("module_upgraded", "module_added"):
            out.append(_f("info", k, obj, f"{obj}: {k.replace('_', ' ')}.", v))
        elif k == "view_deactivated":
            out.append(_f("warning", "view_deactivated", obj, f"{obj} was deactivated.", v))
        elif k == "view_orphaned":
            out.append(
                _f(
                    "warning",
                    "view_orphaned",
                    obj,
                    f"{obj} inherits from or belongs to a module that is not installed.",
                    v,
                )
            )
        elif k == "view_removed":
            out.append(_f("info", "view_removed", obj, f"{obj} was removed.", v))
        else:
            out.append(_f("info", k, obj, k.replace("_", " "), v))
    return sort_findings(out)


def sort_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        findings, key=lambda f: (SEVERITY_ORDER[f["severity"]], f["category"], f["object"])
    )


def verdict(findings: list[dict[str, Any]]) -> str:
    sev = {f["severity"] for f in findings}
    if "blocking" in sev:
        return "do_not_promote"
    if "warning" in sev:
        return "review_warnings"
    return "safe_to_promote"


def build_report(
    kind: str,
    inputs: dict[str, Any],
    findings: list[dict[str, Any]],
    skipped: list[str],
    **extra: Any,
) -> dict[str, Any]:
    findings = sort_findings(findings)
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITY_ORDER}
    report = {
        "id": new_id("rpt"),
        "created_at": utcnow(),
        "kind": kind,
        "inputs": inputs,
        "verdict": verdict(findings),
        "counts": counts,
        "findings": findings,
        "skipped_checks": sorted(set(skipped)),
    }
    report.update({k: v for k, v in extra.items() if v is not None})
    return report


def summary(report: dict[str, Any], top: int = 20) -> dict[str, Any]:
    out = {
        "report_id": report["id"],
        "verdict": report["verdict"],
        "counts": report["counts"],
        "top_findings": report["findings"][:top],
        "skipped_checks": report["skipped_checks"],
    }
    for k in (
        "step_results",
        "check_results",
        "rehearsal_id",
        "job_state",
        "run_id",
        "report_excerpt",
    ):
        if k in report:
            out[k] = report[k]
    return out
