"""GitLabRuntime: everything the GitLab tools need, plus the guarded-write helper (G7)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from vps_mcp.errors import ConnectionLost, ErrorCategory, VpsMcpError
from vps_mcp.gitlab import access
from vps_mcp.gitlab.client import GitLabClient
from vps_mcp.gitlab.config import GitLabSettings, ProjectDecl
from vps_mcp.gitlab.credentials import KeychainToken
from vps_mcp.gitlab.render import Masker

log = logging.getLogger(__name__)
REFUSALS = (
    ErrorCategory.READ_ONLY,
    ErrorCategory.NOT_PERMITTED,
    ErrorCategory.INVALID_INPUT,
    ErrorCategory.CONFLICT,
    ErrorCategory.CONFIRMATION_REQUIRED,
    ErrorCategory.INSUFFICIENT_DISK,
    ErrorCategory.NOT_FOUND,
)


@dataclass
class GitLabRuntime:
    app: Any  # AppContext
    settings: GitLabSettings
    token: KeychainToken
    client: GitLabClient
    mask: Masker
    target_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _default_branch: dict[str, str] = field(default_factory=dict)

    def project(self, alias: object, need: str = "read") -> ProjectDecl:
        decl = access.resolve(self.settings, alias, need)
        self.token.get()  # token_missing before any request
        return decl

    async def default_branch(self, decl: ProjectDecl) -> str:
        if decl.alias not in self._default_branch:
            info = await self.client.get_json(f"/projects/{access.pid(decl)}")
            self._default_branch[decl.alias] = str(info.get("default_branch") or "main")
        return self._default_branch[decl.alias]

    def lock(self, target: str) -> asyncio.Lock:
        return self.target_locks.setdefault(target, asyncio.Lock())


@dataclass
class Write:
    action: str  # audit action
    target: str | None  # audit service_name (project alias or deploy target)
    kind: str  # audit kind
    params: dict[str, Any]
    confirm: bool
    session_id: str | None
    timeout: float = 120.0


def refuse(rt: GitLabRuntime, req: Write, err: VpsMcpError) -> VpsMcpError:
    """Audit an attempt refused before it reached :func:`guarded` (bad alias, permission...)."""
    outcome = "refused" if err.category in REFUSALS else "failed"
    err.extra.setdefault("outcome", outcome)
    err.extra["audit_id"] = rt.app.audit.record(
        session_id=req.session_id,
        action=req.action,
        service_name=(req.target or "")[:128] or None,
        kind=req.kind,
        params=req.params,
        confirmed=req.confirm,
        outcome=outcome,
        error_category=err.category.value,
        error_message=err.message,
    )
    return err


async def guarded(
    rt: GitLabRuntime,
    req: Write,
    preview: Callable[[], Awaitable[dict[str, Any]]],
    body: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Read-only refusal, preview-before-confirm, and an audit record for every attempt."""
    app = rt.app
    started = time.monotonic()

    def audit(outcome: str, err: VpsMcpError | None = None) -> int:
        return int(
            app.audit.record(
                session_id=req.session_id,
                action=req.action,
                service_name=req.target,
                kind=req.kind,
                params=req.params,
                confirmed=req.confirm,
                outcome=outcome,
                error_category=err.category.value if err else None,
                error_message=err.message if err else None,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )

    def fail(err: VpsMcpError, outcome: str) -> VpsMcpError:
        err.extra.setdefault("outcome", outcome)
        err.extra["audit_id"] = audit(str(err.extra["outcome"]), err)
        return err

    if app.policy.read_only:
        raise fail(
            VpsMcpError(
                ErrorCategory.READ_ONLY,
                "the server is in read-only mode; GitLab writes and deploys are disabled",
            ),
            "refused",
        )
    if not req.confirm:
        try:
            shown = await asyncio.wait_for(preview(), req.timeout)
        except VpsMcpError as err:
            raise fail(err, "refused" if err.category in REFUSALS else "failed") from None
        return {"preview": True, "applied": False, **shown}
    try:
        result = await asyncio.wait_for(body(), req.timeout)
    except TimeoutError:
        raise fail(
            VpsMcpError(ErrorCategory.TIMED_OUT, f"'{req.action}' did not finish in time"),
            "timed_out",
        ) from None
    except ConnectionLost as err:
        raise fail(err, "unknown") from None
    except VpsMcpError as err:
        raise fail(err, "refused" if err.category in REFUSALS else "failed") from None
    except Exception as exc:  # noqa: BLE001 - never leave an attempt unaudited
        log.exception("unexpected error in %s", req.action)
        raise fail(
            VpsMcpError(
                ErrorCategory.FAILED,
                f"internal error during '{req.action}': {exc.__class__.__name__}",
            ),
            "failed",
        ) from exc
    outcome = str(result.pop("outcome", "success"))
    return {"applied": True, **result, "audit_id": audit(outcome)}


def build_runtime(
    app: Any,
    settings: GitLabSettings,
    *,
    transport: Any = None,
    keychain_reader: Any = None,
    platform: str | None = None,
) -> GitLabRuntime:
    token = KeychainToken(
        settings.keychain_service,
        settings.keychain_account,
        reader=keychain_reader,
        platform=platform or ("darwin" if keychain_reader is not None else None),
    )
    client = GitLabClient(settings, token, transport=transport)
    mask = Masker(app.config.policy.secret_keys, app.config.secret_strings)
    return GitLabRuntime(app=app, settings=settings, token=token, client=client, mask=mask)
