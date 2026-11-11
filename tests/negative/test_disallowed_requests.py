"""SC-006: malformed or disallowed requests never cause a mutating remote command (T076)."""

import pytest

from tests.conftest import SessionPerCall, call, make_config
from vps_mcp.server import build_context, create_server

INJECT = ["a;reboot", "$(reboot)", "`id`", "a|sh", "a&&b", "-rf", "a\nb", "../../etc", "a b", ""]
NGINX = "/etc/nginx/nginx.conf"

CASES: list[tuple[str, dict, str]] = []

# 1) injection strings in every service-name field (9 tools x 10 strings = 90)
for tool in (
    "start_service",
    "stop_service",
    "restart_service",
    "rollback_service",
    "remove_service",
):
    for s in INJECT:
        CASES.append((tool, {"service": s, "confirm": True}, "invalid_input"))
for s in INJECT:
    CASES.append(
        ("set_boot_start", {"service": s, "enabled": False, "confirm": True}, "invalid_input")
    )
    CASES.append(
        ("update_service", {"service": s, "image": "redis:7", "confirm": True}, "invalid_input")
    )
    CASES.append(
        ("configure_service", {"service": s, "spec_patch": {}, "confirm": True}, "invalid_input")
    )
    CASES.append(
        (
            "deploy_service",
            {"name": s, "kind": "container", "container": {"image": "redis:7"}, "confirm": True},
            "invalid_input",
        )
    )

CASES += [
    # 2) outside the managed set / remote-access units
    ("restart_service", {"service": "postgres", "confirm": True}, "not_permitted"),
    ("stop_service", {"service": "sshd", "confirm": True}, "not_permitted"),
    ("stop_service", {"service": "ssh", "kind": "os", "confirm": True}, "not_permitted"),
    ("set_boot_start", {"service": "sshd", "enabled": False, "confirm": True}, "not_permitted"),
    ("remove_service", {"service": "ssh", "confirm": True}, "not_permitted"),
    (
        "deploy_service",
        {"name": "sshd", "kind": "os", "os": {"package": "openssh-server"}, "confirm": True},
        "not_permitted",
    ),
    (
        "deploy_service",
        {"name": "login", "kind": "os", "os": {"package": "nginx", "unit": "ssh"}, "confirm": True},
        "not_permitted",
    ),
    ("configure_service", {"service": "sshd", "files": [], "confirm": True}, "not_permitted"),
    # 3) protected services
    ("stop_service", {"service": "nginx"}, "confirmation_required"),
    ("restart_service", {"service": "nginx"}, "confirmation_required"),
    ("set_boot_start", {"service": "nginx", "enabled": False}, "confirmation_required"),
    ("remove_service", {"service": "nginx", "confirm": True}, "protected"),
    ("remove_service", {"service": "nginx", "confirm": True, "delete_data": True}, "protected"),
    # 4) unapproved sources
    (
        "deploy_service",
        {
            "name": "x",
            "kind": "container",
            "container": {"image": "quay.io/evil/x"},
            "confirm": True,
        },
        "unapproved_source",
    ),
    (
        "deploy_service",
        {
            "name": "x",
            "kind": "container",
            "container": {"image": "evil.example.com/library/redis:7"},
            "confirm": True,
        },
        "unapproved_source",
    ),
    (
        "deploy_service",
        {"name": "x", "kind": "os", "os": {"package": "evilpkg"}, "confirm": True},
        "unapproved_source",
    ),
    (
        "update_service",
        {"service": "api", "image": "quay.io/evil/x", "confirm": True},
        "unapproved_source",
    ),
    (
        "configure_service",
        {"service": "api", "spec_patch": {"image": "quay.io/evil/x"}, "confirm": True},
        "unapproved_source",
    ),
    # 5) undeclared config paths and bad container specs
    (
        "configure_service",
        {"service": "nginx", "files": [{"path": "/etc/shadow", "content": "x"}], "confirm": True},
        "not_permitted",
    ),
    (
        "configure_service",
        {
            "service": "nginx",
            "files": [{"path": "/etc/nginx/../shadow", "content": "x"}],
            "confirm": True,
        },
        "invalid_input",
    ),
    (
        "configure_service",
        {"service": "api", "spec_patch": {"volumes": ["/:/host"]}, "confirm": True},
        "invalid_input",
    ),
    (
        "configure_service",
        {"service": "api", "spec_patch": {"networks": ["host"]}, "confirm": True},
        "invalid_input",
    ),
    (
        "configure_service",
        {"service": "api", "spec_patch": {"privileged": True}, "confirm": True},
        "invalid_input",
    ),
    (
        "deploy_service",
        {
            "name": "x",
            "kind": "container",
            "confirm": True,
            "container": {"image": "redis:7", "volumes": ["/var/run/docker.sock:/s"]},
        },
        "invalid_input",
    ),
    (
        "deploy_service",
        {
            "name": "x",
            "kind": "container",
            "confirm": True,
            "container": {"image": "redis:7", "env": {"A": "x\nB=y"}},
        },
        "invalid_input",
    ),
    (
        "deploy_service",
        {
            "name": "x",
            "kind": "container",
            "confirm": True,
            "container": {"image": "redis:7", "cap_add": ["SYS_ADMIN"]},
        },
        "invalid_input",
    ),
    (
        "deploy_service",
        {"name": "x", "kind": "os", "os": {"package": "nginx; reboot"}, "confirm": True},
        "invalid_input",
    ),
    (
        "update_service",
        {"service": "nginx", "version": "1.0; reboot", "confirm": True},
        "invalid_input",
    ),
    # 6) log filters (read-only, but still must not mutate)
    ("get_service_logs", {"service": "api", "lines": 0}, "invalid_input"),
    ("get_service_logs", {"service": "api", "lines": 5001}, "invalid_input"),
    ("get_service_logs", {"service": "api", "keyword": "x" * 201}, "invalid_input"),
    ("get_service_logs", {"service": "api", "since": "now; reboot"}, "invalid_input"),
    # 7) conflicts
    (
        "deploy_service",
        {"name": "api", "kind": "container", "container": {"image": "redis:7"}, "confirm": True},
        "conflict",
    ),
]


def test_suite_size():
    assert len(CASES) >= 50


@pytest.mark.parametrize(
    ("tool", "args", "category"), CASES, ids=[f"{i}-{c[0]}" for i, c in enumerate(CASES)]
)
async def test_disallowed(client, vps, tool, args, category):
    data = await call(client, tool, **args)
    assert data["ok"] is False
    assert data["error"]["category"] == category, data["error"]
    assert vps.mutating_calls() == [], vps.commands()


READ_ONLY_CASES = [
    ("start_service", {"service": "api"}),
    ("stop_service", {"service": "api"}),
    ("restart_service", {"service": "worker"}),
    ("set_boot_start", {"service": "worker", "enabled": False}),
    (
        "deploy_service",
        {"name": "x", "kind": "container", "container": {"image": "redis:7"}, "confirm": True},
    ),
    ("update_service", {"service": "api", "image": "ghcr.io/myorg/api:1.1", "confirm": True}),
    ("configure_service", {"service": "api", "spec_patch": {"env": {"A": "1"}}, "confirm": True}),
    ("rollback_service", {"service": "api", "confirm": True}),
    ("remove_service", {"service": "worker", "confirm": True}),
]


@pytest.mark.parametrize(("tool", "args"), READ_ONLY_CASES)
async def test_read_only_blocks_every_state_change(vps, db_path, tool, args):
    app = build_context(make_config(read_only=True), executor=vps, db_path=db_path)
    data = await call(SessionPerCall(create_server(app)), tool, **args)
    assert data["error"]["category"] == "read_only"
    assert vps.mutating_calls() == []
    assert app.audit.query(limit=5)[0]["outcome"] == "refused"
