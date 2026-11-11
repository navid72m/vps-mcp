"""Opt-in migration checks against a VM with PostgreSQL (T040).

Needs a policy file with a [migration] section whose targets exist on the VM:
    uv run pytest -m integration --vps-config tests/integration/vps-mcp.test.toml -k migration
Read-only scenarios (quickstart 1-6) are safe; rehearsal scenarios need free disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import SessionPerCall, call
from vps_mcp.config.loader import load_config
from vps_mcp.server import build_context, create_server

pytestmark = pytest.mark.integration


@pytest.fixture
def real(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[SessionPerCall, list[str]]:
    path = request.config.getoption("--vps-config")
    if not path:
        pytest.skip("--vps-config not given")
    cfg = load_config(path)
    if cfg.migration is None or len(cfg.migration.target) < 2:
        pytest.skip("config has no [migration] section with two targets")
    app = build_context(cfg, db_path=tmp_path / "it.db")
    return SessionPerCall(create_server(app)), cfg.migration.target_names()


@pytest.fixture(autouse=True)
def fast_polling() -> None:  # real hosts need real polling
    return None


async def test_migration_overview_and_compare(real):
    client, targets = real
    ov = await call(client, "db_overview", db=targets[0])
    assert ov["size_bytes"] > 0
    rep = await call(client, "db_compare", before=targets[0], after=targets[1])
    assert rep["verdict"] in ("safe_to_promote", "review_warnings", "do_not_promote")
    page = await call(client, "get_report", report_id=rep["report_id"])
    assert page["total"] == sum(rep["counts"].values())


async def test_migration_refusals(real):
    client, _ = real
    r = await call(client, "db_overview", db="definitely_not_allowlisted")
    assert r["error"]["category"] == "not_permitted"
