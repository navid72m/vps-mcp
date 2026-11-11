"""Lifecycle control: start / stop / restart / boot-start (US2)."""

from __future__ import annotations

from typing import Any

from vps_mcp.config.policy import ServiceDeclaration
from vps_mcp.core.context import AppContext
from vps_mcp.core.guard import ActionRequest, run_action
from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.models.views import ServiceStatus

FAILED_START_LOG_LINES = 20


async def _attach_logs(ctx: AppContext, decl: ServiceDeclaration, err: VpsMcpError) -> None:
    try:
        entries = await ctx.backend(decl.kind).logs(decl, FAILED_START_LOG_LINES, None, None)
        err.extra["logs"] = [e.render() for e in entries]
    except Exception:  # noqa: BLE001, S110 - never mask the original failure
        pass


async def control(
    ctx: AppContext,
    verb: str,
    service: str,
    kind: str | None,
    confirm: bool,
    session_id: str | None,
) -> dict[str, Any]:
    assert verb in ("start", "stop", "restart")

    async def body(decl: ServiceDeclaration) -> dict[str, Any]:
        backend = ctx.backend(decl.kind)
        try:
            st: ServiceStatus = await getattr(backend, verb)(decl)
        except VpsMcpError as err:
            if verb != "stop" and err.category is ErrorCategory.FAILED:
                await _attach_logs(ctx, decl, err)
            raise
        return {"service": st.to_dict()}

    req = ActionRequest(
        action=verb,
        service=service,
        kind=kind,
        confirm=confirm,
        params={"service": service, "kind": kind, "confirm": confirm},
        session_id=session_id,
        timeout=float(ctx.policy.timeouts.control_s),
        needs_confirm=lambda d: verb in ("stop", "restart") and d.protected,
    )
    return await run_action(ctx, req, body)


async def set_boot_start(
    ctx: AppContext,
    service: str,
    kind: str | None,
    enabled: bool,
    confirm: bool,
    session_id: str | None,
) -> dict[str, Any]:
    async def body(decl: ServiceDeclaration) -> dict[str, Any]:
        st = await ctx.backend(decl.kind).set_boot(decl, enabled)
        return {"service": st.to_dict()}

    req = ActionRequest(
        action="enable" if enabled else "disable",
        service=service,
        kind=kind,
        confirm=confirm,
        params={"service": service, "kind": kind, "enabled": enabled, "confirm": confirm},
        session_id=session_id,
        timeout=float(ctx.policy.timeouts.control_s),
        needs_confirm=lambda d: (not enabled) and d.protected,
    )
    return await run_action(ctx, req, body)
