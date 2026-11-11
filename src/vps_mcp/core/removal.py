"""Remove a service (US6, FR-021, FR-028)."""

from __future__ import annotations

from typing import Any

from vps_mcp.config.policy import ServiceDeclaration
from vps_mcp.core.context import AppContext
from vps_mcp.core.guard import ActionRequest, run_action
from vps_mcp.models.inputs import ServiceKind


async def _plan(ctx: AppContext, decl: ServiceDeclaration, delete_data: bool) -> dict[str, Any]:
    backend = ctx.backend(decl.kind)
    plan: dict[str, Any] = {
        "will_remove": f"{decl.kind.value} service '{decl.name}'",
        "delete_data": delete_data,
    }
    if decl.kind is ServiceKind.CONTAINER:
        data = await backend.inspect(decl.name)
        volumes = [
            m["Name"]
            for m in (data or {}).get("Mounts") or []
            if m.get("Type") == "volume" and m.get("Name")
        ]
        nets = list((((data or {}).get("NetworkSettings") or {}).get("Networks") or {}).keys())
        plan["volumes_deleted" if delete_data else "volumes_kept"] = volumes
        plan["networks_checked_for_cleanup"] = nets
    else:
        plan["package"] = decl.package
        plan["data_paths_deleted" if delete_data else "data_paths_kept"] = list(decl.data_paths)
    return plan


async def remove_service(
    ctx: AppContext,
    *,
    service: str,
    kind: str | None,
    delete_data: bool,
    confirm: bool,
    session_id: str | None,
) -> dict[str, Any]:
    async def preview(decl: ServiceDeclaration) -> dict[str, Any]:
        return await _plan(ctx, decl, delete_data)

    async def body(decl: ServiceDeclaration) -> dict[str, Any]:
        result: dict[str, Any] = await ctx.backend(decl.kind).remove(decl, delete_data)
        declared = ctx.managed.declared(decl.name, decl.kind.value) is not None
        ctx.services.mark_removed(decl.name, decl.kind.value, declared=declared)
        result.setdefault("networks_removed", [])
        return result

    req = ActionRequest(
        action="remove",
        service=service,
        kind=kind,
        confirm=confirm,
        params={"service": service, "kind": kind, "delete_data": delete_data, "confirm": confirm},
        session_id=session_id,
        timeout=float(ctx.policy.timeouts.deploy_s),
        needs_confirm=lambda _d: True,
        preview=preview,
        is_remove=True,
    )
    return await run_action(ctx, req, body)
