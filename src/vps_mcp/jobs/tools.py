"""Register the 4 declared-job tools (contracts/job-tools.md)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from vps_mcp.jobs import runs
from vps_mcp.migration.runtime import MigrationRuntime

Wrap = Callable[[Callable[..., Awaitable[dict[str, Any]]]], Callable[..., Awaitable[Any]]]
RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


def register(
    mcp: MCPServer, rt: MigrationRuntime, wrap: Wrap, session_id: Callable[[Any], str | None]
) -> None:
    @mcp.tool(
        annotations=RO,
        description=(
            "List the jobs the operator declared (script, databases it may rebuild, services it "
            "may stop, parameters) and their recent runs."
        ),
    )
    @wrap
    async def list_jobs() -> dict[str, Any]:
        return runs.list_jobs(rt)

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
        description=(
            "Run a declared job (an operator-owned script). WARNING: the job may drop and rebuild "
            "the databases it declares and stop the services it declares. confirm=false returns "
            "the plan (script fingerprint, what will be changed, free disk) and a plan_id; "
            "confirm=true with that plan_id starts it in the background on the VPS. Only declared "
            "parameters are accepted."
        ),
    )
    @wrap
    async def run_job(
        job: str,
        confirm: Annotated[bool, Field(description="false = plan only; true = start")],
        params: Annotated[
            dict[str, str] | None,
            Field(description='Declared parameters only, e.g. {"RUN_NAME": "nightly_1"}'),
        ] = None,
        plan_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await runs.start(rt, job, params, plan_id, confirm, session_id(ctx))

    @mcp.tool(
        annotations=RO,
        description=(
            "Status of a job run (latest if no run_id): state, elapsed time, recognised steps and "
            "recent progress lines since this run started, undeclared-change findings, and the "
            "final report with a verdict once it has ended."
        ),
    )
    @wrap
    async def job_status(run_id: str | None = None) -> dict[str, Any]:
        return await runs.status(rt, run_id)

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
        description=(
            "Stop a running job and all of its processes. Declared databases may be left "
            "half-built and declared services stopped; nothing is repaired automatically."
        ),
    )
    @wrap
    async def cancel_job(
        run_id: str, confirm: bool = False, ctx: Context | None = None
    ) -> dict[str, Any]:
        return await runs.cancel(rt, run_id, confirm, session_id(ctx))
