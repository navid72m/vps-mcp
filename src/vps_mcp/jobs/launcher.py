"""Start, inspect and stop job units (research J1, J2, J9)."""

from __future__ import annotations

import posixpath
from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.migration.config import JobDeclaration
from vps_mcp.migration.runtime import MigrationRuntime

JOB_ROOT = "/var/lib/vps-mcp/jobs"
# Fixed wrapper: records the script's exit code, since successful transient units vanish.
WRAPPER = 'bash "$1"; rc=$?; printf "%s\\n" "$rc" > "$2"; exit $rc'


def rundir(run_id: str) -> str:
    return f"{JOB_ROOT}/{run_id}"


def unit_name(run_id: str) -> str:
    return f"vps-mcp-job-{run_id}"


def argv(job: JobDeclaration, run_id: str, params: dict[str, str]) -> list[str]:
    rd = rundir(run_id)
    out = [
        "systemd-run",
        "--unit",
        unit_name(run_id),
        "--description",
        f"vps-mcp job {job.name}",
        f"--property=RuntimeMaxSec={int(job.max_runtime_h * 3600)}",
        "--property=KillMode=control-group",
        f"--property=StandardOutput=append:{rd}/console.log",
        f"--property=StandardError=append:{rd}/console.log",
    ]
    if job.run_as != "root":
        out.append(f"--uid={job.run_as}")
    out.append(f"--working-directory={posixpath.dirname(job.script) or '/'}")
    out.append(f"--setenv=VPSMCP_JOB_RUN={run_id}")
    for name in sorted(params):
        out.append(f"--setenv={name}={params[name]}")
    out += ["/bin/bash", "-c", WRAPPER, "vps-mcp-job", job.script, f"{rd}/exit_code"]
    return out


async def start(
    rt: MigrationRuntime, job: JobDeclaration, run_id: str, params: dict[str, str]
) -> str:
    rd = rundir(run_id)
    owner = ["-o", job.run_as] if job.run_as != "root" else []
    r = await rt.ex.run(["install", "-d", "-m", "0700", *owner, rd], sudo=True, timeout=30.0)
    if not r.ok:
        raise VpsMcpError(ErrorCategory.FAILED, f"cannot create {rd}: {r.stderr.strip()}")
    r = await rt.ex.run(argv(job, run_id, params), sudo=True, timeout=60.0)
    if not r.ok:
        raise VpsMcpError(ErrorCategory.FAILED, f"could not start the job: {r.stderr.strip()}")
    return unit_name(run_id)


async def file_size(rt: MigrationRuntime, path: str) -> int:
    r = await rt.ex.run(["stat", "-c", "%s", "--", path], sudo=True, timeout=30.0)
    try:
        return int(r.stdout.strip()) if r.ok else 0
    except ValueError:
        return 0


async def read_from(rt: MigrationRuntime, path: str, offset: int) -> str:
    size = await file_size(rt, path)
    start = offset if size >= offset else 0  # file recreated -> read from the start
    r = await rt.ex.run(["tail", "-c", f"+{start + 1}", "--", path], sudo=True, timeout=30.0)
    return r.stdout if r.ok else ""


async def running_elsewhere(rt: MigrationRuntime, script: str) -> str | None:
    r = await rt.ex.run(["pgrep", "-f", "--", script], timeout=30.0)
    pids = r.stdout.split() if r.ok else []
    return ",".join(pids) or None


async def active_units(rt: MigrationRuntime) -> list[str]:
    r = await rt.ex.run(
        [
            "systemctl",
            "list-units",
            "--plain",
            "--no-legend",
            "--state=active",
            "vps-mcp-job-*",
            "vps-mcp-rehearsal-*",
        ],
        timeout=30.0,
    )
    return [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]


async def unit_state(rt: MigrationRuntime, run: dict[str, Any]) -> dict[str, Any]:
    r = await rt.ex.run(
        [
            "systemctl",
            "show",
            "-p",
            "ActiveState,SubState,Result,ExecMainStartTimestamp,ExecMainExitTimestamp",
            "--timestamp=utc",
            run["unit"],
        ],
        timeout=30.0,
    )
    props = dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln)
    ex = await rt.ex.run(["cat", "--", f"{run['rundir']}/exit_code"], sudo=True, timeout=30.0)
    code = None
    if ex.ok and ex.stdout.strip().lstrip("-").isdigit():
        code = int(ex.stdout.strip())
    active = props.get("ActiveState", "") in ("active", "activating", "reloading", "deactivating")
    return {
        "active": active,
        "result": props.get("Result") or None,
        "exit_code": code,
        "started": props.get("ExecMainStartTimestamp") or None,
        "ended": props.get("ExecMainExitTimestamp") or None,
    }


async def console_tail(rt: MigrationRuntime, run: dict[str, Any], n: int = 20) -> list[str]:
    r = await rt.ex.run(
        ["tail", "-n", str(n), "--", f"{run['rundir']}/console.log"], sudo=True, timeout=30.0
    )
    return rt.mask(r.stdout).splitlines()[-n:] if r.ok else []


async def stop(rt: MigrationRuntime, run: dict[str, Any]) -> None:
    r = await rt.ex.run(["systemctl", "stop", run["unit"]], sudo=True, timeout=180.0)
    if not r.ok:
        raise VpsMcpError(ErrorCategory.FAILED, f"could not stop {run['unit']}: {r.stderr}")


async def reset_failed(rt: MigrationRuntime, run: dict[str, Any]) -> None:
    await rt.ex.run(["systemctl", "reset-failed", run["unit"]], sudo=True, timeout=30.0)
