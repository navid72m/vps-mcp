"""Watch everything a job is NOT declared to change (research J5, FR-012, FR-013)."""

from __future__ import annotations

from typing import Any

from vps_mcp.errors import VpsMcpError
from vps_mcp.migration.config import JobDeclaration
from vps_mcp.migration.runtime import MigrationRuntime

RUNNING = "running"


def _guarded_dbs(rt: MigrationRuntime, job: JobDeclaration) -> list[str]:
    return sorted(t for t in rt.settings.target_names() if t not in job.may_modify)


def _guarded_services(rt: MigrationRuntime, job: JobDeclaration) -> list[Any]:
    return [d for d in rt.app.managed.all() if d.name not in job.services]


def scope(rt: MigrationRuntime, job: JobDeclaration) -> dict[str, list[str]]:
    return {
        "databases": _guarded_dbs(rt, job),
        "services": sorted(d.name for d in _guarded_services(rt, job)),
    }


async def _service_states(rt: MigrationRuntime, job: JobDeclaration) -> dict[str, str]:
    states: dict[str, str] = {}
    decls = _guarded_services(rt, job)
    by_kind: dict[Any, list[Any]] = {}
    for d in decls:
        by_kind.setdefault(d.kind, []).append(d)
    for kind, group in by_kind.items():
        try:
            for st in await rt.app.backend(kind).list_status(group):
                states[f"{st.kind.value}:{st.name}"] = st.state.value
        except VpsMcpError:
            continue  # best effort: a backend being unavailable must not break status
    return states


async def readings(rt: MigrationRuntime, job: JobDeclaration) -> dict[str, Any]:
    dbs = _guarded_dbs(rt, job)
    counters = await rt.db.write_counters()
    sizes = await rt.db.query("postgres", "db_sizes") or {}
    return {
        "writes": {d: counters.get(d, {}).get("writes") for d in dbs},
        "sessions": {d: counters.get(d, {}).get("sessions") for d in dbs},
        "exists": {t: t in sizes for t in rt.settings.target_names()},
        "services": await _service_states(rt, job),
    }


def compare(job: JobDeclaration, base: dict[str, Any], now: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for db, before in sorted((base.get("writes") or {}).items()):
        after = (now.get("writes") or {}).get(db)
        if before is None or after is None or after <= before:
            continue
        busy = bool((now.get("sessions") or {}).get(db))
        findings.append(
            {
                "severity": "warning" if busy else "blocking",
                "category": "undeclared_db_write_unattributed" if busy else "undeclared_db_write",
                "object": f"database:{db}",
                "message": (
                    f"{after - before} row writes hit '{db}', which job '{job.name}' is not "
                    "declared to modify" + (" (other sessions were active)." if busy else ".")
                ),
                "values": {"writes": after - before},
            }
        )
    for db, existed in sorted((base.get("exists") or {}).items()):
        if existed and not (now.get("exists") or {}).get(db, True) and db not in job.may_modify:
            findings.append(
                {
                    "severity": "blocking",
                    "category": "undeclared_db_dropped",
                    "object": f"database:{db}",
                    "message": f"Database '{db}' disappeared during job '{job.name}', which does not "
                    "declare it.",
                    "values": {},
                }
            )
    for svc, before in sorted((base.get("services") or {}).items()):
        after = (now.get("services") or {}).get(svc)
        if after is None or after == before:
            continue
        if RUNNING in (before, after):
            findings.append(
                {
                    "severity": "blocking",
                    "category": "undeclared_service_change",
                    "object": f"service:{svc}",
                    "message": f"Service {svc} went from {before} to {after} during job "
                    f"'{job.name}', which does not declare it.",
                    "values": {"before": before, "after": after},
                }
            )
    return findings
