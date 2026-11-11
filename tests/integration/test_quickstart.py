"""End-to-end quickstart scenarios against a real VM (T079). Opt-in: -m integration.

Provision with tests/integration/provision.sh, then:
    uv run pytest -m integration --vps-config tests/integration/vps-mcp.test.toml
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.conftest import SessionPerCall, call
from vps_mcp.config.loader import load_config
from vps_mcp.server import build_context, create_server

pytestmark = pytest.mark.integration


@pytest.fixture
def real(request: pytest.FixtureRequest, tmp_path: Path) -> SessionPerCall:
    path = request.config.getoption("--vps-config")
    if not path:
        pytest.skip("--vps-config not given")
    app = build_context(load_config(path), db_path=tmp_path / "it.db")
    return SessionPerCall(create_server(app))


@pytest.fixture(autouse=True)
def fast_polling() -> None:  # override: real hosts need real polling intervals
    return None


async def test_s01_list_services_fast(real):
    t = time.monotonic()
    data = await call(real, "list_services")
    assert time.monotonic() - t < 10  # SC-001
    assert {("nginx", "os"), ("api", "container")} <= {
        (s["name"], s["kind"]) for s in data["services"]
    }


async def test_s02_undeclared(real):
    data = await call(real, "get_service_status", service="postgres")
    assert data["error"]["category"] == "not_permitted"


async def test_s03_restart_api_fast(real):
    t = time.monotonic()
    data = await call(real, "restart_service", service="api")
    assert data["outcome"] == "success" and time.monotonic() - t < 30  # SC-002


async def test_s04_s05_guards(real):
    assert (await call(real, "stop_service", service="nginx"))["error"][
        "category"
    ] == "confirmation_required"
    assert (await call(real, "stop_service", service="sshd"))["error"][
        "category"
    ] == "not_permitted"


async def test_s07_logs(real):
    data = await call(real, "get_service_logs", service="nginx", lines=50)
    assert data["returned"] <= 50


async def test_s08_s09_s12_s13_deploy_configure_rollback_remove(real):
    data = await call(
        real,
        "deploy_service",
        name="cache",
        kind="container",
        container={"image": "redis:7", "volumes": ["cache-data:/data"]},
        confirm=True,
    )
    assert data["outcome"] == "success", data
    bad = await call(
        real,
        "deploy_service",
        name="evil",
        kind="container",
        container={"image": "quay.io/evil/x"},
        confirm=True,
    )
    assert bad["error"]["category"] == "unapproved_source"
    data = await call(
        real, "configure_service", service="cache", spec_patch={"env": {"MODE": "x"}}, confirm=True
    )
    assert data["outcome"] == "success"
    data = await call(real, "rollback_service", service="cache", confirm=True)
    assert data["outcome"] == "success"
    data = await call(real, "remove_service", service="cache", confirm=True)
    assert data["data_deleted"] is False and "cache-data" in data["volumes_kept"]


async def test_s10_s11_nginx_config(real):
    cfg = await call(real, "get_service_config", service="nginx")
    original = cfg["files"][0]["content"]
    data = await call(
        real,
        "configure_service",
        service="nginx",
        files=[{"path": "/etc/nginx/nginx.conf", "content": original + "\n# vps-mcp\n"}],
        confirm=True,
    )
    assert data["outcome"] == "success"
    data = await call(
        real,
        "configure_service",
        service="nginx",
        files=[{"path": "/etc/nginx/nginx.conf", "content": "INVALID;\n"}],
        confirm=True,
    )
    assert data["ok"] is False
    status = await call(real, "get_service_status", service="nginx")
    assert status["service"]["state"] == "running"  # SC-005
    await call(real, "rollback_service", service="nginx", confirm=True)


async def test_s14_protected_remove(real):
    data = await call(real, "remove_service", service="nginx", confirm=True)
    assert data["error"]["category"] == "protected"


async def test_s15_resources(real):
    data = await call(real, "get_resource_usage")
    assert data["mem_total_bytes"] > 0 and data["volumes"]


async def test_s16a_odoo_with_postgres(real):
    db = {
        "image": "postgres:16",
        "networks": ["odoo-net"],
        "env": {
            "POSTGRES_USER": "odoo",
            "POSTGRES_PASSWORD": "odoo-it-pw",
            "POSTGRES_DB": "postgres",
        },
    }
    odoo = {
        "image": "odoo:18",
        "networks": ["odoo-net"],
        "ports": ["8069:8069"],
        "env": {"HOST": "odoo-db", "USER": "odoo", "PASSWORD": "odoo-it-pw"},
    }
    assert (
        await call(
            real, "deploy_service", name="odoo-db", kind="container", container=db, confirm=True
        )
    )["outcome"] == "success"
    assert (
        await call(
            real, "deploy_service", name="odoo", kind="container", container=odoo, confirm=True
        )
    )["outcome"] == "success"
    await call(real, "remove_service", service="odoo", confirm=True)
    data = await call(real, "remove_service", service="odoo-db", delete_data=True, confirm=True)
    assert data["networks_removed"] == ["odoo-net"]


async def test_s16_audit(real):
    data = await call(real, "get_audit_log", limit=100)
    assert data["records"]
