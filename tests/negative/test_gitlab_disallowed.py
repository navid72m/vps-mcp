"""Disallowed GitLab requests (T038, SC-003): each is refused, with no GitLab write and no
VPS write."""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import SessionPerCall
from tests.fakegitlab import SHA_A, FakeGitLab
from vps_mcp.server import create_server

pytest_plugins = ["tests.unit.gitlab.conftest"]

ALIASES = ["other", "../addons", "ADDONS", "addons ", "acme/odoo-addons", "11", "33", "аddons"]
WRITE_TOOLS: list[tuple[str, dict[str, Any]]] = [
    ("gitlab_write", {"action": "create_issue", "title": "t", "confirm": True}),
    (
        "gitlab_write",
        {"action": "comment", "kind": "issue", "iid": 1, "body": "b", "confirm": True},
    ),
    (
        "gitlab_write",
        {
            "action": "create_mr",
            "source_branch": "feature/x",
            "target_branch": "main",
            "title": "t",
            "confirm": True,
        },
    ),
    ("gitlab_write", {"action": "close_issue", "iid": 2, "confirm": True}),
    ("gitlab_ci", {"action": "retry_job", "job_id": 9001, "confirm": True}),
    ("gitlab_ci", {"action": "trigger_pipeline", "ref": "main", "confirm": True}),
]


def no_writes(fgl: FakeGitLab, vps: Any) -> None:
    assert fgl.writes() == []
    assert vps.mutating_calls() == []


async def refused(client: SessionPerCall, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    res = await client.call_tool(tool, args)
    assert res.is_error, res.structured_content
    return dict(res.structured_content or {})


@pytest.mark.parametrize("alias", ALIASES)
async def test_unknown_aliases(
    gclient: SessionPerCall, fgl: FakeGitLab, gvps: Any, alias: str
) -> None:
    for tool, args in [("gitlab_file", {"path": "README.md"}), *WRITE_TOOLS[:1]]:
        e = await refused(gclient, tool, {"project": alias, **args})
        assert e["error"]["category"] == "not_permitted"
    assert fgl.requests == []
    no_writes(fgl, gvps)


@pytest.mark.parametrize(("tool", "args"), WRITE_TOOLS)
async def test_missing_permission(
    gclient: SessionPerCall, fgl: FakeGitLab, gvps: Any, tool: str, args: dict[str, Any]
) -> None:
    e = await refused(gclient, tool, {"project": "scripts", **args})
    assert e["error"]["category"] == "not_permitted"
    no_writes(fgl, gvps)


@pytest.mark.parametrize(("tool", "args"), WRITE_TOOLS)
async def test_read_only_mode(
    make_gapp: Any, fgl: FakeGitLab, gvps: Any, tool: str, args: dict[str, Any]
) -> None:
    app = make_gapp(read_only=True)
    e = await refused(SessionPerCall(create_server(app)), tool, {"project": "addons", **args})
    assert e["error"]["category"] == "read_only"
    assert e["audit_id"]
    no_writes(fgl, gvps)


@pytest.mark.parametrize(
    "args",
    [
        {"ref": "feature/x"},
        {"ref": "tag:main"},
        {"ref": "branch:v1.0"},
        {"ref": "nope"},
        {"ref": "d" * 40},
        {"ref": "../../etc"},
    ],
)
async def test_disallowed_refs(
    gclient: SessionPerCall, fgl: FakeGitLab, gvps: Any, args: dict[str, Any]
) -> None:
    await refused(gclient, "deploy_from_gitlab", {"target": "addons", "confirm": False, **args})
    no_writes(fgl, gvps)


@pytest.mark.parametrize("ref", ["feature/x", "tag:v1.0", "v*"])
async def test_trigger_outside_trigger_refs(
    gclient: SessionPerCall, fgl: FakeGitLab, gvps: Any, ref: str
) -> None:
    e = await refused(
        gclient,
        "gitlab_ci",
        {"project": "addons", "action": "trigger_pipeline", "ref": ref, "confirm": True},
    )
    assert e["error"]["category"] == "invalid_input"
    no_writes(fgl, gvps)


async def test_close_foreign_issue(gclient: SessionPerCall, fgl: FakeGitLab, gvps: Any) -> None:
    e = await refused(
        gclient,
        "gitlab_write",
        {"project": "addons", "action": "close_issue", "iid": 1, "confirm": True},
    )
    assert e["error"]["category"] == "not_permitted"
    no_writes(fgl, gvps)


@pytest.mark.parametrize("action", ["merge", "merge_mr", "accept", "delete"])
async def test_no_merge_or_delete(
    gclient: SessionPerCall, fgl: FakeGitLab, gvps: Any, action: str
) -> None:
    await refused(
        gclient, "gitlab_write", {"project": "addons", "action": action, "iid": 5, "confirm": True}
    )
    no_writes(fgl, gvps)


@pytest.mark.parametrize("plan_id", ["forged", "", "0" * 32])
async def test_forged_plans(
    gclient: SessionPerCall, fgl: FakeGitLab, gvps: Any, plan_id: str
) -> None:
    e = await refused(
        gclient,
        "deploy_from_gitlab",
        {"target": "addons", "ref": "v1.0", "confirm": True, "plan_id": plan_id},
    )
    assert e["error"]["category"] in ("conflict", "invalid_input")
    no_writes(fgl, gvps)


@pytest.mark.parametrize("release", ["../x", "a/b", "20261111T000000-xyz", "/etc"])
async def test_bad_rollback_release(
    gclient: SessionPerCall, fgl: FakeGitLab, gvps: Any, release: str
) -> None:
    e = await refused(
        gclient, "rollback_deploy", {"target": "addons", "release": release, "confirm": True}
    )
    assert e["error"]["category"] == "invalid_input"
    no_writes(fgl, gvps)


@pytest.mark.parametrize("target", ["nope", "../addons", "addons/x"])
async def test_unknown_targets(
    gclient: SessionPerCall, fgl: FakeGitLab, gvps: Any, target: str
) -> None:
    e = await refused(
        gclient, "deploy_from_gitlab", {"target": target, "ref": "v1.0", "confirm": False}
    )
    assert e["error"]["category"] == "not_permitted"
    no_writes(fgl, gvps)


async def test_tar_slip_archive_is_contained(
    gclient: SessionPerCall, fgl: FakeGitLab, gvps: Any
) -> None:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (
            ("top/ok.py", b"ok"),
            ("top/../../etc/cron.d/x", b"evil"),
            ("/etc/passwd", b"evil"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("top/esc")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/shadow"
        tar.addfile(link)
    fgl.repos[11].raw_archives[SHA_A] = buf.getvalue()
    before = set(gvps.files)
    plan = await gclient.call_tool(
        "deploy_from_gitlab", {"target": "addons", "ref": "v1.0", "confirm": False}
    )
    pid = plan.structured_content["plan"]["plan_id"]
    res = await gclient.call_tool(
        "deploy_from_gitlab", {"target": "addons", "ref": "v1.0", "confirm": True, "plan_id": pid}
    )
    data = res.structured_content
    assert data["ok"] is True, data
    new = set(gvps.files) - before
    assert new and all(f.startswith("/var/lib/vps-mcp/deploys/addons/releases/") for f in new)
    assert all("/etc" not in t for t in gvps.links.values())
    assert any("esc" in s for s in data["skipped"])
