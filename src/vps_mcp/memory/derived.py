"""Derived facts from configuration and the local state DB (research M6, M7).

Local only: nothing here talks to the VPS. Output is Markdown and never includes the host
address, only the configuration's VPS name.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from vps_mcp.config.policy import AppConfig

STALE_AFTER = timedelta(days=7)
_PURPOSE_ROLE = {"dump": "source dump", "test": "test / result", "reference": "reference"}


def _ago(ts: str, now: datetime) -> str:
    try:
        then = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    secs = max(0, int((now - then).total_seconds()))
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs // 60}m ago"
    if secs < 172800:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _day(ts: str | None) -> str:
    return (ts or "")[:16].replace("T", " ")


def observations(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple[str, str]]:
    rows = conn.execute("SELECT name, kind, state, observed_at FROM service_observations")
    return {(r["name"], r["kind"]): (r["state"], r["observed_at"]) for r in rows}


def _obs_text(obs: tuple[str, str] | None, now: datetime) -> str:
    if obs is None:
        return "state not observed yet"
    state, at = obs
    try:
        then = datetime.fromisoformat(at)
        stale = now - (then if then.tzinfo else then.replace(tzinfo=UTC)) > STALE_AFTER
    except ValueError:
        stale = True
    if stale:
        return f"unknown (last seen {state} {_ago(at, now)})"
    return f"{state} (seen {_ago(at, now)})"


def environment(
    config: AppConfig,
    obs: dict[tuple[str, str], tuple[str, str]],
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    lines = [f"VPS: {config.vps.name}" + (" (read-only mode)" if config.policy.read_only else "")]
    lines.append("")
    lines.append("Services:")
    for s in config.service:
        flags = " protected" if s.protected else ""
        desc = f" - {s.description}" if s.description else ""
        lines.append(f"- {s.name} [{s.kind.value}{flags}]: {_obs_text(obs.get(s.key), now)}{desc}")
    mig = config.migration
    if mig is not None:
        roles: dict[str, set[str]] = {}
        for t in mig.target:
            roles.setdefault(t.name, set()).add(_PURPOSE_ROLE.get(t.purpose, t.purpose))
        for j in mig.job:
            if j.source:
                roles.setdefault(j.source, set()).add(f"source of job {j.name}")
            if j.result:
                roles.setdefault(j.result, set()).add(f"result of job {j.name}")
            for db in j.may_modify:
                roles.setdefault(db, set()).add(f"rebuilt by job {j.name}")
        if roles:
            lines += ["", "Databases (allowlisted):"]
            lines += [f"- {db}: {', '.join(sorted(r))}" for db, r in roles.items()]
        if mig.pipeline:
            lines += ["", "Pipelines (rehearse on a disposable copy):"]
            for p in mig.pipeline:
                steps = ", ".join(f"{s.name}({s.kind})" for s in p.steps)
                lines.append(f"- {p.name}: {steps}")
        if mig.check:
            lines += ["", "Checks:"]
            for c in mig.check:
                lines.append(f"- {c.name}: {c.access}{', blocking' if c.blocking else ''}")
        if mig.job:
            lines += ["", "Declared jobs:"]
            for j in mig.job:
                bits = [f"script {j.script.rsplit('/', 1)[-1]}"]
                if j.may_modify:
                    bits.append(f"rebuilds {', '.join(j.may_modify)}")
                if j.services:
                    bits.append(f"may stop {', '.join(j.services)}")
                if j.source and j.result:
                    bits.append(f"compares {j.source} -> {j.result}")
                if j.params:
                    bits.append(f"params {', '.join(p.name for p in j.params)}")
                lines.append(f"- {j.name}: {'; '.join(bits)}")
    return "\n".join(lines)


def _verdicts(conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, verdict FROM migration_reports WHERE id IN ({marks})",  # noqa: S608
        ids,
    )
    return {r["id"]: r["verdict"] for r in rows}


def outcomes(
    conn: sqlite3.Connection, jobs: int = 3, rehearsals: int = 3, reports: int = 5
) -> list[str]:
    """Latest outcomes as Markdown bullet lines, newest first per kind."""
    lines: list[str] = deployed(conn) + gitlab_activity(conn)
    jrows = conn.execute(
        "SELECT run_id, job, status, started_at, ended_at, report_id FROM job_runs "
        "WHERE status != 'planned' ORDER BY created_at DESC LIMIT ?",
        (jobs,),
    ).fetchall()
    rrows = conn.execute(
        "SELECT rehearsal_id, pipeline, source, status, created_at, report_id "
        "FROM migration_rehearsals ORDER BY created_at DESC LIMIT ?",
        (rehearsals,),
    ).fetchall()
    verdicts = _verdicts(conn, [r["report_id"] for r in [*jrows, *rrows]])
    for r in jrows:
        v = verdicts.get(r["report_id"] or "")
        when = _day(r["ended_at"] or r["started_at"])
        lines.append(
            f"- job {r['job']} run {r['run_id']}: {r['status']}"
            + (f", verdict {v}" if v else "")
            + (f" ({when})" if when else "")
        )
    for r in rrows:
        v = verdicts.get(r["report_id"] or "")
        lines.append(
            f"- rehearsal {r['rehearsal_id']} of {r['pipeline']} on {r['source']}: {r['status']}"
            + (f", verdict {v}" if v else "")
            + f" ({_day(r['created_at'])})"
        )
    for r in conn.execute(
        "SELECT id, created_at, kind, verdict, inputs_json, json FROM migration_reports "
        "ORDER BY created_at DESC LIMIT ?",
        (reports,),
    ):
        try:
            inputs: dict[str, Any] = json.loads(r["inputs_json"] or "{}")
            counts: dict[str, Any] = json.loads(r["json"] or "{}").get("counts", {})
        except (ValueError, AttributeError):
            inputs, counts = {}, {}
        a = inputs.get("source") or inputs.get("before") or inputs.get("a")
        b = inputs.get("result") or inputs.get("after") or inputs.get("b")
        what = f" {a} -> {b}" if a and b else ""
        c = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
        lines.append(
            f"- report {r['id']} ({r['kind']}{what}): {r['verdict']}"
            + (f" [{c}]" if c else "")
            + f" ({_day(r['created_at'])})"
        )
    return lines


def changes(conn: sqlite3.Connection, n: int = 20) -> list[str]:
    rows = conn.execute(
        "SELECT ts, action, service_name, outcome FROM audit_records "
        "WHERE outcome != 'preview' ORDER BY id DESC LIMIT ?",
        (n,),
    )
    return [
        f"- {_day(r['ts'])} {r['action']}"
        + (f" {r['service_name']}" if r["service_name"] else "")
        + f": {r['outcome']}"
        for r in rows
    ]


def deployed(conn: sqlite3.Connection) -> list[str]:
    """Latest deploy (or rollback) per GitLab deploy target (feature 005)."""
    rows = conn.execute(
        "SELECT target, kind, status, commit_sha, ref, updated_at FROM gitlab_deploys d "
        "WHERE status IN ('succeeded', 'rolled_back', 'failed') AND updated_at = ("
        "SELECT MAX(updated_at) FROM gitlab_deploys WHERE target = d.target "
        "AND status IN ('succeeded', 'rolled_back', 'failed')) ORDER BY target"
    ).fetchall()
    out = []
    for r in rows:
        what = "rollback to" if r["kind"] == "rollback" else "deployed"
        ref = f" ({r['ref']})" if r["ref"] else ""
        out.append(
            f"- target {r['target']}: {what} {str(r['commit_sha'] or '?')[:12]}{ref} "
            f"{r['status']} ({_day(r['updated_at'])})"
        )
    return out


def gitlab_activity(conn: sqlite3.Connection, n: int = 5) -> list[str]:
    rows = conn.execute(
        "SELECT ts, action, service_name, params_json, outcome FROM audit_records "
        "WHERE action LIKE 'gitlab_%' AND outcome = 'success' ORDER BY id DESC LIMIT ?",
        (n,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            params = json.loads(r["params_json"] or "{}")
        except ValueError:
            params = {}
        ref = params.get("web_ref") or params.get("project") or r["service_name"] or ""
        act = params.get("action") or r["action"]
        out.append(f"- gitlab {act} {ref} ({_day(r['ts'])})")
    return out
