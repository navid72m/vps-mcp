"""vps-mcp on agent-memory: tools, pinned core, generated facts, degradation (T021)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_memory import Memory
from tests.conftest import PASSPHRASE, SessionPerCall, call, config_dict
from tests.unit.jobs.conftest import job_config, make_jvps
from vps_mcp.config.policy import AppConfig
from vps_mcp.server import INSTRUCTIONS, build_context, create_server

OLD_TOOLS = {"memory_briefing", "memory_health", "memory_read", "memory_write"}
NEW_TOOLS = {"memory_search", "memory_get", "memory_save", "memory_update", "memory_archive"}


def with_store(cfg: AppConfig, store: Path, **mem: Any) -> AppConfig:
    data = cfg.model_dump(by_alias=True)
    data["memory"] = {"store": str(store), **mem}
    return AppConfig.model_validate(data)


@pytest.fixture
def make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VPS_MCP_TEST_PASSPHRASE", PASSPHRASE)

    def make(store: Path | None = None, **mem: Any) -> Any:
        cfg = with_store(job_config(), store or tmp_path / "mem", **mem)
        return build_context(cfg, executor=make_jvps(), db_path=tmp_path / "state.db")

    return make


async def test_tools_and_pinned_core(make_app: Any, tmp_path: Path) -> None:
    Memory.open(tmp_path / "mem", project="vps-test-vps", allow_scopes=["*"]).save(
        {
            "type": "procedural",
            "subject": "deploy.order",
            "title": "Stop the app first",
            "content": "Stop odoo before running database migrations.",
            "pinned": True,
        },
        operator=True,
    )
    server = create_server(make_app())
    names = {t.name for t in (await SessionPerCall(server).list_tools()).tools}
    assert NEW_TOOLS <= names and not names & OLD_TOOLS
    text = server.instructions or ""
    assert text.startswith(INSTRUCTIONS) and "Stop the app first" in text


async def test_generated_service_facts_in_place(make_app: Any, tmp_path: Path) -> None:
    client = SessionPerCall(create_server(make_app()))
    await call(client, "list_services")
    m = Memory.open(tmp_path / "mem", project="vps-test-vps", environment="test-vps")
    res = m.search("", subject="service.worker")
    item = res["groups"]["facts"][0]
    assert item["scope"] == "environment:test-vps" and item["source"] == "tool_generated"
    assert "live_state: verify" in item["flags"]
    await call(client, "list_services")
    await call(client, "get_service_status", service="worker")
    assert m.search("", subject="service.worker")["total"] == 1
    total = len(m.index.all_rows())
    await call(client, "get_service_logs", service="worker")  # not a refresh tool
    assert len(m.index.all_rows()) == total


async def test_agent_saves_through_vps_mcp(make_app: Any) -> None:
    client = SessionPerCall(create_server(make_app()))
    saved = await call(
        client,
        "memory_save",
        type="decision",
        subject="promote.rule",
        title="Promote only on safe_to_promote",
        content="Promote a migrated DB only when the verdict is safe_to_promote.",
        alternatives=["promote on review_warnings"],
    )
    assert saved["outcome"] == "created"
    found = await call(client, "memory_search", query="promote verdict")
    assert found["groups"]["decisions"][0]["rejected_alternatives"]


async def test_writable_false(make_app: Any) -> None:
    server = create_server(make_app(writable=False))
    names = {t.name for t in (await SessionPerCall(server).list_tools()).tools}
    assert {"memory_search", "memory_get"} <= names and "memory_save" not in names


def test_unavailable_store_degrades(make_app: Any, tmp_path: Path) -> None:
    bad = tmp_path / "file-not-dir"
    bad.write_text("x")
    app = make_app(store=bad)
    server = create_server(app)
    assert "Memory unavailable" in (server.instructions or "")


async def test_unavailable_store_other_tools_work(make_app: Any, tmp_path: Path) -> None:
    bad = tmp_path / "file-not-dir"
    bad.write_text("x")
    client = SessionPerCall(create_server(make_app(store=bad)))
    assert (await call(client, "list_services"))["ok"] is True
    names = {t.name for t in (await client.list_tools()).tools}
    assert not names & NEW_TOOLS


def test_memory_disabled(make_app: Any) -> None:
    server = create_server(make_app(enabled=False))
    assert server.instructions == INSTRUCTIONS


def test_check_config_migration_hint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.unit.test_policy import to_toml
    from vps_mcp import cli

    kh = tmp_path / "kh"
    kh.write_text("203.0.113.10 ssh-ed25519 AAAA\n")
    d = config_dict()
    d["vps"]["known_hosts_path"] = str(kh)
    cfg = tmp_path / "myvps.toml"
    cfg.write_text(to_toml(d))
    legacy = tmp_path / "myvps.memory" / "notes"
    legacy.mkdir(parents=True)
    (legacy / "a.md").write_text("---\ntitle: x\n---\ny\n")
    assert cli.main(["check-config", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "memory: " in out and "agent-memory migrate-004" in out
