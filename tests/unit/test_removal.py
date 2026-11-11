"""US6: removal (T061)."""

from tests.conftest import call
from tests.unit.test_deploy import ODOO, ODOO_DB


async def test_protected_never_removed(client, vps):
    for confirm in (False, True):
        data = await call(client, "remove_service", service="nginx", confirm=confirm)
        assert data["error"]["category"] == "protected"
    assert vps.mutating_calls() == []


async def test_remote_access_not_permitted(client):
    data = await call(client, "remove_service", service="ssh", confirm=True)
    assert data["error"]["category"] == "not_permitted"


async def test_preview_lists_what_happens(client, vps):
    data = await call(client, "remove_service", service="worker", confirm=False)
    assert data["outcome"] == "preview" and data["data_paths_kept"] == ["/srv/worker"]
    data = await call(client, "remove_service", service="worker", delete_data=True, confirm=False)
    assert data["data_paths_deleted"] == ["/srv/worker"]
    assert vps.mutating_calls() == []


async def test_os_remove_keep_data(client, vps):
    data = await call(client, "remove_service", service="worker", confirm=True)
    assert data["outcome"] == "success" and data["data_deleted"] is False
    assert "worker" not in vps.installed and "/srv/worker/data.db" in vps.files
    assert ["apt-get", "remove", "-y", "-q", "worker"] in [c.argv for c in vps.calls]
    listed = await call(client, "list_services")
    assert "worker" not in {s["name"] for s in listed["services"]}


async def test_os_remove_delete_data(client, vps):
    data = await call(client, "remove_service", service="worker", delete_data=True, confirm=True)
    assert data["data_deleted"] is True and "/srv/worker/data.db" not in vps.files
    assert ["apt-get", "purge", "-y", "-q", "worker"] in [c.argv for c in vps.calls]
    rec = (await call(client, "get_audit_log"))["records"][0]
    assert rec["action"] == "remove" and rec["data_deleted"] is True


async def test_container_remove_keeps_volume_and_gcs_network(client, vps):
    await call(
        client, "deploy_service", name="odoo-db", kind="container", container=ODOO_DB, confirm=True
    )
    await call(
        client, "deploy_service", name="odoo", kind="container", container=ODOO, confirm=True
    )
    data = await call(client, "remove_service", service="odoo", confirm=True)
    assert data["networks_removed"] == []  # odoo-db still attached
    assert "odoo-data" in vps.volumes and data["volumes_kept"] == ["odoo-data"]
    data = await call(client, "remove_service", service="odoo-db", delete_data=True, confirm=True)
    assert data["networks_removed"] == ["odoo-net"] and "odoo-net" not in vps.networks
    assert "odoo-db-data" not in vps.volumes


async def test_operator_network_never_deleted(client, vps):
    vps.networks["shared"] = {}  # no managed label
    await call(
        client,
        "deploy_service",
        name="cache",
        kind="container",
        container={"image": "redis:7", "networks": ["shared"]},
        confirm=True,
    )
    data = await call(client, "remove_service", service="cache", confirm=True)
    assert data["networks_removed"] == [] and "shared" in vps.networks


async def test_redeploy_after_removal(client, vps):
    await call(
        client,
        "deploy_service",
        name="cache",
        kind="container",
        container={"image": "redis:7"},
        confirm=True,
    )
    await call(client, "remove_service", service="cache", confirm=True)
    data = await call(
        client,
        "deploy_service",
        name="cache",
        kind="container",
        container={"image": "redis:7"},
        confirm=True,
    )
    assert data["outcome"] == "success"
