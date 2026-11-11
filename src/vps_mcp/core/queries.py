"""Read-only use cases: status, logs, resources (US1, US3, US7)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from vps_mcp.config.policy import ServiceDeclaration
from vps_mcp.core.context import AppContext
from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.models.inputs import (
    Keyword,
    LineCount,
    ServiceKind,
    TimeFilter,
    resolve_time,
    validated,
)
from vps_mcp.models.views import LogResult, ServiceStatus


async def bounded[T](ctx: AppContext, aw: Awaitable[T], what: str) -> T:
    """Bound a query by ``timeouts.query_s`` (SC-008); never fall back to cached state."""
    try:
        return await asyncio.wait_for(aw, ctx.policy.timeouts.query_s)
    except TimeoutError:
        raise VpsMcpError(
            ErrorCategory.TIMED_OUT,
            f"{what} did not complete within {ctx.policy.timeouts.query_s}s",
        ) from None


def _decorate(ctx: AppContext, statuses: list[ServiceStatus]) -> list[dict[str, Any]]:
    out = []
    for st in statuses:
        st.pending_reconciliation = ctx.audit.pending_reconciliation(st.name, st.kind.value)
        out.append(st.to_dict())
    return out


async def _statuses(ctx: AppContext, decls: list[ServiceDeclaration]) -> list[ServiceStatus]:
    by_kind: dict[ServiceKind, list[ServiceDeclaration]] = {}
    for d in decls:
        by_kind.setdefault(d.kind, []).append(d)
    results: list[ServiceStatus] = []
    for kind, group in by_kind.items():
        results += await ctx.backend(kind).list_status(group)
    return results


async def list_services(ctx: AppContext, kind: str | None = None) -> dict[str, Any]:
    k = validated(ServiceKind, kind, "kind") if kind else None
    decls = [d for d in ctx.managed.all() if k is None or d.kind is k]
    decls = [d for d in decls if d.kind in ctx.backends]
    statuses = await bounded(ctx, _statuses(ctx, decls), "listing services")
    statuses.sort(key=lambda s: (s.kind.value, s.name))
    return {
        "services": _decorate(ctx, statuses),
        "vps": {"name": ctx.config.vps.name, "reachable": True},
    }


async def get_service_status(
    ctx: AppContext, service: str, kind: str | None = None
) -> dict[str, Any]:
    decl = ctx.managed.resolve(service, kind)
    st = await bounded(ctx, ctx.backend(decl.kind).status(decl), f"status of '{decl.name}'")
    return {"service": _decorate(ctx, [st])[0]}


def filter_entries(entries: list[Any], keyword: str | None, lines: int) -> tuple[list[Any], bool]:
    """Literal, case-insensitive keyword filter, then keep the newest ``lines`` entries."""
    if keyword:
        needle = keyword.casefold()
        entries = [e for e in entries if needle in e.message.casefold()]
    truncated = len(entries) > lines
    return entries[-lines:], truncated


async def get_service_logs(
    ctx: AppContext,
    service: str,
    kind: str | None = None,
    lines: int | None = None,
    since: str | None = None,
    until: str | None = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    decl = ctx.managed.resolve(service, kind)
    pol = ctx.policy
    n = validated(LineCount, lines if lines is not None else pol.log_default_lines, "lines")
    n = min(n, pol.log_max_lines)
    since_dt = resolve_time(validated(TimeFilter, since, "since")) if since else None
    until_dt = resolve_time(validated(TimeFilter, until, "until")) if until else None
    kw = validated(Keyword, keyword, "keyword") if keyword else None
    fetch = (pol.log_max_lines if kw else n) + 1  # one extra line reveals truncation
    entries = await bounded(
        ctx, ctx.backend(decl.kind).logs(decl, fetch, since_dt, until_dt), f"logs of '{decl.name}'"
    )
    hit_cap = len(entries) >= fetch
    kept, truncated = filter_entries(entries, kw, n)
    if kw and hit_cap:
        truncated = True  # older matching lines may exist beyond the scan window
    result = LogResult(entries=kept, returned=len(kept), truncated=truncated)
    return dict(result.model_dump(mode="json"))


async def get_resource_usage(ctx: AppContext, include_services: bool = True) -> dict[str, Any]:
    from vps_mcp.core.resources import host_snapshot

    async def collect() -> dict[str, Any]:
        snap = await host_snapshot(ctx)
        if include_services:
            for kind, backend in ctx.backends.items():
                decls = [d for d in ctx.managed.all() if d.kind is kind]
                snap.services += await backend.resource_usage(decls)
        return dict(snap.model_dump(mode="json"))

    return await bounded(ctx, collect(), "resource usage")
