"""Plan, start, status and cancel orchestration for declared jobs (FR-003–FR-016)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.jobs import guard, launcher, params, progress
from vps_mcp.jobs.integrity import check_script
from vps_mcp.jobs.report import finalize
from vps_mcp.jobs.store import FINAL, JobStore
from vps_mcp.migration import risk
from vps_mcp.migration.config import JobDeclaration
from vps_mcp.migration.runtime import DbAction, MigrationRuntime, run_db_action
from vps_mcp.migration.store import new_id
from vps_mcp.store.db import utcnow

PLAN_TTL = timedelta(minutes=30)
RECENT_LINES = 50
HASHED = (
    "job",
    "script",
    "script_sha256",
    "script_mode",
    "script_owner",
    "run_as",
    "params",
    "may_modify_names",
    "services_names",
    "max_runtime_h",
    "success_marker",
)


def store(rt: MigrationRuntime) -> JobStore:
    return JobStore(rt.app.db)


def declaration(rt: MigrationRuntime, name: Any) -> JobDeclaration:
    job = rt.settings.get_job(name) if isinstance(name, str) else None
    if job is None:
        raise VpsMcpError(ErrorCategory.NOT_FOUND, f"job '{str(name)[:60]}' is not declared")
    return job


def _hash(plan: dict[str, Any]) -> str:
    stable = {k: plan.get(k) for k in HASHED}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:16]


async def _busy(rt: MigrationRuntime, job: JobDeclaration) -> dict[str, Any] | None:
    for run in store(rt).running():
        return {"kind": "job", "id": run["run_id"]}
    if rt.store.active_rehearsals():
        return {"kind": "rehearsal", "id": rt.store.active_rehearsals()[0]["rehearsal_id"]}
    units = await launcher.active_units(rt)
    if units:
        return {"kind": "unit", "id": units[0]}
    pid = await launcher.running_elsewhere(rt, job.script)
    if pid:
        return {"kind": "hand_started", "id": pid}
    return None


async def _service_states(rt: MigrationRuntime, names: list[str]) -> list[dict[str, Any]]:
    decls = [d for d in rt.app.managed.all() if d.name in names]
    out = []
    for d in decls:
        try:
            st = await rt.app.backend(d.kind).status(d)
            out.append({"name": d.name, "kind": d.kind.value, "state": st.state.value})
        except VpsMcpError as err:
            out.append({"name": d.name, "kind": d.kind.value, "state": f"unknown ({err.message})"})
    return out


async def build_plan(rt: MigrationRuntime, job: JobDeclaration, supplied: Any) -> dict[str, Any]:
    resolved = params.resolve(job, supplied, datetime.now(UTC))
    integ = await check_script(rt, job)
    free, _total = await rt.db.free_and_total()
    min_free = int(job.min_free_gb * 1024**3)
    if free < min_free:
        raise VpsMcpError(
            ErrorCategory.INSUFFICIENT_DISK,
            f"job '{job.name}' needs at least {job.min_free_gb:g} GB free; "
            f"{free / 1024**3:.1f} GB available",
            extra={"disk": {"free_bytes": free, "min_free_bytes": min_free}},
        )
    may_modify = []
    for db in job.may_modify:
        info = await rt.db.db_info(db)
        may_modify.append(
            {"db": db, "exists": bool(info.get("exists")), "size_bytes": info.get("size_bytes")}
        )
    return {
        "job": job.name,
        "script": job.script,
        "script_sha256": integ["sha256"],
        "script_mode": integ["mode"],
        "script_owner": integ["owner"],
        "script_mtime": integ["mtime"],
        "run_as": job.run_as,
        "params": resolved,
        "may_modify": may_modify,
        "may_modify_names": list(job.may_modify),
        "services": await _service_states(rt, job.services),
        "services_names": list(job.services),
        "guarded": guard.scope(rt, job),
        "disk": {"free_bytes": free, "min_free_bytes": min_free},
        "max_runtime_h": job.max_runtime_h,
        "success_marker": job.success_marker,
        "running": await _busy(rt, job),
        "notes": [
            f"The script runs as {job.run_as} and cannot be confined: changes to anything not "
            "declared are detected and reported, not prevented.",
            "Databases in may_modify may be dropped and rebuilt by the script.",
            "The script is fingerprinted now and re-checked just before start.",
        ],
    }


async def plan(rt: MigrationRuntime, name: str, supplied: Any) -> dict[str, Any]:
    job = declaration(rt, name)
    p = await build_plan(rt, job, supplied)
    plan_id = _hash(p)
    store(rt).add_plan(new_id("job"), job.name, plan_id, p)
    public = {k: v for k, v in p.items() if not k.endswith("_names")}
    return {"plan_id": plan_id, "plan": public}


async def start(
    rt: MigrationRuntime,
    name: str,
    supplied: Any,
    plan_id: str | None,
    confirm: bool,
    session_id: str | None,
) -> dict[str, Any]:
    job = declaration(rt, name)

    async def preview() -> dict[str, Any]:
        return await plan(rt, name, supplied)

    async def body() -> dict[str, Any]:
        row = store(rt).by_plan(plan_id) if isinstance(plan_id, str) and plan_id else None
        stale = (
            row is None
            or row["status"] != "planned"
            or row["job"] != job.name
            or datetime.fromisoformat(row["created_at"]) < datetime.now(UTC) - PLAN_TTL
        )
        if stale:
            raise VpsMcpError(
                ErrorCategory.CONFLICT,
                "plan_id is unknown, used or older than 30 minutes; review the "
                "fresh plan and confirm with its plan_id",
                extra=await plan(rt, name, supplied),
            )
        assert row is not None
        fresh = await build_plan(rt, job, row["plan"]["params"])
        if _hash(fresh) != plan_id:
            raise VpsMcpError(
                ErrorCategory.CONFLICT,
                "the script or declaration changed since the plan; review the fresh plan",
                extra=await plan(rt, name, supplied),
            )
        if fresh["running"]:
            raise VpsMcpError(
                ErrorCategory.CONFLICT, f"something is already running: {fresh['running']}"
            )
        run_id = row["run_id"]
        offset = await launcher.file_size(rt, job.progress_file)
        base = await guard.readings(rt, job)
        unit = await launcher.start(rt, job, run_id, fresh["params"])
        store(rt).update(
            run_id,
            status="running",
            unit=unit,
            rundir=launcher.rundir(run_id),
            progress_offset=offset,
            baseline_json=base,
            started_at=utcnow(),
        )
        return {
            "run_id": run_id,
            "status": "running",
            "unit": unit,
            "next": "poll job_status; the job continues if this session ends",
        }

    return await run_db_action(
        rt,
        DbAction(
            action="run_job",
            target=job.name,
            confirm=confirm,
            params={"job": job.name, "params": supplied, "plan_id": plan_id},
            session_id=session_id,
            timeout=float(rt.settings.analysis_timeout_s) * 2,
            preview=preview,
        ),
        body,
    )


def _derive_state(
    run: dict[str, Any], unit: dict[str, Any], lines: list[progress.ProgressLine]
) -> str:
    if unit["active"]:
        return "running"
    if run.get("cancel_requested"):
        return "cancelled"
    if unit.get("result") == "timeout":
        return "timed_out"
    done = any(pl.outcome == "done" for pl in lines)
    if unit.get("exit_code") == 0 and done:
        return "succeeded"
    return "failed"


async def status(rt: MigrationRuntime, run_id: str | None = None) -> dict[str, Any]:
    st = store(rt)
    run = st.get(run_id) if isinstance(run_id, str) and run_id else st.latest()
    if run is None or run["status"] == "planned":
        raise VpsMcpError(ErrorCategory.NOT_FOUND, "no such job run (or it was never confirmed)")
    job = declaration(rt, run["job"])
    text = await launcher.read_from(rt, job.progress_file, int(run.get("progress_offset") or 0))
    lines = progress.parse(rt.mask(text), job.success_marker, job.outcome_patterns)
    findings: list[dict[str, Any]] = []
    if run["status"] == "running":
        unit = await launcher.unit_state(rt, run)
        now = await guard.readings(rt, job)
        findings = guard.compare(job, run.get("baseline") or {}, now)
        st.update(run["run_id"], last_guard_json=now)
        state = _derive_state(run, unit, lines)
        if state != "running":
            st.update(
                run["run_id"],
                status=state,
                exit_code=unit.get("exit_code"),
                systemd_result=unit.get("result"),
                ended_at=utcnow(),
            )
            await launcher.reset_failed(rt, run)
    else:
        findings = guard.compare(job, run.get("baseline") or {}, run.get("last_guard") or {})
    run = st.get(run["run_id"]) or run
    if run["status"] in FINAL and not run.get("report_id"):
        sha = None
        try:
            r = await rt.ex.run(["sha256sum", "--", job.script], sudo=True, timeout=60.0)
            sha = r.stdout.split()[0] if r.ok and r.stdout.split() else None
        except VpsMcpError:
            sha = None
        rep = await finalize(rt, run, job, lines, findings, run["status"], sha)
        st.update(run["run_id"], report_id=rep["id"])
        run = st.get(run["run_id"]) or run
    started = datetime.fromisoformat(run["started_at"]) if run.get("started_at") else None
    ended = datetime.fromisoformat(run["ended_at"]) if run.get("ended_at") else None
    elapsed = int(((ended or datetime.now(UTC)) - started).total_seconds()) if started else None
    out: dict[str, Any] = {
        "run_id": run["run_id"],
        "job": run["job"],
        "status": run["status"],
        "elapsed_s": elapsed,
        "steps": [pl.to_dict() for pl in lines if pl.outcome or pl.reported_failures],
        "recent": [pl.to_dict() for pl in lines[-RECENT_LINES:]],
        "guard_findings": findings,
    }
    disk = next((pl.disk_free for pl in reversed(lines) if pl.disk_free), None)
    if disk:
        out["disk_free"] = disk
    if run["status"] != "running" or any(pl.outcome == "failed" for pl in lines):
        out["console_tail"] = await launcher.console_tail(rt, run)
    if run.get("report_id"):
        stored = rt.store.get_report(run["report_id"])
        if stored:
            out["report"] = risk.summary(stored)
    return out


async def cancel(
    rt: MigrationRuntime, run_id: str, confirm: bool, session_id: str | None
) -> dict[str, Any]:
    st = store(rt)
    run = st.get(run_id) if isinstance(run_id, str) else None
    if run is None or run["status"] != "running":
        raise VpsMcpError(ErrorCategory.NOT_FOUND, "no running job with that run_id")
    job = declaration(rt, run["job"])

    async def preview() -> dict[str, Any]:
        text = await launcher.read_from(rt, job.progress_file, int(run.get("progress_offset") or 0))
        lines = progress.parse(rt.mask(text), job.success_marker, job.outcome_patterns)
        return {
            "will_stop": run["unit"],
            "current_step": lines[-1].to_dict() if lines else None,
            "warning": "declared databases may be left half-built and declared services "
            "stopped; nothing is repaired automatically",
        }

    async def body() -> dict[str, Any]:
        st.update(run["run_id"], cancel_requested=1)
        await launcher.stop(rt, run)
        unit = await launcher.unit_state(rt, run)
        left = await launcher.running_elsewhere(rt, job.script)
        if unit["active"] or left:
            raise VpsMcpError(
                ErrorCategory.FAILED,
                f"the job did not stop cleanly (unit active={unit['active']}, pids={left})",
            )
        st.update(
            run["run_id"], status="cancelled", ended_at=utcnow(), systemd_result=unit.get("result")
        )
        dbs = {}
        for db in job.may_modify:
            info = await rt.db.db_info(db)
            dbs[db] = {"exists": bool(info.get("exists")), "size_bytes": info.get("size_bytes")}
        services = {s["name"]: s["state"] for s in await _service_states(rt, job.services)}
        await status(rt, run["run_id"])  # builds the final report once
        return {
            "status": "cancelled",
            "run_id": run["run_id"],
            "left_behind": {"services": services, "databases": dbs},
        }

    return await run_db_action(
        rt,
        DbAction(
            action="cancel_job",
            target=job.name,
            confirm=confirm,
            params={"run_id": run_id},
            session_id=session_id,
            timeout=600.0,
            preview=preview,
        ),
        body,
    )


def list_jobs(rt: MigrationRuntime) -> dict[str, Any]:
    jobs = [
        {
            "name": j.name,
            "script": j.script,
            "may_modify": j.may_modify,
            "services": j.services,
            "source": j.source,
            "result": j.result,
            "max_runtime_h": j.max_runtime_h,
            "params": [
                {
                    "name": p.name,
                    "pattern": p.pattern,
                    "default": p.default,
                    "description": p.description,
                }
                for p in j.params
            ],
        }
        for j in rt.settings.job
    ]
    runs = [
        {
            "run_id": r["run_id"],
            "job": r["job"],
            "status": r["status"],
            "started_at": r.get("started_at"),
            "ended_at": r.get("ended_at"),
            "report_id": r.get("report_id"),
        }
        for r in store(rt).recent(10)
    ]
    return {"jobs": jobs, "runs": runs}
