"""US4: deploy with approved sources, pre-checks, cleanup and networks (T046)."""

from tests.conftest import call
from vps_mcp.core.sources import check_image_approved, normalize_image

ODOO_DB = {
    "image": "postgres:16",
    "networks": ["odoo-net"],
    "env": {
        "POSTGRES_USER": "odoo",
        "POSTGRES_PASSWORD": "pg-secret-pw",
        "POSTGRES_DB": "postgres",
    },
    "volumes": ["odoo-db-data:/var/lib/postgresql/data"],
}
ODOO = {
    "image": "odoo:18",
    "networks": ["odoo-net"],
    "ports": ["8069:8069"],
    "env": {"HOST": "odoo-db", "USER": "odoo", "PASSWORD": "pg-secret-pw"},
    "volumes": ["odoo-data:/var/lib/odoo"],
}


def test_normalize_image():
    assert normalize_image("redis:7") == "docker.io/library/redis:7"
    assert normalize_image("user/x") == "docker.io/user/x"
    assert normalize_image("ghcr.io/myorg/api:1") == "ghcr.io/myorg/api:1"
    assert normalize_image("index.docker.io/library/redis") == "docker.io/library/redis"
    check_image_approved("ghcr.io/myorg/api:1", ["ghcr.io/myorg/"])


async def test_preview_then_confirm(client, vps, app):
    data = await call(
        client,
        "deploy_service",
        name="cache",
        kind="container",
        container={"image": "redis:7"},
        confirm=False,
    )
    assert data["outcome"] == "preview" and data["applied"] is False
    assert data["image"] == "docker.io/library/redis:7"
    assert vps.mutating_calls() == []
    data = await call(
        client,
        "deploy_service",
        name="cache",
        kind="container",
        container={"image": "redis:7"},
        confirm=True,
    )
    assert data["outcome"] == "success" and data["service"]["state"] == "running"
    listed = await call(client, "list_services")
    assert ("cache", "container") in {(s["name"], s["kind"]) for s in listed["services"]}
    run = next(c for c in vps.calls if c.argv[:2] == ["docker", "run"])
    assert run.sudo and "io.vps-mcp.managed=true" in run.argv and run.argv[-1] == "redis:7"


async def test_unapproved_image_refused_nothing_pulled(client, vps):
    data = await call(
        client,
        "deploy_service",
        name="x",
        kind="container",
        container={"image": "quay.io/evil/x"},
        confirm=True,
    )
    assert data["error"]["category"] == "unapproved_source" and data["outcome"] == "refused"
    assert vps.mutating_calls() == [] and "quay.io/evil/x" not in vps.images


async def test_unapproved_apt_origin(client, vps):
    data = await call(
        client, "deploy_service", name="evil", kind="os", os={"package": "evilpkg"}, confirm=False
    )
    assert data["error"]["category"] == "unapproved_source"
    assert "Evil" in data["error"]["message"]
    assert vps.mutating_calls() == []


async def test_os_deploy(client, vps):
    data = await call(
        client,
        "deploy_service",
        name="redis-server",
        kind="os",
        os={"package": "redis-server"},
        confirm=True,
    )
    assert data["outcome"] == "success", data
    assert vps.installed["redis-server"] == "7.0.15-1"
    install = next(c for c in vps.calls if c.argv[:2] == ["apt-get", "install"])
    assert install.env == {"DEBIAN_FRONTEND": "noninteractive"} and install.sudo
    assert ["systemctl", "enable", "redis-server.service"] in [c.argv for c in vps.calls]


async def test_conflict(client, vps):
    data = await call(
        client,
        "deploy_service",
        name="api",
        kind="container",
        container={"image": "redis:7"},
        confirm=True,
    )
    assert data["error"]["category"] == "conflict"
    vps.add_container("stray", "redis:7")
    data = await call(
        client,
        "deploy_service",
        name="stray",
        kind="container",
        container={"image": "redis:7"},
        confirm=False,
    )
    assert data["error"]["category"] == "conflict"


async def test_insufficient_disk(client, vps):
    vps.free_mb["/var/lib/docker"] = 100
    data = await call(
        client,
        "deploy_service",
        name="cache",
        kind="container",
        container={"image": "redis:7"},
        confirm=True,
    )
    assert data["error"]["category"] == "insufficient_disk"
    assert vps.mutating_calls() == []


async def test_failed_deploy_is_cleaned_up(client, vps):
    vps.crash_images.add("redis:7")
    data = await call(
        client,
        "deploy_service",
        name="cache",
        kind="container",
        container={"image": "redis:7", "networks": ["cache-net"]},
        confirm=True,
    )
    assert data["outcome"] == "failed" and data["cleaned_up"] is True
    assert "cache" not in vps.containers
    assert "redis:7" not in vps.images  # pulled by this attempt -> removed
    assert "cache-net" not in vps.networks  # created by this attempt -> removed
    listed = await call(client, "list_services")
    assert "cache" not in {s["name"] for s in listed["services"]}


async def test_preinstalled_package_never_removed_on_failure(client, vps):
    vps.installed["redis-server"] = "7.0.15-1"
    vps.repo["redis-server"]["7.0.15-1"] = [
        "http://archive.ubuntu.com/ubuntu noble/main amd64 Packages"
    ]
    vps.pkg_units["redis-server"] = "redis-server.service"
    vps.add_unit("redis-server", active="inactive", fail_on_start=True)
    vps.units.pop("redis-server.service")  # unit missing -> conflict check passes
    vps.pkg_units["redis-server"] = "missing.service"
    data = await call(
        client,
        "deploy_service",
        name="redis-server",
        kind="os",
        os={"package": "redis-server"},
        confirm=True,
    )
    assert data["outcome"] == "failed"
    assert not any(c.argv[:2] == ["apt-get", "remove"] for c in vps.calls)


async def test_odoo_with_postgres_on_private_network(client, vps):
    data = await call(
        client, "deploy_service", name="odoo-db", kind="container", container=ODOO_DB, confirm=False
    )
    assert data["networks_to_create"] == ["odoo-net"]
    assert data["would_install"]["env"]["POSTGRES_PASSWORD"] == "***"
    db = await call(
        client, "deploy_service", name="odoo-db", kind="container", container=ODOO_DB, confirm=True
    )
    assert db["networks_created"] == ["odoo-net"]
    app = await call(
        client, "deploy_service", name="odoo", kind="container", container=ODOO, confirm=True
    )
    assert app["networks_created"] == []  # already exists
    assert vps.networks["odoo-net"] == {"io.vps-mcp.managed": "true"}
    assert vps.containers["odoo"].networks == ["odoo-net"]
    assert vps.containers["odoo-db"].spec["ports"] == []  # database port never published
    # secrets go through stdin, never argv
    run = next(c for c in vps.calls if c.argv[:2] == ["docker", "run"] and "odoo-db" in c.argv)
    assert "pg-secret-pw" not in " ".join(run.argv) and "pg-secret-pw" in (run.stdin or "")
    st = await call(client, "get_service_status", service="odoo")
    assert st["service"]["networks"] == ["odoo-net"]


async def test_second_network_connected(client, vps):
    vps.networks["shared"] = {}
    spec = {"image": "redis:7", "networks": ["a-net", "shared"]}
    await call(
        client, "deploy_service", name="cache", kind="container", container=spec, confirm=True
    )
    assert vps.containers["cache"].networks == ["a-net", "shared"]
    assert ["docker", "network", "connect", "shared", "cache"] in [c.argv for c in vps.calls]
