"""Service revisions for preview and rollback (FR-019, FR-020, data-model.md §3).

Transitions:
  apply change:    active -> previous ; new -> active ; older previous -> superseded
  auto-rollback:   new -> rolled_back ; previous -> active
  manual rollback: active -> rolled_back ; previous -> active
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.store.db import utcnow


@dataclass(frozen=True)
class Revision:
    id: int
    version: str | None
    content: dict[str, Any]
    status: str
    created_at: str

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "status": self.status,
            "created_at": self.created_at,
        }


def _row(r: sqlite3.Row) -> Revision:
    return Revision(
        r["id"], r["version"], json.loads(r["content_json"]), r["status"], r["created_at"]
    )


class RevisionStore:
    def __init__(self, conn: sqlite3.Connection, *, keep: int = 5) -> None:
        self.conn = conn
        self.keep = keep

    def _one(self, service: str, kind: str, status: str) -> Revision | None:
        r = self.conn.execute(
            "SELECT * FROM service_revisions WHERE service_name = ? AND kind = ? AND status = ? "
            "ORDER BY id DESC LIMIT 1",
            (service, kind, status),
        ).fetchone()
        return _row(r) if r else None

    def active(self, service: str, kind: str) -> Revision | None:
        return self._one(service, kind, "active")

    def previous(self, service: str, kind: str) -> Revision | None:
        return self._one(service, kind, "previous")

    def list(self, service: str, kind: str) -> list[Revision]:
        return [
            _row(r)
            for r in self.conn.execute(
                "SELECT * FROM service_revisions WHERE service_name = ? AND kind = ? ORDER BY id DESC",
                (service, kind),
            )
        ]

    def ensure_baseline(
        self, service: str, kind: str, version: str | None, content: dict[str, Any]
    ) -> Revision:
        """Record the current live state as the active revision if none exists yet."""
        cur = self.active(service, kind)
        if cur is not None:
            return cur
        rid = self._insert(service, kind, version, content, "active", None)
        rev = self._one(service, kind, "active")
        assert rev is not None and rev.id == rid
        return rev

    def _insert(
        self,
        service: str,
        kind: str,
        version: str | None,
        content: dict[str, Any],
        status: str,
        audit_id: int | None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO service_revisions (service_name, kind, version, content_json, status, "
            "created_at, audit_id) VALUES (?,?,?,?,?,?,?)",
            (service, kind, version, json.dumps(content), status, utcnow(), audit_id),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def _set(self, rev_id: int, status: str) -> None:
        self.conn.execute("UPDATE service_revisions SET status = ? WHERE id = ?", (status, rev_id))

    def activate_new(
        self,
        service: str,
        kind: str,
        version: str | None,
        content: dict[str, Any],
        audit_id: int | None = None,
    ) -> int:
        self.conn.execute("BEGIN")
        try:
            prev = self.previous(service, kind)
            if prev:
                self._set(prev.id, "superseded")
            act = self.active(service, kind)
            if act:
                self._set(act.id, "previous")
            new_id = self._insert(service, kind, version, content, "active", audit_id)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self.prune(service, kind)
        return new_id

    def mark_rolled_back(self, service: str, kind: str) -> Revision:
        """Active -> rolled_back, previous -> active. Used by auto and manual rollback."""
        prev = self.previous(service, kind)
        if prev is None:
            raise VpsMcpError(
                ErrorCategory.NOT_FOUND,
                f"service '{service}' has no previous revision to roll back to",
            )
        self.conn.execute("BEGIN")
        try:
            act = self.active(service, kind)
            if act:
                self._set(act.id, "rolled_back")
            self._set(prev.id, "active")
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        restored = self.active(service, kind)
        assert restored is not None
        return restored

    def set_audit(self, rev_id: int, audit_id: int) -> None:
        self.conn.execute(
            "UPDATE service_revisions SET audit_id = ? WHERE id = ?", (audit_id, rev_id)
        )

    def prune(self, service: str, kind: str) -> None:
        self.conn.execute(
            "DELETE FROM service_revisions WHERE service_name = ? AND kind = ? AND id NOT IN "
            "(SELECT id FROM service_revisions WHERE service_name = ? AND kind = ? "
            " ORDER BY id DESC LIMIT ?) AND status NOT IN ('active', 'previous')",
            (service, kind, service, kind, self.keep),
        )
