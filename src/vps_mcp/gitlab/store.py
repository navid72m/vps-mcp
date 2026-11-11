"""gitlab_deploys accessors (data-model.md §2)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from vps_mcp.store.db import utcnow

_ALLOWED = {
    "status",
    "commit_sha",
    "ref",
    "release_dir",
    "previous_dir",
    "pipeline_status",
    "error",
    "audit_id",
}
PLAN_TTL = timedelta(minutes=30)
STALE_RUNNING = timedelta(hours=1)


class DeployStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_plan(
        self, deploy_id: str, target: str, kind: str, plan_hash: str, plan: dict[str, Any]
    ) -> None:
        now = utcnow()
        self.conn.execute(
            "INSERT INTO gitlab_deploys (deploy_id, target, kind, plan_hash, plan_json, status, "
            "commit_sha, ref, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                deploy_id,
                target,
                kind,
                plan_hash,
                json.dumps(plan),
                "planned",
                (plan.get("commit") or {}).get("sha"),
                plan.get("ref"),
                now,
                now,
            ),
        )

    def update(self, deploy_id: str, **fields: Any) -> None:
        for k, v in fields.items():
            if k not in _ALLOWED:
                raise ValueError(k)
            self.conn.execute(
                f"UPDATE gitlab_deploys SET {k} = ?, updated_at = ? WHERE deploy_id = ?",  # noqa: S608
                (v, utcnow(), deploy_id),
            )

    def by_plan(self, target: str, plan_hash: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM gitlab_deploys WHERE target = ? AND plan_hash = ? AND status = 'planned' "
            "ORDER BY created_at DESC LIMIT 1",
            (target, plan_hash),
        ).fetchone()
        if row is None:
            return None
        d = _row(row)
        created = datetime.fromisoformat(d["created_at"])
        if datetime.now(UTC) - created > PLAN_TTL:
            return None
        return d

    def running(self, target: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM gitlab_deploys WHERE target = ? AND status = 'running' "
            "ORDER BY created_at DESC LIMIT 1",
            (target,),
        ).fetchone()
        if row is None:
            return None
        d = _row(row)
        if datetime.now(UTC) - datetime.fromisoformat(d["updated_at"]) > STALE_RUNNING:
            return None
        return d

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM gitlab_deploys WHERE status != 'planned' ORDER BY created_at DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row(r) for r in rows]


def _row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["plan"] = json.loads(d.pop("plan_json") or "null")
    return d
