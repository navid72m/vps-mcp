"""vps-mcp's adapter onto agent-memory (specs/006-agent-memory-service, research A11).

vps-mcp no longer owns a memory: it opens the shared agent-memory store, serves the five
memory tools, puts the pinned core into its connect instructions, and contributes generated,
live-state facts (service states, deploys, job/rehearsal outcomes, report verdicts) that are
updated in place. If the store cannot be opened, vps-mcp keeps working without memory.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from agent_memory import AgentMemoryError, Memory
from agent_memory.model import iso
from vps_mcp.config.policy import AppConfig

log = logging.getLogger(__name__)

# Successful calls to these tools refresh the generated facts.
REFRESH_TOOLS = frozenset(
    {
        "list_services",
        "get_service_status",
        "start_service",
        "stop_service",
        "restart_service",
        "set_boot_start",
        "deploy_service",
        "update_service",
        "configure_service",
        "rollback_service",
        "remove_service",
        "run_job",
        "job_status",
        "cancel_job",
        "rehearse",
        "rehearsal_status",
        "cancel_rehearsal",
        "db_compare",
        "run_check",
        "deploy_from_gitlab",
        "rollback_deploy",
        "gitlab_write",
        "gitlab_ci",
    }
)


def scope_name(text: str) -> str:
    out = "".join(c if c.isalnum() or c in "._-" else "-" for c in text.lower()).strip("-._")
    return (out or "vps")[:64]


def _verdict(conn: sqlite3.Connection, report_id: str | None) -> str | None:
    if not report_id:
        return None
    row = conn.execute(
        "SELECT verdict FROM migration_reports WHERE id = ?", (report_id,)
    ).fetchone()
    return str(row["verdict"]) if row else None


class MemoryAdapter:
    def __init__(self, config: AppConfig, conn: sqlite3.Connection) -> None:
        settings = config.memory
        self.config = config
        self.conn = conn
        self.environment = scope_name(config.vps.name)
        self.project = scope_name(settings.project or f"vps-{config.vps.name}")
        self.scope = f"environment:{self.environment}"
        self.writable = settings.writable
        self.pinned_chars = settings.pinned_chars
        self.error: str | None = None
        self.mem: Memory | None = None
        try:
            self.mem = Memory.open(
                settings.store, project=self.project, environment=self.environment, client="vps-mcp"
            )
        except (AgentMemoryError, OSError) as exc:
            self.error = getattr(exc, "message", None) or exc.__class__.__name__
            log.warning("memory unavailable: %s", self.error)

    # ---------------------------------------------------------- connect

    def connect_instructions(self) -> str:
        if self.mem is None:
            return f"Memory unavailable: {self.error}."
        try:
            self.refresh()
            return self.mem.pinned_core(self.pinned_chars)
        except Exception as exc:  # noqa: BLE001 - the server must still start
            self.error = getattr(exc, "message", None) or exc.__class__.__name__
            log.warning("memory unavailable: %s", self.error)
            return f"Memory unavailable: {self.error}."

    # ---------------------------------------------------- generated facts

    def after_tool(self, tool: str, payload: dict[str, Any]) -> None:
        if self.mem is None or tool not in REFRESH_TOOLS:
            return
        try:
            self.observe(tool, payload)
            self.refresh()
        except Exception as exc:  # noqa: BLE001 - memory is best effort
            self.error = f"refresh: {getattr(exc, 'message', exc)!s}"[:200]
            log.warning("memory refresh failed: %s", self.error)

    def _fact(
        self,
        key: str,
        subject: str,
        title: str,
        content: str,
        ref: str,
        importance: int = 3,
        fresh_days: int = 7,
    ) -> None:
        assert self.mem is not None
        self.mem.upsert_generated(
            f"{self.environment}:{key}",
            subject=subject,
            title=title,
            content=content,
            scope=self.scope,
            importance=importance,
            fresh_days=fresh_days,
            source_ref=ref,
        )

    def observe(self, tool: str, payload: dict[str, Any]) -> None:
        if tool == "list_services":
            items = payload.get("services") or []
        elif tool == "get_service_status":
            items = [payload.get("service")] if payload.get("service") else []
        else:
            return
        when = iso(self.mem.now()) if self.mem else ""
        for s in items:
            if not isinstance(s, dict) or not s.get("name") or not s.get("state"):
                continue
            name = scope_name(str(s["name"]))
            kind = str(s.get("kind") or "os")
            self._fact(
                f"service.{name}.{kind}.state",
                f"service.{name}.state",
                f"{s['name']} ({kind}) is {s['state']}",
                f"{s['name']} ({kind}) was {s['state']} on {self.config.vps.name} at {when}.",
                ref=f"vps-mcp:{tool}",
                importance=2,
                fresh_days=1,
            )

    def refresh(self) -> None:
        """Latest outcomes from the local state DB (jobs, rehearsals, reports, deploys)."""
        c = self.conn
        for r in c.execute(
            "SELECT job, run_id, status, ended_at, started_at, report_id FROM job_runs j "
            "WHERE status != 'planned' AND created_at = (SELECT MAX(created_at) FROM job_runs "
            "WHERE job = j.job AND status != 'planned')"
        ).fetchall():
            verdict = _verdict(c, r["report_id"])
            when = str(r["ended_at"] or r["started_at"] or "")[:16]
            v = f", verdict {verdict}" if verdict else ""
            self._fact(
                f"job.{r['job']}.last_outcome",
                f"job.{scope_name(r['job'])}.last.outcome",
                f"Job {r['job']}: last run {r['status']}{v}",
                f"Declared job {r['job']}, run {r['run_id']}: {r['status']}{v}"
                + (f" ({when})" if when else "")
                + ".",
                ref=f"vps-mcp:job_runs/{r['run_id']}",
                importance=4,
                fresh_days=30,
            )
        row = c.execute(
            "SELECT rehearsal_id, pipeline, source, status, created_at, report_id "
            "FROM migration_rehearsals ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row:
            verdict = _verdict(c, row["report_id"])
            v = f", verdict {verdict}" if verdict else ""
            self._fact(
                "rehearsal.last_outcome",
                "rehearsal.last.outcome",
                f"Last rehearsal of {row['pipeline']}: {row['status']}{v}",
                f"Rehearsal {row['rehearsal_id']} of pipeline {row['pipeline']} on "
                f"{row['source']}: {row['status']}{v} ({str(row['created_at'])[:16]}).",
                ref=f"vps-mcp:rehearsal/{row['rehearsal_id']}",
                importance=4,
                fresh_days=30,
            )
        row = c.execute(
            "SELECT id, created_at, kind, verdict, inputs_json, json FROM migration_reports "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row:
            try:
                inputs = json.loads(row["inputs_json"] or "{}")
                counts = json.loads(row["json"] or "{}").get("counts", {})
            except (ValueError, AttributeError):
                inputs, counts = {}, {}
            a = inputs.get("source") or inputs.get("before")
            b = inputs.get("result") or inputs.get("after")
            what = f" {a} -> {b}" if a and b else ""
            cnt = ", ".join(f"{n} {k}" for k, n in counts.items() if n)
            self._fact(
                "report.latest_verdict",
                "report.latest.verdict",
                f"Latest migration report{what}: {row['verdict']}",
                f"Report {row['id']} ({row['kind']}{what}): verdict {row['verdict']}"
                + (f" [{cnt}]" if cnt else "")
                + f" ({str(row['created_at'])[:16]}).",
                ref=f"vps-mcp:report/{row['id']}",
                importance=4,
                fresh_days=30,
            )
        for r in c.execute(
            "SELECT target, kind, status, commit_sha, ref, updated_at FROM gitlab_deploys d "
            "WHERE status IN ('succeeded', 'rolled_back', 'failed') AND updated_at = ("
            "SELECT MAX(updated_at) FROM gitlab_deploys WHERE target = d.target "
            "AND status IN ('succeeded', 'rolled_back', 'failed'))"
        ).fetchall():
            sha = str(r["commit_sha"] or "?")[:12]
            what = "rolled back to" if r["kind"] == "rollback" else "deployed"
            ref = f" ({r['ref']})" if r["ref"] else ""
            self._fact(
                f"deploy.{r['target']}.version",
                f"deploy.{scope_name(r['target'])}.version",
                f"{r['target']}: {what} {sha}{ref} {r['status']}",
                f"Deploy target {r['target']}: {what} {sha}{ref}, {r['status']} at "
                f"{str(r['updated_at'])[:16]}.",
                ref=f"vps-mcp:gitlab_deploys/{r['target']}",
                importance=4,
                fresh_days=30,
            )
