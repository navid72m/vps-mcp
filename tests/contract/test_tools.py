"""Contract tests: tool surface vs contracts/mcp-tools.md (T029, T035, T047, T071)."""

import pytest

from tests.conftest import call

READ_ONLY = {
    "list_services",
    "get_service_status",
    "get_service_logs",
    "get_service_config",
    "get_resource_usage",
    "get_audit_log",
    "list_revisions",
}
STATE_CHANGING = {
    "start_service",
    "stop_service",
    "restart_service",
    "set_boot_start",
    "deploy_service",
    "update_service",
    "configure_service",
    "rollback_service",
    "remove_service",
}
DESTRUCTIVE = {
    "stop_service",
    "restart_service",
    "update_service",
    "configure_service",
    "rollback_service",
    "remove_service",
}


@pytest.fixture
async def tools(client):
    return {t.name: t for t in (await client.list_tools()).tools}


async def test_exact_tool_set(tools):
    assert set(tools) == READ_ONLY | STATE_CHANGING
    assert len(tools) == 16


async def test_annotations(tools):
    for name, t in tools.items():
        assert t.description, name
        assert t.annotations.read_only_hint is (name in READ_ONLY), name
        if name in STATE_CHANGING:
            assert t.annotations.destructive_hint is (name in DESTRUCTIVE), name


async def test_input_schemas(tools):
    svc = tools["get_service_status"].input_schema
    assert svc["required"] == ["service"]
    assert "pattern" in svc["properties"]["service"]
    deploy = tools["deploy_service"].input_schema
    assert set(deploy["required"]) == {"name", "kind", "confirm"}
    ctr = deploy["properties"]["container"]
    assert "networks" in str(ctr) and "image" in str(ctr)
    logs = tools["get_service_logs"].input_schema["properties"]
    assert {"lines", "since", "until", "keyword"} <= set(logs)
    for name in STATE_CHANGING:
        assert "confirm" in tools[name].input_schema["properties"], name
        assert "ctx" not in tools[name].input_schema["properties"], name


async def test_status_tools(client):
    data = await call(client, "list_services")
    names = {(s["name"], s["kind"]) for s in data["services"]}
    assert names == {("api", "container"), ("nginx", "os"), ("worker", "os")}
    assert data["vps"] == {"name": "test-vps", "reachable": True}
    one = await call(client, "get_service_status", service="api")
    assert one["service"]["state"] == "running"


async def test_not_permitted_does_not_reveal_existence(client, vps):
    vps.add_unit("postgres")
    a = await call(client, "get_service_status", service="postgres")
    b = await call(client, "get_service_status", service="doesnotexist")
    assert a["error"]["category"] == b["error"]["category"] == "not_permitted"
    assert a["error"]["message"].replace("postgres", "X") == b["error"]["message"].replace(
        "doesnotexist", "X"
    )


async def test_ambiguous_kind(client, vps, app):
    from vps_mcp.config.policy import ServiceDeclaration

    vps.add_unit("api")
    app.config.service.append(ServiceDeclaration(name="api", kind="os"))
    data = await call(client, "get_service_status", service="api")
    assert data["error"]["category"] == "invalid_input" and "ambiguous" in data["error"]["message"]


async def test_connection_failure_envelope(client, vps):
    vps.unreachable = True
    data = await call(client, "list_services")
    assert data["error"] == {
        "category": "connection_failure",
        "message": "host unreachable",
        "subtype": "unreachable",
    }


async def test_control_contract(client, vps):
    data = await call(client, "stop_service", service="nginx")
    assert data["error"]["category"] == "confirmation_required"
    assert vps.units["nginx.service"].active == "active"
    data = await call(client, "stop_service", service="sshd")
    assert data["error"]["category"] == "not_permitted"
    data = await call(client, "restart_service", service="api")
    assert data["outcome"] == "success" and data["service"]["state"] == "running"
    assert isinstance(data["audit_id"], int)


async def test_deploy_contract_validation(client):
    data = await call(
        client, "deploy_service", name="x", kind="os", confirm=False, container={"image": "redis:7"}
    )
    assert data["error"]["category"] == "invalid_input"
    data = await call(
        client,
        "deploy_service",
        name="x",
        kind="container",
        confirm=False,
        container={"image": "redis:7", "networks": ["host"]},
    )
    assert data["error"]["category"] == "invalid_input" and "host" in data["error"]["message"]
    data = await call(
        client,
        "deploy_service",
        name="x",
        kind="container",
        confirm=False,
        container={"image": "redis:7", "ports": ["nope"]},
    )
    assert data["error"]["category"] == "invalid_input"


async def test_audit_tool_contract(client):
    await call(client, "restart_service", service="api")
    await call(client, "stop_service", service="nginx")  # refused
    data = await call(client, "get_audit_log", limit=10)
    recs = data["records"]
    assert [r["outcome"] for r in recs] == ["refused", "success"]
    assert recs[0]["action"] == "stop" and recs[0]["error_category"] == "confirmation_required"
    filtered = await call(client, "get_audit_log", outcome="success")
    assert len(filtered["records"]) == 1
    bad = await call(client, "get_audit_log", limit=0)
    assert bad["error"]["category"] == "invalid_input"
