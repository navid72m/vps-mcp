from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.conftest import SessionPerCall, config_dict, make_vps
from tests.fakepg import FakePg, odoo_db
from vps_mcp.config.policy import AppConfig
from vps_mcp.server import build_context, create_server

SQL = "/opt/mig/cleanup.sql"
SCRIPT = "/opt/mig/orphans.sh"
CHECK = "/opt/mig/check.py"
PY = "/opt/odoo/venv/bin/python3"


def migration_section(**over: Any) -> dict[str, Any]:
    m: dict[str, Any] = {
        "port": 5435,
        "approved_dirs": ["/opt/mig"],
        "target": [
            {"name": "shop_17", "purpose": "dump"},
            {"name": "shop_19", "purpose": "test"},
            {"name": "plain_db", "purpose": "reference"},
        ],
        "odoo": {
            "python": PY,
            "odoo_bin": "/opt/odoo/odoo19/odoo-bin",
            "config": "/etc/odoo.conf",
            "addons_paths": ["/opt/odoo/addons"],
        },
        "pipeline": [
            {
                "name": "core",
                "progress_file": "/root/logs/STEPS.txt",
                "log_globs": ["/var/log/odoo/upgrade_v19_*.log"],
                "steps": [
                    {"name": "cleanup", "kind": "sql", "file": SQL},
                    {"name": "orphans", "kind": "script", "file": SCRIPT, "run_as": "root"},
                    {"name": "upgrade-all", "kind": "odoo_upgrade", "modules": "all"},
                    {"name": "re-upgrade", "kind": "odoo_upgrade", "modules": "stale", "rounds": 2},
                ],
            }
        ],
        "check": [
            {
                "name": "post-checks",
                "command": [PY, CHECK, "--db", "{db}"],
                "run_as": "odoo",
                "access": "read_only",
                "blocking": True,
            },
            {
                "name": "fixer",
                "command": [PY, CHECK, "--fix", "--db={db}"],
                "run_as": "odoo",
                "access": "may_modify",
            },
        ],
    }
    m.update(over)
    return m


def mig_config(**over: Any) -> AppConfig:
    d = config_dict(**{k: v for k, v in over.items() if k == "read_only"})
    d["migration"] = migration_section(**{k: v for k, v in over.items() if k != "read_only"})
    return AppConfig.model_validate(d)


def make_mvps() -> Any:
    vps = make_vps()
    pg = FakePg(vps)
    pg.dbs["shop_17"] = odoo_db("17")
    pg.dbs["shop_19"] = odoo_db("19")
    plain = odoo_db("17")
    plain.modules = None
    plain.views = None
    plain.tables = {k: v for k, v in plain.tables.items() if not k.startswith("public.ir_")}
    pg.dbs["plain_db"] = plain
    pg.dbs["secret_db"] = odoo_db("17")  # exists but NOT allowlisted
    pg.dirs["/opt/odoo/addons"] = ["base", "sale"]
    vps.files[SQL] = "DELETE FROM res_partner WHERE active = false;\n"
    vps.files[SCRIPT] = "#!/bin/bash\n$PSQL -c 'select 1'\n"
    vps.pg = pg
    return vps


@pytest.fixture
def mvps() -> Any:
    return make_mvps()


@pytest.fixture
def mapp(mvps: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VPS_MCP_TEST_PASSPHRASE", "correct-horse-battery-staple")
    return build_context(mig_config(), executor=mvps, db_path=tmp_path / "state.db")


@pytest.fixture
def mclient(mapp: Any) -> SessionPerCall:
    return SessionPerCall(create_server(mapp))


@pytest.fixture
def rt(mapp: Any) -> Any:
    return mapp.migration
