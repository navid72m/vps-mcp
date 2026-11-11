"""US2: compare two databases and produce a stored risk report (FR-011–FR-015)."""

from __future__ import annotations

from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.migration import diff as diffmod
from vps_mcp.migration import risk, snapshot
from vps_mcp.migration.runtime import MigrationRuntime
from vps_mcp.migration.understand import addons_listing, resolve_db


async def compare_snapshots(
    rt: MigrationRuntime,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    exact_counts: bool = True,
    extra_findings: list[dict[str, Any]] | None = None,
    kind: str = "compare",
    **extra: Any,
) -> dict[str, Any]:
    code = await addons_listing(rt) if after.get("modules") is not None else None
    raw, skipped = diffmod.diff(before, after, code)
    if exact_counts:
        th = rt.settings.thresholds
        b_names, a_names = diffmod.exact_count_candidates(
            raw, before, after, th.exact_count_trigger_pct, th.exact_count_top_n
        )
        if b_names:
            await snapshot.add_exact_counts(rt, before, b_names)
        if a_names:
            await snapshot.add_exact_counts(rt, after, a_names)
        if b_names or a_names:
            raw, skipped = diffmod.diff(before, after, code)
    else:
        skipped.append("exact row counts: disabled; row losses are estimates")
    findings = risk.classify(raw, rt.settings.thresholds) + list(extra_findings or [])
    report = risk.build_report(
        kind,
        {
            "before": before["database"],
            "after": after["database"],
            "before_snapshot": before["id"],
            "after_snapshot": after["id"],
        },
        findings,
        skipped,
        **extra,
    )
    rt.store.save_report(report)
    return report


async def compare(
    rt: MigrationRuntime, before: str, after: str, exact_counts: bool = True
) -> dict[str, Any]:
    b_name, b_copy = resolve_db(rt, before)
    a_name, a_copy = resolve_db(rt, after)
    if b_name == a_name:
        raise VpsMcpError(ErrorCategory.INVALID_INPUT, "before and after must differ")
    b = await snapshot.capture(rt, b_name, is_copy=b_copy)
    a = await snapshot.capture(rt, a_name, is_copy=a_copy)
    report = await compare_snapshots(rt, b, a, exact_counts=exact_counts)
    return risk.summary(report)


def get_report(
    rt: MigrationRuntime,
    report_id: str,
    severity: str | None = None,
    category: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict[str, Any]:
    if not isinstance(report_id, str) or not report_id.startswith("rpt_") or len(report_id) > 20:
        raise VpsMcpError(ErrorCategory.INVALID_INPUT, "report_id must look like rpt_<hex>")
    report = rt.store.get_report(report_id)
    if report is None:
        raise VpsMcpError(ErrorCategory.NOT_FOUND, f"report '{report_id}' not found")
    findings = report["findings"]
    if severity:
        findings = [f for f in findings if f["severity"] == severity]
    if category:
        findings = [f for f in findings if f["category"] == category]
    pages = max(1, -(-len(findings) // per_page))
    if not 1 <= page <= pages:
        raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"page must be 1..{pages}")
    return {
        "summary": risk.summary(report, top=0),
        "findings": findings[(page - 1) * per_page : page * per_page],
        "page": page,
        "pages": pages,
        "total": len(findings),
    }
