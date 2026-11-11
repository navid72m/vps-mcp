"""SC-002: the token appears nowhere: tool output, logs, audit, memory files, VPS traffic (T018)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import SessionPerCall
from tests.fakegitlab import TOKEN, FakeGitLab
from vps_mcp.server import create_server

CALLS: list[tuple[str, dict[str, Any]]] = [
    ("gitlab_projects", {}),
    ("gitlab_refs", {"project": "addons"}),
    ("gitlab_tree", {"project": "addons", "recursive": True}),
    ("gitlab_file", {"project": "addons", "path": "README.md", "ref": "v2.0"}),
    ("gitlab_commits", {"project": "addons"}),
    ("gitlab_commits", {"project": "addons", "sha": "b" * 40}),
    ("gitlab_compare", {"project": "addons", "from_ref": "v1.0", "to_ref": "main"}),
    ("gitlab_list_items", {"project": "addons", "kind": "issue"}),
    ("gitlab_read_item", {"project": "addons", "kind": "issue", "iid": 1}),
    (
        "gitlab_write",
        {
            "project": "addons",
            "action": "create_issue",
            "title": TOKEN,
            "description": f"leak {TOKEN}",
            "confirm": True,
        },
    ),
    (
        "gitlab_write",
        {
            "project": "addons",
            "action": "comment",
            "kind": "issue",
            "iid": 1,
            "body": f"x {TOKEN}",
            "confirm": True,
        },
    ),
    ("gitlab_pipelines", {"project": "addons"}),
    ("gitlab_pipelines", {"project": "addons", "pipeline_id": 900}),
    ("gitlab_job_log", {"project": "addons", "job_id": 9001}),
    ("gitlab_ci", {"project": "addons", "action": "retry_job", "job_id": 9001, "confirm": True}),
    ("deploy_targets", {}),
    ("rollback_deploy", {"target": "addons", "confirm": False}),
]


def everything(app: Any, vps: Any, mem: Path, results: list[Any], caplog: Any) -> str:
    parts = [repr(r.content) + repr(r.structured_content) for r in results]
    parts += [rec.getMessage() for rec in caplog.records]
    parts += [repr(dict(r)) for r in app.db.execute("SELECT * FROM audit_records")]
    parts += [repr(dict(r)) for r in app.db.execute("SELECT * FROM gitlab_deploys")]
    parts += [p.read_text(errors="replace") for p in mem.rglob("*") if p.is_file()]
    for c in vps.calls:
        parts += [repr(c.argv), repr(c.env), repr(c.stdin)]
        if c.stdin_bytes:
            parts.append(c.stdin_bytes.decode("latin-1"))
    return "\n".join(parts)


@pytest.mark.parametrize("fail", [None, 401, 403, 404, 429, 500, "timeout", "connect"])
async def test_token_never_leaks(
    make_gapp: Any,
    fgl: FakeGitLab,
    gvps: Any,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    fail: Any,
) -> None:
    caplog.set_level(logging.DEBUG)
    mem = tmp_path / "myvps.memory"
    app = make_gapp(memory=mem)
    client = SessionPerCall(create_server(app))
    fgl.echo = True
    results = []
    # a full deploy first (success path), then everything else, optionally failing
    plan = await client.call_tool(
        "deploy_from_gitlab", {"target": "addons", "ref": "v2.0", "confirm": False}
    )
    results.append(plan)
    pid = plan.structured_content["plan"]["plan_id"]
    results.append(
        await client.call_tool(
            "deploy_from_gitlab",
            {"target": "addons", "ref": "v2.0", "confirm": True, "plan_id": pid},
        )
    )
    assert results[-1].structured_content["ok"] is True, results[-1].structured_content
    if fail is not None:
        fgl.fail[r"^/(projects|personal_access_tokens|user)"] = fail
    for tool, args in CALLS:
        results.append(await client.call_tool(tool, args))
    blob = everything(app, gvps, mem, results, caplog)
    assert TOKEN not in blob
    assert TOKEN[:16] not in blob
    if fail is not None:
        assert any(r.is_error for r in results[2:])
