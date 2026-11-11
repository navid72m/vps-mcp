"""US5: run operator-declared checks against a database (FR-025, research M8)."""

from __future__ import annotations

import time
from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.migration import risk
from vps_mcp.migration.config import CheckDefinition
from vps_mcp.migration.runtime import DbAction, MigrationRuntime, run_db_action
from vps_mcp.migration.understand import resolve_db


def render_argv(check: CheckDefinition, db: str, port: int) -> list[str]:
    out = []
    for arg in check.command:
        if arg == "{db}":
            out.append(db)
        elif arg == "{port}":
            out.append(str(port))
        elif arg.endswith("={db}"):
            out.append(arg[: -len("{db}")] + db)
        elif arg.endswith("={port}"):
            out.append(arg[: -len("{port}")] + str(port))
        else:
            out.append(arg)
    return out


def _foreign(
    before: dict[str, dict[str, int]],
    after: dict[str, dict[str, int]],
    watch: list[str],
    skip: str | None,
) -> list[dict[str, Any]]:
    out = []
    for db in sorted(watch):
        if db == skip or db not in before or db not in after:
            continue
        delta = after[db]["writes"] - before[db]["writes"]
        if delta > 0:
            out.append({"database": db, "writes": delta, "attributed": after[db]["sessions"] == 0})
    return out


async def _execute(
    rt: MigrationRuntime, check: CheckDefinition, db: str, is_copy: bool
) -> dict[str, Any]:
    argv = render_argv(check, db, rt.settings.port)
    watch = rt.settings.target_names()
    before = await rt.db.write_counters()
    env = (
        {"PGOPTIONS": "-c default_transaction_read_only=on"}
        if check.access == "read_only"
        else None
    )
    sudo = check.run_as != "vpsmcp"
    user = check.run_as if sudo and check.run_as != "root" else None
    started = time.monotonic()
    try:
        r = await rt.ex.run(argv, sudo=sudo, user=user, env=env, timeout=float(check.timeout_s))
        status = "pass" if r.ok else "fail"
        exit_code: int | None = r.exit_status
        output = (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()
    except VpsMcpError as err:
        if err.category is not ErrorCategory.TIMED_OUT:
            raise
        status, exit_code, output = "timeout", None, err.message
    duration = round(time.monotonic() - started, 1)
    after = await rt.db.write_counters()
    # A read-only check must not write anywhere; a may_modify check may write to its copy.
    foreign = _foreign(before, after, watch, skip=db if is_copy else None)
    result = {
        "check": check.name,
        "database": db,
        "status": status,
        "exit_code": exit_code,
        "duration_s": duration,
        "output_tail": rt.mask(output).splitlines()[-30:],
        "foreign_writes": foreign,
    }
    findings: list[dict[str, Any]] = []
    if status != "pass":
        findings.append(
            {
                "severity": "blocking" if check.blocking else "warning",
                "category": "check_failed",
                "object": f"check:{check.name}",
                "message": f"Check '{check.name}' on '{db}' {status}"
                + (f" (exit {exit_code})." if exit_code is not None else "."),
                "values": {"exit_code": exit_code},
            }
        )
    for f in foreign:
        findings.append(
            {
                "severity": "blocking" if f["attributed"] else "warning",
                "category": "foreign_write" if f["attributed"] else "foreign_write_unattributed",
                "object": f"database:{f['database']}",
                "message": f"While check '{check.name}' ran, {f['writes']} row writes hit "
                f"'{f['database']}'"
                + ("." if f["attributed"] else " (other sessions were active)."),
                "values": f,
            }
        )
    report = rt.store.latest_report_for(db)
    if report is not None:
        report.setdefault("check_results", []).append(result)
        report["findings"] = risk.sort_findings(report["findings"] + findings)
        report["counts"] = {
            s: sum(1 for f in report["findings"] if f["severity"] == s) for s in risk.SEVERITY_ORDER
        }
        report["verdict"] = risk.verdict(report["findings"])
        rt.store.save_report(report)
        result["report_id"] = report["id"]
    elif findings:
        rep = risk.build_report("check", {"db": db}, findings, [], check_results=[result])
        rt.store.save_report(rep)
        result["report_id"] = rep["id"]
    result["findings"] = findings
    return result


async def run_check(
    rt: MigrationRuntime, name: str, db: str, confirm: bool, session_id: str | None
) -> dict[str, Any]:
    check = rt.settings.get_check(name) if isinstance(name, str) else None
    if check is None:
        raise VpsMcpError(ErrorCategory.NOT_FOUND, f"check '{str(name)[:60]}' is not declared")
    dbname, is_copy = resolve_db(rt, db)
    if check.access == "read_only":
        return await _execute(rt, check, dbname, is_copy)
    if not is_copy:
        raise VpsMcpError(
            ErrorCategory.NOT_PERMITTED,
            f"check '{check.name}' may modify data and can only run on a rehearsal copy",
        )

    async def body() -> dict[str, Any]:
        return await _execute(rt, check, dbname, is_copy)

    return await run_db_action(
        rt,
        DbAction(
            action="run_check",
            target=dbname,
            confirm=confirm,
            params={"check": check.name, "db": dbname},
            session_id=session_id,
            timeout=float(check.timeout_s) + 120,
        ),
        body,
    )
