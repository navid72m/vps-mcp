"""US2: control commands, verification, failed-start logs, reconciliation (T034, T039)."""

from datetime import UTC, datetime

from tests.conftest import call
from vps_mcp.core.reconcile import Reconciler


def sudo_cmds(vps):
    return [
        " ".join(c.argv)
        for c in vps.calls
        if c.sudo
        and c.argv[0] in ("systemctl", "docker")
        and c.argv[1] in ("start", "stop", "restart", "enable", "disable", "update")
    ]


async def test_systemd_golden_argv(client, vps):
    await call(client, "restart_service", service="worker")
    await call(client, "stop_service", service="worker")
    await call(client, "start_service", service="worker")
    await call(client, "set_boot_start", service="worker", enabled=False)
    assert sudo_cmds(vps) == [
        "systemctl restart worker.service",
        "systemctl stop worker.service",
        "systemctl start worker.service",
        "systemctl disable worker.service",
    ]
    assert all(c.sudo for c in vps.calls if c.argv[:2] == ["systemctl", "restart"])


async def test_container_golden_argv(client, vps):
    await call(client, "stop_service", service="api")
    await call(client, "start_service", service="api")
    await call(client, "set_boot_start", service="api", enabled=False)
    assert sudo_cmds(vps) == [
        "docker stop api",
        "docker start api",
        "docker update --restart no api",
    ]


async def test_verified_state_reported(client, vps):
    data = await call(client, "stop_service", service="api")
    assert data["service"]["state"] == "stopped"
    assert vps.containers["api"].status == "exited"


async def test_failed_start_includes_logs_and_never_claims_success(client, vps):
    vps.units["worker.service"].fail_on_start = True
    vps.units["worker.service"].logs.append((datetime(2026, 11, 11, tzinfo=UTC), "boot ok"))
    data = await call(client, "restart_service", service="worker")
    assert data["ok"] is False and data["outcome"] == "failed"
    assert data["error"]["category"] == "failed"
    assert any("fatal error" in line for line in data["logs"])
    assert data["error"]["last_known_state"]["state"] == "failed"


async def test_protected_with_confirm(client, vps):
    data = await call(client, "restart_service", service="nginx", confirm=True)
    assert data["outcome"] == "success"
    data = await call(client, "set_boot_start", service="nginx", enabled=False)
    assert data["error"]["category"] == "confirmation_required"
    data = await call(client, "set_boot_start", service="nginx", enabled=True)
    assert data["outcome"] == "success"


async def test_unknown_outcome_then_reconciled(client, vps, app):
    vps.drop_connection_on = ("systemctl", "restart")
    data = await call(client, "restart_service", service="worker")
    assert data["outcome"] == "unknown"
    status = await call(client, "get_service_status", service="worker")
    assert status["service"]["pending_reconciliation"] is True
    assert await Reconciler(app).run() == 1
    rec = (await call(client, "get_audit_log"))["records"][0]
    assert rec["outcome"] == "unknown" and rec["reconciled_state"] == "running"
    status = await call(client, "get_service_status", service="worker")
    assert status["service"]["pending_reconciliation"] is False


async def test_read_only_mode_blocks_control(vps, db_path, monkeypatch):
    from tests.conftest import SessionPerCall, make_config
    from vps_mcp.server import build_context, create_server

    app = build_context(make_config(read_only=True), executor=vps, db_path=db_path)
    data = await call(SessionPerCall(create_server(app)), "restart_service", service="api")
    assert data["error"]["category"] == "read_only"
    assert vps.mutating_calls() == []
