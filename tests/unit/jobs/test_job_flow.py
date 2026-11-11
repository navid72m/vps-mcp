"""US1 (T011, T013), US2 (T020), US4 (T025), US3 (T027), US5 (T030), US6 (T033)."""

from typing import Any

from tests.conftest import SessionPerCall, call
from tests.fakepg import Table
from tests.unit.jobs.conftest import PROGRESS, SCRIPT, append_progress, job_config, make_jvps
from vps_mcp.server import build_context, create_server

GOOD = [
    "START clean rehearsal r1",
    "PIPELINE EXIT 0: == DONE",
    "RESTORE psql exit 0 , ERROR lines 0",
    "DEPLOY EXIT 0 (log /x)",
    "COMPARE SUMMARY 6 PASS, 0 FAIL, 1 INFO",
    "ALL DONE",
]


def good_effect(lines=GOOD, code=0):
    def eff(run: dict[str, Any], pg: Any) -> int:
        append_progress(pg.vps, *lines)
        pg.mutate("shop_19", "rebuilt by job")  # declared: allowed
        return code

    return eff


async def plan_and_start(client, **params):
    pv = await call(
        client,
        "run_job",
        job="full-rehearsal",
        confirm=False,
        **({"params": params} if params else {}),
    )
    assert pv["outcome"] == "preview", pv
    st = await call(
        client,
        "run_job",
        job="full-rehearsal",
        confirm=True,
        plan_id=pv["plan_id"],
        **({"params": params} if params else {}),
    )
    return pv, st


# ------------------------------------------------------------------ US1


async def test_tools_registered(jclient, jvps, tmp_path, monkeypatch):
    from tests.unit.migration.conftest import mig_config

    names = {t.name for t in (await jclient.list_tools()).tools}
    assert {"list_jobs", "run_job", "job_status", "cancel_job"} <= names
    assert len(names) == 16 + 13 + 4
    monkeypatch.setenv("VPS_MCP_TEST_PASSPHRASE", "correct-horse-battery-staple")
    plain = build_context(mig_config(), executor=jvps, db_path=tmp_path / "p.db")
    plain_names = {t.name for t in (await SessionPerCall(create_server(plain)).list_tools()).tools}
    assert "run_job" not in plain_names


async def test_plan_changes_nothing(jclient, jvps):
    pv = await call(jclient, "run_job", job="full-rehearsal", confirm=False)
    p = pv["plan"]
    assert p["script"] == SCRIPT and len(p["script_sha256"]) == 64 and p["script_mode"] == "755"
    assert p["may_modify"][0]["db"] == "shop_19" and p["may_modify"][0]["exists"] is True
    assert p["services"] == [{"name": "worker", "kind": "os", "state": "running"}]
    assert "shop_17" in p["guarded"]["databases"] and "nginx" in p["guarded"]["services"]
    assert p["params"]["RUN_NAME"].startswith("rehearsal_") and p["running"] is None
    assert any("cannot be confined" in n for n in p["notes"])
    assert jvps.pg.job_units == {} and jvps.pg.pg_mutations() == []


async def test_start_runs_unit_with_exact_argv(jclient, jvps, japp):
    jvps.pg.job_autorun = False
    _, st = await plan_and_start(jclient, RUN_NAME="nightly_1")
    assert st["outcome"] == "success" and st["status"] == "running"
    unit = jvps.pg.job_units[st["unit"]]
    assert unit["env"]["RUN_NAME"] == "nightly_1" and unit["script"] == SCRIPT
    assert "--property=KillMode=control-group" in unit["argv"]
    run = japp.migration.app.db.execute(
        "SELECT progress_offset FROM job_runs WHERE run_id=?", (st["run_id"],)
    ).fetchone()
    assert run[0] == len(make_jvps().files[PROGRESS].encode())
    rec = (await call(jclient, "get_audit_log"))["records"][0]
    assert rec["action"] == "run_job" and rec["outcome"] == "success"


async def test_start_refusals(jclient, jvps):
    jvps.pg.job_autorun = False
    pv = await call(jclient, "run_job", job="full-rehearsal", confirm=False)
    jvps.files[SCRIPT] += "# edited\n"
    r = await call(jclient, "run_job", job="full-rehearsal", confirm=True, plan_id=pv["plan_id"])
    assert r["error"]["category"] == "conflict" and "plan_id" in r
    jvps.pg.file_meta[SCRIPT] = ("root", "777", 1)
    r = await call(jclient, "run_job", job="full-rehearsal", confirm=False)
    assert r["error"]["category"] == "not_permitted"
    jvps.pg.file_meta[SCRIPT] = ("root", "755", 1)
    jvps.pg.file_meta["/root"] = ("root", "777", 1)
    r = await call(jclient, "run_job", job="full-rehearsal", confirm=False)
    assert r["error"]["category"] == "not_permitted"
    jvps.pg.file_meta["/root"] = ("root", "700", 1)
    jvps.pg.file_meta[SCRIPT] = ("mallory", "755", 1)
    r = await call(jclient, "run_job", job="full-rehearsal", confirm=False)
    assert r["error"]["category"] == "not_permitted"
    jvps.pg.file_meta[SCRIPT] = ("root", "755", 1)
    jvps.pg.disk_free = 2 * 1024**3
    r = await call(jclient, "run_job", job="full-rehearsal", confirm=False)
    assert r["error"]["category"] == "insufficient_disk"
    jvps.pg.disk_free = 100 * 1024**3
    jvps.pg.processes[SCRIPT] = [999]  # started by hand
    pv = await call(jclient, "run_job", job="full-rehearsal", confirm=False)
    assert pv["plan"]["running"] == {"kind": "hand_started", "id": "999"}
    r = await call(jclient, "run_job", job="full-rehearsal", confirm=True, plan_id=pv["plan_id"])
    assert r["error"]["category"] == "conflict"
    assert jvps.pg.job_units == {}


async def test_one_job_at_a_time_and_rehearsal_exclusion(jclient, jvps):
    jvps.pg.job_autorun = False
    await plan_and_start(jclient)
    pv = await call(jclient, "run_job", job="full-rehearsal", confirm=False)
    r = await call(jclient, "run_job", job="full-rehearsal", confirm=True, plan_id=pv["plan_id"])
    assert r["error"]["category"] == "conflict"
    rh = await call(jclient, "rehearse", pipeline="core", source="shop_17", confirm=False)
    rs = await call(
        jclient,
        "rehearse",
        pipeline="core",
        source="shop_17",
        confirm=True,
        plan_hash=rh["plan_hash"],
    )
    assert rs["error"]["category"] == "conflict"  # jobs and rehearsals exclude each other
    assert len(jvps.pg.job_units) == 1


async def test_read_only_refuses(jvps, tmp_path, monkeypatch):
    monkeypatch.setenv("VPS_MCP_TEST_PASSPHRASE", "correct-horse-battery-staple")
    app = build_context(job_config(read_only=True), executor=jvps, db_path=tmp_path / "s.db")
    r = await call(
        SessionPerCall(create_server(app)), "run_job", job="full-rehearsal", confirm=False
    )
    assert r["error"]["category"] == "read_only" and jvps.pg.job_units == {}


# ------------------------------------------------------------------ US2


async def test_status_running_then_succeeded(jclient, jvps):
    jvps.pg.job_autorun = False
    _, st = await plan_and_start(jclient)
    append_progress(jvps, *GOOD[:3], "secret: token=abc123def456")
    s = await call(jclient, "job_status")
    assert s["status"] == "running" and s["run_id"] == st["run_id"]
    texts = [x["text"] for x in s["recent"]]
    assert texts[0].startswith("START clean rehearsal r1")  # earlier run's lines excluded
    assert not any("DEPLOY EXIT 1" in t for t in texts)
    assert [x.get("outcome") for x in s["steps"]] == ["ok", "ok"]
    assert "abc123def456" not in str(s) and s["disk_free"] == "14G"
    append_progress(jvps, *GOOD[3:])
    jvps.pg.finish_job(st["unit"], code=0)
    s = await call(jclient, "job_status", run_id=st["run_id"])
    assert s["status"] == "succeeded" and s["report"]["verdict"] in (
        "safe_to_promote",
        "review_warnings",
    )
    assert jvps.pg.job_units[st["unit"]].get("reset") is True


async def test_states_failed_and_timeout(jclient, jvps):
    jvps.pg.job_effect = good_effect(GOOD[:-1], code=0)  # exit 0 but no ALL DONE
    await plan_and_start(jclient)
    assert (await call(jclient, "job_status"))["status"] == "failed"
    jvps2 = make_jvps()
    jvps2.pg.job_autorun = False
    import tempfile
    from pathlib import Path

    app = build_context(job_config(), executor=jvps2, db_path=Path(tempfile.mkdtemp()) / "s.db")
    c = SessionPerCall(create_server(app))
    _, st = await plan_and_start(c)
    jvps2.pg.finish_job(st["unit"], result="timeout")
    s = await call(c, "job_status")
    assert s["status"] == "timed_out" and s["report"]["verdict"] == "do_not_promote"


async def test_progress_file_recreated(jclient, jvps):
    def eff(run, pg):
        pg.vps.files[PROGRESS] = "2026-11-11 10:00:00 ALL DONE\n"  # shorter than old offset
        return 0

    jvps.pg.job_effect = eff
    await plan_and_start(jclient)
    s = await call(jclient, "job_status")
    assert s["status"] == "succeeded" and s["recent"][0]["text"] == "ALL DONE"


# ------------------------------------------------------------------ US4 guard (SC-003)


async def test_guard_planted_side_effects(jclient, jvps):
    def eff(run, pg):
        append_progress(pg.vps, *GOOD)
        pg.mutate("shop_19", "declared")
        pg.mutate("shop_17", "undeclared write")  # not declared
        del pg.dbs["plain_db"]  # undeclared drop
        pg.vps.units["nginx.service"].active = "inactive"  # undeclared service stop
        pg.vps.units["worker.service"].active = "inactive"  # declared: fine
        return 0

    jvps.pg.job_effect = eff
    await plan_and_start(jclient)
    s = await call(jclient, "job_status")
    cats = {(f["category"], f["object"]): f["severity"] for f in s["guard_findings"]}
    assert cats == {
        ("undeclared_db_write", "database:shop_17"): "blocking",
        ("undeclared_db_dropped", "database:plain_db"): "blocking",
        ("undeclared_service_change", "service:os:nginx"): "blocking",
    }
    assert s["report"]["verdict"] == "do_not_promote"


async def test_guard_unattributed_write(jclient, jvps):
    def eff(run, pg):
        append_progress(pg.vps, *GOOD)
        pg.dbs["shop_17"].sessions = 2
        pg.mutate("shop_17", "someone else")
        return 0

    jvps.pg.job_effect = eff
    await plan_and_start(jclient)
    s = await call(jclient, "job_status")
    assert [(f["category"], f["severity"]) for f in s["guard_findings"]] == [
        ("undeclared_db_write_unattributed", "warning")
    ]


# ------------------------------------------------------------------ US3 report


async def test_report_row_loss_blocks(jclient, jvps):
    def eff(run, pg):
        append_progress(pg.vps, *GOOD[:4], "COMPARE SUMMARY 6 PASS, 8 FAIL, 1 INFO", "ALL DONE")
        t = pg.dbs["shop_19"].tables["public.account_move"]
        pg.dbs["shop_19"].tables["public.account_move"] = Table(t.columns, int(t.rows * 0.6))
        return 0

    jvps.pg.job_effect = eff
    await plan_and_start(jclient)
    s = await call(jclient, "job_status")
    rep = s["report"]
    assert rep["verdict"] == "do_not_promote" and rep["job_state"] == "succeeded"
    cats = {f["category"]: f["severity"] for f in rep["top_findings"]}
    assert cats["row_drop"] == "blocking" and cats["job_reported_failures"] == "warning"
    assert rep["report_excerpt"]["lines"][0] == "# Compare"
    assert "hunter22" not in str(rep)
    full = await call(jclient, "get_report", report_id=rep["report_id"])
    assert full["summary"]["report_id"] == rep["report_id"]
    again = await call(jclient, "job_status")
    assert again["report"]["report_id"] == rep["report_id"]  # built once


async def test_report_failed_job_skips_compare(jclient, jvps):
    jvps.pg.job_effect = good_effect(["START x", "DEPLOY EXIT 1 (log /x)", "STOP: deploy"], 1)
    await plan_and_start(jclient)
    rep = (await call(jclient, "job_status"))["report"]
    cats = {f["category"] for f in rep["top_findings"]}
    assert {"step_failed", "job_not_succeeded"} <= cats and rep["verdict"] == "do_not_promote"
    assert any("did not succeed" in s for s in rep["skipped_checks"])


async def test_script_changed_during_run(jclient, jvps):
    def eff(run, pg):
        append_progress(pg.vps, *GOOD)
        pg.vps.files[SCRIPT] += "# changed mid-run\n"
        return 0

    jvps.pg.job_effect = eff
    await plan_and_start(jclient)
    rep = (await call(jclient, "job_status"))["report"]
    assert "script_changed_during_run" in {f["category"] for f in rep["top_findings"]}


# ------------------------------------------------------------------ US5 cancel


async def test_cancel(jclient, jvps):
    jvps.pg.job_autorun = False
    _, st = await plan_and_start(jclient)
    append_progress(jvps, "PIPELINE EXIT 0")
    jvps.units["worker.service"].active = "inactive"  # the job stopped its declared service
    pv = await call(jclient, "cancel_job", run_id=st["run_id"])
    assert pv["outcome"] == "preview" and "nothing is repaired" in pv["warning"]
    c = await call(jclient, "cancel_job", run_id=st["run_id"], confirm=True)
    assert c["status"] == "cancelled"
    assert c["left_behind"]["services"] == {"worker": "stopped"}  # not restarted
    assert c["left_behind"]["databases"]["shop_19"]["exists"] is True
    assert jvps.pg.job_units[st["unit"]]["active"] is False
    s = await call(jclient, "job_status")
    assert s["status"] == "cancelled" and s["report"]["verdict"] == "do_not_promote"
    nf = await call(jclient, "cancel_job", run_id=st["run_id"], confirm=True)
    assert nf["error"]["category"] == "not_found"


async def test_list_jobs(jclient, jvps):
    jvps.pg.job_effect = good_effect()
    await plan_and_start(jclient)
    data = await call(jclient, "list_jobs")
    assert data["jobs"][0]["name"] == "full-rehearsal"
    assert data["jobs"][0]["params"][0]["name"] == "RUN_NAME"
    assert data["runs"][0]["status"] == "running"  # status not yet polled
