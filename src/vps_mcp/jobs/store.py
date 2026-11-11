"""job_runs table accessors (data-model.md §2)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from vps_mcp.store.db import utcnow

_JSON = {"plan_json", "baseline_json", "last_guard_json"}
_ALLOWED = {
    "status",
    "unit",
    "rundir",
    "progress_offset",
    "baseline_json",
    "last_guard_json",
    "started_at",
    "ended_at",
    "exit_code",
    "systemd_result",
    "cancel_requested",
    "report_id",
    "audit_id",
}
FINAL = ("succeeded", "failed", "timed_out", "cancelled")


class JobStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_plan(self, run_id: str, job: str, plan_hash: str, plan: dict[str, Any]) -> None:
        now = utcnow()
        self.conn.execute(
            "INSERT INTO job_runs (run_id, job, plan_hash, plan_json, status, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            (run_id, job, plan_hash, json.dumps(plan), "planned", now, now),
        )

    def update(self, run_id: str, **fields: Any) -> None:
        for k, v in fields.items():
            if k not in _ALLOWED:
                raise ValueError(k)
            if k in _JSON and not isinstance(v, str):
                v = json.dumps(v)
            self.conn.execute(
                f"UPDATE job_runs SET {k} = ?, updated_at = ? WHERE run_id = ?",
                (v, utcnow(), run_id),
            )

    def get(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM job_runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._r(row)

    def by_plan(self, plan_hash: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM job_runs WHERE plan_hash = ? ORDER BY created_at DESC LIMIT 1",
            (plan_hash,),
        ).fetchone()
        return self._r(row)

    def latest(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM job_runs WHERE status != 'planned' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return self._r(row)

    def running(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM job_runs WHERE status = 'running'").fetchall()
        return [r for r in (self._r(x) for x in rows) if r]

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM job_runs WHERE status != 'planned' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r for r in (self._r(x) for x in rows) if r]

    @staticmethod
    def _r(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        for k in _JSON:
            d[k.removesuffix("_json")] = json.loads(d.pop(k) or "null")
        d["cancel_requested"] = bool(d["cancel_requested"])
        return d
