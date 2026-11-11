"""Audit trail of state-changing attempts (FR-034, FR-035)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from vps_mcp.remote.redact import mask_mapping, scrub, scrub_obj
from vps_mcp.store.db import utcnow

ACTIONS = (
    "start",
    "stop",
    "restart",
    "enable",
    "disable",
    "deploy",
    "update",
    "configure",
    "rollback",
    "remove",
    # database migration feature (002)
    "copy",
    "rehearse",
    "cancel_rehearsal",
    "run_check",
    "drop_copy",
    # declared jobs (003)
    "run_job",
    "cancel_job",
    # agent memory (004)
    "memory_write",
    "memory_archive",
)
OUTCOMES = ("success", "failed", "refused", "timed_out", "unknown", "rolled_back", "preview")


class AuditStore:
    def __init__(
        self, conn: sqlite3.Connection, *, secret_globs: list[str], secret_strings: list[str]
    ) -> None:
        self.conn = conn
        self.secret_globs = secret_globs
        self.secret_strings = secret_strings

    def _mask_params(self, params: dict[str, Any]) -> dict[str, Any]:
        def walk(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {k: walk(v) for k, v in mask_mapping(obj, self.secret_globs).items()}
            if isinstance(obj, list):
                return [walk(v) for v in obj]
            return obj

        masked = walk(params)
        # File contents may embed secrets in arbitrary formats; never store them.
        if isinstance(masked.get("files"), list):
            masked["files"] = [
                {"path": f.get("path"), "content_bytes": len(f.get("content") or "")}
                if isinstance(f, dict)
                else f
                for f in masked["files"]
            ]
        return dict(scrub_obj(masked, self.secret_strings))

    def record(
        self,
        *,
        session_id: str | None,
        action: str,
        service_name: str | None,
        kind: str | None,
        params: dict[str, Any],
        confirmed: bool,
        outcome: str,
        error_category: str | None = None,
        error_message: str | None = None,
        resulting_state: str | None = None,
        data_deleted: bool = False,
        duration_ms: int = 0,
    ) -> int:
        if action not in ACTIONS:
            raise ValueError(f"unknown audit action {action!r}")
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown audit outcome {outcome!r}")
        message = scrub(error_message, self.secret_strings) if error_message else None
        cur = self.conn.execute(
            """INSERT INTO audit_records (ts, session_id, action, service_name, kind, params_json,
               confirmed, outcome, error_category, error_message, resulting_state, data_deleted,
               duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                utcnow(),
                session_id,
                action,
                (service_name or "")[:128] or None,
                kind,
                json.dumps(self._mask_params(params), default=str),
                int(confirmed),
                outcome,
                error_category,
                message,
                resulting_state,
                int(data_deleted),
                duration_ms,
            ),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def set_reconciled(self, audit_id: int, state: str) -> None:
        self.conn.execute(
            "UPDATE audit_records SET reconciled_state = ? WHERE id = ?", (state, audit_id)
        )

    def pending_reconciliation(self, service_name: str, kind: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM audit_records WHERE service_name = ? AND kind = ? "
            "AND outcome = 'unknown' AND reconciled_state IS NULL LIMIT 1",
            (service_name, kind),
        ).fetchone()
        return row is not None

    def unreconciled(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT id, service_name, kind FROM audit_records "
                "WHERE outcome = 'unknown' AND reconciled_state IS NULL ORDER BY id"
            )
        )

    def query(
        self,
        *,
        limit: int = 50,
        service: str | None = None,
        action: str | None = None,
        outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be 1..500")
        sql = "SELECT * FROM audit_records WHERE 1=1"
        args: list[Any] = []
        for col, val in (("service_name", service), ("action", action), ("outcome", outcome)):
            if val is not None:
                sql += f" AND {col} = ?"
                args.append(val)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        records = []
        for row in self.conn.execute(sql, args):
            rec = dict(row)
            rec["params"] = json.loads(rec.pop("params_json") or "{}")
            rec["confirmed"] = bool(rec["confirmed"])
            rec["data_deleted"] = bool(rec["data_deleted"])
            records.append({k: v for k, v in rec.items() if v is not None})
        return records
