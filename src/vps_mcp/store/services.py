"""Registry of services added by deploy or removed by remove (data-model.md §3)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from vps_mcp.store.db import utcnow


@dataclass(frozen=True)
class ServiceRow:
    name: str
    kind: str
    origin: str
    spec: dict[str, Any] | None
    removed: bool


class ServicesStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_deployed(self, name: str, kind: str, spec: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO managed_services (name, kind, origin, spec_json, added_at, removed_at)
               VALUES (?, ?, 'deployed', ?, ?, NULL)
               ON CONFLICT (name, kind) DO UPDATE SET origin = 'deployed',
                 spec_json = excluded.spec_json, added_at = excluded.added_at, removed_at = NULL""",
            (name, kind, json.dumps(spec), utcnow()),
        )

    def update_spec(self, name: str, kind: str, spec: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE managed_services SET spec_json = ? WHERE name = ? AND kind = ?",
            (json.dumps(spec), name, kind),
        )

    def mark_removed(self, name: str, kind: str, *, declared: bool) -> None:
        now = utcnow()
        self.conn.execute(
            """INSERT INTO managed_services (name, kind, origin, spec_json, added_at, removed_at)
               VALUES (?, ?, ?, NULL, ?, ?)
               ON CONFLICT (name, kind) DO UPDATE SET removed_at = excluded.removed_at""",
            (name, kind, "declared" if declared else "deployed", now, now),
        )

    def list_rows(self) -> list[ServiceRow]:
        return [
            ServiceRow(
                name=r["name"],
                kind=r["kind"],
                origin=r["origin"],
                spec=json.loads(r["spec_json"]) if r["spec_json"] else None,
                removed=r["removed_at"] is not None,
            )
            for r in self.conn.execute("SELECT * FROM managed_services ORDER BY name, kind")
        ]
