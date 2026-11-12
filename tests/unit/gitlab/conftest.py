from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import PASSPHRASE, SessionPerCall, config_dict, make_vps
from tests.fakegitlab import TOKEN, FakeGitLab
from vps_mcp.config.policy import AppConfig
from vps_mcp.server import build_context, create_server

DEST = "/opt/odoo/custom-addons"


def gitlab_section(**over: Any) -> dict[str, Any]:
    g: dict[str, Any] = {
        "url": "https://gitlab.example.test",
        "keychain_account": "deploy-bot",
        "project": [
            {
                "alias": "addons",
                "path": "acme/odoo-addons",
                "permissions": ["read", "write_issues", "write_mrs", "ci", "deploy"],
                "close_issues": True,
                "trigger_refs": ["main", "release/*"],
            },
            {"alias": "scripts", "id": 22, "permissions": ["read"]},
        ],
        "deploy": [
            {
                "name": "addons",
                "project": "addons",
                "refs": ["tag:v*", "branch:main"],
                "destination": DEST,
                "owner": "worker",
                "group": "worker",
                "exclude": ["tests/**"],
                "services": ["worker"],
            }
        ],
    }
    g.update(over)
    return g


def gl_config(read_only: bool = False, memory: Path | None = None, **over: Any) -> AppConfig:
    d = config_dict(**({"read_only": True} if read_only else {}))
    d["gitlab"] = gitlab_section(**over)
    if memory is not None:
        d["memory"] = {"store": str(memory)}
    return AppConfig.model_validate(d)


def keychain(token: str | None = TOKEN) -> Any:
    def reader(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if token is None:
            return subprocess.CompletedProcess(argv, 44, "", "not found")
        return subprocess.CompletedProcess(argv, 0, token + "\n", "")

    return reader


@pytest.fixture
def fgl() -> FakeGitLab:
    return FakeGitLab()


@pytest.fixture
def gvps() -> Any:
    return make_vps()


@pytest.fixture
def make_gapp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fgl: FakeGitLab, gvps: Any) -> Any:
    monkeypatch.setenv("VPS_MCP_TEST_PASSPHRASE", PASSPHRASE)

    def make(token: str | None = TOKEN, cfg: AppConfig | None = None, **kw: Any) -> Any:
        return build_context(
            cfg or gl_config(**kw),
            executor=gvps,
            db_path=tmp_path / "state.db",
            gitlab_transport=fgl.transport,
            keychain_reader=keychain(token),
        )

    return make


@pytest.fixture
def gapp(make_gapp: Any) -> Any:
    return make_gapp()


@pytest.fixture
def gclient(gapp: Any) -> SessionPerCall:
    return SessionPerCall(create_server(gapp))


async def gerr(client: Any, tool: str, **args: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    data = dict(result.structured_content)
    assert result.is_error and data["ok"] is False, data
    return data
