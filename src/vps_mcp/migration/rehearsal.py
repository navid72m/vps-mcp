"""US3: plan, start, poll, cancel and finalise rehearsals (FR-016–FR-023, research M6)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.migration import copies, risk, runner, snapshot, sqlscan
from vps_mcp.migration.compare import compare_snapshots
from vps_mcp.migration.config import DB_NAME_RE, PipelineDefinition
from vps_mcp.migration.diff import base_major
from vps_mcp.migration.runtime import DbAction, MigrationRuntime, run_db_action
from vps_mcp.migration.store import new_id
from vps_mcp.migration.understand import resolve_db

PLAN_TTL = timedelta(minutes=30)
ACTIVE = ("copying", "running")


def _pipeline(rt: MigrationRuntime, name: str) -> PipelineDefinition:
    p = rt.settings.get_pipeline(name) if isinstance(name, str) else None
    if p is None:
        raise VpsMcpError(ErrorCategory.NOT_FOUND, f"pipeline '{str(name)[:60]}' is not declared")
    return p


async def _file_shas(rt: MigrationRuntime, pipe: PipelineDefinition) -> dict[str, str]:
    files = sorted({s.file for s in pipe.steps if s.file})
    if not files:
        return {}
    r = await rt.ex.run(["sha256sum", "--", *files], sudo=True, timeout=60.0)
    shas: dict[str, str] = {}
    for line in r.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            shas[parts[1].strip().lstrip("*")] = parts[0]
    missing = [f for f in files if f not in shas]
    if missing:
        raise VpsMcpError(
            ErrorCategory.INVALID_INPUT,
            f"pipeline file(s) not found or unreadable: {', '.join(missing)}",
        )
    return shas


async def _scan_sql(rt: MigrationRuntime, pipe: PipelineDefinition) -> None:
    for step in pipe.steps:
        if step.kind != "sql" or not step.file:
            continue
        r = await rt.ex.run(["cat", "--", step.file], sudo=True, timeout=60.0)
        if not r.ok:
            raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"cannot read {step.file}")
        issues = sqlscan.scan(r.stdout)
        if issues:
            raise VpsMcpError(
                ErrorCategory.INVALID_INPUT,
                f"step '{step.name}' ({step.file}) contains forbidden constructs: "
                + "; ".join(issues),
            )


async def _active_units(rt: MigrationRuntime) -> list[str]:
    r = await rt.ex.run(
        [
            "systemctl",
            "list-units",
            "--plain",
            "--no-legend",
            "--state=active",
            "vps-mcp-rehearsal-*",
            "vps-mcp-job-*",
        ],
        timeout=30.0,
    )
    return [line.split()[0] for line in r.stdout.splitlines() if line.strip()]


HASHED_FIELDS = (
    "pipeline",
    "source",
    "copy_name",
    "method",
    "steps",
    "runner_sha256",
    "watched_databases",
    "min_free_bytes",
)


def _plan_hash(plan: dict[str, Any]) -> str:
    """Hash of what the operator approves; live free-disk numbers are re-checked separately."""
    stable = {k: plan.get(k) for k in HASHED_FIELDS}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:16]


async def _build(
    rt: MigrationRuntime,
    pipe: PipelineDefinition,
    source: str,
    method: str,
    *,
    rehearsal_id: str | None = None,
    copy_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    info = await rt.db.db_info(source)
    if not info.get("exists"):
        raise VpsMcpError(ErrorCategory.NOT_FOUND, f"database '{source}' does not exist")
    chosen = copies.choose_method(method, int(info.get("sessions") or 0))
    shas = await _file_shas(rt, pipe)
    await _scan_sql(rt, pipe)
    old_major = None
    if any(s.kind == "odoo_upgrade" and s.modules == "stale" for s in pipe.steps):
        mods = (
            await rt.db.query(source, "modules")
            if (await rt.db.query(source, "overview")).get("has_odoo")
            else None
        )
        old_major = base_major(mods)
        if not old_major:
            raise VpsMcpError(
                ErrorCategory.INVALID_INPUT,
                f"'{source}' has no Odoo base module; cannot plan stale upgrades",
            )
    disk = await copies.disk_plan(rt, int(info.get("size_bytes") or 0))
    rid = rehearsal_id or new_id("rhs")
    row = copy_row or copies.new_copy_row(rt, source, chosen, rid)
    watch = [t for t in rt.settings.target_names() if DB_NAME_RE.fullmatch(t)]
    files, runner_sha = runner.render(
        rehearsal_id=rid,
        pipeline=pipe,
        settings=rt.settings,
        source=source,
        copy=row["name"],
        owner=str(info.get("owner") or "postgres"),
        method=chosen,
        marker=row["marker"],
        file_shas=shas,
        watch=watch,
        min_free=disk["min_free_bytes"],
        old_major=old_major,
    )
    steps = []
    for n, s in enumerate(pipe.steps, start=1):
        steps.append(
            {
                "n": n,
                "name": s.name,
                "kind": s.kind,
                "file": s.file,
                "sha256": shas.get(s.file or "") or None,
                "guarantee": "detected" if s.kind == "script" else "bound",
            }
        )
    public = {
        "pipeline": pipe.name,
        "source": source,
        "copy_name": row["name"],
        "method": chosen,
        "steps": steps,
        "runner_sha256": runner_sha,
        "watched_databases": sorted(watch),
        **{k: v for k, v in disk.items() if k != "total_bytes"},
    }
    return {
        "rehearsal_id": rid,
        "copy_row": row,
        "public": public,
        "files": files,
        "owner": info.get("owner"),
        "old_major": old_major,
    }


async def plan(
    rt: MigrationRuntime, pipeline: str, source: str, method: str = "auto"
) -> dict[str, Any]:
    pipe = _pipeline(rt, pipeline)
    src, _ = resolve_db(rt, source, allow_copies=False)
    built = await _build(rt, pipe, src, method)
    public = built["public"]
    phash = _plan_hash(public)
    rt.store.add_rehearsal(
        {
            "rehearsal_id": built["rehearsal_id"],
            "pipeline": pipe.name,
            "source": src,
            "plan_hash": phash,
            "status": "planned",
            "plan": {"public": public, "copy_row": built["copy_row"], "method": method},
        }
    )
    return {"plan_hash": phash, "plan": public}


async def _upload(rt: MigrationRuntime, rid: str, files: dict[str, str]) -> None:
    rdir = runner.rehearsal_dir(rid)
    r = await rt.ex.run(
        ["install", "-d", "-m", "0700", "-o", "root", "-g", "root", rdir], sudo=True, timeout=30.0
    )
    if not r.ok:
        raise VpsMcpError(ErrorCategory.FAILED, f"cannot create {rdir}: {r.stderr.strip()}")
    for name, content in sorted(files.items()):
        w = await rt.ex.run(["tee", "--", f"{rdir}/{name}"], sudo=True, stdin=content, timeout=30.0)
        if not w.ok:
            raise VpsMcpError(ErrorCategory.FAILED, f"cannot write {rdir}/{name}")


async def start(
    rt: MigrationRuntime,
    pipeline: str,
    source: str,
    method: str,
    plan_hash: str,
    confirm: bool,
    session_id: str | None,
) -> dict[str, Any]:
    pipe = _pipeline(rt, pipeline)
    src, _ = resolve_db(rt, source, allow_copies=False)

    async def preview() -> dict[str, Any]:
        return await plan(rt, pipeline, src, method)

    async def body() -> dict[str, Any]:
        row = rt.store.rehearsal_by_plan(plan_hash) if isinstance(plan_hash, str) else None
        fresh_needed = (
            row is None
            or row["status"] != "planned"
            or row["pipeline"] != pipe.name
            or row["source"] != src
            or datetime.fromisoformat(row["created_at"]) < datetime.now(UTC) - PLAN_TTL
        )
        if fresh_needed:
            new = await plan(rt, pipeline, src, method)
            raise VpsMcpError(
                ErrorCategory.CONFLICT,
                "plan_hash is unknown, used or older than 30 minutes; review the "
                "fresh plan and confirm with its plan_hash",
                extra=new,
            )
        assert row is not None
        await refresh_active(rt)
        if rt.store.active_rehearsals() or await _active_units(rt):
            raise VpsMcpError(ErrorCategory.CONFLICT, "another rehearsal is already running")
        stored = row["plan"]
        built = await _build(
            rt,
            pipe,
            src,
            stored["method"],
            rehearsal_id=row["rehearsal_id"],
            copy_row=stored["copy_row"],
        )
        if _plan_hash(built["public"]) != plan_hash:
            new = await plan(rt, pipeline, src, method)
            raise VpsMcpError(
                ErrorCategory.CONFLICT,
                "something changed since the plan was made (files, disk or source "
                "sessions); review the fresh plan",
                extra=new,
            )
        rid = row["rehearsal_id"]
        snap = await snapshot.capture(rt, src)
        copy_row = dict(stored["copy_row"])
        copy_row["state"] = "creating"
        rt.store.add_copy(copy_row)
        await _upload(rt, rid, built["files"])
        unit = f"vps-mcp-rehearsal-{rid}"
        r = await rt.ex.run(
            [
                "systemd-run",
                "--unit",
                unit,
                "--collect",
                "--property=TimeoutStopSec=90",
                "/bin/bash",
                f"{runner.rehearsal_dir(rid)}/run.sh",
            ],
            sudo=True,
            timeout=60.0,
        )
        if not r.ok:
            rt.store.update_copy(copy_row["copy_id"], state="failed")
            raise VpsMcpError(ErrorCategory.FAILED, f"could not start the runner: {r.stderr}")
        await copies.schedule_expiry(rt, copy_row)
        rt.store.update_rehearsal(
            rid,
            status="copying",
            unit=unit,
            copy_id=copy_row["copy_id"],
            source_snapshot_id=snap["id"],
        )
        return {
            "rehearsal_id": rid,
            "status": "copying",
            "unit": unit,
            "copy": copy_row["name"],
            "next": "poll rehearsal_status; the run continues if this session ends",
        }

    return await run_db_action(
        rt,
        DbAction(
            action="rehearse",
            target=src,
            confirm=confirm,
            params={"pipeline": pipeline, "source": src, "method": method, "plan_hash": plan_hash},
            session_id=session_id,
            timeout=float(rt.settings.analysis_timeout_s) * 2,
            preview=preview,
        ),
        body,
    )


async def _poll(rt: MigrationRuntime, row: dict[str, Any]) -> dict[str, Any]:
    rid = row["rehearsal_id"]
    rdir = runner.rehearsal_dir(rid)
    r = await rt.ex.run(["cat", "--", f"{rdir}/events.log"], sudo=True, timeout=30.0)
    n = len(row["plan"]["public"]["steps"])
    parsed = runner.parse_events(r.stdout if r.ok else "", n)
    if parsed["final"] is None:
        u = await rt.ex.run(
            ["systemctl", "show", "-p", "ActiveState", "--value", row["unit"] or ""], timeout=30.0
        )
        if u.stdout.strip() in ("inactive", "failed", ""):
            parsed["final"] = parsed["status"] = "failed"
            parsed["runner_crashed"] = True
    for st, plan_step in zip(parsed["steps"], row["plan"]["public"]["steps"], strict=True):
        st["name"] = plan_step["name"]
        st["kind"] = plan_step["kind"]
        if st["status"] in ("failed", "timeout", "cancelled", "running"):
            t = await rt.ex.run(
                ["tail", "-n", "20", "--", f"{rdir}/step-{st['n']}.log"], sudo=True, timeout=30.0
            )
            st["log_tail"] = rt.mask(t.stdout).splitlines()[-20:]
    return parsed


async def refresh_active(rt: MigrationRuntime) -> None:
    for row in rt.store.active_rehearsals():
        parsed = await _poll(rt, row)
        await _record(rt, row, parsed)


async def _record(rt: MigrationRuntime, row: dict[str, Any], parsed: dict[str, Any]) -> None:
    rid = row["rehearsal_id"]
    status = parsed["final"] or parsed["status"]
    rt.store.update_rehearsal(rid, status=status, step_results_json=parsed["steps"])
    if parsed["final"] and not row.get("report_id"):
        await finalize(rt, rt.store.rehearsal(rid) or row, parsed)


async def finalize(rt: MigrationRuntime, row: dict[str, Any], parsed: dict[str, Any]) -> None:
    rid = row["rehearsal_id"]
    copy_row = rt.store.copy_by_id(row["copy_id"]) if row.get("copy_id") else None
    extra: list[dict[str, Any]] = []
    for st in parsed["steps"]:
        if st["status"] in ("failed", "timeout"):
            reason = st.get("reason") or f"exit code {st.get('exit_code')}"
            extra.append(
                {
                    "severity": "blocking",
                    "category": "step_failed",
                    "object": f"step:{st['n']}:{st.get('name')}",
                    "message": f"Step '{st.get('name')}' {st['status']} ({reason}).",
                    "values": {k: st.get(k) for k in ("exit_code", "reason")},
                }
            )
    for f in parsed["foreign"]:
        extra.append(
            {
                "severity": "blocking" if f["attributed"] else "warning",
                "category": "foreign_write" if f["attributed"] else "foreign_write_unattributed",
                "object": f"database:{f['database']}",
                "message": (
                    f"During step {f['step']}, {f['writes']} row writes hit non-copy "
                    f"database '{f['database']}'"
                    + (
                        "."
                        if f["attributed"]
                        else ", which also had other active sessions (may not be the step)."
                    )
                ),
                "values": f,
            }
        )
    final = parsed["final"]
    if final == "aborted_disk":
        extra.append(
            {
                "severity": "blocking",
                "category": "disk_abort",
                "object": "disk",
                "message": "The rehearsal was aborted because free disk fell below the minimum.",
                "values": {},
            }
        )
    if final == "copy_failed" or copy_row is None or parsed.get("copy") != "ok":
        extra.append(
            {
                "severity": "blocking",
                "category": "step_failed",
                "object": "copy",
                "message": "The disposable copy was not completed (failed or cancelled); "
                "nothing was rehearsed.",
                "values": {},
            }
        )
        report = risk.build_report(
            "rehearsal",
            {"before": row["source"], "after": None, "rehearsal_id": rid},
            extra,
            [],
            step_results=parsed["steps"],
            rehearsal_id=rid,
        )
        rt.store.save_report(report)
        if copy_row:  # the runner dropped any partial copy; nothing is left to clean up
            rt.store.update_copy(
                copy_row["copy_id"],
                state="dropped",
                dropped_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
            )
    else:
        rt.store.update_copy(copy_row["copy_id"], state="ready")
        before = (
            rt.store.get_snapshot(row["source_snapshot_id"])
            if row.get("source_snapshot_id")
            else None
        )
        if before is None:
            before = await snapshot.capture(rt, row["source"])
        after = await snapshot.capture(rt, copy_row["name"], is_copy=True)
        report = await compare_snapshots(
            rt,
            before,
            after,
            extra_findings=extra,
            kind="rehearsal",
            step_results=parsed["steps"],
            rehearsal_id=rid,
        )
        report["inputs"]["rehearsal_id"] = rid
        rt.store.save_report(report)
        rt.store.update_copy(copy_row["copy_id"], report_id=report["id"])
    rt.store.update_rehearsal(rid, report_id=report["id"])


async def status(rt: MigrationRuntime, rehearsal_id: str | None = None) -> dict[str, Any]:
    row = rt.store.rehearsal(rehearsal_id) if rehearsal_id else rt.store.latest_rehearsal()
    if row is None or row["status"] == "planned":
        raise VpsMcpError(ErrorCategory.NOT_FOUND, "no such rehearsal (or it was never confirmed)")
    if row["status"] in ACTIVE:
        parsed = await _poll(rt, row)
        await _record(rt, row, parsed)
        row = rt.store.rehearsal(row["rehearsal_id"]) or row
    out: dict[str, Any] = {
        "rehearsal_id": row["rehearsal_id"],
        "pipeline": row["pipeline"],
        "source": row["source"],
        "status": row["status"],
        "steps": row["step_results"],
    }
    if row.get("copy_id"):
        c = rt.store.copy_by_id(row["copy_id"])
        if c:
            out["copy"] = {"name": c["name"], "state": c["state"], "expires_at": c["expires_at"]}
    if row.get("report_id"):
        rep = rt.store.get_report(row["report_id"])
        if rep:
            out["report"] = risk.summary(rep)
    return out


async def cancel(
    rt: MigrationRuntime, rehearsal_id: str, confirm: bool, session_id: str | None
) -> dict[str, Any]:
    row = rt.store.rehearsal(rehearsal_id) if isinstance(rehearsal_id, str) else None
    if row is None or row["status"] not in ACTIVE:
        raise VpsMcpError(ErrorCategory.NOT_FOUND, "no running rehearsal with that id")

    async def preview() -> dict[str, Any]:
        running = [s for s in row["step_results"] if s.get("status") == "running"]
        return {
            "will_stop": row["unit"],
            "current_step": running[0] if running else None,
            "copy_kept": True,
        }

    async def body() -> dict[str, Any]:
        r = await rt.ex.run(["systemctl", "stop", row["unit"]], sudo=True, timeout=120.0)
        if not r.ok:
            raise VpsMcpError(ErrorCategory.FAILED, f"could not stop {row['unit']}: {r.stderr}")
        parsed = await _poll(rt, row)
        if parsed["final"] is None or parsed["final"] == "failed":
            parsed["final"] = parsed["status"] = "cancelled"
        await _record(rt, row, parsed)
        return {"status": "cancelled", "rehearsal_id": rehearsal_id}

    return await run_db_action(
        rt,
        DbAction(
            action="cancel_rehearsal",
            target=row["source"],
            confirm=confirm,
            params={"rehearsal_id": rehearsal_id},
            session_id=session_id,
            timeout=300.0,
            preview=preview,
        ),
        body,
    )
