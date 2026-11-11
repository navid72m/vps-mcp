"""Disposable copies: naming, ownership marker, disk guard, expiry, drop (research M4)."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.migration.runtime import MigrationRuntime
from vps_mcp.migration.store import new_id

log = logging.getLogger(__name__)
MARKER_PREFIX = "vps-mcp-copy:"


def copy_name(source: str, now: datetime | None = None, rand: str | None = None) -> str:
    now = now or datetime.now(UTC)
    return f"vmcp_{source[:30]}_{now:%Y%m%d%H%M}_{rand or secrets.token_hex(2)}"


def _key_path(rt: MigrationRuntime) -> Path:
    row = rt.app.db.execute("PRAGMA database_list").fetchone()
    db_file = row[2] if row and row[2] else ""
    base = Path(db_file).parent if db_file else Path.home() / ".local" / "state" / "vps-mcp"
    return base / f"{rt.app.config.vps.name}.migration.key"


def marker_key(rt: MigrationRuntime) -> bytes:
    p = _key_path(rt)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(secrets.token_bytes(32))
    return p.read_bytes()


def make_marker(key: bytes, copy_id: str, name: str) -> str:
    mac = hmac.new(key, f"{copy_id}:{name}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{MARKER_PREFIX}{copy_id}:{mac}"


def verify_marker(key: bytes, copy_id: str, name: str, live_comment: str | None) -> bool:
    return bool(live_comment) and hmac.compare_digest(
        make_marker(key, copy_id, name), str(live_comment)
    )


async def disk_plan(rt: MigrationRuntime, source_size: int) -> dict[str, int]:
    est = int(source_size * rt.settings.copy_growth_factor)
    free, total = await rt.db.free_and_total()
    min_free = rt.db.min_free_bytes(total)
    plan = {
        "est_copy_bytes": est,
        "free_bytes_before": free,
        "free_bytes_after": free - est,
        "min_free_bytes": min_free,
        "total_bytes": total,
    }
    if free - est < min_free:
        raise VpsMcpError(
            ErrorCategory.INSUFFICIENT_DISK,
            f"a copy needs ~{est / 1024**3:.1f} GB; only {free / 1024**3:.1f} GB free and at "
            f"least {min_free / 1024**3:.1f} GB must stay free. Free space on the VPS or lower "
            "[migration] min_free_gb / min_free_pct.",
            extra={"disk": plan},
        )
    return plan


def choose_method(requested: str, sessions: int) -> str:
    if requested == "template" and sessions > 0:
        raise VpsMcpError(
            ErrorCategory.CONFLICT,
            f"the source has {sessions} active session(s); a template copy needs "
            "none. Use method='dump' (or 'auto').",
        )
    if requested == "auto":
        return "template" if sessions == 0 else "dump"
    if requested not in ("template", "dump"):
        raise VpsMcpError(ErrorCategory.INVALID_INPUT, "method must be auto, template or dump")
    return requested


def copy_commands(method: str, source: str, copy: str, owner: str, port: int, marker: str) -> str:
    """Bash fragment (run as root) creating the copy and stamping its marker."""
    q = shlex.quote
    p = str(port)
    pg = "runuser -u postgres --"
    if method == "template":
        create = f"{pg} createdb -p {p} -T {q(source)} -O {q(owner)} {q(copy)}"
    else:
        inner = (
            f"set -o pipefail; pg_dump -p {p} -Fc {q(source)} | "
            f"pg_restore -p {p} -d {q(copy)} --exit-on-error"
        )
        create = f"{pg} createdb -p {p} -O {q(owner)} {q(copy)} && {pg} bash -c {q(inner)}"
    stamp = (
        f"{pg} psql -X -p {p} -d postgres -v ON_ERROR_STOP=1 -v copy={q(copy)} "
        f"-v marker={q(marker)} -f - <<'VMCP_EOSQL'\n"
        "COMMENT ON DATABASE :\"copy\" IS :'marker';\n"
        "VMCP_EOSQL"
    )
    return f"{create} && {stamp}"


def expiry_script(copy: str, marker: str, port: int) -> str:
    q = shlex.quote
    return (
        f"m=$(runuser -u postgres -- psql -X -p {port} -d postgres -At -v db={q(copy)} -f - "
        "<<'VMCP_EOSQL'\n"
        "SELECT coalesce(shobj_description(oid, 'pg_database'), '') FROM pg_database "
        "WHERE datname = :'db';\n"
        "VMCP_EOSQL\n"
        f'); [ "$m" = {q(marker)} ] && runuser -u postgres -- dropdb -p {port} --if-exists '
        f"{q(copy)}"
    )


async def schedule_expiry(rt: MigrationRuntime, row: dict[str, Any]) -> None:
    hours = rt.settings.copy_retention_h
    r = await rt.ex.run(
        [
            "systemd-run",
            f"--on-active={hours}h",
            "--unit",
            f"vps-mcp-expire-{row['copy_id']}",
            "--collect",
            "/bin/bash",
            "-c",
            expiry_script(row["name"], row["marker"], rt.settings.port),
        ],
        sudo=True,
        timeout=30.0,
    )
    if not r.ok:
        log.warning("could not schedule expiry for %s: %s", row["name"], r.stderr.strip())


def new_copy_row(
    rt: MigrationRuntime, source: str, method: str, rehearsal_id: str | None
) -> dict[str, Any]:
    copy_id = new_id("cpy")
    name = copy_name(source)
    now = datetime.now(UTC)
    return {
        "copy_id": copy_id,
        "name": name,
        "source": source,
        "method": method,
        "marker": make_marker(marker_key(rt), copy_id, name),
        "state": "creating",
        "created_at": now.isoformat(timespec="milliseconds"),
        "expires_at": (now + timedelta(hours=rt.settings.copy_retention_h)).isoformat(
            timespec="milliseconds"
        ),
        "rehearsal_id": rehearsal_id,
    }


async def drop(rt: MigrationRuntime, row: dict[str, Any]) -> tuple[bool, str]:
    """Drop one copy after verifying both ownership proofs. Returns (dropped, reason)."""
    info = await rt.db.db_info(row["name"])
    if not info.get("exists"):
        rt.store.update_copy(
            row["copy_id"],
            state="dropped",
            dropped_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
        )
        return True, "already_gone"
    if not verify_marker(marker_key(rt), row["copy_id"], row["name"], info.get("comment")):
        return False, "marker_mismatch"
    if info.get("sessions"):
        return False, "in_use"
    r = await rt.ex.run(
        ["dropdb", "-p", str(rt.settings.port), "--if-exists", row["name"]],
        sudo=True,
        user="postgres",
        timeout=600.0,
    )
    if not r.ok:
        return False, f"dropdb failed: {r.stderr.strip()[-200:]}"
    await rt.ex.run(
        ["systemctl", "stop", f"vps-mcp-expire-{row['copy_id']}.timer"], sudo=True, timeout=30.0
    )
    rt.store.update_copy(
        row["copy_id"],
        state="dropped",
        dropped_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
    )
    return True, "dropped"


def expired(row: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return datetime.fromisoformat(row["expires_at"]) <= now


async def sweep(rt: MigrationRuntime) -> list[str]:
    """Drop expired copies (best effort). Skipped in read-only mode."""
    if rt.app.policy.read_only:
        return []
    dropped = []
    for row in rt.store.copies():
        if row["state"] in ("ready", "failed") and expired(row):
            try:
                ok, _ = await drop(rt, row)
            except VpsMcpError as err:
                log.warning("sweep could not drop %s: %s", row["name"], err.message)
                continue
            if ok:
                dropped.append(row["name"])
    return dropped
