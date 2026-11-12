from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from tests.fakes import FakeVps
from vps_mcp.backends import common
from vps_mcp.config.policy import AppConfig
from vps_mcp.core.context import AppContext
from vps_mcp.server import build_context, create_server

KEY_PATH = "/Users/operator/.ssh/super_secret_vps_key"
PASSPHRASE = "correct-horse-battery-staple"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--vps-config", default=None, help="policy file for integration tests")


@pytest.fixture(autouse=True)
def isolated_memory_store(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never let a test touch the real agent-memory store."""
    monkeypatch.setenv("AGENT_MEMORY_HOME", str(tmp_path_factory.mktemp("agent-memory")))


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "POLL_INTERVAL", 0)
    monkeypatch.setattr(common, "SETTLE_DELAY", 0)


def config_dict(**policy: Any) -> dict[str, Any]:
    return {
        "vps": {
            "name": "test-vps",
            "host": "203.0.113.10",
            "user": "vpsmcp",
            "key_path": KEY_PATH,
            "key_passphrase_env": "VPS_MCP_TEST_PASSPHRASE",
            "known_hosts_path": "/tmp/known_hosts",
            "container_runtime": "docker",
            "package_manager": "apt",
        },
        "policy": {
            "health_stabilize_s": 0,
            "approved_sources": {
                "apt_origins": ["Ubuntu"],
                "image_prefixes": ["docker.io/library/", "ghcr.io/myorg/"],
            },
            **policy,
        },
        "service": [
            {
                "name": "nginx",
                "kind": "os",
                "package": "nginx",
                "protected": True,
                "validate": "nginx",
                "activate": "reload",
                "config_files": [{"path": "/etc/nginx/nginx.conf"}],
            },
            {"name": "api", "kind": "container", "description": "Public API"},
            {"name": "worker", "kind": "os", "package": "worker", "data_paths": ["/srv/worker"]},
        ],
    }


def make_config(**policy: Any) -> AppConfig:
    return AppConfig.model_validate(config_dict(**policy))


def make_vps() -> FakeVps:
    vps = FakeVps()
    vps.add_unit("nginx", package="nginx")
    vps.add_unit("worker", package="worker")
    vps.installed.update({"nginx": "1.24.0-2", "worker": "2.0"})
    vps.pkg_units.update(
        {
            "nginx": "nginx.service",
            "worker": "worker.service",
            "redis-server": "redis-server.service",
        }
    )
    vps.repo["nginx"] = {
        "1.24.0-2": ["http://archive.ubuntu.com/ubuntu noble/main amd64 Packages"],
        "1.26.0-1": ["http://archive.ubuntu.com/ubuntu noble/main amd64 Packages"],
    }
    vps.repo["redis-server"] = {
        "7.0.15-1": ["http://archive.ubuntu.com/ubuntu noble/main amd64 Packages"]
    }
    vps.repo["evilpkg"] = {"1.0": ["http://evil.example.com/apt stable/main amd64 Packages"]}
    vps.files["/etc/nginx/nginx.conf"] = (
        "worker_processes auto;\nevents { worker_connections 768; }\n"
    )
    vps.files["/srv/worker/data.db"] = "data"
    vps.add_container(
        "api",
        "ghcr.io/myorg/api:1.0",
        env={"API_TOKEN": "s3cr3t", "MODE": "prod"},
        ports=["8080:80"],
    )
    vps.registry.update(
        {
            "redis:7",
            "postgres:16",
            "odoo:18",
            "ghcr.io/myorg/api:1.1",
            "ghcr.io/myorg/api:2.0-broken",
            "quay.io/evil/x",
        }
    )
    vps.crash_images.add("ghcr.io/myorg/api:2.0-broken")
    return vps


@pytest.fixture
def vps() -> FakeVps:
    return make_vps()


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.setenv("VPS_MCP_TEST_PASSPHRASE", PASSPHRASE)
    return make_config()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def app(config: AppConfig, vps: FakeVps, db_path: Path) -> AppContext:
    return build_context(config, executor=vps, db_path=db_path)


class SessionPerCall:
    """Opens a fresh in-process MCP session per call.

    A long-lived ``Client`` cannot live in an async fixture: pytest-asyncio enters and exits
    fixtures in different tasks, which anyio's cancel scopes reject.
    """

    def __init__(self, server: Any) -> None:
        self.server = server

    async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> Any:
        async with Client(self.server) as c:
            return await c.call_tool(name, args or {})

    async def list_tools(self) -> Any:
        async with Client(self.server) as c:
            return await c.list_tools()


@pytest.fixture
def client(app: AppContext) -> SessionPerCall:
    return SessionPerCall(create_server(app))


async def call(client: Any, tool: str, **args: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    assert isinstance(result.structured_content, dict), result.content
    data = dict(result.structured_content)
    assert data["ok"] is (not result.is_error), data
    return data
