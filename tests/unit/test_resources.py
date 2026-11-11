"""US7: resource parsing (T066)."""

from tests.conftest import call
from vps_mcp.backends.common import parse_size
from vps_mcp.core.resources import parse_df, parse_loadavg, parse_meminfo


def test_parsers():
    assert parse_loadavg("0.52 0.40 0.31 1/123 4567") == [0.52, 0.40, 0.31]
    total, used = parse_meminfo("MemTotal: 2000 kB\nMemFree: 100 kB\nMemAvailable: 500 kB\n")
    assert (total, used) == (2000 * 1024, 1500 * 1024)
    vols = parse_df(
        "Filesystem 1-blocks Used Available Capacity Mounted on\n"
        "/dev/vda1 100 40 60 40% /\n/dev/vdb1 10 1 9 10% /mnt/my data\n"
    )
    assert [(v.mount, v.total_bytes, v.used_bytes) for v in vols] == [
        ("/", 100, 40),
        ("/mnt/my data", 10, 1),
    ]
    assert parse_size("64MiB") == 64 * 2**20
    assert parse_size("1.5GB") == 1_500_000_000


async def test_resource_tool(client):
    data = await call(client, "get_resource_usage")
    assert data["load_avg"] == [0.52, 0.40, 0.31] and data["cpu_count"] == 2
    assert data["mem_total_bytes"] == 2048000 * 1024
    assert data["volumes"][0]["mount"] == "/"
    by_name = {s["name"]: s for s in data["services"]}
    assert by_name["api"]["cpu_pct"] == 1.5 and by_name["api"]["mem_bytes"] == 64 * 2**20
    assert by_name["nginx"]["mem_bytes"] == 10485760
    data = await call(client, "get_resource_usage", include_services=False)
    assert data["services"] == []
