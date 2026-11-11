"""US1: status parsing for both backends (T027, T028)."""

import json
from datetime import UTC, datetime

import pytest

from tests.fakes import FakeVps
from vps_mcp.backends.container import spec_from_inspect, status_from_inspect
from vps_mcp.backends.systemd import parse_show, status_from_props
from vps_mcp.config.policy import ServiceDeclaration
from vps_mcp.models.views import ServiceState

OS = ServiceDeclaration(name="nginx", kind="os", package="nginx")
CTR = ServiceDeclaration(name="api", kind="container")


def props(active, **extra):
    base = {
        "LoadState": "loaded",
        "ActiveState": active,
        "UnitFileState": "enabled",
        "ActiveEnterTimestamp": "@1700000000",
        "StateChangeTimestamp": "@1700000000",
        "ExecMainStatus": "0",
        "Result": "success",
        "MemoryCurrent": "1024",
    }
    base.update(extra)
    return base


@pytest.mark.parametrize(
    ("active", "state"),
    [
        ("active", ServiceState.RUNNING),
        ("inactive", ServiceState.STOPPED),
        ("failed", ServiceState.FAILED),
        ("activating", ServiceState.TRANSITIONING),
        ("deactivating", ServiceState.TRANSITIONING),
        ("reloading", ServiceState.TRANSITIONING),
    ],
)
def test_systemd_state_mapping(active, state):
    assert status_from_props(OS, props(active)).state is state


def test_systemd_running_has_uptime_and_boot():
    st = status_from_props(OS, props("active"), version="1.24")
    assert st.uptime_s and st.uptime_s > 0
    assert st.boot_enabled is True and st.version == "1.24"
    assert st.last_state_change == datetime.fromtimestamp(1700000000, tz=UTC)
    assert st.resources.mem_bytes == 1024


def test_systemd_failed_exit_reason():
    st = status_from_props(OS, props("failed", ExecMainStatus="203", Result="exit-code"))
    assert st.last_exit_reason == "result=exit-code, exit_status=203"


def test_systemd_not_found():
    st = status_from_props(OS, {"LoadState": "not-found", "ActiveState": "inactive"})
    assert st.state is ServiceState.NOT_FOUND and st.boot_enabled is None


def test_parse_show_multiple_blocks():
    blocks = parse_show("Id=a\nActiveState=active\n\nId=b\nActiveState=failed\n")
    assert [b["Id"] for b in blocks] == ["a", "b"]


def inspect(status, code=0, restart="unless-stopped", health=None):
    vps = FakeVps()
    c = vps.add_container("api", "ghcr.io/myorg/api:1.0", restart=restart)
    c.status, c.exit_code, c.health = status, code, health
    return json.loads(json.dumps(vps._inspect_json(c)))


@pytest.mark.parametrize(
    ("status", "code", "state"),
    [
        ("running", 0, ServiceState.RUNNING),
        ("exited", 0, ServiceState.STOPPED),
        ("created", 0, ServiceState.STOPPED),
        ("exited", 1, ServiceState.FAILED),
        ("dead", 0, ServiceState.FAILED),
        ("restarting", 0, ServiceState.TRANSITIONING),
        ("removing", 0, ServiceState.TRANSITIONING),
    ],
)
def test_container_state_mapping(status, code, state):
    assert status_from_inspect(CTR, inspect(status, code)).state is state


def test_container_not_found_and_boot():
    assert status_from_inspect(CTR, None).state is ServiceState.NOT_FOUND
    assert status_from_inspect(CTR, inspect("running")).boot_enabled is True
    assert status_from_inspect(CTR, inspect("running", restart="no")).boot_enabled is False


def test_container_networks_and_failure_reason():
    st = status_from_inspect(CTR, inspect("exited", 3))
    assert st.networks == ["bridge"] and st.last_exit_reason == "exit_code=3"


def test_spec_from_inspect_roundtrip():
    vps = FakeVps()
    c = vps.add_container(
        "db",
        "postgres:16",
        env={"POSTGRES_PASSWORD": "pw"},
        ports=["127.0.0.1:5432:5432"],
        volumes=["pgdata:/var/lib/postgresql/data"],
        networks=["odoo-net"],
    )
    spec = spec_from_inspect(vps._inspect_json(c))
    assert spec.image == "postgres:16"
    assert spec.env["POSTGRES_PASSWORD"] == "pw"
    assert spec.ports == ["127.0.0.1:5432:5432"]
    assert spec.volumes == ["pgdata:/var/lib/postgresql/data"]
    assert spec.networks == ["odoo-net"]


def test_systemd_timestamp_formats():
    from vps_mcp.backends.systemd import parse_systemd_ts

    want = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
    assert parse_systemd_ts("Tue 2026-11-10 10:00:00 UTC") == want
    assert parse_systemd_ts(f"@{int(want.timestamp())}") == want
    assert parse_systemd_ts("n/a") is None and parse_systemd_ts("") is None


async def test_show_falls_back_without_timestamp_flag(app, vps):
    from vps_mcp.remote.executor import CommandResult

    original = vps._h_systemctl

    def old_systemd(args, stdin):
        if "--timestamp=utc" in args:
            return CommandResult(1, "", 'Invalid value "utc" for --timestamp.')
        return original(args, stdin)

    vps._h_systemctl = old_systemd
    decl = app.managed.resolve("worker", "os")
    st = await app.backend("os").status(decl)
    assert st.state.value == "running"
