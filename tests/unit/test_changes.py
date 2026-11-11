"""US5: configure / update / rollback with health gate (T053, T054)."""

import pytest

from tests.conftest import call
from vps_mcp.errors import VpsMcpError
from vps_mcp.store.revisions import RevisionStore

NGINX = "/etc/nginx/nginx.conf"
NEW_CONF = "worker_processes auto;\nevents { worker_connections 2048; }\n"


# ---------------------------------------------------------------- revision store


def test_revision_transitions(app):
    rs: RevisionStore = app.revisions
    rs.ensure_baseline("svc", "os", "1", {"v": 1})
    r2 = rs.activate_new("svc", "os", "2", {"v": 2})
    assert rs.active("svc", "os").id == r2 and rs.previous("svc", "os").content == {"v": 1}
    r3 = rs.activate_new("svc", "os", "3", {"v": 3})
    statuses = {r.content["v"]: r.status for r in rs.list("svc", "os")}
    assert statuses == {1: "superseded", 2: "previous", 3: "active"}
    restored = rs.mark_rolled_back("svc", "os")
    assert restored.content == {"v": 2}
    statuses = {r.content["v"]: r.status for r in rs.list("svc", "os")}
    assert statuses == {1: "superseded", 2: "active", 3: "rolled_back"}
    assert r3


def test_revision_pruning_and_no_previous(app):
    rs: RevisionStore = app.revisions
    for i in range(10):
        rs.activate_new("svc", "os", str(i), {"v": i})
    assert len(rs.list("svc", "os")) == 5
    with pytest.raises(VpsMcpError) as exc:
        rs.mark_rolled_back("other", "os")
    assert exc.value.category.value == "not_found"


# ------------------------------------------------------------------- OS configure


async def test_get_config_masks_secrets(client, vps):
    vps.files[NGINX] += "auth_password = hunter22\n"
    data = await call(client, "get_service_config", service="nginx")
    content = data["files"][0]["content"]
    assert "hunter22" not in content and "auth_password = ***" in content


async def test_configure_preview_then_apply(client, vps):
    data = await call(
        client,
        "configure_service",
        service="nginx",
        files=[{"path": NGINX, "content": NEW_CONF}],
        confirm=False,
    )
    assert data["outcome"] == "preview"
    assert "-events { worker_connections 768; }" in data["diff"]
    assert data["validation"]["ok"] is True
    assert vps.files[NGINX] != NEW_CONF
    assert not any(c.argv[0] == "install" for c in vps.calls)
    data = await call(
        client,
        "configure_service",
        service="nginx",
        files=[{"path": NGINX, "content": NEW_CONF}],
        confirm=True,
    )
    assert data["outcome"] == "success" and vps.files[NGINX] == NEW_CONF
    assert ["systemctl", "reload", "nginx.service"] in [c.argv for c in vps.calls]
    revs = (await call(client, "list_revisions", service="nginx"))["revisions"]
    assert [r["status"] for r in revs] == ["active", "previous"]


async def test_undeclared_path_not_permitted(client, vps):
    data = await call(
        client,
        "configure_service",
        service="nginx",
        files=[{"path": "/etc/passwd", "content": "x"}],
        confirm=True,
    )
    assert data["error"]["category"] == "not_permitted"
    assert vps.mutating_calls() == []


async def test_invalid_config_restored(client, vps):
    original = vps.files[NGINX]
    data = await call(
        client,
        "configure_service",
        service="nginx",
        files=[{"path": NGINX, "content": "INVALID;\n"}],
        confirm=False,
    )
    assert data["validation"]["ok"] is False
    data = await call(
        client,
        "configure_service",
        service="nginx",
        files=[{"path": NGINX, "content": "INVALID;\n"}],
        confirm=True,
    )
    assert data["outcome"] == "failed" and "validation failed" in data["error"]["message"]
    assert vps.files[NGINX] == original
    assert vps.units["nginx.service"].active == "active"


async def test_unhealthy_after_change_auto_rolls_back(client, vps):
    original = vps.files[NGINX]
    vps.units["nginx.service"].fail_on_start = True  # reload kills it
    data = await call(
        client,
        "configure_service",
        service="nginx",
        files=[{"path": NGINX, "content": NEW_CONF}],
        confirm=True,
    )
    assert data["ok"] is False and "rollback" in data["error"]["message"].lower()
    assert vps.files[NGINX] == original


async def test_rollback_restores_file_and_service_running(client, vps):
    await call(
        client,
        "configure_service",
        service="nginx",
        files=[{"path": NGINX, "content": NEW_CONF}],
        confirm=True,
    )
    original_first_line = "events { worker_connections 768; }"
    data = await call(client, "rollback_service", service="nginx", confirm=False)
    assert data["outcome"] == "preview" and data["to_revision"]["status"] == "previous"
    data = await call(client, "rollback_service", service="nginx", confirm=True)
    assert data["outcome"] == "success"
    assert original_first_line in vps.files[NGINX]
    data = await call(client, "rollback_service", service="worker", confirm=True)
    assert data["error"]["category"] == "not_found"


# ------------------------------------------------------------- container changes


async def test_container_configure_env_and_rollback(client, vps):
    data = await call(
        client,
        "configure_service",
        service="api",
        spec_patch={"env": {"MODE": "debug", "API_TOKEN": "new-token-val"}},
        confirm=False,
    )
    assert '"MODE": "debug"' in data["diff"] and "new-token-val" not in data["diff"]
    assert "secret env values changed: API_TOKEN" in data["diff"]
    data = await call(
        client,
        "configure_service",
        service="api",
        spec_patch={"env": {"MODE": "debug"}},
        confirm=True,
    )
    assert data["outcome"] == "success", data
    assert vps.containers["api"].spec["env"]["MODE"] == "debug"
    assert vps.containers["api"].spec["env"]["API_TOKEN"] == "s3cr3t"
    assert "api.vps-mcp-old" not in vps.containers
    data = await call(client, "rollback_service", service="api", confirm=True)
    assert data["outcome"] == "success"
    assert vps.containers["api"].spec["env"]["MODE"] == "prod"


async def test_container_update_auto_rollback(client, vps):
    data = await call(
        client, "update_service", service="api", image="ghcr.io/myorg/api:2.0-broken", confirm=False
    )
    assert data == {
        **data,
        "from_version": "ghcr.io/myorg/api:1.0",
        "to_version": "ghcr.io/myorg/api:2.0-broken",
    }
    data = await call(
        client, "update_service", service="api", image="ghcr.io/myorg/api:2.0-broken", confirm=True
    )
    # the broken container exits immediately -> recreate restores the old one
    assert data["ok"] is False and "previous container was restored" in data["error"]["message"]
    assert vps.containers["api"].spec["image"] == "ghcr.io/myorg/api:1.0"
    assert vps.containers["api"].status == "running"


async def test_container_unhealthy_gate_rolls_back(client, vps):
    vps.unhealthy_images.add("ghcr.io/myorg/api:1.1")
    data = await call(
        client, "update_service", service="api", image="ghcr.io/myorg/api:1.1", confirm=True
    )
    assert data["outcome"] == "rolled_back", data
    assert vps.containers["api"].spec["image"] == "ghcr.io/myorg/api:1.0"
    revs = (await call(client, "list_revisions", service="api"))["revisions"]
    assert revs[0]["status"] == "rolled_back" and revs[1]["status"] == "active"


async def test_container_update_success_and_unapproved(client, vps):
    data = await call(
        client, "update_service", service="api", image="ghcr.io/myorg/api:1.1", confirm=True
    )
    assert data["outcome"] == "success"
    assert vps.containers["api"].spec["image"] == "ghcr.io/myorg/api:1.1"
    data = await call(client, "update_service", service="api", image="quay.io/evil/x", confirm=True)
    assert data["error"]["category"] == "unapproved_source"


async def test_os_update_version(client, vps):
    data = await call(client, "update_service", service="nginx", version="1.26.0-1", confirm=True)
    assert data["outcome"] == "success" and vps.installed["nginx"] == "1.26.0-1"
    data = await call(client, "rollback_service", service="nginx", confirm=True)
    assert data["outcome"] == "success" and vps.installed["nginx"] == "1.24.0-2"


async def test_network_change_garbage_collects(client, vps):
    await call(
        client,
        "deploy_service",
        name="cache",
        kind="container",
        container={"image": "redis:7", "networks": ["old-net"]},
        confirm=True,
    )
    data = await call(
        client,
        "configure_service",
        service="cache",
        spec_patch={"networks": ["new-net"]},
        confirm=True,
    )
    assert data["outcome"] == "success"
    assert "old-net" not in vps.networks and "new-net" in vps.networks
    assert vps.containers["cache"].networks == ["new-net"]
