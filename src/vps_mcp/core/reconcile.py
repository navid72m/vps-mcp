"""Re-check services whose last action ended with an unknown outcome (FR-013)."""

from __future__ import annotations

import logging

from vps_mcp.core.context import AppContext
from vps_mcp.errors import VpsMcpError

log = logging.getLogger(__name__)


class Reconciler:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._running = False

    def register(self) -> None:
        if self.ctx.connection is not None:
            self.ctx.connection.on_reconnect.append(self.run)

    async def run(self) -> int:
        """Resolve every unreconciled ``unknown`` audit record. Returns how many were updated."""
        if self._running:
            return 0
        self._running = True
        updated = 0
        try:
            for row in self.ctx.audit.unreconciled():
                name, kind = row["service_name"], row["kind"]
                try:
                    decl = self.ctx.managed.resolve(name, kind)
                    st = await self.ctx.backend(decl.kind).status(decl)
                    state = st.state.value
                except VpsMcpError as err:
                    if err.category.value == "connection_failure":
                        break
                    state = f"unresolved ({err.category.value})"
                self.ctx.audit.set_reconciled(row["id"], state)
                updated += 1
                log.info("reconciled audit #%s: %s/%s is %s", row["id"], kind, name, state)
        finally:
            self._running = False
        return updated
