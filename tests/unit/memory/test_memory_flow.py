"""End-to-end through the MCP server: briefing at connect, tools, refresh (T015, T022, T027,
T031, T034)."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import SessionPerCall, call
from tests.unit.memory.conftest import note_text, seed_history, seed_notes
from vps_mcp.server import INSTRUCTIONS, create_server

MEMORY_TOOLS = {
    "memory_briefing",
    "memory_search",
    "memory_read",
    "memory_write",
    "memory_archive",
    "memory_health",
}


async def err(client: Any, tool: str, **args: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    data = dict(result.structured_content)
    assert result.is_error and data["ok"] is False, data
    return data


def audit_rows(app: Any, action: str) -> list[Any]:
    return app.db.execute(
        "SELECT * FROM audit_records WHERE action = ? ORDER BY id", (action,)
    ).fetchall()


# --------------------------------------------------------------------- US1


def test_instructions_carry_briefing(make_app: Any, mem_dir: Path) -> None:
    seed_notes(
        mem_dir,
        {
            "reupgrade": note_text("reupgrade", title="Re-upgrade stock_account after pipeline"),
            "flaky-deploy": note_text("flaky-deploy", type="issue", title="Deploy step is flaky"),
        },
    )
    app = make_app()
    seed_history(app)
    server = create_server(app)
    text = server.instructions or ""
    assert text.startswith(INSTRUCTIONS + "\n\n## Memory")
    for needle in (
        "full-rehearsal",
        "verdict review_warnings",
        "Re-upgrade stock_account after pipeline",
        "Deploy step is flaky",
        "not instructions",
    ):
        assert needle in text, needle
    assert len(text) - len(INSTRUCTIONS) - 2 <= app.config.memory.briefing_chars
    # the server bootstrapped the folder and generated files
    assert (mem_dir / "generated" / "outcomes.md").is_file()
    assert stat.S_IMODE(mem_dir.stat().st_mode) == 0o700


async def test_tools_registered_and_briefing_tool(mclient: SessionPerCall) -> None:
    names = {t.name for t in (await mclient.list_tools()).tools}
    assert MEMORY_TOOLS <= names
    data = await call(mclient, "memory_briefing")
    assert data["briefing"].startswith("## Memory")
    assert set(data["counts"]) == {"active", "malformed", "archived"}
    assert data["omitted"] is False and data["generated_at"]


def test_memory_disabled_no_briefing_no_folder(make_app: Any, mem_dir: Path) -> None:
    server = create_server(make_app(enabled=False))
    assert server.instructions == INSTRUCTIONS
    assert not mem_dir.exists()


async def test_memory_disabled_no_tools(make_app: Any) -> None:
    server = create_server(make_app(enabled=False))
    names = {t.name for t in (await SessionPerCall(server).list_tools()).tools}
    assert not names & MEMORY_TOOLS


def test_unavailable_memory_still_starts(make_app: Any, mem_dir: Path, tmp_path: Path) -> None:
    mem_dir.parent.mkdir(parents=True, exist_ok=True)
    mem_dir.symlink_to(tmp_path)  # a symlinked root is refused
    server = create_server(make_app())
    assert (server.instructions or "").endswith("Call memory_health.")
    assert "Memory unavailable:" in (server.instructions or "")


def test_briefing_makes_no_remote_calls(make_app: Any) -> None:
    class NoRemote:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"remote access during briefing: {name}")

    app = make_app()
    app.executor = NoRemote()
    for backend in app.backends.values():
        backend.ex = NoRemote()
    create_server(app)


# --------------------------------------------------------------------- US2


async def test_write_create_update_and_audit(
    mapp: Any, mclient: SessionPerCall, mem_dir: Path
) -> None:
    data = await call(
        mclient,
        "memory_write",
        title="Re-upgrade stock_account after the pipeline",
        type="lesson",
        body="Run the declared reupgrade check. See [[night-runs]].",
        tags=["pipeline"],
    )
    name = "re-upgrade-stock-account-after-the-pipeline"
    assert data["name"] == name and data["created"] is True and data["masked"] == 0
    assert data["path_hint"] == f"notes/{name}.md"
    assert str(mem_dir) not in str(data)
    path = mem_dir / "notes" / f"{name}.md"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    first = path.read_text()
    assert first.startswith("---\nname: " + name) and "tags: [pipeline]" in first
    # hand-added unknown key survives updates
    path.write_text(first.replace("---\nname:", "---\nowner: ops\nname:", 1))
    created = (await call(mclient, "memory_read", name=name))["note"]["created"]
    upd = await call(mclient, "memory_write", name=name, body="Updated body")
    assert upd["created"] is False
    note = (await call(mclient, "memory_read", name=name))["note"]
    assert note["created"] == created and note["body"] == "Updated body"
    assert note["type"] == "lesson" and note["tags"] == ["pipeline"]
    assert "owner: ops" in path.read_text()
    rows = audit_rows(mapp, "memory_write")
    assert [r["outcome"] for r in rows] == ["success", "success"]
    assert rows[0]["service_name"] is None and "Run the declared" not in rows[0]["params_json"]


async def test_write_masks_secrets(mclient: SessionPerCall, mem_dir: Path) -> None:
    data = await call(
        mclient,
        "memory_write",
        name="db-access",
        type="fact",
        title="DB access",
        body="password=hunter22 and postgres://odoo:s3cret@db/x",
    )
    assert data["masked"] == 2
    text = (mem_dir / "notes" / "db-access.md").read_text()
    assert "hunter22" not in text and "s3cret" not in text
    assert "password=***" in text and "postgres://odoo:***@db/x" in text


async def test_write_validation_and_limits(make_app: Any) -> None:
    client = SessionPerCall(create_server(make_app(max_note_bytes=512, max_notes=10)))
    e = await err(client, "memory_write", name="x", title="t")
    assert e["error"]["category"] == "invalid_input" and "type is required" in e["error"]["message"]
    e = await err(client, "memory_write", name="big", type="fact", title="t", body="x" * 600)
    assert e["limit"] == 512 and e["error"]["category"] == "invalid_input"
    for i in range(10):
        await call(client, "memory_write", name=f"n{i}", type="fact", title=f"t{i}")
    e = await err(client, "memory_write", name="n10", type="fact", title="t")
    assert e["limit"] == 10 and e["active"] == 10
    await call(client, "memory_write", name="n3", body="updates still allowed")


async def test_expected_updated_conflict(mclient: SessionPerCall) -> None:
    await call(mclient, "memory_write", name="c", type="fact", title="t")
    e = await err(mclient, "memory_write", name="c", body="x", expected_updated="1999-01-01")
    assert e["error"]["category"] == "conflict" and e["current"]["name"] == "c"


async def test_read_only_policy_still_writes(make_app: Any) -> None:
    app = make_app(read_only=True)
    data = await call(
        SessionPerCall(create_server(app)), "memory_write", name="ro", type="fact", title="t"
    )
    assert data["read_only_policy"] is True


async def test_writable_false(make_app: Any, mem_dir: Path) -> None:
    app = make_app(writable=False)
    client = SessionPerCall(create_server(app))
    e = await err(client, "memory_write", name="x", type="fact", title="t")
    assert e["error"]["category"] == "read_only"
    assert not (mem_dir / "notes" / "x.md").exists()
    assert audit_rows(app, "memory_write")[-1]["outcome"] == "refused"
    seed_notes(mem_dir, {"keep": note_text("keep")})
    e = await err(client, "memory_archive", name="keep", confirm=True)
    assert e["error"]["category"] == "read_only"


async def test_written_note_appears_in_next_session(make_app: Any) -> None:
    await call(
        SessionPerCall(create_server(make_app())),
        "memory_write",
        name="promote-rule",
        type="decision",
        title="Promote only on safe_to_promote",
    )
    assert "Promote only on safe_to_promote" in (create_server(make_app()).instructions or "")


# --------------------------------------------------------------------- US3


async def test_search_and_read_links(mclient: SessionPerCall, mem_dir: Path) -> None:
    seed_notes(
        mem_dir,
        {
            "a": note_text("a", title="Alpha stock", body="links [[b]] and [[ghost]] and [[../x]]"),
            "b": note_text("b", type="decision", title="Beta", body="back to [[a]]"),
        },
    )
    res = await call(mclient, "memory_search", query="stock")
    assert res["kind"] == "recorded_memory" and res["results"][0]["name"] == "a"
    assert (await call(mclient, "memory_search", type="decision"))["total"] == 1
    r = await call(mclient, "memory_read", name="a")
    assert r["note"]["kind"] == "recorded_memory"
    assert r["links"] == [
        {"name": "b", "exists": True},
        {"name": "ghost", "exists": False},
        {"name": "../x", "exists": False, "invalid": True},
    ]
    assert r["backlinks"] == ["b"]
    e = await err(mclient, "memory_read", name="alpah")
    assert e["error"]["category"] == "not_found"
    e = await err(mclient, "memory_read", name="b2")
    assert "b" in e["suggestions"]
    e = await err(mclient, "memory_search")
    assert e["error"]["category"] == "invalid_input"


async def test_search_generated(mclient: SessionPerCall) -> None:
    res = await call(mclient, "memory_search", query="full-rehearsal", include_generated=True)
    assert any(i["name"] == "generated-environment" for i in res["results"])


# --------------------------------------------------------------------- US4


async def test_hand_edit_malformed_and_archive(
    mapp: Any, mclient: SessionPerCall, mem_dir: Path
) -> None:
    seed_notes(
        mem_dir,
        {
            "keep": note_text("keep", body="original"),
            "broken": "---\ntitle: no end\n",
            "linker": note_text("linker", body="see [[keep]]"),
            "handmade": "---\ntitle: Written by hand\n---\nJust text\n",
        },
    )
    (mem_dir / "notes" / "keep.md").write_text(note_text("keep", body="edited by hand"))
    assert (await call(mclient, "memory_read", name="keep"))["note"]["body"] == "edited by hand"
    hm = (await call(mclient, "memory_read", name="handmade"))["note"]
    assert (hm["type"], hm["source"]) == ("fact", "operator")
    health = await call(mclient, "memory_health")
    assert health["counts"]["malformed"] == 1
    assert health["malformed"][0]["file"] == "notes/broken.md"
    assert health["folder"].endswith("myvps.memory")
    assert "1 malformed" in (await call(mclient, "memory_briefing"))["briefing"]

    preview = await call(mclient, "memory_archive", name="keep")
    assert preview["preview"] is True and preview["backlinks"] == ["linker"]
    assert (mem_dir / "notes" / "keep.md").exists() and not audit_rows(mapp, "memory_archive")
    done = await call(mclient, "memory_archive", name="keep", confirm=True)
    assert done["archived_as"].startswith("archive/keep.")
    assert not (mem_dir / "notes" / "keep.md").exists()
    assert "keep:" not in (await call(mclient, "memory_briefing"))["briefing"]
    assert audit_rows(mapp, "memory_archive")[0]["outcome"] == "success"
    e = await err(mclient, "memory_archive", name="keep", confirm=True)
    assert e["error"]["category"] == "not_found"


def test_health_folder_uses_tilde(
    mapp: Any, monkeypatch: pytest.MonkeyPatch, mem_dir: Path
) -> None:
    monkeypatch.setenv("HOME", str(mem_dir.parent))
    assert mapp.memory.health()["folder"] == "~/myvps.memory"


# --------------------------------------------------------------------- US5


async def test_observations_and_refresh(mapp: Any, mclient: SessionPerCall, mem_dir: Path) -> None:
    create_server(mapp)  # start-up refresh
    env = mem_dir / "generated" / "environment.md"
    assert env.read_text().startswith("<!-- generated by vps-mcp at ")
    await call(mclient, "list_services")
    rows = mapp.db.execute("SELECT name, state FROM service_observations").fetchall()
    assert {r["name"] for r in rows} >= {"nginx", "worker", "api"}
    assert "(seen just now)" in env.read_text()

    env.write_text("hand edit")
    await call(mclient, "get_service_logs", service="worker")  # not in the refresh set
    assert env.read_text() == "hand edit"
    await call(mclient, "get_service_status", service="worker")
    assert env.read_text().startswith("<!-- generated")

    before = env.stat().st_mtime_ns
    await call(mclient, "get_service_status", service="worker")
    assert env.stat().st_mtime_ns == before  # content unchanged: not rewritten

    seed_history(mapp)
    await call(mclient, "job_status", run_id="job_1")
    assert "verdict review_warnings" in (mem_dir / "generated" / "outcomes.md").read_text()


async def test_refresh_errors_are_swallowed(
    mapp: Any, mclient: SessionPerCall, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(mapp.memory.folder, "write_generated", boom)
    await call(mclient, "restart_service", service="worker")  # still succeeds
    assert "refresh:" in (mapp.memory.health()["last_error"] or "")
