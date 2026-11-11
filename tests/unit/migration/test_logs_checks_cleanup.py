"""US4 (T032), US5 (T034), US6 (T036)."""

from datetime import UTC, datetime, timedelta

from tests.conftest import call
from tests.unit.migration.conftest import CHECK
from vps_mcp.migration import copies
from vps_mcp.migration.pipeline_logs import normalise, parse_progress, summarise_log_lines

STEPS = """START Tue Nov  3 09:24:26 AM +0000 2026
STEP5 cleanup ok
STEP5 orphan views rc=0
STEP6 -u all START Tue Nov  3 09:25:00 AM +0000 2026
STEP6 -u all EXIT 1 Tue Nov  3 11:00:00 AM +0000 2026
"""
LOG = "\n".join(
    [
        "2026-11-03 09:30:01,123 4242 ERROR shop_19_initial odoo.modules.loading: Failed to load "
        "module 'hr_payroll' (id 1234)",
        "2026-11-03 09:31:01,123 4242 ERROR shop_19_initial odoo.modules.loading: Failed to load "
        "module 'website_x' (id 99)",
        "2026-11-03 09:32:01,123 4242 WARNING shop_19_initial odoo.fields: Field x.y has 3 issues",
        "2026-11-03 09:33:01,123 4242 CRITICAL shop_19_initial odoo.service: Failed to initialize",
        "2026-11-03 09:34:01,123 4242 ERROR shop_19_initial odoo.sql_db: bad query: password=s3cret",
        "not a log line",
    ]
)


def test_progress_parsing():
    p = parse_progress(STEPS)
    assert p["started"].startswith("Tue Nov")
    assert [(s["name"], s["status"]) for s in p["steps"]] == [
        ("STEP5 cleanup", "ok"),
        ("STEP5 orphan views", "ok"),
        ("STEP6 -u all", "running"),
        ("STEP6 -u all", "failed"),
    ]


def test_log_summary_and_normalisation():
    s = summarise_log_lines(LOG.splitlines())
    assert s["count_by_level"] == {"CRITICAL": 1, "ERROR": 3, "WARNING": 1}
    assert s["top"][0]["level"] == "CRITICAL"
    load = next(g for g in s["top"] if "loading" in g["message"])
    assert load["count"] == 2 and "'…'" in load["message"] and "#" in load["message"]
    assert normalise("id 42 at 0xff 'abc'") == "id # at # '…'"


async def test_pipeline_last_run(mclient, mvps):
    mvps.files["/root/logs/STEPS.txt"] = STEPS
    mvps.files["/var/log/odoo/upgrade_v19_all.log"] = LOG
    mvps.files["/var/log/odoo/other.log"] = LOG
    mvps.pg.dirs["/var/log/odoo"] = ["upgrade_v19_all.log", "other.log"]
    data = await call(mclient, "pipeline_last_run", pipeline="core")
    assert data["log_files"] == ["/var/log/odoo/upgrade_v19_all.log"]
    assert data["steps"][-1]["status"] == "failed"
    assert data["errors"]["count_by_level"]["ERROR"] == 3
    assert "s3cret" not in str(data)  # secret-looking values masked


async def test_read_only_check_and_report_attach(mclient, mvps):
    seen = {}

    def chk(argv, env, pg):
        seen.update(argv=argv, env=env)
        return 1, "2 problems found\npassword = hunter2"

    mvps.pg.check_effects[CHECK] = chk
    s = await call(mclient, "db_compare", before="shop_17", after="shop_19")
    r = await call(mclient, "run_check", check="post-checks", db="shop_19")
    assert r["status"] == "fail" and r["report_id"] == s["report_id"]
    assert seen["argv"][-2:] == ["--db", "shop_19"]
    assert "default_transaction_read_only=on" in seen["env"]["PGOPTIONS"]
    assert mvps.pg.check_calls[-1]["user"] == "odoo"
    assert "hunter2" not in str(r)
    rep = await call(mclient, "get_report", report_id=s["report_id"])
    assert rep["summary"]["verdict"] == "do_not_promote"  # blocking=true check failed


async def test_check_writes_detected(mclient, mvps):
    def sneaky(argv, env, pg):
        pg.mutate("shop_17", "check wrote")
        return 0, "ok"

    mvps.pg.check_effects[CHECK] = sneaky
    r = await call(mclient, "run_check", check="post-checks", db="shop_19")
    assert r["foreign_writes"][0]["database"] == "shop_17"
    assert r["findings"][0]["category"] == "foreign_write"


async def test_may_modify_check_needs_copy(mclient, mvps):
    mvps.pg.check_effects[CHECK] = lambda a, e, pg: (0, "fixed")
    r = await call(mclient, "run_check", check="fixer", db="shop_19", confirm=True)
    assert r["error"]["category"] == "not_permitted"
    assert mvps.pg.check_calls == []


async def make_copy(mclient):
    pv = await call(mclient, "rehearse", pipeline="core", source="shop_17", confirm=False)
    st = await call(
        mclient,
        "rehearse",
        pipeline="core",
        source="shop_17",
        confirm=True,
        plan_hash=pv["plan_hash"],
    )
    await call(mclient, "rehearsal_status", rehearsal_id=st["rehearsal_id"])
    return pv["plan"]["copy_name"]


async def test_may_modify_check_on_copy(mclient, mvps):
    copy = await make_copy(mclient)
    mvps.pg.check_effects[CHECK] = lambda a, e, pg: (0, "fixed")
    pv = await call(mclient, "run_check", check="fixer", db=copy)
    assert pv["error"]["category"] == "confirmation_required"
    r = await call(mclient, "run_check", check="fixer", db=copy, confirm=True)
    assert r["status"] == "pass" and r["outcome"] == "success"
    assert mvps.pg.check_calls[-1]["argv"][-1] == f"--db={copy}"


async def test_drop_copies(mclient, mvps, rt):
    copy = await make_copy(mclient)
    row = rt.store.copy_by_name(copy)
    pv = await call(mclient, "drop_copies", copy_ids=[row["copy_id"]], confirm=False)
    assert pv["will_drop"][0]["name"] == copy and pv["reclaim_bytes"] > 0
    r = await call(mclient, "drop_copies", copy_ids=[row["copy_id"]], confirm=True)
    assert r["dropped"] == [copy] and copy not in mvps.pg.dbs
    assert (await call(mclient, "db_list"))["copies"] == []
    r = await call(mclient, "db_overview", db=copy)
    assert r["error"]["category"] == "not_permitted"


async def test_forged_marker_never_dropped(mclient, mvps, rt):
    copy = await make_copy(mclient)
    mvps.pg.dbs[copy].comment = "vps-mcp-copy:forged"
    row = rt.store.copy_by_name(copy)
    r = await call(mclient, "drop_copies", copy_ids=[row["copy_id"]], confirm=True)
    assert r["error"]["category"] == "not_permitted"
    assert r["refused"][0]["reason"] == "marker_mismatch" and copy in mvps.pg.dbs
    # a real database with a copy-like name that we never created is not even addressable
    from tests.fakepg import odoo_db

    mvps.pg.dbs["vmcp_fake_202601010000_abcd"] = odoo_db()
    r = await call(mclient, "drop_copies", copy_ids=["cpy_doesnotexist"], confirm=True)
    assert "vmcp_fake_202601010000_abcd" in mvps.pg.dbs


async def test_expiry_sweep(mclient, mvps, rt):
    copy = await make_copy(mclient)
    row = rt.store.copy_by_name(copy)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="milliseconds")
    rt.store.conn.execute(
        "UPDATE migration_copies SET expires_at = ? WHERE copy_id = ?", (past, row["copy_id"])
    )
    pv = await call(mclient, "drop_copies", expired_only=True, confirm=False)
    assert [c["name"] for c in pv["will_drop"]] == [copy]
    assert await copies.sweep(rt) == [copy]
    assert copy not in mvps.pg.dbs
