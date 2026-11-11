"""Register the 13 migration tools (contracts/migration-tools.md)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from vps_mcp.migration import (
    checks,
    cleanup,
    compare,
    copies,
    pipeline_logs,
    rehearsal,
    understand,
)
from vps_mcp.migration.runtime import MigrationRuntime

log = logging.getLogger(__name__)

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
DbParam = Annotated[
    str,
    Field(
        description="Allowlisted database (db_list) or a vmcp_ copy",
        json_schema_extra={"maxLength": 63},
    ),
]
ConfirmRequired = Annotated[
    bool, Field(description="false returns a plan/preview and changes nothing; true applies")
]

Wrap = Callable[[Callable[..., Awaitable[dict[str, Any]]]], Callable[..., Awaitable[Any]]]


def _rw(destructive: bool) -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=False,
        destructive_hint=destructive,
        idempotent_hint=False,
        open_world_hint=False,
    )


def register(
    mcp: MCPServer, rt: MigrationRuntime, wrap: Wrap, session_id: Callable[[Any], str | None]
) -> None:
    async def sweep() -> None:
        try:
            await copies.sweep(rt)
        except Exception:  # noqa: BLE001 - best effort; expiry timers on the VPS are primary
            log.warning("copy sweep failed", exc_info=True)

    # ------------------------------------------------------------ US1: understand

    @mcp.tool(
        annotations=RO,
        description=(
            "List databases you may analyse (allowlisted targets) and the disposable rehearsal "
            "copies vps-mcp created, with sizes."
        ),
    )
    @wrap
    async def db_list() -> dict[str, Any]:
        await sweep()
        return await understand.db_list(rt)

    @mcp.tool(
        annotations=RO,
        description=(
            "Overview of a database: size, largest tables, Odoo module counts by state and major "
            "version, and problem indicators (transitional modules, missing code, inactive and "
            "orphaned views)."
        ),
    )
    @wrap
    async def db_overview(db: DbParam) -> dict[str, Any]:
        return await understand.overview(rt, db)

    @mcp.tool(
        annotations=RO,
        description=(
            "List Odoo modules of a database with state, version and whether their code exists in "
            "the addons paths. Filter by state, major version (e.g. '17') or code_missing."
        ),
    )
    @wrap
    async def db_modules(
        db: DbParam,
        state: str | None = None,
        major: str | None = None,
        code_missing: bool | None = None,
    ) -> dict[str, Any]:
        return await understand.modules(rt, db, state, major, code_missing)

    @mcp.tool(
        annotations=RO,
        description=(
            "Structure of one table: columns, types, nullability, defaults, indexes, constraints."
        ),
    )
    @wrap
    async def db_table(
        db: DbParam, table: Annotated[str, Field(description="'table' or 'schema.table'")]
    ) -> dict[str, Any]:
        return await understand.table(rt, db, table)

    # ------------------------------------------------------------ US2: compare

    @mcp.tool(
        annotations=RO,
        description=(
            "Compare two databases (e.g. pre-upgrade dump vs upgraded result) and produce a stored "
            "risk report: findings classified blocking/warning/info and a verdict "
            "(safe_to_promote / review_warnings / do_not_promote)."
        ),
    )
    @wrap
    async def db_compare(
        before: DbParam, after: DbParam, exact_counts: bool = True
    ) -> dict[str, Any]:
        return await compare.compare(rt, before, after, exact_counts)

    @mcp.tool(
        annotations=RO,
        description=(
            "Fetch a stored report's findings, 50 per page, optionally filtered by severity or "
            "category."
        ),
    )
    @wrap
    async def get_report(
        report_id: str,
        severity: Literal["blocking", "warning", "info"] | None = None,
        category: str | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        return compare.get_report(rt, report_id, severity, category, page)

    @mcp.tool(annotations=RO, description="List recent migration reports with their verdicts.")
    @wrap
    async def list_reports(
        limit: Annotated[int, Field(json_schema_extra={"minimum": 1, "maximum": 100})] = 20,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            from vps_mcp.errors import invalid

            raise invalid("limit must be 1..100")
        return {"reports": rt.store.list_reports(limit)}

    # ------------------------------------------------------------ US3: rehearse

    @mcp.tool(
        annotations=_rw(False),
        description=(
            "Rehearse a declared pipeline on a disposable COPY of an allowlisted database. "
            "confirm=false returns the plan (copy name, steps with file fingerprints, disk before/"
            "after) and a plan_hash; confirm=true with that plan_hash starts it in the background "
            "on the VPS. The source database is never modified."
        ),
    )
    @wrap
    async def rehearse(
        pipeline: str,
        source: DbParam,
        confirm: ConfirmRequired,
        method: Literal["auto", "template", "dump"] = "auto",
        plan_hash: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        await sweep()
        return await rehearsal.start(
            rt, pipeline, source, method, plan_hash or "", confirm, session_id(ctx)
        )

    @mcp.tool(
        annotations=RO,
        description=(
            "Status of a rehearsal (latest if no id): per-step results and, when finished, the "
            "risk report summary."
        ),
    )
    @wrap
    async def rehearsal_status(rehearsal_id: str | None = None) -> dict[str, Any]:
        return await rehearsal.status(rt, rehearsal_id)

    @mcp.tool(
        annotations=_rw(True),
        description=("Stop a running rehearsal. The copy is kept for inspection until it expires."),
    )
    @wrap
    async def cancel_rehearsal(
        rehearsal_id: str, confirm: ConfirmRequired, ctx: Context | None = None
    ) -> dict[str, Any]:
        return await rehearsal.cancel(rt, rehearsal_id, confirm, session_id(ctx))

    # ------------------------------------------------------------ US4: your runs

    @mcp.tool(
        annotations=RO,
        description=(
            "Summarise the latest run of a declared pipeline that you ran yourself: step statuses "
            "from its progress file and the most frequent errors in its Odoo logs."
        ),
    )
    @wrap
    async def pipeline_last_run(pipeline: str) -> dict[str, Any]:
        return await pipeline_logs.last_run(rt, pipeline)

    # ------------------------------------------------------------ US5: checks

    @mcp.tool(
        annotations=_rw(False),
        description=(
            "Run a declared check (e.g. post-upgrade checks) against a database. read_only checks "
            "run immediately with a read-only session; may_modify checks only run on a rehearsal "
            "copy and need confirm=true. Results are added to the latest report for that database."
        ),
    )
    @wrap
    async def run_check(
        check: str, db: DbParam, confirm: bool = False, ctx: Context | None = None
    ) -> dict[str, Any]:
        return await checks.run_check(rt, check, db, confirm, session_id(ctx))

    # ------------------------------------------------------------ US6: cleanup

    @mcp.tool(
        annotations=_rw(True),
        description=(
            "Drop disposable rehearsal copies (by copy_id, or all expired ones). Only copies "
            "created by vps-mcp can ever be dropped. confirm=false previews what would be dropped."
        ),
    )
    @wrap
    async def drop_copies(
        confirm: ConfirmRequired,
        copy_ids: list[str] | None = None,
        expired_only: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await cleanup.drop_copies(rt, copy_ids, expired_only, confirm, session_id(ctx))
