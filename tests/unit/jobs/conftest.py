from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.conftest import SessionPerCall
from tests.unit.migration.conftest import make_mvps, mig_config
from vps_mcp.server import build_context, create_server

SCRIPT = "/root/full_rehearsal.sh"
PROGRESS = "/root/rehearsal_logs/STEPS.txt"
REPORT = "/root/rehearsal_logs/compare.md"
OLD_RUN = (
    "2026-11-09 10:00:00 START clean rehearsal old | free 20G\n"
    "2026-11-09 10:05:00 DEPLOY EXIT 1 (log x) | free 19G\n"
)


def job_decl(**over: Any) -> dict[str, Any]:
    j: dict[str, Any] = {
        "name": "full-rehearsal",
        "script": SCRIPT,
        "progress_file": PROGRESS,
        "report_file": REPORT,
        "may_modify": ["shop_19"],
        "services": ["worker"],
        "source": "shop_17",
        "result": "shop_19",
        "min_free_gb": 5,
        "params": [
            {
                "name": "RUN_NAME",
                "pattern": "^[a-z0-9_]{1,40}$",
                "default": "rehearsal_{date:%Y%m%d_%H%M}",
                "description": "run name",
            }
        ],
    }
    j.update(over)
    return j


def job_config(read_only: bool = False, **over: Any) -> Any:
    kw: dict[str, Any] = {"job": [job_decl(**over)]}
    if read_only:
        kw["read_only"] = True
    return mig_config(**kw)


def make_jvps() -> Any:
    vps = make_mvps()
    vps.files[SCRIPT] = "#!/bin/bash\necho hi\n"
    vps.files[PROGRESS] = OLD_RUN
    vps.files[REPORT] = "# Compare\nSUMMARY 6 PASS, 8 FAIL\ndb password = hunter22\n"
    vps.pg.file_meta[SCRIPT] = ("root", "755", 1762600000)
    vps.pg.file_meta["/root"] = ("root", "700", 1762600000)
    return vps


def append_progress(vps: Any, *lines: str) -> None:
    vps.files[PROGRESS] = vps.files.get(PROGRESS, "") + "".join(
        f"2026-11-11 10:{i:02d}:00 {ln} | free 14G\n" for i, ln in enumerate(lines)
    )


@pytest.fixture
def jvps() -> Any:
    return make_jvps()


@pytest.fixture
def japp(jvps: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VPS_MCP_TEST_PASSPHRASE", "correct-horse-battery-staple")
    return build_context(job_config(), executor=jvps, db_path=tmp_path / "state.db")


@pytest.fixture
def jclient(japp: Any) -> SessionPerCall:
    return SessionPerCall(create_server(japp))


@pytest.fixture
def jrt(japp: Any) -> Any:
    return japp.migration
