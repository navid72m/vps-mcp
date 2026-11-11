"""Client errors (T008), read tools (T016), token flow (T017), items (T031), CI (T034)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import SessionPerCall, call
from tests.fakegitlab import SHA_A, SHA_B, FakeGitLab
from tests.unit.gitlab.conftest import gerr
from vps_mcp.server import create_server

GITLAB_TOOLS = {
    "gitlab_projects",
    "gitlab_refs",
    "gitlab_tree",
    "gitlab_file",
    "gitlab_commits",
    "gitlab_compare",
    "gitlab_list_items",
    "gitlab_read_item",
    "gitlab_write",
    "gitlab_pipelines",
    "gitlab_job_log",
    "gitlab_ci",
    "deploy_targets",
    "deploy_from_gitlab",
    "rollback_deploy",
}


async def test_tools_registered(gclient: SessionPerCall) -> None:
    names = {t.name for t in (await gclient.list_tools()).tools}
    assert GITLAB_TOOLS <= names
    assert not any("merge" in n for n in names)


# ------------------------------------------------------------------------ read


async def test_projects_and_refs(gclient: SessionPerCall, fgl: FakeGitLab) -> None:
    p = await call(gclient, "gitlab_projects")
    assert [x["alias"] for x in p["projects"]] == ["addons", "scripts"]
    assert p["projects"][0]["deploy_targets"] == ["addons"]
    r = await call(gclient, "gitlab_refs", project="addons")
    names = {(x["kind"], x["name"]) for x in r["refs"]}
    assert ("branch", "main") in names and ("tag", "v2.0") in names
    assert fgl.requests[-1].headers["private-token"]  # header is set
    r = await call(gclient, "gitlab_refs", project="addons", kind="tags", search="v1")
    assert [x["name"] for x in r["refs"]] == ["v1.0"]


async def test_tree_file_commits_compare(gclient: SessionPerCall) -> None:
    t = await call(gclient, "gitlab_tree", project="addons")
    assert t["ref"] == "main" and {"path": "stock_fix", "type": "tree"} in t["entries"]
    t = await call(gclient, "gitlab_tree", project="addons", recursive=True, limit=3)
    assert len(t["entries"]) == 3 and t["truncated"] is True
    f = await call(gclient, "gitlab_file", project="addons", path="README.md", ref="v2.0")
    assert f["content"]["kind"] == "external_content"
    assert "password=***" in f["content"]["text"] and "hunter22" not in str(f)
    f = await call(gclient, "gitlab_file", project="addons", path="README.md", max_lines=1)
    assert f["range"] == [1, 1] and f["truncated"] is True
    c = await call(gclient, "gitlab_commits", project="addons", ref="v2.0")
    assert c["commits"][0].startswith("bbbbbbbbbbbb 2026-11-05 10:00 dev: Fix stock valuation")
    d = await call(gclient, "gitlab_commits", project="addons", sha=SHA_B)
    assert d["message"]["kind"] == "external_content" and d["files"]
    cmp_ = await call(gclient, "gitlab_compare", project="addons", from_ref="v1.0", to_ref="v2.0")
    assert "A stock_fix/new.py +1 -0" in cmp_["files"] and cmp_["commits"]
    one = await call(
        gclient,
        "gitlab_compare",
        project="addons",
        from_ref="v1.0",
        to_ref="v2.0",
        path="stock_fix/models.py",
    )
    assert "+new" in one["diff"]["text"]


async def test_binary_file_summarised(gclient: SessionPerCall, fgl: FakeGitLab) -> None:
    fgl.repos[11].trees["c" * 40]["logo.png"] = (b"\x89PNG\x00\x01", 0o644)
    f = await call(gclient, "gitlab_file", project="addons", path="logo.png")
    assert f["binary"] is True and f["size"] == 6 and "content" not in f


@pytest.mark.parametrize("alias", ["other", "../x", "acme/secret", "33"])
async def test_unknown_alias_refused_without_request(
    gclient: SessionPerCall, fgl: FakeGitLab, alias: str
) -> None:
    e = await gerr(gclient, "gitlab_refs", project=alias)
    assert e["error"]["category"] == "not_permitted"
    assert (
        e["error"]["message"] == "that is not an allowlisted GitLab project (see gitlab_projects)"
    )
    assert fgl.requests == []


async def test_path_traversal_in_repo_path(gclient: SessionPerCall, fgl: FakeGitLab) -> None:
    e = await gerr(gclient, "gitlab_file", project="addons", path="../../etc/passwd")
    assert e["error"]["category"] == "invalid_input"


# ------------------------------------------------------------------ errors/token


@pytest.mark.parametrize(
    ("fail", "category", "subtype"),
    [
        (401, "permission_denied", "gitlab_auth"),
        (403, "permission_denied", "gitlab_scope"),
        (404, "not_found", None),
        (429, "timed_out", "gitlab_rate_limited"),
        (500, "failed", None),
        ("timeout", "timed_out", None),
        ("connect", "connection_failure", "gitlab_unreachable"),
    ],
)
async def test_error_mapping(
    gclient: SessionPerCall, fgl: FakeGitLab, fail: Any, category: str, subtype: str | None
) -> None:
    fgl.fail[r"/repository/branches"] = fail
    fgl.echo = True
    e = await gerr(gclient, "gitlab_refs", project="addons", kind="branches")
    assert e["error"]["category"] == category
    assert e["error"].get("subtype") == subtype
    if fail == 429:
        assert e["retry_after_s"] == 7
    assert sum(1 for r in fgl.requests if "branches" in r.path) == 1  # no retry loop


async def test_token_missing(make_gapp: Any, fgl: FakeGitLab) -> None:
    client = SessionPerCall(create_server(make_gapp(token=None)))
    e = await gerr(client, "gitlab_refs", project="addons")
    assert e["error"]["subtype"] == "gitlab_token_missing"
    assert (
        "security add-generic-password -U -s vps-mcp-gitlab -a deploy-bot -w"
        in (e["error"]["message"])
    )
    assert fgl.requests == []
    assert (await call(client, "list_services"))["ok"] is True  # other tools unaffected


async def test_wrong_token(make_gapp: Any, fgl: FakeGitLab) -> None:
    client = SessionPerCall(create_server(make_gapp(token="glpat-WRONGTOKEN000000000000")))
    e = await gerr(client, "gitlab_refs", project="addons", kind="tags")
    assert e["error"]["subtype"] == "gitlab_auth"


# ------------------------------------------------------------------------ items


async def test_list_and_read_items(gclient: SessionPerCall) -> None:
    lst = await call(gclient, "gitlab_list_items", project="addons", kind="issue")
    assert lst["items"][0].startswith("#1 [opened] Old bug (alice")
    it = await call(gclient, "gitlab_read_item", project="addons", kind="issue", iid=1)
    assert it["web_ref"] == "addons#1"
    assert it["description"]["kind"] == "external_content"
    assert len(it["notes"]) == 1 and "token: ***" in it["notes"][0]["text"]
    mr = await call(gclient, "gitlab_read_item", project="addons", kind="mr", iid=5)
    assert (mr["source"], mr["target"], mr["pipeline_status"]) == ("feature/x", "main", "success")
    assert mr["web_ref"] == "addons!5"


async def test_create_issue_preview_then_confirm(
    gapp: Any, gclient: SessionPerCall, fgl: FakeGitLab
) -> None:
    pv = await call(
        gclient,
        "gitlab_write",
        project="addons",
        action="create_issue",
        confirm=False,
        title="Rehearsal failed",
        description="password=x blocking",
    )
    assert pv["preview"] is True and fgl.writes() == []
    assert pv["request"]["fields"]["description"].endswith("_Posted via vps-mcp_")
    assert "password=***" in pv["request"]["fields"]["description"]
    done = await call(
        gclient,
        "gitlab_write",
        project="addons",
        action="create_issue",
        confirm=True,
        title="Rehearsal failed",
        description="details",
    )
    assert done["web_ref"] == "addons#3" and len(fgl.writes()) == 1
    row = gapp.db.execute("SELECT * FROM audit_records WHERE action='gitlab_issue'").fetchone()
    assert row["outcome"] == "success" and row["service_name"] == "addons"
    assert "details" not in row["params_json"]


async def test_comment_mr_and_close(gclient: SessionPerCall, fgl: FakeGitLab) -> None:
    c = await call(
        gclient,
        "gitlab_write",
        project="addons",
        action="comment",
        kind="mr",
        iid=5,
        body="LGTM",
        confirm=True,
    )
    assert c["web_ref"] == "addons!5"
    mr = await call(
        gclient,
        "gitlab_write",
        project="addons",
        action="create_mr",
        confirm=True,
        source_branch="feature/x",
        target_branch="main",
        title="T",
    )
    assert mr["web_ref"].startswith("addons!")
    e = await gerr(
        gclient,
        "gitlab_write",
        project="addons",
        action="create_mr",
        confirm=True,
        source_branch="nope",
        target_branch="main",
        title="T",
    )
    assert e["error"]["category"] == "not_found"
    e = await gerr(
        gclient, "gitlab_write", project="addons", action="close_issue", iid=1, confirm=True
    )
    assert (
        e["error"]["category"] == "not_permitted"
        and "created through vps-mcp" in (e["error"]["message"])
    )
    ok_ = await call(
        gclient, "gitlab_write", project="addons", action="close_issue", iid=2, confirm=True
    )
    assert ok_["web_ref"] == "addons#2" and fgl.repos[11].issues[2]["state"] == "closed"


async def test_write_permissions_and_read_only(make_gapp: Any, fgl: FakeGitLab) -> None:
    client = SessionPerCall(create_server(make_gapp()))
    e = await gerr(
        client, "gitlab_write", project="scripts", action="create_issue", confirm=True, title="x"
    )
    assert e["error"]["category"] == "not_permitted"
    merge = await client.call_tool(
        "gitlab_write", {"project": "addons", "action": "merge", "confirm": True}
    )
    assert merge.is_error  # rejected by the tool's input schema
    ro = make_gapp(read_only=True)
    e = await gerr(
        SessionPerCall(create_server(ro)),
        "gitlab_write",
        project="addons",
        action="create_issue",
        confirm=True,
        title="x",
    )
    assert e["error"]["category"] == "read_only"
    assert (
        ro.db.execute("SELECT outcome FROM audit_records WHERE action='gitlab_issue'").fetchone()[
            "outcome"
        ]
        == "refused"
    )
    assert fgl.writes() == []


# --------------------------------------------------------------------------- CI


async def test_pipelines_and_logs(gclient: SessionPerCall) -> None:
    p = await call(gclient, "gitlab_pipelines", project="addons")
    assert p["pipelines"][0] == "900 failed main cccccccccccc 2m05s failed:[test]"
    j = await call(gclient, "gitlab_pipelines", project="addons", pipeline_id=900)
    assert any(line.startswith("9001 test test failed") for line in j["jobs"])
    log = await call(gclient, "gitlab_job_log", project="addons", job_id=9001)
    assert log["lines"] == 200 and log["truncated"] is True
    assert "API_TOKEN=***" in log["log"]["text"] and "supersecret99" not in str(log)


async def test_ci_actions(gapp: Any, gclient: SessionPerCall, fgl: FakeGitLab) -> None:
    pv = await call(
        gclient, "gitlab_ci", project="addons", action="retry_job", job_id=9001, confirm=False
    )
    assert pv["preview"] and fgl.writes() == []
    await call(
        gclient, "gitlab_ci", project="addons", action="retry_job", job_id=9001, confirm=True
    )
    await call(
        gclient,
        "gitlab_ci",
        project="addons",
        action="cancel_pipeline",
        pipeline_id=900,
        confirm=True,
    )
    t = await call(
        gclient, "gitlab_ci", project="addons", action="trigger_pipeline", ref="main", confirm=True
    )
    assert t["result"]["status"] == "created"
    assert len(fgl.writes()) == 3
    e = await gerr(
        gclient,
        "gitlab_ci",
        project="addons",
        action="trigger_pipeline",
        ref="feature/x",
        confirm=True,
    )
    assert e["error"]["category"] == "invalid_input"
    e = await gerr(
        gclient, "gitlab_ci", project="scripts", action="retry_job", job_id=1, confirm=True
    )
    assert e["error"]["category"] == "not_permitted"
    assert len(fgl.writes()) == 3
    rows = gapp.db.execute(
        "SELECT COUNT(*) n FROM audit_records WHERE action='gitlab_ci'"
    ).fetchone()
    assert rows["n"] >= 4
    assert SHA_A  # silence unused import in some runs
