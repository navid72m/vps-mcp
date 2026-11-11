"""gitlab-check and sudoers (T019, T030), memory integration (T037)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.conftest import SessionPerCall, call
from tests.fakegitlab import TOKEN, FakeGitLab
from tests.unit.gitlab.conftest import gl_config, keychain
from vps_mcp import cli
from vps_mcp.server import create_server


async def run_check(
    fgl: FakeGitLab, gvps: Any, tmp_path: Path, token: str | None = TOKEN, capsys: Any = None
) -> tuple[int, str]:
    rc = await cli._gitlab_check(
        gl_config(),
        with_vps=True,
        executor=gvps,
        db_path=tmp_path / "s.db",
        gitlab_transport=fgl.transport,
        keychain_reader=keychain(token),
    )
    return rc, capsys.readouterr().out


async def test_gitlab_check_ok(
    fgl: FakeGitLab,
    gvps: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VPS_MCP_TEST_PASSPHRASE", "x" * 12)
    fgl.expires_at = "2026-11-20"
    rc, out = await run_check(fgl, gvps, tmp_path, capsys=capsys)
    assert rc == 0, out
    assert "instance: https://gitlab.example.test" in out
    assert "scopes: api" in out and "user: deploy-bot" in out
    assert "WARNING expires: 2026-11-20" in out
    assert "project addons: ok (id 11) - consider pinning id = 11" in out
    assert "project scripts: ok (id 22)" in out
    assert "deploy addons: not deployed yet" in out
    assert TOKEN not in out


async def test_gitlab_check_scope_gap_and_errors(
    fgl: FakeGitLab,
    gvps: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VPS_MCP_TEST_PASSPHRASE", "x" * 12)
    fgl.scopes = ["read_api"]
    fgl.repos.pop(22)
    rc, out = await run_check(fgl, gvps, tmp_path, capsys=capsys)
    assert rc == 1
    assert "WARNING project addons: declares write/CI permissions" in out
    assert "ERROR project scripts" in out
    rc, out = await run_check(fgl, gvps, tmp_path, token=None, capsys=capsys)
    assert rc == 1 and "security add-generic-password" in out


def test_sudoers_with_gitlab(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["bootstrap", "--print-sudoers", "--with-gitlab"]) == 0
    out = capsys.readouterr().out
    assert "Cmnd_Alias VPSMCP_DEPLOY = /usr/bin/tar" in out
    assert "vpsmcp ALL=(root) NOPASSWD: VPSMCP_DEPLOY" in out


async def test_memory_shows_deployed_version_and_activity(
    make_gapp: Any, tmp_path: Path, gvps: Any
) -> None:
    mem = tmp_path / "myvps.memory"
    app = make_gapp(memory=mem)
    client = SessionPerCall(create_server(app))
    p = (await call(client, "deploy_from_gitlab", target="addons", ref="v1.0", confirm=False))[
        "plan"
    ]
    await call(
        client,
        "deploy_from_gitlab",
        target="addons",
        ref="v1.0",
        confirm=True,
        plan_id=p["plan_id"],
    )
    await call(
        client,
        "gitlab_write",
        project="addons",
        action="create_issue",
        confirm=True,
        title="Rehearsal failed",
    )
    outcomes = (mem / "generated" / "outcomes.md").read_text()
    assert "target addons: deployed aaaaaaaaaaaa (tag:v1.0) succeeded" in outcomes
    assert "gitlab create_issue addons#3" in outcomes
    text = create_server(make_gapp(memory=mem)).instructions or ""
    assert "target addons: deployed aaaaaaaaaaaa" in text
