"""Local storage for snapshots, reports, copies and rehearsals (research M9)."""

from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Any

from vps_mcp.store.db import utcnow


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


class MigrationStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ----------------------------------------------------------------- snapshots

    def save_snapshot(self, snap: dict[str, Any]) -> str:
        sid = snap.get("id") or new_id("snp")
        snap["id"] = sid
        self.conn.execute(
            "INSERT OR REPLACE INTO migration_snapshots (id, database, is_copy, taken_at, json) "
            "VALUES (?,?,?,?,?)",
            (
                sid,
                snap["database"],
                int(bool(snap.get("is_copy"))),
                snap["taken_at"],
                json.dumps(snap),
            ),
        )
        return str(sid)

    def get_snapshot(self, sid: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT json FROM migration_snapshots WHERE id = ?", (sid,)
        ).fetchone()
        return json.loads(row["json"]) if row else None

    # ------------------------------------------------------------------- reports

    def save_report(self, report: dict[str, Any]) -> str:
        self.conn.execute(
            "INSERT OR REPLACE INTO migration_reports (id, created_at, kind, verdict, inputs_json, "
            "json) VALUES (?,?,?,?,?,?)",
            (
                report["id"],
                report["created_at"],
                report["kind"],
                report["verdict"],
                json.dumps(report["inputs"]),
                json.dumps(report),
            ),
        )
        return str(report["id"])

    def get_report(self, rid: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT json FROM migration_reports WHERE id = ?", (rid,)
        ).fetchone()
        return json.loads(row["json"]) if row else None

    def list_reports(self, limit: int) -> list[dict[str, Any]]:
        return [
            {
                "id": r["id"],
                "kind": r["kind"],
                "created_at": r["created_at"],
                "verdict": r["verdict"],
                "inputs": json.loads(r["inputs_json"]),
            }
            for r in self.conn.execute(
                "SELECT id, kind, created_at, verdict, inputs_json FROM migration_reports "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            )
        ]

    def latest_report_for(self, db: str) -> dict[str, Any] | None:
        for r in self.list_reports(50):
            if db in (r["inputs"].get("before"), r["inputs"].get("after"), r["inputs"].get("db")):
                return self.get_report(r["id"])
        return None

    # -------------------------------------------------------------------- copies

    def add_copy(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO migration_copies (copy_id, name, source, method, marker, state, "
            "created_at, expires_at, rehearsal_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                row["copy_id"],
                row["name"],
                row["source"],
                row["method"],
                row["marker"],
                row["state"],
                row["created_at"],
                row["expires_at"],
                row.get("rehearsal_id"),
            ),
        )

    def update_copy(self, copy_id: str, **fields: Any) -> None:
        allowed = {"state", "dropped_at", "size_bytes", "report_id", "rehearsal_id"}
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(k)
            self.conn.execute(
                f"UPDATE migration_copies SET {k} = ? WHERE copy_id = ?",
                (v, copy_id),
            )

    def copies(self, include_dropped: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM migration_copies"
        if not include_dropped:
            sql += " WHERE dropped_at IS NULL"
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY created_at")]

    def copy_by_name(self, name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM migration_copies WHERE name = ? AND dropped_at IS NULL", (name,)
        ).fetchone()
        return dict(row) if row else None

    def copy_by_id(self, copy_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM migration_copies WHERE copy_id = ?", (copy_id,)
        ).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- rehearsals

    def add_rehearsal(self, row: dict[str, Any]) -> None:
        now = utcnow()
        self.conn.execute(
            "INSERT INTO migration_rehearsals (rehearsal_id, pipeline, source, plan_hash, "
            "plan_json, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                row["rehearsal_id"],
                row["pipeline"],
                row["source"],
                row["plan_hash"],
                json.dumps(row["plan"]),
                row["status"],
                now,
                now,
            ),
        )

    def update_rehearsal(self, rid: str, **fields: Any) -> None:
        allowed = {
            "copy_id",
            "status",
            "unit",
            "source_snapshot_id",
            "step_results_json",
            "report_id",
            "audit_id",
        }
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(k)
            if k == "step_results_json" and not isinstance(v, str):
                v = json.dumps(v)
            self.conn.execute(
                f"UPDATE migration_rehearsals SET {k} = ?, updated_at = ? WHERE rehearsal_id = ?",
                (v, utcnow(), rid),
            )

    def rehearsal(self, rid: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM migration_rehearsals WHERE rehearsal_id = ?", (rid,)
        ).fetchone()
        return self._r(row)

    def rehearsal_by_plan(self, plan_hash: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM migration_rehearsals WHERE plan_hash = ? ORDER BY created_at DESC",
            (plan_hash,),
        ).fetchone()
        return self._r(row)

    def latest_rehearsal(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM migration_rehearsals WHERE status != 'planned' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return self._r(row)

    def active_rehearsals(self) -> list[dict[str, Any]]:
        return [
            r
            for r in (
                self._r(x)
                for x in self.conn.execute(
                    "SELECT * FROM migration_rehearsals WHERE status IN ('copying', 'running')"
                )
            )
            if r
        ]

    @staticmethod
    def _r(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        d["plan"] = json.loads(d.pop("plan_json"))
        d["step_results"] = json.loads(d.pop("step_results_json") or "[]")
        return d
