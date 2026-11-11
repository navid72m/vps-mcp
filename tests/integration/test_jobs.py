"""Opt-in declared-job checks on a disposable VM (T036).

Needs a policy file with a [[migration.job]] whose script is a tiny test job, e.g.:
    #!/bin/bash
    L=/root/it_job/STEPS.txt; echo "$(date '+%F %T') START" >> $L; sleep 5
    echo "$(date '+%F %T') WORK EXIT 0" >> $L; echo "$(date '+%F %T') ALL DONE" >> $L
Run: uv run pytest -m integration --vps-config <file> -k jobs
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.conftest import SessionPerCall, call
from vps_mcp.config.loader import load_config
from vps_mcp.server import build_context, create_server

pytestmark = pytest.mark.integration


@pytest.fixture
def real(request: pytest.FixtureRequest, tmp_path: Path):
    path = request.config.getoption("--vps-config")
    if not path:
        pytest.skip("--vps-config not given")
    cfg = load_config(path)
    if cfg.migration is None or not cfg.migration.job:
        pytest.skip("config declares no [[migration.job]]")
    app = build_context(cfg, db_path=tmp_path / "it.db")
    return SessionPerCall(create_server(app)), cfg.migration.job[0].name


@pytest.fixture(autouse=True)
def fast_polling() -> None:
    return None


async def test_job_plan_start_status(real):
    client, job = real
    pv = await call(client, "run_job", job=job, confirm=False)
    st = await call(client, "run_job", job=job, confirm=True, plan_id=pv["plan_id"])
    assert st["status"] == "running"
    for _ in range(60):
        s = await call(client, "job_status", run_id=st["run_id"])
        if s["status"] != "running":
            break
        await asyncio.sleep(2)
    assert s["status"] == "succeeded" and "report" in s


async def test_job_cancel(real):
    client, job = real
    pv = await call(client, "run_job", job=job, confirm=False)
    st = await call(client, "run_job", job=job, confirm=True, plan_id=pv["plan_id"])
    c = await call(client, "cancel_job", run_id=st["run_id"], confirm=True)
    assert c["status"] == "cancelled"
