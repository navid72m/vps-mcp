"""SC-004: disallowed migration requests never modify a non-copy database (T038)."""

from typing import Any

import pytest

from tests.conftest import SessionPerCall, call
from tests.unit.migration.conftest import SQL, make_mvps, mig_config
from vps_mcp.server import build_context, create_server

NON_COPY = ("shop_17", "shop_19", "plain_db", "secret_db", "postgres")
INJECT = [
    "x'; drop database shop_17;--",
    "shop_17; select 1",
    "../shop_17",
    "SHOP_17",
    "$(reboot)",
    "",
    "a" * 70,
    "shop 17",
]

CASES: list[tuple[str, dict[str, Any], str]] = []
for s in INJECT:
    CASES += [
        ("db_overview", {"db": s}, "invalid_input"),
        ("db_compare", {"before": s, "after": "shop_19"}, "invalid_input"),
        (
            "rehearse",
            {"pipeline": "core", "source": s, "confirm": True, "plan_hash": "x"},
            "invalid_input",
        ),
    ]
CASES += [
    ("db_table", {"db": "shop_17", "table": "res_partner; drop table x"}, "invalid_input"),
    ("db_table", {"db": "shop_17", "table": "a.b.c"}, "invalid_input"),
    ("db_overview", {"db": "secret_db"}, "not_permitted"),
    ("db_modules", {"db": "secret_db"}, "not_permitted"),
    ("db_compare", {"before": "shop_17", "after": "secret_db"}, "not_permitted"),
    ("db_overview", {"db": "vmcp_shop_17_202601010000_abcd"}, "not_permitted"),  # forged copy
    ("rehearse", {"pipeline": "core", "source": "secret_db", "confirm": False}, "not_permitted"),
    (
        "rehearse",
        {"pipeline": "core", "source": "vmcp_shop_17_202601010000_abcd", "confirm": False},
        "not_permitted",
    ),  # copies of copies
    ("rehearse", {"pipeline": "nope", "source": "shop_17", "confirm": False}, "not_found"),
    (
        "rehearse",
        {"pipeline": "core", "source": "shop_17", "confirm": True},
        "conflict",
    ),  # confirm without any plan_hash
    (
        "rehearse",
        {
            "pipeline": "core",
            "source": "shop_17",
            "confirm": True,
            "plan_hash": "0123456789abcdef",
        },
        "conflict",
    ),  # wrong/stale plan_hash
    (
        "rehearse",
        {
            "pipeline": "core",
            "source": "shop_17",
            "confirm": True,
            "method": "template",
            "plan_hash": "x",
        },
        "conflict",
    ),
    ("run_check", {"check": "fixer", "db": "shop_19", "confirm": True}, "not_permitted"),
    ("run_check", {"check": "nope", "db": "shop_19"}, "not_found"),
    ("run_check", {"check": "post-checks", "db": "secret_db"}, "not_permitted"),
    ("drop_copies", {"copy_ids": ["shop_17"], "confirm": True}, "not_permitted"),
    ("drop_copies", {"confirm": True}, "invalid_input"),
    ("cancel_rehearsal", {"rehearsal_id": "rhs_nope", "confirm": True}, "not_found"),
    ("get_report", {"report_id": "../../etc"}, "invalid_input"),
    ("pipeline_last_run", {"pipeline": "../core"}, "not_found"),
]


def test_suite_size():
    assert len(CASES) >= 30


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VPS_MCP_TEST_PASSPHRASE", "correct-horse-battery-staple")
    vps = make_mvps()
    vps.pg.dbs["shop_17"].sessions = 2  # makes method=template impossible
    app = build_context(mig_config(), executor=vps, db_path=tmp_path / "s.db")
    return vps, SessionPerCall(create_server(app))


@pytest.mark.parametrize(
    ("tool", "args", "category"), CASES, ids=[f"{i}-{c[0]}" for i, c in enumerate(CASES)]
)
async def test_disallowed(env, tool, args, category):
    vps, client = env
    data = await call(client, tool, **args)
    assert data["ok"] is False
    assert data["error"]["category"] == category, data["error"]
    assert vps.pg.pg_mutations(*NON_COPY) == []
    assert not [u for u in vps.pg.units if u.startswith("vps-mcp-rehearsal-")]


async def test_forbidden_sql_step_refused(env):
    vps, client = env
    vps.files[SQL] = "DELETE FROM x;\n\\c shop_19\nDROP TABLE res_partner;\n"
    data = await call(client, "rehearse", pipeline="core", source="shop_19", confirm=False)
    assert data["error"]["category"] == "invalid_input"
    assert vps.pg.pg_mutations() == []


READ_ONLY = [
    ("rehearse", {"pipeline": "core", "source": "shop_19", "confirm": False}),
    ("cancel_rehearsal", {"rehearsal_id": "rhs_x", "confirm": True}),
    ("run_check", {"check": "fixer", "db": "shop_19", "confirm": True}),
    ("drop_copies", {"expired_only": True, "confirm": True}),
]


@pytest.mark.parametrize(("tool", "args"), READ_ONLY)
async def test_read_only_mode(tmp_path, monkeypatch, tool, args):
    monkeypatch.setenv("VPS_MCP_TEST_PASSPHRASE", "correct-horse-battery-staple")
    vps = make_mvps()
    app = build_context(mig_config(read_only=True), executor=vps, db_path=tmp_path / "s.db")
    data = await call(SessionPerCall(create_server(app)), tool, **args)
    assert data["error"]["category"] in ("read_only", "not_found", "not_permitted")
    assert vps.pg.pg_mutations() == [] and vps.pg.units == {}
    if tool in ("rehearse", "drop_copies"):
        assert data["error"]["category"] == "read_only"
