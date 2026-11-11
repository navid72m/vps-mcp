"""SC-004: no declared job starts unless every check passes (T034)."""

from typing import Any

import pytest

from tests.conftest import SessionPerCall, call
from tests.unit.jobs.conftest import SCRIPT, job_config, make_jvps
from vps_mcp.server import build_context, create_server

INJECT = ["x; reboot", "$(id)", "../full-rehearsal", "FULL", "", "a" * 60]


def setup(tmp_path, monkeypatch, read_only=False, mutate=None):
    monkeypatch.setenv("VPS_MCP_TEST_PASSPHRASE", "correct-horse-battery-staple")
    vps = make_jvps()
    vps.pg.job_autorun = False
    if mutate:
        mutate(vps)
    app = build_context(job_config(read_only=read_only), executor=vps, db_path=tmp_path / "s.db")
    return vps, SessionPerCall(create_server(app))


def no_units(vps: Any) -> None:
    assert vps.pg.job_units == {}


SIMPLE: list[tuple[str, dict[str, Any], str]] = []
for s in INJECT:
    SIMPLE.append(("run_job", {"job": s, "confirm": True, "plan_id": "x"}, "not_found"))
for v in ["bad name!", "$(id)", "a" * 300, "a\nb", "../x"]:
    SIMPLE.append(
        (
            "run_job",
            {"job": "full-rehearsal", "confirm": False, "params": {"RUN_NAME": v}},
            "invalid_input",
        )
    )
SIMPLE += [
    (
        "run_job",
        {"job": "full-rehearsal", "confirm": False, "params": {"PATH": "/tmp"}},
        "invalid_input",
    ),
    ("run_job", {"job": "full-rehearsal", "confirm": True}, "conflict"),  # no plan_id
    ("run_job", {"job": "full-rehearsal", "confirm": True, "plan_id": "0" * 16}, "conflict"),
    ("cancel_job", {"run_id": "job_nope", "confirm": True}, "not_found"),
    ("cancel_job", {"run_id": "x; reboot", "confirm": True}, "not_found"),
    ("job_status", {"run_id": "job_nope"}, "not_found"),
]


@pytest.mark.parametrize(
    ("tool", "args", "category"), SIMPLE, ids=[f"{i}-{c[0]}" for i, c in enumerate(SIMPLE)]
)
async def test_simple_refusals(tmp_path, monkeypatch, tool, args, category):
    vps, client = setup(tmp_path, monkeypatch)
    data = await call(client, tool, **args)
    assert data["error"]["category"] == category, data["error"]
    no_units(vps)


def _meta(path, owner, mode):
    def m(vps):
        vps.pg.file_meta[path] = (owner, mode, 1)

    return m


STATE: list[tuple[str, Any, str]] = [
    ("script world-writable", _meta(SCRIPT, "root", "777"), "not_permitted"),
    ("script group-writable", _meta(SCRIPT, "root", "775"), "not_permitted"),
    ("dir world-writable", _meta("/root", "root", "777"), "not_permitted"),
    ("wrong owner", _meta(SCRIPT, "mallory", "755"), "not_permitted"),
    ("script missing", lambda v: v.pg.file_meta.pop(SCRIPT), "invalid_input"),
    ("low disk", lambda v: setattr(v.pg, "disk_free", 1024**3), "insufficient_disk"),
]


@pytest.mark.parametrize(("label", "mutate", "category"), STATE, ids=[s[0] for s in STATE])
async def test_state_refusals(tmp_path, monkeypatch, label, mutate, category):
    vps, client = setup(tmp_path, monkeypatch, mutate=mutate)
    data = await call(client, "run_job", job="full-rehearsal", confirm=False)
    assert data["error"]["category"] == category, (label, data["error"])
    no_units(vps)


async def test_changed_after_plan(tmp_path, monkeypatch):
    vps, client = setup(tmp_path, monkeypatch)
    pv = await call(client, "run_job", job="full-rehearsal", confirm=False)
    vps.files[SCRIPT] += "# edit\n"
    data = await call(client, "run_job", job="full-rehearsal", confirm=True, plan_id=pv["plan_id"])
    assert data["error"]["category"] == "conflict"
    no_units(vps)


@pytest.mark.parametrize("busy", ["hand_started", "rehearsal_unit"])
async def test_busy_refusals(tmp_path, monkeypatch, busy):
    vps, client = setup(tmp_path, monkeypatch)
    pv = await call(client, "run_job", job="full-rehearsal", confirm=False)
    if busy == "hand_started":
        vps.pg.processes[SCRIPT] = [123]
    else:
        vps.pg.units["vps-mcp-rehearsal-rhs_x"] = {"active": True, "events": "/x", "dir": "/x"}
    data = await call(client, "run_job", job="full-rehearsal", confirm=True, plan_id=pv["plan_id"])
    assert data["error"]["category"] == "conflict"
    no_units(vps)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("run_job", {"job": "full-rehearsal", "confirm": False}),
        ("run_job", {"job": "full-rehearsal", "confirm": True, "plan_id": "x"}),
    ],
)
async def test_read_only(tmp_path, monkeypatch, tool, args):
    vps, client = setup(tmp_path, monkeypatch, read_only=True)
    data = await call(client, tool, **args)
    assert data["error"]["category"] == "read_only"
    no_units(vps)


def test_suite_size():
    assert len(SIMPLE) + len(STATE) + 1 + 2 + 2 >= 20
