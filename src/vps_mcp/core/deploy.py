"""Deploy a new service from an approved source (US4, FR-014–FR-016)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from vps_mcp.config.policy import ServiceDeclaration
from vps_mcp.core.context import AppContext
from vps_mcp.core.guard import ActionRequest, run_action
from vps_mcp.core.resources import free_disk_mb
from vps_mcp.core.sources import check_image_approved
from vps_mcp.errors import ErrorCategory, VpsMcpError, invalid, not_permitted
from vps_mcp.models.inputs import (
    PackageName,
    PackageVersion,
    ServiceKind,
    ServiceName,
    first_error,
    validated,
)
from vps_mcp.models.views import ContainerSpec, ServiceState
from vps_mcp.remote.redact import mask_mapping


class OsDeploy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package: PackageName
    version: PackageVersion | None = None
    unit: ServiceName | None = None
    enable_on_boot: bool = True


def _parse(model: type[BaseModel], data: Any, field: str) -> Any:
    if not isinstance(data, dict):
        raise invalid(f"{field}: must be an object")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise invalid(f"{field}.{first_error(exc)}") from None


def _decl_for(
    ctx: AppContext, name: str, kind: ServiceKind, os_req: OsDeploy | None
) -> ServiceDeclaration:
    declared = ctx.managed.declared(name, kind.value)
    if kind is ServiceKind.OS:
        assert os_req is not None
        base = declared.model_dump(by_alias=True) if declared else {"name": name, "kind": "os"}
        base["package"] = os_req.package
        if os_req.unit:
            base["unit"] = os_req.unit
        decl = ServiceDeclaration.model_validate(base)
    else:
        decl = declared or ServiceDeclaration(name=name, kind=ServiceKind.CONTAINER)
    if ctx.managed.is_remote_access(decl) or ctx.managed.is_remote_access_name(name):
        raise not_permitted(name)
    return decl


async def _prechecks(
    ctx: AppContext, decl: ServiceDeclaration, os_req: OsDeploy | None, spec: ContainerSpec | None
) -> dict[str, Any]:
    """Approved source -> conflict -> free disk. Shared by preview and apply."""
    backend = ctx.backend(decl.kind)
    info: dict[str, Any] = {}
    if spec is not None:
        info["image"] = check_image_approved(spec.image, ctx.policy.approved_sources.image_prefixes)
    else:
        assert os_req is not None
        info["version"] = await backend.check_origin(os_req.package, os_req.version)

    deployed = ctx.managed.deployed_spec(decl.name, decl.kind.value) is not None
    st = await backend.status(decl)
    if deployed or st.state is not ServiceState.NOT_FOUND:
        raise VpsMcpError(
            ErrorCategory.CONFLICT,
            f"a {decl.kind.value} service named '{decl.name}' already exists",
        )

    paths = ["/"]
    if spec is not None:
        root = await backend.data_root()
        if root:
            paths.append(root)
    free = await free_disk_mb(ctx, paths)
    low = {m: mb for m, mb in free.items() if mb < ctx.policy.min_free_disk_mb}
    if low:
        raise VpsMcpError(
            ErrorCategory.INSUFFICIENT_DISK,
            f"free disk below {ctx.policy.min_free_disk_mb} MB on {low}",
        )
    info["free_disk_mb"] = free
    if spec is not None:
        info["networks_to_create"] = await backend.missing_networks(list(spec.networks))
    return info


async def deploy_service(
    ctx: AppContext,
    *,
    name: str,
    kind: str,
    os: dict[str, Any] | None,
    container: dict[str, Any] | None,
    start: bool,
    confirm: bool,
    session_id: str | None,
) -> dict[str, Any]:
    masked_container = None
    if isinstance(container, dict):
        masked_container = dict(container)
        if isinstance(container.get("env"), dict):
            masked_container["env"] = mask_mapping(container["env"], ctx.policy.secret_keys)
    params = {
        "name": name,
        "kind": kind,
        "os": os,
        "container": masked_container,
        "start": start,
        "confirm": confirm,
    }
    parsed: dict[str, Any] = {}

    async def resolver() -> ServiceDeclaration:
        validated(ServiceName, name, "name")
        k = validated(ServiceKind, kind, "kind")
        if k is ServiceKind.OS:
            if os is None or container is not None:
                raise invalid("kind='os' requires 'os' and no 'container'")
            parsed["os"] = _parse(OsDeploy, os, "os")
        else:
            if container is None or os is not None:
                raise invalid("kind='container' requires 'container' and no 'os'")
            parsed["spec"] = _parse(ContainerSpec, container, "container")
        ctx.backend(k)  # not_permitted if the runtime is disabled
        return _decl_for(ctx, name, k, parsed.get("os"))

    def describe(decl: ServiceDeclaration, info: dict[str, Any]) -> dict[str, Any]:
        spec: ContainerSpec | None = parsed.get("spec")
        os_req: OsDeploy | None = parsed.get("os")
        if spec is not None:
            what = spec.model_dump(mode="json", exclude_none=True)
            what["env"] = mask_mapping(spec.env, ctx.policy.secret_keys)
        else:
            assert os_req is not None
            what = {**os_req.model_dump(exclude_none=True), "unit": decl.unit_name}
        return {"would_install": what, **info}

    async def preview(decl: ServiceDeclaration) -> dict[str, Any]:
        info = await _prechecks(ctx, decl, parsed.get("os"), parsed.get("spec"))
        return describe(decl, info)

    async def body(decl: ServiceDeclaration) -> dict[str, Any]:
        backend = ctx.backend(decl.kind)
        spec: ContainerSpec | None = parsed.get("spec")
        os_req: OsDeploy | None = parsed.get("os")
        try:
            await _prechecks(ctx, decl, os_req, spec)
        except VpsMcpError as err:
            # A stale package index can hide the package; refresh once and re-check. Policy
            # refusals (unapproved source, conflict, disk) are never retried.
            if os_req is None or err.category is not ErrorCategory.FAILED:
                raise
            await backend.refresh_index()
            await _prechecks(ctx, decl, os_req, spec)
        try:
            if spec is not None:
                state = await backend.deploy(decl, spec, start=start)
            else:
                assert os_req is not None
                state = await backend.deploy(
                    decl, os_req.version, start=start, enable_on_boot=os_req.enable_on_boot
                )
        except VpsMcpError as err:
            cleanup = err.extra.pop("_cleanup", None)
            if spec is not None:
                ok = await backend.cleanup_failed_deploy(decl, spec, cleanup or {})
            else:
                ok = await backend.cleanup_failed_deploy(
                    decl, bool((cleanup or {}).get("was_installed", True))
                )
            err.extra["cleaned_up"] = ok
            raise
        st = state["status"]
        record: dict[str, Any] = {"declaration": decl.model_dump(mode="json", by_alias=True)}
        if spec is not None:
            record["container"] = spec.model_dump(mode="json")
            content: dict[str, Any] = {"spec": spec.model_dump(mode="json")}
            version: str | None = spec.image
        else:
            version = await backend.package_version(decl.package)
            content = {"files": await backend.read_files(decl)} if decl.config_files else {}
        ctx.services.add_deployed(decl.name, decl.kind.value, record)
        rev_id = ctx.revisions.activate_new(decl.name, decl.kind.value, version, content)
        result: dict[str, Any] = {"service": st.to_dict()}
        if spec is not None:
            result["networks_created"] = state.get("created_networks", [])
        result["_after_audit"] = lambda audit_id: ctx.revisions.set_audit(rev_id, audit_id)
        return result

    req = ActionRequest(
        action="deploy",
        service=name,
        kind=kind,
        confirm=confirm,
        params=params,
        session_id=session_id,
        timeout=float(ctx.policy.timeouts.deploy_s),
        needs_confirm=lambda _d: True,
        preview=preview,
        resolver=resolver,
    )
    return await run_action(ctx, req, body)
