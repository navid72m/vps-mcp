"""US1 (T012, T013) and US2 (T016, T017): understand, compare, risk."""

import pytest

from tests.conftest import call
from tests.fakepg import Table, col
from vps_mcp.migration import risk
from vps_mcp.migration.config import RiskThresholds
from vps_mcp.migration.diff import diff

MIGRATION_TOOLS = {
    "db_list",
    "db_overview",
    "db_modules",
    "db_table",
    "db_compare",
    "get_report",
    "list_reports",
    "rehearse",
    "rehearsal_status",
    "cancel_rehearsal",
    "pipeline_last_run",
    "run_check",
    "drop_copies",
}
MIG_RO = {
    "db_list",
    "db_overview",
    "db_modules",
    "db_table",
    "db_compare",
    "get_report",
    "list_reports",
    "rehearsal_status",
    "pipeline_last_run",
}


async def test_tools_registered_only_with_config(mclient, client):
    names = {t.name for t in (await mclient.list_tools()).tools}
    assert MIGRATION_TOOLS <= names and len(names) == 16 + 13
    plain = {t.name for t in (await client.list_tools()).tools}
    assert not (MIGRATION_TOOLS & plain)
    for t in (await mclient.list_tools()).tools:
        if t.name in MIGRATION_TOOLS:
            assert t.annotations.read_only_hint is (t.name in MIG_RO), t.name


# ------------------------------------------------------------------------ US1


async def test_overview_indicators(mclient, mvps):
    db = mvps.pg.dbs["shop_19"]
    db.modules["stuck"] = {"state": "to upgrade", "version": "17.0.1"}
    db.modules["ghost"] = {"state": "installed", "version": "19.0.1"}  # no code dir
    db.views["ghost.v"] = {"active": True, "inherit": "base.v1", "module": "old_mod"}
    db.views["x.inactive"] = {"active": False, "inherit": None, "module": "base"}
    data = await call(mclient, "db_overview", db="shop_19")
    assert data["size_bytes"] == 8 * 1024**3 and data["tables"] == 4
    assert data["largest"][0]["table"] == "public.account_move"
    assert data["modules"]["by_state"] == {"installed": 3, "to upgrade": 1, "uninstalled": 1}
    assert data["modules"]["by_major"] == {"19": 3}
    ind = data["indicators"]
    assert ind["transitional"] == ["stuck"] and ind["code_missing"] == ["ghost"]
    assert ind["inactive_views"] == 1 and ind["orphaned_views"] == 1


async def test_modules_filters_and_code(mclient):
    data = await call(mclient, "db_modules", db="shop_17", major="17")
    assert [(m["name"], m["code"]) for m in data["modules"]] == [("base", True), ("sale", True)]
    data = await call(mclient, "db_modules", db="shop_17", state="uninstalled")
    assert [m["name"] for m in data["modules"]] == ["old_mod"]


async def test_table_detail_and_errors(mclient):
    data = await call(mclient, "db_table", db="shop_17", table="res_partner")
    assert [c["name"] for c in data["columns"]] == ["id", "name", "vat", "credit"]
    bad = await call(mclient, "db_table", db="shop_17", table="x'; drop table y;--")
    assert bad["error"]["category"] == "invalid_input"
    missing = await call(mclient, "db_table", db="shop_17", table="nope")
    assert missing["error"]["category"] == "not_found"


async def test_non_odoo_database(mclient):
    data = await call(mclient, "db_overview", db="plain_db")
    assert "modules" not in data and "not an Odoo database" in data["skipped"][0]


async def test_db_list(mclient):
    data = await call(mclient, "db_list")
    assert [t["name"] for t in data["targets"]] == ["shop_17", "shop_19", "plain_db"]
    assert data["copies"] == []


# ------------------------------------------------------------------------ US2 pure


def snap(tables=None, modules=None, views=None):
    return {"id": "s", "database": "d", "tables": tables or {}, "modules": modules, "views": views}


def tbl(cols, rows, exact=True, idx=(), cons=()):
    return {
        "columns": {c: {"type": t} for c, t in cols.items()},
        "indexes": list(idx),
        "constraints": list(cons),
        "est_rows": rows,
        "exact_rows": rows if exact else None,
    }


T = RiskThresholds()


def classify(before, after):
    raw, skipped = diff(before, after)
    return risk.classify(raw, T), skipped


@pytest.mark.parametrize(
    ("old", "new", "kind"),
    [
        ("character varying(64)", "character varying(16)", "narrowed"),
        ("character varying(16)", "character varying(64)", "widened"),
        ("text", "character varying(10)", "narrowed"),
        ("character varying(10)", "text", "widened"),
        ("numeric(12,2)", "numeric(10,2)", "narrowed"),
        ("numeric(12,2)", "numeric(12,4)", "widened"),
        ("numeric", "numeric(12,2)", "narrowed"),
        ("bigint", "integer", "narrowed"),
        ("integer", "bigint", "widened"),
        ("integer", "numeric", "widened"),
        ("timestamp with time zone", "timestamp without time zone", "narrowed"),
        ("double precision", "real", "narrowed"),
        ("integer", "text", "narrowed"),
        ("integer[]", "integer", "narrowed"),
    ],
)
def test_type_rules(old, new, kind):
    assert risk.type_change(old, new) == kind


def test_every_category_and_severity():
    b = snap(
        {
            "public.gone": tbl({"id": "integer"}, 10),
            "public.empty_gone": tbl({"id": "integer"}, 0),
            "public.t": tbl(
                {"id": "integer", "x": "text", "y": "character varying(64)", "z": "integer"},
                1000,
                idx=["t_idx"],
                cons=["t_fk:f", "t_nn:n"],
            ),
            "public.shrink_w": tbl({"id": "integer"}, 100),
            "public.shrink_b": tbl({"id": "integer"}, 100),
            "public.shrink_i": tbl({"id": "integer"}, 100),
        },
        modules={
            "base": {"state": "installed", "version": "17.0.1"},
            "a": {"state": "installed", "version": "17.0.1"},
            "b": {"state": "installed", "version": "17.0.1"},
            "c": {"state": "installed", "version": "17.0.1"},
        },
        views={
            "a.v": {"active": True, "inherit": None, "module": "a"},
            "b.v": {"active": True, "inherit": "a.v", "module": "b"},
        },
    )
    a = snap(
        {
            "public.t": tbl(
                {"id": "integer", "y": "character varying(16)", "z": "bigint", "new": "text"},
                1000,
                idx=[],
                cons=["t_new:c"],
            ),
            "public.shrink_w": tbl({"id": "integer"}, 90),  # -10% warning
            "public.shrink_b": tbl({"id": "integer"}, 50),  # -50% blocking
            "public.shrink_i": tbl({"id": "integer"}, 99),  # -1% info
            "public.added": tbl({"id": "integer"}, 0),
        },
        modules={
            "base": {"state": "installed", "version": "19.0.1"},
            "a": {"state": "installed", "version": "17.0.1"},  # not upgraded
            "b": {"state": "to upgrade", "version": "17.0.1"},  # transitional
            "c": {"state": "uninstalled", "version": None},
        },  # removed
        views={
            "a.v": {"active": False, "inherit": None, "module": "a"},
            "b.v": {"active": True, "inherit": "a.v", "module": "c"},
        },
    )
    findings, _ = classify(b, a)
    got = {(f["category"], f["object"]): f["severity"] for f in findings}
    expected = {
        ("table_removed", "public.gone"): "blocking",
        ("table_removed", "public.empty_gone"): "info",
        ("column_removed", "public.t.x"): "blocking",
        ("type_narrowed", "public.t.y"): "warning",
        ("type_widened", "public.t.z"): "info",
        ("column_added", "public.t.new"): "info",
        ("table_added", "public.added"): "info",
        ("index_removed", "public.t:t_idx"): "info",
        ("constraint_removed", "public.t:t_fk"): "warning",
        ("constraint_removed", "public.t:t_nn"): "info",
        ("constraint_added", "public.t:t_new"): "info",
        ("row_drop", "public.shrink_w"): "warning",
        ("row_drop", "public.shrink_b"): "blocking",
        ("row_drop", "public.shrink_i"): "info",
        ("module_not_upgraded", "module:a"): "blocking",
        ("module_transitional", "module:b"): "blocking",
        ("module_removed", "module:c"): "warning",
        ("view_deactivated", "view:a.v"): "warning",
        ("view_orphaned", "view:b.v"): "warning",
    }
    for k, sev in expected.items():
        assert got.get(k) == sev, (k, got.get(k))
    assert risk.verdict(findings) == "do_not_promote"
    sevs = [risk.SEVERITY_ORDER[f["severity"]] for f in findings]
    assert sevs == sorted(sevs)
    assert classify(b, a)[0] == findings  # deterministic


def test_estimated_drop_never_blocks():
    b = snap({"public.t": tbl({"id": "integer"}, 100, exact=False)})
    a = snap({"public.t": tbl({"id": "integer"}, 10, exact=False)})
    findings, _ = classify(b, a)
    assert findings[0]["severity"] == "warning" and "estimated" in findings[0]["message"]


def test_verdicts():
    assert risk.verdict([]) == "safe_to_promote"
    assert risk.verdict([{"severity": "warning"}]) == "review_warnings"


# ------------------------------------------------------------------ US2 planted (SC-003)


def plant_defects(pg):
    after = pg.dbs["shop_19"]
    before = pg.dbs["shop_17"]
    del after.tables["public.res_partner"].columns["vat"]  # 1 dropped col
    after.tables["public.res_partner"].columns["name"] = col("character varying(16)", False)  # 2
    after.tables["public.account_move"].rows = int(before.tables["public.account_move"].rows * 0.6)
    after.tables["public.account_move"].est = before.tables["public.account_move"].rows  # stale
    after.modules["sale"] = {"state": "installed", "version": "17.0.1.2"}  # 4 not upgr.
    after.modules["stuck"] = {"state": "to upgrade", "version": "17.0.1"}  # 5 transitional
    before.views["sale.v2"]["active"] = True
    after.views["sale.v2"] = {"active": False, "inherit": "base.v1", "module": "sale"}  # 6
    after.tables["public.res_partner"].constraints.remove("res_partner_company_fk:f")  # 7


async def test_planted_defects_full_detection(mclient, mvps):
    plant_defects(mvps.pg)
    s = await call(mclient, "db_compare", before="shop_17", after="shop_19")
    assert s["verdict"] == "do_not_promote"
    full = await call(mclient, "get_report", report_id=s["report_id"])
    got = {(f["category"], f["object"]): f["severity"] for f in full["findings"]}
    assert got[("column_removed", "public.res_partner.vat")] == "blocking"
    assert got[("type_narrowed", "public.res_partner.name")] == "warning"
    assert got[("row_drop", "public.account_move")] == "blocking"  # exact count kicked in
    assert got[("module_not_upgraded", "module:sale")] == "blocking"
    assert got[("module_transitional", "module:stuck")] == "blocking"
    assert got[("view_deactivated", "view:sale.v2")] == "warning"
    assert got[("constraint_removed", "public.res_partner:res_partner_company_fk")] == "warning"
    again = await call(mclient, "db_compare", before="shop_17", after="shop_19")
    full2 = await call(mclient, "get_report", report_id=again["report_id"])
    assert full2["findings"] == full["findings"]  # reproducible (FR-015)
    listed = await call(mclient, "list_reports", limit=5)
    assert {r["id"] for r in listed["reports"]} >= {s["report_id"], again["report_id"]}
    assert mvps.pg.pg_mutations("shop_17", "shop_19") == []


async def test_report_paging_and_filters(mclient, mvps):
    for i in range(70):
        mvps.pg.dbs["shop_19"].tables[f"public.new_{i:02d}"] = Table({"id": col("integer")}, 1)
    s = await call(mclient, "db_compare", before="shop_17", after="shop_19")
    p1 = await call(mclient, "get_report", report_id=s["report_id"])
    assert p1["pages"] >= 2 and len(p1["findings"]) == 50
    p2 = await call(mclient, "get_report", report_id=s["report_id"], page=2)
    assert p2["findings"] and p2["findings"][0] not in p1["findings"]
    info = await call(mclient, "get_report", report_id=s["report_id"], severity="blocking")
    assert all(f["severity"] == "blocking" for f in info["findings"])
    nf = await call(mclient, "get_report", report_id="rpt_000000000000")
    assert nf["error"]["category"] == "not_found"
