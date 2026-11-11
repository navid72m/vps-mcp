"""Script fingerprint, owner and mode checks (research J3, FR-004, FR-005)."""

from __future__ import annotations

import posixpath
from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError, invalid
from vps_mcp.migration.config import JobDeclaration
from vps_mcp.migration.runtime import MigrationRuntime


async def check_script(rt: MigrationRuntime, job: JobDeclaration) -> dict[str, Any]:
    script = job.script
    directory = posixpath.dirname(script) or "/"
    st = await rt.ex.run(
        ["stat", "-c", "%U %a %Y %n", "--", script, directory], sudo=True, timeout=30.0
    )
    rows = [ln.split(" ", 3) for ln in st.stdout.splitlines() if ln.strip()]
    meta = {r[3]: r for r in rows if len(r) == 4}
    if not st.ok or script not in meta:
        raise invalid(f"job '{job.name}': script {script} not found or unreadable")
    owner, mode_s, mtime, _ = meta[script]
    mode = int(mode_s, 8)
    dir_mode = int(meta[directory][1], 8) if directory in meta else 0o777
    if owner not in ("root", job.run_as):
        raise VpsMcpError(
            ErrorCategory.NOT_PERMITTED,
            f"job '{job.name}': script owner is '{owner}', expected root or '{job.run_as}'",
        )
    if mode & 0o022 or dir_mode & 0o022:
        raise VpsMcpError(
            ErrorCategory.NOT_PERMITTED,
            f"job '{job.name}': the script or its directory is writable by group "
            f"or others (script {mode_s}, dir {oct(dir_mode)[2:]}); refusing",
        )
    sh = await rt.ex.run(["sha256sum", "--", script], sudo=True, timeout=60.0)
    sha = sh.stdout.split()[0] if sh.ok and sh.stdout.split() else ""
    if len(sha) != 64:
        raise invalid(f"job '{job.name}': could not fingerprint {script}")
    return {
        "sha256": sha,
        "owner": owner,
        "mode": mode_s,
        "mtime": int(mtime),
        "dir_mode": oct(dir_mode)[2:],
    }
