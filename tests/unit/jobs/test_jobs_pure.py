"""T003 config, T019 progress parser, T015/T033 params, T012 launcher argv."""

import shlex
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tests.unit.jobs.conftest import SCRIPT, job_config, job_decl
from vps_mcp.errors import VpsMcpError
from vps_mcp.jobs import launcher, params, progress
from vps_mcp.migration.config import JobDeclaration

# ------------------------------------------------------------------ config (T003)


def bad(**over):
    with pytest.raises(ValidationError) as e:
        job_config(**over)
    return str(e.value)


def test_valid_and_defaults():
    j = job_config().migration.job[0]
    assert (j.run_as, j.max_runtime_h, j.success_marker) == ("root", 24, "ALL DONE")


@pytest.mark.parametrize("name", ["Full", "a b", "x" * 41, ""])
def test_rule1_name(name):
    assert "job name" in bad(name=name)


def test_rule2_paths():
    assert "'..'" in bad(script="/root/../etc/x.sh")
    assert "wildcards only in the file name" in bad(log_globs=["/var/*/x.log"])
    bad(script="relative.sh")


def test_rule3_databases():
    assert "not a [[migration.target]]" in bad(may_modify=["shop_19", "prod_db"])
    assert "result must be listed in may_modify" in bad(result="shop_17")


def test_rule4_services():
    assert "not a declared [[service]]" in bad(services=["postgres"])


def test_rule5_run_as():
    assert "not allowed" in bad(run_as="mallory")


@pytest.mark.parametrize(
    "pname", ["PATH", "LD_PRELOAD", "PGPASSWORD", "VPSMCP_X", "BASH_ENV", "lower", "1ABC"]
)
def test_rule6_param_names(pname):
    bad(params=[{"name": pname, "pattern": "^x$"}])


def test_rule6_param_pattern_and_default():
    assert "bad pattern" in bad(params=[{"name": "A", "pattern": "("}])
    assert "default does not match" in bad(params=[{"name": "A", "pattern": "^x$", "default": "y"}])
    job_config(params=[{"name": "A", "pattern": "^r_\\d+$", "default": "r_{date:%Y}"}])


def test_rule7_outcome_patterns():
    assert "only use groups" in bad(outcome_patterns=["(?P<foo>x)"])
    job_config(outcome_patterns=["^(?P<step>\\w+) (?P<status>ok|failed)$"])


def test_rule8_ranges():
    bad(max_runtime_h=0)
    bad(max_runtime_h=200)
    bad(min_free_gb=-1)


def test_no_jobs_still_loads():
    from tests.unit.migration.conftest import mig_config

    assert mig_config().migration.job == []


# ------------------------------------------------------------------ progress (T019)


def one(text, **kw):
    return progress.parse(text, **kw)[0]


@pytest.mark.parametrize(
    ("line", "step", "outcome"),
    [
        ("PIPELINE EXIT 0: skip_8a=1 == DONE", "PIPELINE", "ok"),
        ("RESTORE psql exit 0 , ERROR lines 3", "RESTORE psql", "ok"),
        ("DEPLOY EXIT 1 (log /x)", "DEPLOY", "failed"),
        ("STOP: no dump /x", "STOP", "failed"),
        ("STEP5 cleanup ok", "STEP5 cleanup", "ok"),
        ("STEP5 orphan views rc=0", "STEP5 orphan views", "ok"),
        ("migration FAILED badly", "migration", "failed"),
        ("ALL DONE", None, "done"),
        ("freed: old db dropped", None, None),
    ],
)
def test_progress_rules(line, step, outcome):
    pl = one(f"2026-11-10 08:21:12 {line} | free 14G")
    assert (pl.step, pl.outcome, pl.disk_free, pl.ts) == (
        step,
        outcome,
        "14G",
        "2026-11-10 08:21:12",
    )


def test_progress_reported_failures_and_no_timestamp():
    pl = one("COMPARE SUMMARY 6 PASS, 8 FAIL, 1 INFO")
    assert pl.reported_failures == 8 and pl.ts is None and pl.outcome is None
    assert one("COMPARE SUMMARY 6 PASS, 0 FAIL").reported_failures is None


def test_progress_custom_patterns_and_marker():
    pl = one(
        "phase upgrade => good",
        patterns=["^phase (?P<step>\\w+) => (?P<status>ok|failed)$", "=> good$"],
    )
    assert pl.outcome is None  # second pattern matched but sets no status
    pl = one(
        "phase upgrade => failed", patterns=["^phase (?P<step>\\w+) => (?P<status>ok|failed)$"]
    )
    assert (pl.step, pl.outcome) == ("upgrade", "failed")
    assert one("FINISHED OK", marker="FINISHED").outcome == "done"


# ------------------------------------------------------------------ params (T015/T033)

NOW = datetime(2026, 11, 11, 9, 30, tzinfo=UTC)


def job(**over):
    return JobDeclaration.model_validate(job_decl(**over))


def test_params_default_and_valid():
    assert params.resolve(job(), None, NOW) == {"RUN_NAME": "rehearsal_20261111_0930"}
    assert params.resolve(job(), {"RUN_NAME": "nightly_1"}, NOW) == {"RUN_NAME": "nightly_1"}


@pytest.mark.parametrize("value", ["bad name!", "../x", "$(id)", "a" * 300, "a\nb", "A_UPPER"])
def test_params_invalid(value):
    with pytest.raises(VpsMcpError) as e:
        params.resolve(job(), {"RUN_NAME": value}, NOW)
    assert e.value.category.value == "invalid_input"


def test_params_undeclared_and_types():
    for supplied in ({"FOO": "x"}, {"RUN_NAME": 5}, ["RUN_NAME"]):
        with pytest.raises(VpsMcpError):
            params.resolve(job(), supplied, NOW)


# ------------------------------------------------------------------ launcher argv (T012)


def test_launcher_argv_golden():
    argv = launcher.argv(job(), "job_abc", {"RUN_NAME": "it's $(x) y"})
    rd = "/var/lib/vps-mcp/jobs/job_abc"
    assert argv[:3] == ["systemd-run", "--unit", "vps-mcp-job-job_abc"]
    assert "--property=RuntimeMaxSec=86400" in argv
    assert "--property=KillMode=control-group" in argv
    assert f"--property=StandardOutput=append:{rd}/console.log" in argv
    assert f"--property=StandardError=append:{rd}/console.log" in argv
    assert "--working-directory=/root" in argv and "--setenv=VPSMCP_JOB_RUN=job_abc" in argv
    assert "--setenv=RUN_NAME=it's $(x) y" in argv  # one element, never shell-parsed
    assert not any(a.startswith("--uid") for a in argv) and "--collect" not in argv
    assert argv[-6:] == [
        "/bin/bash",
        "-c",
        launcher.WRAPPER,
        "vps-mcp-job",
        SCRIPT,
        f"{rd}/exit_code",
    ]
    # the wrapper never interpolates the script path (passed as $1)
    assert SCRIPT not in launcher.WRAPPER and shlex.split(launcher.WRAPPER)
    odoo = launcher.argv(job(run_as="odoo"), "job_x", {})
    assert "--uid=odoo" in odoo
