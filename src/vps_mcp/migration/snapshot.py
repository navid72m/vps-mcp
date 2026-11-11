"""Snapshots: one captured description of a database (data-model §2, research M3)."""

from __future__ import annotations

import json
from typing import Any

from vps_mcp.migration.runtime import MigrationRuntime
from vps_mcp.migration.store import new_id
from vps_mcp.store.db import utcnow

EXACT_COUNT_BATCH = 100


async def capture(rt: MigrationRuntime, db: str, *, is_copy: bool = False) -> dict[str, Any]:
    tables_raw = await rt.db.query(db, "tables") or {}
    has_odoo = "public.ir_module_module" in tables_raw
    modules = await rt.db.query(db, "modules") if has_odoo else None
    views = (
        await rt.db.query(db, "views") if has_odoo and "public.ir_ui_view" in tables_raw else None
    )
    info = await rt.db.db_info(db)
    tables = {
        name: {
            "columns": t.get("columns") or {},
            "indexes": t.get("indexes") or [],
            "constraints": t.get("constraints") or [],
            "est_rows": t.get("est_rows"),
            "exact_rows": None,
            "total_bytes": t.get("total_bytes"),
        }
        for name, t in tables_raw.items()
    }
    snap = {
        "id": new_id("snp"),
        "database": db,
        "is_copy": is_copy,
        "taken_at": utcnow(),
        "size_bytes": info.get("size_bytes"),
        "tables": tables,
        "modules": modules,
        "views": views,
    }
    rt.store.save_snapshot(snap)
    return snap


async def add_exact_counts(rt: MigrationRuntime, snap: dict[str, Any], names: list[str]) -> None:
    """Fill ``exact_rows`` for the given tables (only those present), then re-save."""
    wanted = sorted(
        n for n in set(names) if n in snap["tables"] and snap["tables"][n]["exact_rows"] is None
    )
    for i in range(0, len(wanted), EXACT_COUNT_BATCH):
        chunk = wanted[i : i + EXACT_COUNT_BATCH]
        counts = (
            await rt.db.query(snap["database"], "exact_counts", {"tables": json.dumps(chunk)}) or {}
        )
        for name, n in counts.items():
            if name in snap["tables"] and n is not None:
                snap["tables"][name]["exact_rows"] = int(n)
    rt.store.save_snapshot(snap)
