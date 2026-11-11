"""US1: database overview, modules, table detail, problem indicators (FR-007–FR-010)."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError, invalid
from vps_mcp.migration import catalog
from vps_mcp.migration.config import DB_NAME_RE
from vps_mcp.migration.db import COPY_NAME_RE_PATTERN, not_permitted_db
from vps_mcp.migration.runtime import MigrationRuntime

COPY_NAME_RE = re.compile(COPY_NAME_RE_PATTERN)
TRANSITIONAL = ("to upgrade", "to install", "to remove")


def resolve_db(rt: MigrationRuntime, name: Any, *, allow_copies: bool = True) -> tuple[str, bool]:
    """Return (name, is_copy) for an allowlisted target or a live copy we created."""
    if not isinstance(name, str) or not (
        DB_NAME_RE.fullmatch(name) or COPY_NAME_RE.fullmatch(name)
    ):
        raise invalid(f"database: {str(name)[:80]!r} is not a valid database name")
    if name in rt.settings.target_names():
        return name, False
    if allow_copies and COPY_NAME_RE.fullmatch(name):
        row = rt.store.copy_by_name(name)
        if row and row["state"] in ("ready", "in_use"):
            return name, True
    raise not_permitted_db(name)


def major_of(version: str | None) -> str | None:
    if not version:
        return None
    head = version.split(".", 1)[0]
    return head if head.isdigit() else None


async def addons_listing(rt: MigrationRuntime) -> set[str] | None:
    """Module directory names found in the configured addons paths (None if unreadable)."""
    odoo = rt.settings.odoo
    if odoo is None or not odoo.addons_paths:
        return None
    found: set[str] = set()
    for path in odoo.addons_paths:
        r = await rt.ex.run(["ls", "-1", "--", path], timeout=30.0)
        if not r.ok:
            return None
        found.update(line.strip() for line in r.stdout.splitlines() if line.strip())
    return found


def module_rows(modules: dict[str, Any], code: set[str] | None) -> list[dict[str, Any]]:
    rows = []
    for name in sorted(modules):
        m = modules[name]
        row: dict[str, Any] = {"name": name, "state": m.get("state"), "version": m.get("version")}
        if code is not None:
            row["code"] = name in code
        rows.append(row)
    return rows


async def overview(rt: MigrationRuntime, db: str) -> dict[str, Any]:
    name, _ = resolve_db(rt, db)
    ov = await rt.db.query(name, "overview")
    out: dict[str, Any] = {
        "database": name,
        "size_bytes": ov["size_bytes"],
        "tables": ov["tables"],
        "largest": ov["largest"],
    }
    if not ov.get("has_odoo"):
        out["skipped"] = ["modules and views: not an Odoo database"]
        return out
    modules = await rt.db.query(name, "modules") or {}
    views = await rt.db.query(name, "view_stats") or {}
    code = await addons_listing(rt)
    installed = {k: v for k, v in modules.items() if v.get("state") == "installed"}
    out["modules"] = {
        "by_state": dict(sorted(Counter(m.get("state") for m in modules.values()).items())),
        "by_major": dict(
            sorted(
                Counter(major_of(m.get("version")) or "unknown" for m in installed.values()).items()
            )
        ),
    }
    indicators: dict[str, Any] = {
        "transitional": sorted(k for k, v in modules.items() if v.get("state") in TRANSITIONAL),
        "inactive_views": views.get("inactive", 0),
        "orphaned_views": views.get("orphaned", 0),
    }
    if code is None:
        out["skipped"] = ["code presence: addons paths not configured or not readable"]
    else:
        indicators["code_missing"] = sorted(k for k in installed if k not in code)
    out["indicators"] = indicators
    return out


async def modules(
    rt: MigrationRuntime,
    db: str,
    state: str | None = None,
    major: str | None = None,
    code_missing: bool | None = None,
) -> dict[str, Any]:
    name, _ = resolve_db(rt, db)
    ov = await rt.db.query(name, "overview")
    if not ov.get("has_odoo"):
        raise VpsMcpError(ErrorCategory.NOT_FOUND, f"'{name}' is not an Odoo database")
    mods = await rt.db.query(name, "modules") or {}
    code = await addons_listing(rt)
    rows = module_rows(mods, code)
    if state:
        rows = [r for r in rows if r["state"] == state]
    if major:
        rows = [r for r in rows if major_of(r["version"]) == major]
    if code_missing is not None:
        if code is None:
            raise VpsMcpError(ErrorCategory.FAILED, "addons paths are not configured or readable")
        rows = [r for r in rows if r["code"] is (not code_missing)]
    return {"database": name, "count": len(rows), "modules": rows}


async def table(rt: MigrationRuntime, db: str, table_name: str) -> dict[str, Any]:
    name, _ = resolve_db(rt, db)
    sch, tbl = catalog.split_table(table_name)
    data = await rt.db.query(name, "table", {"sch": sch, "tbl": tbl})
    if data is None:
        raise VpsMcpError(ErrorCategory.NOT_FOUND, f"table '{sch}.{tbl}' not found in '{name}'")
    return dict(data)


async def db_list(rt: MigrationRuntime) -> dict[str, Any]:
    sizes = await rt.db.query("postgres", "db_sizes") or {}
    targets = [
        {"name": t.name, "purpose": t.purpose, "size_bytes": sizes.get(t.name)}
        for t in rt.settings.target
    ]
    copies = [
        {
            "copy_id": c["copy_id"],
            "name": c["name"],
            "source": c["source"],
            "state": c["state"],
            "created_at": c["created_at"],
            "expires_at": c["expires_at"],
            "size_bytes": sizes.get(c["name"]),
            "rehearsal_id": c["rehearsal_id"],
            "report_id": c["report_id"],
        }
        for c in rt.store.copies()
    ]
    return {"targets": targets, "copies": copies}
