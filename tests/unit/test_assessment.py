from __future__ import annotations

import pytest

from tests.conftest import call
from vps_mcp.core import assessment


@pytest.mark.asyncio
async def test_assess_infrastructure_healthy(client):
    res = await call(client, "assess_infrastructure")
    assert "error" not in res, res

    vps = res["vps"]
    assert vps["name"] == "test-vps"
    assert vps["reachable"] is True
    assert vps["os"]["distro"] == "Ubuntu 22.04.4 LTS"
    assert vps["os"]["kernel"] == "5.15.0-101-generic"
    assert vps["os"]["arch"] == "x86_64"
    assert vps["os"]["uptime_seconds"] == 123456.78

    compute = res["compute"]
    assert compute["cpu_cores"] == 2
    assert compute["load_average"] == [0.52, 0.40, 0.31]
    assert compute["memory"]["total_mb"] == 2000
    assert compute["memory"]["used_mb"] == 1000
    assert compute["memory"]["used_percent"] == 50.0

    storage = res["storage"]
    assert len(storage["volumes"]) >= 1
    root = storage["volumes"][0]
    assert root["mount"] == "/"
    assert root["total_mb"] > 0
    assert root["free_mb"] > 0

    runtimes = res["runtimes"]
    assert runtimes["systemd"]["available"] is True
    assert runtimes["container"]["runtime"] == "docker"

    services = res["services"]
    assert services["total_managed"] == 3
    assert services["running"] == 3
    assert services["failed"] == 0
    assert services["failed_services"] == []

    assert res["verdict"]["status"] == "healthy"
    assert "status is healthy" in res["verdict"]["summary"]


@pytest.mark.asyncio
async def test_assess_infrastructure_options(client):
    res = await call(client, "assess_infrastructure", include_services=False, include_databases=False)
    assert "error" not in res
    assert res["services"]["total_managed"] == 0
    assert res["databases"]["configured"] is False


@pytest.mark.asyncio
async def test_assess_infrastructure_failed_service_triggers_critical(client, vps):
    # Set one unit to failed state
    u = vps.units.get("worker.service")
    assert u is not None
    u.active = "failed"

    res = await call(client, "assess_infrastructure")
    assert "error" not in res
    assert res["services"]["failed"] == 1
    assert "worker" in res["services"]["failed_services"]
    assert any("worker" in w for w in res["warnings"])
    assert res["verdict"]["status"] == "critical"


def test_parse_helpers():
    os_rel = """NAME="Debian GNU/Linux"
VERSION_ID="12"
VERSION="12 (bookworm)"
ID=debian
PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
"""
    parsed = assessment._parse_os_release(os_rel)
    assert parsed["ID"] == "debian"
    assert parsed["VERSION_ID"] == "12"
    assert parsed["PRETTY_NAME"] == "Debian GNU/Linux 12 (bookworm)"

    uname_out = "Linux 6.1.0-21-amd64 x86_64\n"
    u = assessment._parse_uname(uname_out)
    assert u["kernel_name"] == "Linux"
    assert u["kernel_release"] == "6.1.0-21-amd64"
    assert u["arch"] == "x86_64"

    uptime_out = "98765.43 12345.67\n"
    assert assessment._parse_uptime(uptime_out) == 98765.43
    assert assessment._parse_uptime("invalid") is None
