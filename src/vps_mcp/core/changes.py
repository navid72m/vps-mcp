"""Configure, update and roll back services with a health gate (US5, FR-017–FR-020)."""

from __future__ import annotations

import asyncio
import difflib
import json
import time
from typing import Any

from vps_mcp.backends import common
from vps_mcp.config.policy import ServiceDeclaration
from vps_mcp.core.context import AppContext
from vps_mcp.core.guard import ActionRequest, run_action
from vps_mcp.core.sources import check_image_approved
from vps_mcp.errors import ErrorCategory, VpsMcpError, invalid, not_permitted
from vps_mcp.models.inputs import (
    AbsPath,
    ImageRef,
    PackageVersion,
    ServiceKind,
    first_error,
    validated,
)
from vps_mcp.models.views import ContainerSpec, ServiceState, ServiceStatus
from vps_mcp.remote.redact import mask_mapping, mask_text_lines

MAX_FILE_BYTES = 1_000_000

# ----------------------------------------------------------------------------- helpers


def _unified(old: str, new: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a{name}" if name.startswith("/") else f"a/{name}",
            tofile=f"b{name}" if name.startswith("/") else f"b/{name}",
        )
    )


def masked_spec(spec: ContainerSpec, globs: list[str]) -> dict[str, Any]:
    d = spec.model_dump(mode="json", exclude_none=True)
    d["env"] = mask_mapping(spec.env, globs)
    return d


def spec_diff(old: ContainerSpec, new: ContainerSpec, globs: list[str]) -> str:
    a = json.dumps(masked_spec(old, globs), indent=2, sort_keys=True) + "\n"
    b = json.dumps(masked_spec(new, globs), indent=2, sort_keys=True) + "\n"
    diff = _unified(a, b, "container-spec.json")
    # A secret value change is invisible after masking; say so explicitly.
    changed = sorted(
        k
        for k in set(old.env) | set(new.env)
        if old.env.get(k) != new.env.get(k) and mask_mapping({k: "x"}, globs)[k] != "x"
    )
    if changed:
        diff += f"# secret env values changed: {', '.join(changed)}\n"
    return diff


def files_diff(old: dict[str, str], new: dict[str, str], globs: list[str]) -> str:
    return "".join(
        _unified(mask_text_lines(old.get(p, ""), globs), mask_text_lines(new[p], globs), p)
        for p in sorted(new)
        if old.get(p, "") != new[p]
    )


def merge_spec(current: ContainerSpec, patch: dict[str, Any]) -> ContainerSpec:
    base = current.model_dump(mode="json")
    defaults = ContainerSpec(image=current.image).model_dump(mode="json")
    for key, value in patch.items():
        if key not in base:
            raise invalid(f"spec_patch: unknown field '{key}'")
        if value is None:
            if key == "image":
                raise invalid("spec_patch: image cannot be removed")
            base[key] = defaults[key]
        elif key == "env":
            if not isinstance(value, dict):
                raise invalid("spec_patch.env must be an object")
            env = dict(base["env"])
            for k, v in value.items():
                if v is None:
                    env.pop(k, None)
                else:
                    env[k] = v
            base["env"] = env
        else:
            base[key] = value  # lists (networks, ports, volumes) are replaced whole
    try:
        return ContainerSpec.model_validate(base)
    except Exception as exc:  # pydantic ValidationError
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            raise invalid(f"spec_patch: {first_error(exc)}") from None
        raise


async def health_gate(ctx: AppContext, decl: ServiceDeclaration) -> tuple[bool, ServiceStatus]:
    """Healthy = running (and container healthcheck not unhealthy) for the stabilise window."""
    backend = ctx.backend(decl.kind)

    def healthy(st: ServiceStatus) -> bool:
        return st.state is ServiceState.RUNNING and st.health not in ("unhealthy", "starting")

    def settled(st: ServiceStatus) -> bool:
        if st.state in (ServiceState.FAILED, ServiceState.NOT_FOUND, ServiceState.STOPPED):
            return True
        return healthy(st) or st.health == "unhealthy"

    st = await common.poll_status(
        lambda: backend.status(decl), done=settled, deadline_s=float(ctx.policy.timeouts.control_s)
    )
    if not healthy(st):
        return False, st
    end = time.monotonic() + ctx.policy.health_stabilize_s
    while time.monotonic() < end:
        await asyncio.sleep(min(common.POLL_INTERVAL or 0.01, max(0.0, end - time.monotonic())))
        st = await backend.status(decl)
        if not healthy(st):
            return False, st
    st = await backend.status(decl)
    return healthy(st), st


async def current_container_spec(ctx: AppContext, decl: ServiceDeclaration) -> ContainerSpec:
    rev = ctx.revisions.active(decl.name, decl.kind.value)
    if rev and "spec" in rev.content:
        return ContainerSpec.model_validate(rev.content["spec"])
    deployed = ctx.managed.deployed_spec(decl.name, decl.kind.value)
    if deployed and "container" in deployed:
        return ContainerSpec.model_validate(deployed["container"])
    spec: ContainerSpec | None = await ctx.backend(decl.kind).current_spec(decl)
    if spec is None:
        raise VpsMcpError(ErrorCategory.NOT_FOUND, f"container '{decl.name}' does not exist")
    return spec


async def current_os_state(
    ctx: AppContext, decl: ServiceDeclaration
) -> tuple[str | None, dict[str, str]]:
    backend = ctx.backend(decl.kind)
    version = await backend.package_version(decl.package) if decl.package else None
    files = await backend.read_files(decl) if decl.config_files else {}
    return version, files


def _record_spec(ctx: AppContext, decl: ServiceDeclaration, spec: ContainerSpec) -> None:
    deployed = ctx.managed.deployed_spec(decl.name, decl.kind.value)
    if deployed is not None:
        ctx.services.update_spec(
            decl.name, decl.kind.value, {**deployed, "container": spec.model_dump(mode="json")}
        )


# ----------------------------------------------------------------------- read-only tools


async def get_service_config(ctx: AppContext, service: str, kind: str | None) -> dict[str, Any]:
    decl = ctx.managed.resolve(service, kind)
    globs = ctx.policy.secret_keys
    rev = ctx.revisions.active(decl.name, decl.kind.value)
    out: dict[str, Any] = {"kind": decl.kind.value, "revision_id": rev.id if rev else None}
    if decl.kind is ServiceKind.CONTAINER:
        spec = await current_container_spec(ctx, decl)
        out["spec"] = masked_spec(spec, globs)
        out["version"] = spec.image
    else:
        version, files = await current_os_state(ctx, decl)
        out["files"] = [{"path": p, "content": mask_text_lines(c, globs)} for p, c in files.items()]
        out["version"] = version
    return out


async def list_revisions(ctx: AppContext, service: str, kind: str | None) -> dict[str, Any]:
    decl = ctx.managed.resolve(service, kind)
    return {"revisions": [r.summary() for r in ctx.revisions.list(decl.name, decl.kind.value)]}


# ----------------------------------------------------------------------- apply machinery


async def _apply_os(
    ctx: AppContext,
    decl: ServiceDeclaration,
    *,
    files: dict[str, str] | None,
    version: str | None,
    prev_version: str | None,
    prev_files: dict[str, str],
) -> None:
    backend = ctx.backend(decl.kind)
    if version and version != prev_version:
        await backend.update(decl, version, downgrade=True)
    if files:
        changed = {p: c for p, c in files.items() if prev_files.get(p) != c}
        await backend.write_files(decl, changed)
        ok, output = await backend.validate_in_place(decl)
        if not ok:
            await backend.write_files(decl, {p: prev_files.get(p, "") for p in changed})
            raise VpsMcpError(
                ErrorCategory.FAILED,
                f"config validation failed; previous files restored:\n{output}",
                extra={"validation": {"ok": False, "output": output}},
            )
        await backend.activate(decl)
    else:
        await backend.activate(decl, "restart")


async def _change(
    ctx: AppContext,
    decl: ServiceDeclaration,
    *,
    new_version: str | None,
    new_content: dict[str, Any],
    apply: Any,
    restore: Any,
) -> dict[str, Any]:
    """Apply -> health gate -> keep, or auto-rollback (FR-019)."""
    await apply()
    healthy, st = await health_gate(ctx, decl)
    if healthy:
        rev_id = ctx.revisions.activate_new(decl.name, decl.kind.value, new_version, new_content)
        return {
            "outcome": "success",
            "service": st.to_dict(),
            "revision_id": rev_id,
            "_after_audit": lambda aid: ctx.revisions.set_audit(rev_id, aid),
        }
    failure = (
        f"'{decl.name}' was not healthy after the change (state={st.state.value}"
        + (f", health={st.health}" if st.health else "")
        + ")"
    )
    try:
        await restore()
        ok, restored = await health_gate(ctx, decl)
    except VpsMcpError as err:
        raise VpsMcpError(
            ErrorCategory.FAILED,
            f"{failure}; rollback ALSO failed: {err.message}",
            last_known_state=st.to_dict(),
        ) from None
    rev_id = ctx.revisions.activate_new(decl.name, decl.kind.value, new_version, new_content)
    ctx.revisions.mark_rolled_back(decl.name, decl.kind.value)
    if not ok:
        raise VpsMcpError(
            ErrorCategory.FAILED,
            f"{failure}; previous revision restored but it is not healthy either "
            f"(state={restored.state.value})",
            last_known_state=restored.to_dict(),
        )
    return {
        "outcome": "rolled_back",
        "service": restored.to_dict(),
        "revision_id": rev_id,
        "reason": failure,
        "_after_audit": lambda aid: ctx.revisions.set_audit(rev_id, aid),
    }


def _params(**kw: Any) -> dict[str, Any]:
    return {k: v for k, v in kw.items() if v is not None}


# ------------------------------------------------------------------------- configure


async def configure_service(
    ctx: AppContext,
    *,
    service: str,
    kind: str | None,
    files: list[dict[str, Any]] | None,
    spec_patch: dict[str, Any] | None,
    confirm: bool,
    session_id: str | None,
) -> dict[str, Any]:
    globs = ctx.policy.secret_keys
    planned: dict[str, Any] = {}

    async def plan(decl: ServiceDeclaration) -> dict[str, Any]:
        if planned:
            return planned
        if decl.kind is ServiceKind.OS:
            if not files or spec_patch is not None:
                raise invalid("os services take 'files' (and no 'spec_patch')")
            allowed = {f.path for f in decl.config_files}
            new: dict[str, str] = {}
            for f in files:
                if not isinstance(f, dict) or not isinstance(f.get("content"), str):
                    raise invalid("files[] items need 'path' and 'content'")
                path = validated(AbsPath, f.get("path"), "files[].path")
                if path not in allowed:
                    raise not_permitted(f"{decl.name}:{path}")
                if len(f["content"].encode()) > MAX_FILE_BYTES:
                    raise invalid(f"{path}: content larger than {MAX_FILE_BYTES} bytes")
                new[path] = f["content"]
            version, current = await current_os_state(ctx, decl)
            proposed = {**current, **new}
            planned.update(
                kind="os",
                version=version,
                current=current,
                proposed=proposed,
                diff=files_diff(current, new, globs),
            )
        else:
            if not isinstance(spec_patch, dict) or files is not None:
                raise invalid("container services take 'spec_patch' (and no 'files')")
            cur = await current_container_spec(ctx, decl)
            new_spec = merge_spec(cur, spec_patch)
            if new_spec.image != cur.image:
                check_image_approved(new_spec.image, ctx.policy.approved_sources.image_prefixes)
            planned.update(
                kind="container",
                current=cur,
                proposed=new_spec,
                diff=spec_diff(cur, new_spec, globs),
            )
        return planned

    async def preview(decl: ServiceDeclaration) -> dict[str, Any]:
        p = await plan(decl)
        out: dict[str, Any] = {"diff": p["diff"] or "(no changes)"}
        if p["kind"] == "os":
            validation = await ctx.backend(decl.kind).validate_staged(decl, p["proposed"])
            if validation is not None:
                out["validation"] = validation
        else:
            out["networks_to_create"] = await ctx.backend(decl.kind).missing_networks(
                list(p["proposed"].networks)
            )
        return out

    async def body(decl: ServiceDeclaration) -> dict[str, Any]:
        p = await plan(decl)
        if not p["diff"]:
            return {
                "outcome": "success",
                "service": (await ctx.backend(decl.kind).status(decl)).to_dict(),
                "changed": False,
            }
        backend = ctx.backend(decl.kind)
        if p["kind"] == "os":
            ctx.revisions.ensure_baseline(decl.name, "os", p["version"], {"files": p["current"]})

            async def apply() -> None:
                await _apply_os(
                    ctx,
                    decl,
                    files=p["proposed"],
                    version=None,
                    prev_version=p["version"],
                    prev_files=p["current"],
                )

            async def restore() -> None:
                await backend.write_files(decl, p["current"])
                await backend.activate(decl, "restart")

            return await _change(
                ctx,
                decl,
                new_version=p["version"],
                new_content={"files": p["proposed"]},
                apply=apply,
                restore=restore,
            )

        cur: ContainerSpec = p["current"]
        new_spec: ContainerSpec = p["proposed"]
        ctx.revisions.ensure_baseline(
            decl.name, "container", cur.image, {"spec": cur.model_dump(mode="json")}
        )

        async def apply_c() -> None:
            await backend.recreate(decl, cur, new_spec)

        async def restore_c() -> None:
            await backend.recreate(decl, new_spec, cur)

        result = await _change(
            ctx,
            decl,
            new_version=new_spec.image,
            new_content={"spec": new_spec.model_dump(mode="json")},
            apply=apply_c,
            restore=restore_c,
        )
        _record_spec(ctx, decl, new_spec if result["outcome"] == "success" else cur)
        return result

    masked_patch = None
    if isinstance(spec_patch, dict):
        masked_patch = dict(spec_patch)
        if isinstance(spec_patch.get("env"), dict):
            masked_patch["env"] = mask_mapping(spec_patch["env"], globs)
    req = ActionRequest(
        action="configure",
        service=service,
        kind=kind,
        confirm=confirm,
        params=_params(
            service=service, kind=kind, files=files, spec_patch=masked_patch, confirm=confirm
        ),
        session_id=session_id,
        timeout=float(ctx.policy.timeouts.deploy_s),
        needs_confirm=lambda _d: True,
        preview=preview,
    )
    return await run_action(ctx, req, body)


# ---------------------------------------------------------------------------- update


async def update_service(
    ctx: AppContext,
    *,
    service: str,
    kind: str | None,
    version: str | None,
    image: str | None,
    confirm: bool,
    session_id: str | None,
) -> dict[str, Any]:
    planned: dict[str, Any] = {}

    async def plan(decl: ServiceDeclaration) -> dict[str, Any]:
        if planned:
            return planned
        backend = ctx.backend(decl.kind)
        if decl.kind is ServiceKind.OS:
            if not version or image:
                raise invalid("os services take 'version' (and no 'image')")
            if not decl.package:
                raise invalid(f"service '{decl.name}' has no package declared")
            v = validated(PackageVersion, version, "version")
            target = await backend.check_origin(decl.package, v)
            cur_version, files = await current_os_state(ctx, decl)
            planned.update(kind="os", from_version=cur_version, to_version=target, files=files)
        else:
            if not image or version:
                raise invalid("container services take 'image' (and no 'version')")
            img = validated(ImageRef, image, "image")
            check_image_approved(img, ctx.policy.approved_sources.image_prefixes)
            cur = await current_container_spec(ctx, decl)
            planned.update(
                kind="container",
                from_version=cur.image,
                to_version=img,
                current=cur,
                proposed=cur.model_copy(update={"image": img}),
            )
        return planned

    async def preview(decl: ServiceDeclaration) -> dict[str, Any]:
        p = await plan(decl)
        return {"from_version": p["from_version"], "to_version": p["to_version"]}

    async def body(decl: ServiceDeclaration) -> dict[str, Any]:
        p = await plan(decl)
        backend = ctx.backend(decl.kind)
        if p["kind"] == "os":
            ctx.revisions.ensure_baseline(decl.name, "os", p["from_version"], {"files": p["files"]})

            async def apply() -> None:
                await backend.update(decl, p["to_version"], downgrade=True)
                await backend.activate(decl, "restart")

            async def restore() -> None:
                if p["from_version"]:
                    await backend.update(decl, p["from_version"], downgrade=True)
                await backend.activate(decl, "restart")

            installed_version = p["to_version"]
            return await _change(
                ctx,
                decl,
                new_version=installed_version,
                new_content={"files": p["files"]},
                apply=apply,
                restore=restore,
            )

        cur, new_spec = p["current"], p["proposed"]
        ctx.revisions.ensure_baseline(
            decl.name, "container", cur.image, {"spec": cur.model_dump(mode="json")}
        )

        async def apply_c() -> None:
            await backend.recreate(decl, cur, new_spec)

        async def restore_c() -> None:
            await backend.recreate(decl, new_spec, cur)

        result = await _change(
            ctx,
            decl,
            new_version=new_spec.image,
            new_content={"spec": new_spec.model_dump(mode="json")},
            apply=apply_c,
            restore=restore_c,
        )
        _record_spec(ctx, decl, new_spec if result["outcome"] == "success" else cur)
        return result

    req = ActionRequest(
        action="update",
        service=service,
        kind=kind,
        confirm=confirm,
        params=_params(service=service, kind=kind, version=version, image=image, confirm=confirm),
        session_id=session_id,
        timeout=float(ctx.policy.timeouts.deploy_s),
        needs_confirm=lambda _d: True,
        preview=preview,
    )
    return await run_action(ctx, req, body)


# -------------------------------------------------------------------------- rollback


async def rollback_service(
    ctx: AppContext, *, service: str, kind: str | None, confirm: bool, session_id: str | None
) -> dict[str, Any]:
    def previous(decl: ServiceDeclaration) -> Any:
        prev = ctx.revisions.previous(decl.name, decl.kind.value)
        if prev is None:
            raise VpsMcpError(
                ErrorCategory.NOT_FOUND,
                f"service '{decl.name}' has no previous revision to roll back to",
            )
        return prev

    async def preview(decl: ServiceDeclaration) -> dict[str, Any]:
        prev = previous(decl)
        act = ctx.revisions.active(decl.name, decl.kind.value)
        return {"from_revision": act.summary() if act else None, "to_revision": prev.summary()}

    async def body(decl: ServiceDeclaration) -> dict[str, Any]:
        prev = previous(decl)
        act = ctx.revisions.active(decl.name, decl.kind.value)
        backend = ctx.backend(decl.kind)
        if decl.kind is ServiceKind.CONTAINER:
            cur = await current_container_spec(ctx, decl)
            target = ContainerSpec.model_validate(prev.content["spec"])
            await backend.recreate(decl, cur, target)
            _record_spec(ctx, decl, target)
        else:
            cur_version, cur_files = await current_os_state(ctx, decl)
            await _apply_os(
                ctx,
                decl,
                files=prev.content.get("files") or None,
                version=prev.version,
                prev_version=cur_version,
                prev_files=cur_files,
            )
        restored = ctx.revisions.mark_rolled_back(decl.name, decl.kind.value)
        healthy, st = await health_gate(ctx, decl)
        if not healthy:
            raise VpsMcpError(
                ErrorCategory.FAILED,
                f"rolled back to revision {restored.id} but '{decl.name}' is {st.state.value}",
                last_known_state=st.to_dict(),
            )
        return {
            "service": st.to_dict(),
            "revision_id": restored.id,
            "rolled_back_from": act.id if act else None,
        }

    req = ActionRequest(
        action="rollback",
        service=service,
        kind=kind,
        confirm=confirm,
        params={"service": service, "kind": kind, "confirm": confirm},
        session_id=session_id,
        timeout=float(ctx.policy.timeouts.deploy_s),
        needs_confirm=lambda _d: True,
        preview=preview,
    )
    return await run_action(ctx, req, body)
