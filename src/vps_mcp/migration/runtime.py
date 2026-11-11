"""Migration runtime wiring plus the action guard for database-level state changes.

The guard mirrors core/guard.py (read-only mode, confirmation/preview, one audit record per
attempt) for actions whose target is a database rather than a managed service.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vps_mcp.errors import ConnectionLost, ErrorCategory, VpsMcpError
from vps_mcp.migration.config import MigrationSettings
from vps_mcp.migration.db import MigrationDb
from vps_mcp.migration.store import MigrationStore
from vps_mcp.remote.redact import mask_inline, mask_text_lines, scrub

if TYPE_CHECKING:
    from vps_mcp.core.context import AppContext

log = logging.getLogger(__name__)

REFUSALS = frozenset(
    {
        ErrorCategory.NOT_PERMITTED,
        ErrorCategory.INVALID_INPUT,
        ErrorCategory.CONFLICT,
        ErrorCategory.INSUFFICIENT_DISK,
        ErrorCategory.READ_ONLY,
        ErrorCategory.NOT_FOUND,
        ErrorCategory.CONFIRMATION_REQUIRED,
    }
)


@dataclass
class MigrationRuntime:
    app: AppContext
    settings: MigrationSettings
    db: MigrationDb
    store: MigrationStore
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def ex(self) -> Any:
        return self.app.executor

    def mask(self, text: str) -> str:
        """Mask secret-looking values and configured secret strings in command output."""
        globs = self.app.policy.secret_keys
        masked = mask_inline(mask_text_lines(text, globs), globs)
        return scrub(masked, self.app.secret_strings)


@dataclass
class DbAction:
    action: str  # audit action
    target: str | None
    confirm: bool
    params: dict[str, Any]
    session_id: str | None
    timeout: float
    preview: Callable[[], Awaitable[dict[str, Any]]] | None = None


async def run_db_action(
    rt: MigrationRuntime, req: DbAction, body: Callable[[], Awaitable[dict[str, Any]]]
) -> dict[str, Any]:
    app = rt.app
    started = time.monotonic()

    def audit(outcome: str, err: VpsMcpError | None = None) -> int:
        return app.audit.record(
            session_id=req.session_id,
            action=req.action,
            service_name=req.target,
            kind="database",
            params=req.params,
            confirmed=req.confirm,
            outcome=outcome,
            error_category=err.category.value if err else None,
            error_message=err.message if err else None,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def fail(err: VpsMcpError, outcome: str) -> VpsMcpError:
        err.extra.setdefault("outcome", outcome)
        err.extra["audit_id"] = audit(str(err.extra["outcome"]), err)
        return err

    if app.policy.read_only:
        raise fail(
            VpsMcpError(
                ErrorCategory.READ_ONLY,
                "the server is in read-only mode; state changes are disabled",
            ),
            "refused",
        )
    if not req.confirm:
        if req.preview is None:
            raise fail(
                VpsMcpError(
                    ErrorCategory.CONFIRMATION_REQUIRED, f"'{req.action}' requires confirm=true"
                ),
                "refused",
            )
        try:
            preview = await asyncio.wait_for(req.preview(), req.timeout)
        except VpsMcpError as err:
            raise fail(err, "refused" if err.category in REFUSALS else "failed") from None
        return {"outcome": "preview", "audit_id": audit("preview"), "applied": False, **preview}

    async with rt.lock:
        try:
            result = await asyncio.wait_for(body(), req.timeout)
        except TimeoutError:
            raise fail(
                VpsMcpError(
                    ErrorCategory.TIMED_OUT,
                    f"'{req.action}' did not finish within {req.timeout:.0f}s",
                ),
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
    return {"outcome": outcome, "audit_id": audit(outcome), **result}
