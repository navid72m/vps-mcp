"""Host-level resource collection (US7, FR-023)."""

from __future__ import annotations

import asyncio

from vps_mcp.backends.common import now_utc
from vps_mcp.core.context import AppContext
from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.models.views import ResourceSnapshot, Volume

DF_ARGV = [
    "df",
    "-P",
    "-B1",
    "-x",
    "tmpfs",
    "-x",
    "devtmpfs",
    "-x",
    "overlay",
    "-x",
    "squashfs",
    "-x",
    "efivarfs",
]


def parse_loadavg(text: str) -> list[float]:
    parts = text.split()
    return [float(p) for p in parts[:3]]


def parse_meminfo(text: str) -> tuple[int, int]:
    vals: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            mult = 1024 if len(fields) > 1 and fields[1].lower() == "kb" else 1
            vals[key.strip()] = int(fields[0]) * mult
    total = vals.get("MemTotal", 0)
    avail = vals.get("MemAvailable", vals.get("MemFree", 0))
    return total, max(0, total - avail)


def parse_df(text: str) -> list[Volume]:
    vols = []
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 6 or not f[1].isdigit():
            continue
        vols.append(Volume(mount=" ".join(f[5:]), total_bytes=int(f[1]), used_bytes=int(f[2])))
    return vols


async def host_snapshot(ctx: AppContext) -> ResourceSnapshot:
    t = float(ctx.policy.timeouts.query_s)
    load, mem, cpus, df = await asyncio.gather(
        ctx.executor.run(["cat", "/proc/loadavg"], timeout=t),
        ctx.executor.run(["cat", "/proc/meminfo"], timeout=t),
        ctx.executor.run(["nproc"], timeout=t),
        ctx.executor.run(DF_ARGV, timeout=t),
    )
    if not load.ok or not mem.ok:
        raise VpsMcpError(ErrorCategory.FAILED, "could not read /proc on the VPS")
    total, used = parse_meminfo(mem.stdout)
    return ResourceSnapshot(
        taken_at=now_utc(),
        load_avg=parse_loadavg(load.stdout),
        cpu_count=int(cpus.stdout.strip()) if cpus.ok and cpus.stdout.strip().isdigit() else None,
        mem_total_bytes=total,
        mem_used_bytes=used,
        volumes=parse_df(df.stdout),
    )


async def free_disk_mb(ctx: AppContext, paths: list[str]) -> dict[str, int]:
    r = await ctx.executor.run(
        ["df", "-B1M", "--output=avail,target", *paths], timeout=float(ctx.policy.timeouts.query_s)
    )
    out: dict[str, int] = {}
    for line in r.stdout.splitlines()[1:]:
        f = line.split(None, 1)
        if len(f) == 2 and f[0].isdigit():
            out[f[1].strip()] = int(f[0])
    if not out:
        raise VpsMcpError(ErrorCategory.FAILED, "could not determine free disk space")
    return out
