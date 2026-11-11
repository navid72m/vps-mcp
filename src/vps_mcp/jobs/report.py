"""Final job report and verdict (research J8, FR-014, FR-015)."""

from __future__ import annotations

from typing import Any

from vps_mcp.errors import VpsMcpError
from vps_mcp.jobs.progress import ProgressLine
from vps_mcp.migration import risk, snapshot
from vps_mcp.migration.compare import compare_snapshots
from vps_mcp.migration.config import JobDeclaration
from vps_mcp.migration.runtime import MigrationRuntime

EXCERPT_LINES = 200


async def _excerpt(rt: MigrationRuntime, job: JobDeclaration) -> dict[str, Any] | None:
    if not job.report_file:
        return None
    r = await rt.ex.run(["cat", "--", job.report_file], sudo=True, timeout=60.0)
    if not r.ok:
        return {"file": job.report_file, "lines": [], "note": "not readable or missing"}
    lines = rt.mask(r.stdout).splitlines()
    return {
        "file": job.report_file,
        "lines": lines[:EXCERPT_LINES],
        "truncated": len(lines) > EXCERPT_LINES,
    }


def step_findings(lines: list[ProgressLine]) -> list[dict[str, Any]]:
    out = []
    for i, pl in enumerate(lines):
        if pl.outcome == "failed":
            out.append(
                {
                    "severity": "blocking",
                    "category": "step_failed",
                    "object": f"step:{i + 1}:{pl.step or 'step'}",
                    "message": f"Job step failed: {pl.text[:200]}",
                    "values": {"ts": pl.ts},
                }
            )
        if pl.reported_failures:
            out.append(
                {
                    "severity": "warning",
                    "category": "job_reported_failures",
                    "object": f"step:{i + 1}:{pl.step or 'summary'}",
                    "message": f"The job reported {pl.reported_failures} failure(s): "
                    f"{pl.text[:200]}",
                    "values": {"failures": pl.reported_failures},
                }
            )
    return out


async def finalize(
    rt: MigrationRuntime,
    run: dict[str, Any],
    job: JobDeclaration,
    lines: list[ProgressLine],
    guard_findings: list[dict[str, Any]],
    state: str,
    script_sha_now: str | None,
) -> dict[str, Any]:
    findings = step_findings(lines) + list(guard_findings)
    if state != "succeeded":
        findings.append(
            {
                "severity": "blocking",
                "category": "job_not_succeeded",
                "object": f"job:{job.name}",
                "message": f"Job '{job.name}' ended as '{state}'.",
                "values": {"state": state},
            }
        )
    plan_sha = (run.get("plan") or {}).get("script_sha256")
    if script_sha_now and plan_sha and script_sha_now != plan_sha:
        findings.append(
            {
                "severity": "warning",
                "category": "script_changed_during_run",
                "object": f"script:{job.script}",
                "message": "The job script changed while the job was running.",
                "values": {},
            }
        )
    excerpt = await _excerpt(rt, job)
    steps = [pl.to_dict() for pl in lines if pl.outcome or pl.reported_failures]
    extra = {
        "step_results": steps,
        "report_excerpt": excerpt,
        "job_state": state,
        "run_id": run["run_id"],
    }
    inputs = {"job": job.name, "run_id": run["run_id"], "source": job.source, "result": job.result}
    report: dict[str, Any]
    if state == "succeeded" and job.source and job.result:
        try:
            before = await snapshot.capture(rt, job.source)
            after = await snapshot.capture(rt, job.result)
            report = await compare_snapshots(
                rt, before, after, extra_findings=findings, kind="job", **extra
            )
            report["inputs"].update(inputs)
        except VpsMcpError as err:
            report = risk.build_report(
                "job", inputs, findings, [f"comparison failed: {err.message}"], **extra
            )
    else:
        why = (
            "comparison skipped: job did not succeed"
            if state != "succeeded"
            else "comparison skipped: job declares no source/result"
        )
        report = risk.build_report("job", inputs, findings, [why], **extra)
    if state != "succeeded":
        report["verdict"] = "do_not_promote"
    rt.store.save_report(report)
    return report
