"""Policy enforcement pipeline for every state-changing action (data-model.md §4).

Order: validate -> managed set / remote-access -> read_only -> protected-remove ->
confirmation (preview) -> per-service lock -> execute with timeout -> verify.
Every exit path writes exactly one audit record (FR-034).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from vps_mcp.config.policy import ServiceDeclaration
from vps_mcp.core.context import AppContext
from vps_mcp.errors import ConnectionLost, ErrorCategory, VpsMcpError

log = logging.getLogger(__name__)

Body = Callable[[ServiceDeclaration], Awaitable[dict[str, Any]]]
Preview = Callable[[ServiceDeclaration], Awaitable[dict[str, Any]]]
Resolver = Callable[[], Awaitable[ServiceDeclaration]]

# Policy refusals raised from inside a body (e.g. deploy pre-checks) are audited as "refused".
REFUSAL_CATEGORIES = frozenset(
    {
        ErrorCategory.NOT_PERMITTED,
        ErrorCategory.INVALID_INPUT,
        ErrorCategory.CONFLICT,
        ErrorCategory.UNAPPROVED_SOURCE,
        ErrorCategory.INSUFFICIENT_DISK,
        ErrorCategory.PROTECTED,
        ErrorCategory.READ_ONLY,
        ErrorCategory.CONFIRMATION_REQUIRED,
        ErrorCategory.NOT_FOUND,
    }
)


@dataclass
class ActionRequest:
    action: str  # audit action name
    service: str | None
    kind: str | None
    confirm: bool
    params: dict[str, Any]
    session_id: str | None
    timeout: float
    needs_confirm: Callable[[ServiceDeclaration], bool] = field(default=lambda _d: False)
    preview: Preview | None = None  # unconfirmed + needs_confirm -> preview instead of refusal
    is_remove: bool = False
    resolver: Resolver | None = None  # default: ManagedSet.resolve(service, kind)


def _state_of(result: dict[str, Any]) -> str | None:
    svc = result.get("service")
    if isinstance(svc, dict):
        state = svc.get("state")
        return str(state) if state is not None else None
    return None


async def _last_known(ctx: AppContext, decl: ServiceDeclaration) -> dict[str, Any] | None:
    try:
        st = await asyncio.wait_for(ctx.backend(decl.kind).status(decl), 5)
        return dict(st.to_dict())
    except Exception:  # noqa: BLE001 - best effort only
        return None


async def run_action(ctx: AppContext, req: ActionRequest, body: Body) -> dict[str, Any]:
    started = time.monotonic()
    decl: ServiceDeclaration | None = None

    def audit(
        outcome: str,
        *,
        err: VpsMcpError | None = None,
        state: str | None = None,
        data_deleted: bool = False,
    ) -> int:
        return ctx.audit.record(
            session_id=req.session_id,
            action=req.action,
            service_name=decl.name if decl else req.service,
            kind=decl.kind.value if decl else req.kind,
            params=req.params,
            confirmed=req.confirm,
            outcome=outcome,
            error_category=err.category.value if err else None,
            error_message=err.message if err else None,
            resulting_state=state,
            data_deleted=data_deleted,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def refuse(err: VpsMcpError) -> VpsMcpError:
        err.extra.update(outcome="refused", audit_id=audit("refused", err=err))
        return err

    # 1-2. validate and resolve against the managed set (incl. remote-access guard)
    try:
        if req.resolver is not None:
            decl = await req.resolver()
        else:
            decl = ctx.managed.resolve(req.service or "", req.kind)
    except VpsMcpError as err:
        if err.category is ErrorCategory.CONNECTION_FAILURE:
            err.extra.update(outcome="failed", audit_id=audit("failed", err=err))
            raise err from None
        raise refuse(err) from None
    assert decl is not None

    # 3. read-only mode
    if ctx.policy.read_only:
        raise refuse(
            VpsMcpError(
                ErrorCategory.READ_ONLY,
                "the server is in read-only mode; state changes are disabled",
            )
        )

    # 4. protected services can never be removed
    if req.is_remove and decl.protected:
        raise refuse(
            VpsMcpError(
                ErrorCategory.PROTECTED,
                f"service '{decl.name}' is protected and cannot be removed; clear its protected "
                "flag in the policy file first",
            )
        )

    # 5. confirmation / preview
    if req.needs_confirm(decl) and not req.confirm:
        if req.preview is not None:
            try:
                preview = await asyncio.wait_for(req.preview(decl), ctx.policy.timeouts.query_s)
            except TimeoutError:
                slow = VpsMcpError(ErrorCategory.TIMED_OUT, "building the preview timed out")
                slow.extra.update(outcome="failed", audit_id=audit("failed", err=slow))
                raise slow from None
            except VpsMcpError as err:
                if err.category in (
                    ErrorCategory.CONNECTION_FAILURE,
                    ErrorCategory.TIMED_OUT,
                    ErrorCategory.FAILED,
                    ErrorCategory.PERMISSION_DENIED,
                ):
                    err.extra.update(outcome="failed", audit_id=audit("failed", err=err))
                    raise err from None
                raise refuse(err) from None
            audit_id = audit("preview")
            return {"outcome": "preview", "audit_id": audit_id, "applied": False, **preview}
        raise refuse(
            VpsMcpError(
                ErrorCategory.CONFIRMATION_REQUIRED,
                f"'{req.action}' on '{decl.name}' requires confirm=true",
            )
        )

    # 6-7. lock, execute with timeout, map failures
    async with ctx.locks.acquire(decl.kind.value, decl.name):
        try:
            result = await asyncio.wait_for(body(decl), req.timeout)
        except TimeoutError:
            timed_out = VpsMcpError(
                ErrorCategory.TIMED_OUT,
                f"'{req.action}' on '{decl.name}' did not finish within {req.timeout:.0f}s",
                last_known_state=await _last_known(ctx, decl),
            )
            state = (timed_out.last_known_state or {}).get("state")
            timed_out.extra.update(
                outcome="timed_out", audit_id=audit("timed_out", err=timed_out, state=state)
            )
            raise timed_out from None
        except ConnectionLost as err:
            err.message = (
                f"connection lost during '{req.action}' on '{decl.name}'; outcome "
                "unknown. The state will be re-checked when the connection returns."
            )
            err.extra.update(outcome="unknown", audit_id=audit("unknown", err=err))
            raise err from None
        except VpsMcpError as err:
            if err.category is ErrorCategory.TIMED_OUT and err.last_known_state is None:
                err.last_known_state = await _last_known(ctx, decl)
            if err.category is ErrorCategory.TIMED_OUT:
                outcome = "timed_out"
            elif err.category in REFUSAL_CATEGORIES:
                outcome = "refused"
            else:
                outcome = "failed"
            state = (err.last_known_state or {}).get("state")
            err.extra.setdefault("outcome", outcome)
            err.extra["audit_id"] = audit(str(err.extra["outcome"]), err=err, state=state)
            raise err from None
        except Exception as exc:  # noqa: BLE001 - never leave an attempt unaudited
            log.exception("unexpected error in %s", req.action)
            internal = VpsMcpError(
                ErrorCategory.FAILED,
                f"internal error during '{req.action}': {exc.__class__.__name__}",
            )
            internal.extra.update(outcome="failed", audit_id=audit("failed", err=internal))
            raise internal from exc

    outcome = str(result.pop("outcome", "success"))
    audit_id = audit(
        outcome, state=_state_of(result), data_deleted=bool(result.get("data_deleted", False))
    )
    hook = result.pop("_after_audit", None)
    if callable(hook):
        hook(audit_id)
    return {"outcome": outcome, "audit_id": audit_id, **result}
