"""US6: list and drop disposable copies (FR-026, FR-027)."""

from __future__ import annotations

from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError, invalid
from vps_mcp.migration import copies
from vps_mcp.migration.runtime import DbAction, MigrationRuntime, run_db_action


async def drop_copies(
    rt: MigrationRuntime,
    copy_ids: list[str] | None,
    expired_only: bool,
    confirm: bool,
    session_id: str | None,
) -> dict[str, Any]:
    if not copy_ids and not expired_only:
        raise invalid("give copy_ids or expired_only=true")
    rows = {r["copy_id"]: r for r in rt.store.copies()}
    selected: list[dict[str, Any]] = []
    refused: list[dict[str, str]] = []
    if copy_ids:
        for cid in copy_ids:
            row = rows.get(cid) if isinstance(cid, str) else None
            if row is None:
                refused.append(
                    {"copy_id": str(cid)[:40], "reason": "not a copy created by vps-mcp"}
                )
            elif row["state"] == "in_use" or row["state"] == "creating":
                refused.append({"copy_id": cid, "reason": f"copy is {row['state']}"})
            else:
                selected.append(row)
    else:
        selected = [
            r for r in rows.values() if r["state"] in ("ready", "failed") and copies.expired(r)
        ]
    sizes = await rt.db.query("postgres", "db_sizes") or {}

    async def preview() -> dict[str, Any]:
        return {
            "will_drop": [
                {
                    "copy_id": r["copy_id"],
                    "name": r["name"],
                    "source": r["source"],
                    "size_bytes": sizes.get(r["name"]),
                    "expires_at": r["expires_at"],
                }
                for r in selected
            ],
            "reclaim_bytes": sum(int(sizes.get(r["name"]) or 0) for r in selected),
            "refused": refused,
        }

    async def body() -> dict[str, Any]:
        dropped, reclaimed, out_refused = [], 0, list(refused)
        for r in selected:
            ok, reason = await copies.drop(rt, r)
            if ok:
                dropped.append(r["name"])
                reclaimed += int(sizes.get(r["name"]) or 0)
            else:
                out_refused.append({"copy_id": r["copy_id"], "name": r["name"], "reason": reason})
        if not dropped and out_refused and len(out_refused) == len(selected) + len(refused):
            raise VpsMcpError(
                ErrorCategory.NOT_PERMITTED,
                "no copy could be dropped",
                extra={"refused": out_refused},
            )
        return {"dropped": dropped, "reclaimed_bytes": reclaimed, "refused": out_refused}

    return await run_db_action(
        rt,
        DbAction(
            action="drop_copy",
            target=None,
            confirm=confirm,
            params={"copy_ids": copy_ids, "expired_only": expired_only},
            session_id=session_id,
            timeout=1800.0,
            preview=preview,
        ),
        body,
    )
