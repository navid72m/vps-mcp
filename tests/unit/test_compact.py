"""Unit tests for --mode=compact tool surface (13 consolidated tools)."""

from __future__ import annotations

import pytest

pytest_plugins = [
    "tests.unit.migration.conftest",
    "tests.unit.jobs.conftest",
    "tests.unit.gitlab.conftest",
]

from tests.conftest import SessionPerCall, call
from vps_mcp.server import create_server


@pytest.fixture
def compact_client(app):
    return SessionPerCall(create_server(app, mode="compact"))


@pytest.mark.asyncio
async def test_compact_tools_registered(compact_client):
    tools = await compact_client.list_tools()
    names = {t.name for t in tools.tools}
    expected = {"assess_infrastructure", "service_inspect", "service_control", "service_manage"}
    assert names == expected
    assert len(names) == 4


@pytest.mark.asyncio
async def test_service_inspect_list(compact_client):
    res = await call(compact_client, "service_inspect", action="list")
    assert res["ok"] is True
    assert "services" in res
    assert len(res["services"]) == 3


@pytest.mark.asyncio
async def test_service_inspect_status(compact_client):
    res = await call(compact_client, "service_inspect", action="status", service="nginx")
    assert res["ok"] is True
    assert res["service"]["name"] == "nginx"
    assert res["service"]["state"] == "running"


@pytest.mark.asyncio
async def test_service_inspect_config(compact_client):
    res = await call(compact_client, "service_inspect", action="config", service="nginx")
    assert res["ok"] is True
    assert "files" in res


@pytest.mark.asyncio
async def test_service_inspect_revisions(compact_client):
    res = await call(compact_client, "service_inspect", action="revisions", service="nginx")
    assert res["ok"] is True
    assert "revisions" in res


@pytest.mark.asyncio
async def test_service_inspect_audit(compact_client):
    res = await call(compact_client, "service_inspect", action="audit")
    assert res["ok"] is True
    assert "records" in res


@pytest.mark.asyncio
async def test_service_inspect_missing_service_error(compact_client):
    res = await compact_client.call_tool("service_inspect", {"action": "status"})
    assert res.is_error is True
    err = res.structured_content["error"]
    assert err["category"] == "invalid_input"
    assert "service name is required" in err["message"]


@pytest.mark.asyncio
async def test_service_control_restart(compact_client):
    res = await call(compact_client, "service_control", action="restart", service="nginx", confirm=True)
    assert res["ok"] is True
    assert res["service"]["state"] == "running"


@pytest.mark.asyncio
async def test_service_control_enable_boot(compact_client):
    res = await call(compact_client, "service_control", action="enable_boot", service="nginx")
    assert res["ok"] is True
    assert res["service"]["boot_enabled"] is True


@pytest.mark.asyncio
async def test_service_manage_preview_update(compact_client):
    res = await call(compact_client, "service_manage", action="update", service="nginx", confirm=False, version="1.26.0-1")
    assert res["ok"] is True
    assert res["to_version"] == "1.26.0-1"


@pytest.mark.asyncio
async def test_service_manage_preview_rollback(compact_client):
    # No previous revision initially
    res = await compact_client.call_tool("service_manage", {"action": "rollback", "service": "nginx", "confirm": False})
    # Should report failed or error because no previous revision exists
    assert "revisions" not in res.structured_content or res.is_error


@pytest.mark.asyncio
async def test_compact_migration_tools(mapp):
    c = SessionPerCall(create_server(mapp, mode="compact"))
    names = {t.name for t in (await c.list_tools()).tools}
    expected = {
        "assess_infrastructure",
        "service_inspect",
        "service_control",
        "service_manage",
        "db_inspect",
        "rehearsal_manage",
        "migration_audit",
    }
    assert names == expected
    assert len(names) == 7

    # test db_inspect list
    db_res = await call(c, "db_inspect", action="list")
    assert db_res["ok"] is True
    assert "targets" in db_res


@pytest.mark.asyncio
async def test_compact_jobs_tools(japp):
    c = SessionPerCall(create_server(japp, mode="compact"))
    names = {t.name for t in (await c.list_tools()).tools}
    assert "job_manage" in names
    assert len(names) == 7 + 1  # 4 base + 3 migration + 1 jobs

    res = await call(c, "job_manage", action="list")
    assert res["ok"] is True
    assert "jobs" in res


@pytest.mark.asyncio
async def test_compact_gitlab_tools(gapp):
    c = SessionPerCall(create_server(gapp, mode="compact"))
    names = {t.name for t in (await c.list_tools()).tools}
    expected_gitlab = {"gitlab_repo", "gitlab_items", "gitlab_ci", "gitlab_deploy"}
    assert expected_gitlab <= names
    assert len(names) == 4 + 4  # 4 base + 4 gitlab

    res = await call(c, "gitlab_repo", action="projects")
    assert res["ok"] is True
    assert "projects" in res


def test_cli_mode_flag_parsing():
    from vps_mcp.cli import build_parser

    p = build_parser()
    args_default = p.parse_args(["serve"])
    assert args_default.mode is None

    args_compact = p.parse_args(["serve", "--mode", "compact"])
    assert args_compact.mode == "compact"

    args_full = p.parse_args(["serve", "--mode", "full"])
    assert args_full.mode == "full"


def test_build_server_defaults_to_compact():
    from vps_mcp.server import build_server

    s = build_server()
    # In fallback introspection mode: 4 base + 3 migration + 4 gitlab = 11 compact tools
    assert len(s._tool_manager.list_tools()) == 11
    names = {t.name for t in s._tool_manager.list_tools()}
    assert "assess_infrastructure" in names
    assert "service_inspect" in names
    assert "get_service_logs" not in names  # consolidated into service_inspect


def test_build_server_mode_full():
    from vps_mcp.server import build_server

    s = build_server(mode="full")
    assert len(s._tool_manager.list_tools()) == 45
    names = {t.name for t in s._tool_manager.list_tools()}
    assert "get_service_logs" in names
    assert "list_revisions" in names


def test_build_server_env_override(monkeypatch):
    from vps_mcp.server import build_server

    monkeypatch.setenv("VPS_MCP_MODE", "full")
    s = build_server()
    assert len(s._tool_manager.list_tools()) == 45


