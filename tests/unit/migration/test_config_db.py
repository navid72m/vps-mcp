"""Foundation: config validation (T003), catalogue + read-only runner (T004), CLI (T011)."""

import pytest
from pydantic import ValidationError

from tests.conftest import call
from tests.unit.migration.conftest import CHECK, PY, SQL, mig_config, migration_section
from vps_mcp import cli
from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.migration import catalog


def bad(**over):
    with pytest.raises(ValidationError) as exc:
        mig_config(**over)
    return str(exc.value)


def test_valid_and_optional():
    cfg = mig_config()
    assert cfg.migration.port == 5435 and cfg.migration.min_free_gb == 10
    assert (
        cfg.migration.copy_retention_h == 24 and cfg.migration.thresholds.row_drop_block_pct == 20
    )
    from tests.conftest import make_config

    assert make_config().migration is None  # feature 001 configs unchanged


@pytest.mark.parametrize("name", ["Shop", "vmcp_x", "a-b", "x" * 64, "1abc;drop"])
def test_rule1_target_names(name):
    assert "database name" in bad(target=[{"name": name}])


def test_rule1_unique_targets():
    assert "unique" in bad(target=[{"name": "a"}, {"name": "a"}])


def test_rule2_paths_inside_approved_dirs():
    m = migration_section()
    m["pipeline"][0]["steps"][0]["file"] = "/etc/passwd"
    assert "outside approved_dirs" in bad(pipeline=m["pipeline"])
    m = migration_section()
    m["pipeline"][0]["steps"][0]["file"] = "/opt/mig/../../etc/shadow"
    assert "'..'" in bad(pipeline=m["pipeline"])


def test_rule3_placeholders():
    assert "not allowed" in bad(check=[{"name": "c", "command": [PY, CHECK, "{user}"]}])
    assert "whole argument" in bad(check=[{"name": "c", "command": [PY, CHECK, "x{db}y"]}])
    mig_config(check=[{"name": "c", "command": [PY, CHECK, "--db={db}", "{port}"]}])


def test_rule4_odoo_steps():
    assert "needs [migration.odoo]" in bad(odoo=None)
    m = migration_section()
    m["pipeline"][0]["steps"][2]["rounds"] = 2
    assert "rounds only with" in bad(pipeline=m["pipeline"])
    m = migration_section()
    m["pipeline"][0]["steps"][3]["rounds"] = 9
    bad(pipeline=m["pipeline"])


def test_rule5_run_as():
    m = migration_section()
    m["pipeline"][0]["steps"][1]["run_as"] = "mallory"
    assert "not allowed" in bad(pipeline=m["pipeline"])
    mig_config(pipeline=m["pipeline"], allowed_run_as=["mallory"])


def test_rule6_caps():
    steps = [{"name": f"s{i}", "kind": "sql", "file": SQL} for i in range(31)]
    bad(pipeline=[{"name": "big", "steps": steps}])


def test_rule7_thresholds():
    assert "thresholds" in bad(thresholds={"row_drop_warn_pct": 30, "row_drop_block_pct": 20})


def test_unknown_keys_and_check_interpreter():
    bad(typo=1)
    assert "must be [migration.odoo].python" in bad(
        check=[{"name": "c", "command": ["/usr/bin/bash", "-c", "x"]}]
    )


def test_identifier_helpers():
    assert catalog.split_table("res_partner") == ("public", "res_partner")
    assert catalog.split_table("audit.log_x") == ("audit", "log_x")
    for v in ["Res", "a;b", "a.b.c", "x'--", ""]:
        with pytest.raises(VpsMcpError):
            catalog.split_table(v)


async def test_query_golden_argv_readonly_no_sudo(rt, mvps):
    await rt.db.query("shop_17", "table", {"sch": "public", "tbl": "res_partner"})
    c = [c for c in mvps.calls if c.argv[0] == "psql"][-1]
    assert c.argv == [
        "psql",
        "-X",
        "-p",
        "5435",
        "-d",
        "shop_17",
        "-v",
        "ON_ERROR_STOP=1",
        "-At",
        "-v",
        "sch=public",
        "-v",
        "tbl=res_partner",
        "-f",
        "-",
    ]
    assert c.sudo is False and c.stdin == catalog.Q_TABLE
    assert c.env["PGOPTIONS"].startswith(
        "-c default_transaction_read_only=on -c statement_timeout="
    )


async def test_query_error_maps_to_failed(rt):
    with pytest.raises(VpsMcpError) as exc:
        await rt.db.query("nonexistent", "overview")
    assert exc.value.category is ErrorCategory.FAILED


async def test_not_permitted_identical_wording(mclient):
    a = await call(mclient, "db_overview", db="secret_db")  # exists, not allowlisted
    b = await call(mclient, "db_overview", db="no_such_db")
    assert a["error"]["category"] == b["error"]["category"] == "not_permitted"
    assert a["error"]["message"].replace("secret_db", "X") == b["error"]["message"].replace(
        "no_such_db", "X"
    )


def test_cli_migration_material(capsys):
    assert cli.main(["bootstrap", "--print-sudoers", "--with-migration"]) == 0
    out = capsys.readouterr().out
    assert "ALL=(postgres) NOPASSWD: VPSMCP_PG" in out and "/usr/bin/systemd-run" in out
    assert 'env_keep += "PGOPTIONS"' in out
    assert cli.main(["bootstrap", "--print-migration-sql"]) == 0
    sql = capsys.readouterr().out
    assert "GRANT pg_read_all_data, pg_read_all_stats TO vpsmcp" in sql
    assert "NOSUPERUSER NOCREATEDB" in sql


def test_catalog_sql_is_clean():
    # '#' is not a SQL comment: a stray lint suppression inside a query would break it.
    for name, sql in catalog.CATALOG.items():
        assert "#" not in sql and "noqa" not in sql, name
