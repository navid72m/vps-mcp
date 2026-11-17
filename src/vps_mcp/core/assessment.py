"""Holistic infrastructure assessment tool (reconnaissance & health audit).

Provides a single high-level structured evaluation of the VPS for AI agents:
OS distribution, kernel, compute (CPU/RAM/load), storage headroom, container
runtime health, systemd state, managed services breakdown, database inventory,
and operational security warnings.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from vps_mcp.core.context import AppContext
from vps_mcp.core.resources import host_snapshot


def _parse_os_release(text: str) -> dict[str, str]:
    """Parse key-value pairs from /etc/os-release."""
    data: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip().strip('"').strip("'")
        data[key.strip()] = val
    return data


def _parse_uptime(text: str) -> float | None:
    """Parse /proc/uptime seconds."""
    parts = text.strip().split()
    if parts:
        try:
            return float(parts[0])
        except ValueError:
            pass
    return None


def _parse_uname(text: str) -> dict[str, str]:
    """Parse uname output (e.g. Linux 5.15.0-101-generic x86_64)."""
    parts = text.strip().split()
    if len(parts) >= 3:
        return {"kernel_name": parts[0], "kernel_release": parts[1], "arch": parts[-1]}
    if len(parts) == 1:
        return {"kernel_release": parts[0]}
    return {}


async def assess_infrastructure(
    ctx: AppContext,
    *,
    include_services: bool = True,
    include_databases: bool = True,
) -> dict[str, Any]:
    """Execute consolidated infrastructure assessment bounded by query timeout."""
    t = float(ctx.policy.timeouts.query_s)

    # 1. Gather host resources & OS probes in parallel
    res_snap_task = host_snapshot(ctx)
    os_rel_task = ctx.executor.run(["cat", "/etc/os-release"], timeout=t)
    uname_task = ctx.executor.run(["uname", "-srm"], timeout=t)
    uptime_task = ctx.executor.run(["cat", "/proc/uptime"], timeout=t)
    systemd_task = (
        ctx.executor.run(["systemctl", "is-system-running"], timeout=t)
        if "os" in ctx.backends
        else None
    )

    container_runtime = ctx.config.vps.container_runtime
    container_info_task = (
        ctx.executor.run([container_runtime, "info", "--format", "{{.ContainersRunning}}/{{.Containers}}"], timeout=t)
        if "container" in ctx.backends
        else None
    )

    tasks: list[Any] = [res_snap_task, os_rel_task, uname_task, uptime_task]
    if systemd_task:
        tasks.append(systemd_task)
    if container_info_task:
        tasks.append(container_info_task)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    snap = results[0] if not isinstance(results[0], Exception) else None
    os_rel_res = results[1] if not isinstance(results[1], Exception) else None
    uname_res = results[2] if not isinstance(results[2], Exception) else None
    uptime_res = results[3] if not isinstance(results[3], Exception) else None

    # Parse OS info
    os_info: dict[str, Any] = {
        "distro": "Linux",
        "id": "linux",
        "version_id": None,
        "kernel": None,
        "arch": None,
        "uptime_seconds": None,
    }
    if os_rel_res and os_rel_res.ok:
        parsed_rel = _parse_os_release(os_rel_res.stdout)
        os_info["distro"] = parsed_rel.get("PRETTY_NAME", parsed_rel.get("NAME", "Linux"))
        os_info["id"] = parsed_rel.get("ID", "linux")
        os_info["version_id"] = parsed_rel.get("VERSION_ID")

    if uname_res and uname_res.ok:
        u = _parse_uname(uname_res.stdout)
        os_info["kernel"] = u.get("kernel_release")
        os_info["arch"] = u.get("arch")

    if uptime_res and uptime_res.ok:
        os_info["uptime_seconds"] = _parse_uptime(uptime_res.stdout)

    # 2. Compute & memory breakdown
    compute_info: dict[str, Any] = {}
    storage_info: dict[str, Any] = {"volumes": []}
    warnings: list[str] = []

    if snap is not None:
        mem_total_mb = round(snap.mem_total_bytes / (1024 * 1024))
        mem_used_mb = round(snap.mem_used_bytes / (1024 * 1024))
        mem_avail_mb = max(0, mem_total_mb - mem_used_mb)
        mem_pct = round((snap.mem_used_bytes / max(1, snap.mem_total_bytes)) * 100, 1)

        compute_info = {
            "cpu_cores": snap.cpu_count,
            "load_average": snap.load_avg,
            "memory": {
                "total_mb": mem_total_mb,
                "used_mb": mem_used_mb,
                "available_mb": mem_avail_mb,
                "used_percent": mem_pct,
            },
        }

        # Check load warning
        if snap.cpu_count and snap.load_avg and snap.load_avg[0] > (snap.cpu_count * 2.0):
            warnings.append(
                f"High CPU load: 1-min load average ({snap.load_avg[0]}) exceeds 2x core count ({snap.cpu_count})"
            )

        # Check memory warning
        if mem_pct >= 90.0:
            warnings.append(f"High memory utilization: {mem_pct}% used ({mem_avail_mb} MB available)")

        # Volume breakdown
        for v in snap.volumes:
            tot_mb = round(v.total_bytes / (1024 * 1024))
            usd_mb = round(v.used_bytes / (1024 * 1024))
            free_mb = max(0, tot_mb - usd_mb)
            v_pct = round((v.used_bytes / max(1, v.total_bytes)) * 100, 1)

            storage_info["volumes"].append({
                "mount": v.mount,
                "total_mb": tot_mb,
                "used_mb": usd_mb,
                "free_mb": free_mb,
                "used_percent": v_pct,
            })

            if v_pct >= 85.0:
                warnings.append(f"Low disk space on {v.mount}: {v_pct}% used ({free_mb} MB free)")

    # 3. Runtimes status
    runtimes_info: dict[str, Any] = {}
    idx = 4
    if systemd_task:
        sd_res = results[idx] if not isinstance(results[idx], Exception) else None
        idx += 1
        if sd_res and sd_res.ok:
            sd_state = sd_res.stdout.strip()
            runtimes_info["systemd"] = {"available": True, "state": sd_state}
            if sd_state in ("degraded", "maintenance"):
                warnings.append(f"systemd is in {sd_state} state")
        else:
            runtimes_info["systemd"] = {"available": True, "state": "active"}

    if container_info_task:
        ct_res = results[idx] if not isinstance(results[idx], Exception) else None
        if ct_res and ct_res.ok and "/" in ct_res.stdout:
            running_str, _, total_str = ct_res.stdout.strip().partition("/")
            runtimes_info["container"] = {
                "runtime": container_runtime,
                "available": True,
                "running_containers": int(running_str) if running_str.isdigit() else 0,
                "total_containers": int(total_str) if total_str.isdigit() else 0,
            }
        else:
            runtimes_info["container"] = {
                "runtime": container_runtime,
                "available": ct_res.ok if ct_res else False,
            }

    # 4. Managed services summary
    services_summary: dict[str, Any] = {"total_managed": 0, "running": 0, "failed": 0, "stopped": 0, "failed_services": []}
    if include_services:
        decls = [d for d in ctx.managed.all() if d.kind in ctx.backends]
        if decls:
            try:
                from vps_mcp.core.queries import _statuses
                statuses = await asyncio.wait_for(_statuses(ctx, decls), timeout=t)
                services_summary["total_managed"] = len(statuses)
                for s in statuses:
                    if s.state == "running":
                        services_summary["running"] += 1
                    elif s.state == "failed":
                        services_summary["failed"] += 1
                        services_summary["failed_services"].append(s.name)
                    else:
                        services_summary["stopped"] += 1
                if services_summary["failed_services"]:
                    warnings.append(f"Managed service failure: {', '.join(services_summary['failed_services'])} in failed state")
            except Exception as e:
                warnings.append(f"Could not poll managed services: {e}")

    # 5. Databases summary (if configured)
    db_summary: dict[str, Any] = {"configured": False}
    if include_databases and ctx.migration is not None:
        db_summary["configured"] = True
        targets = ctx.migration.settings.target_names()
        db_summary["allowlisted_targets"] = targets
        db_summary["allowlisted_count"] = len(targets)
        try:
            copies = ctx.migration.store.all_copies()
            active_copies = [c["name"] for c in copies if c.get("state") in ("ready", "in_use")]
            db_summary["active_rehearsal_copies"] = active_copies
            db_summary["active_rehearsal_count"] = len(active_copies)
        except Exception:
            db_summary["active_rehearsal_count"] = 0

    # 6. Overall verdict
    if any("failure" in w.lower() for w in warnings) or any(v["used_percent"] >= 95.0 for v in storage_info.get("volumes", [])):
        status = "critical"
    elif warnings:
        status = "degraded"
    else:
        status = "healthy"

    cpu_s = f"{compute_info.get('cpu_cores', '?')} cores"
    mem_s = f"{compute_info.get('memory', {}).get('used_percent', '?')}% mem used"
    svc_s = f"{services_summary['running']}/{services_summary['total_managed']} services active"
    summary_sentence = (
        f"Host '{ctx.config.vps.name}' status is {status} ({os_info['distro']}, {cpu_s}, {mem_s}, {svc_s})."
    )

    return {
        "vps": {
            "name": ctx.config.vps.name,
            "host": ctx.config.vps.host,
            "reachable": True,
            "os": os_info,
        },
        "compute": compute_info,
        "storage": storage_info,
        "runtimes": runtimes_info,
        "services": services_summary,
        "databases": db_summary,
        "warnings": warnings,
        "verdict": {
            "status": status,
            "summary": summary_sentence,
        },
    }
