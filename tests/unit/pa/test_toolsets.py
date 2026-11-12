"""Grouping, catalog, scoring (feature 008): pure functions over tool specs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assistant import toolsets
from assistant.config import Config, load
from assistant.providers import ToolSpec

ROOT = Path(__file__).resolve().parents[3]
BUILTIN = [
    "memory__search",
    "memory__get",
    "schedule__add",
    "schedule__list",
    "schedule__remove",
    "schedule__pause",
    "schedule__resume",
    "assistant__switch_model",
    "tools__load",
    *(f"files__{x}" for x in "read write edit list search info delete send".split()),
]


def example() -> Config:
    return load(ROOT / "examples" / "assistant.toml")


def specs(cfg: Config) -> list[ToolSpec]:
    never = {k for k, v in cfg.server[0].policy.items() if v == "never"}
    tools = json.loads((ROOT / "tests" / "fixtures" / "vps_tools.json").read_text())
    out = [
        ToolSpec("vps__" + t["name"], t["description"], {"type": "object"})
        for t in tools
        if t["name"] not in never
    ]
    out += [ToolSpec(n, n.replace("__", " "), {"type": "object"}) for n in BUILTIN]
    return out


def catalog(cfg: Config | None = None, up: bool = True) -> toolsets.Catalog:
    cfg = cfg or example()
    return toolsets.build(cfg, specs(cfg), {"vps": up})


def test_example_grouping() -> None:
    c = catalog()
    assert c.core[:6] == list(toolsets.BUILTIN_CORE)
    assert {"vps__list_services", "vps__get_service_status"} <= set(c.core)
    assert set(c.sets) == {
        "db", "deploy", "files", "gitlab", "jobs", "memory-admin", "migration", "schedules",
        "services",
    }  # fmt: skip
    assert c.toolset_of("vps__gitlab_tree") == "gitlab"
    assert c.toolset_of("vps__list_services") is None  # core
    every = [t for ts in c.sets.values() for t in ts.tools] + c.core
    assert sorted(every) == sorted(s.name for s in specs(example()))  # nothing lost or doubled
    assert not c.warnings


def test_never_tools_are_nowhere() -> None:
    c = catalog()
    names = set(c.core) | {t for ts in c.sets.values() for t in ts.tools}
    assert "vps__remove_service" not in names and "vps__deploy_service" not in names
    assert "remove_service" not in c.render()


def test_catalog_lines_are_short_sorted_and_stable() -> None:
    a, b = catalog().render(), catalog().render()
    assert a == b
    lines = [line for line in a.splitlines() if line.startswith("- ")]
    assert [line.split()[1] for line in lines] == sorted(line.split()[1] for line in lines)
    assert all(len(line) <= toolsets.LINE_CAP for line in lines)


def test_tool_list_is_core_then_loaded_in_catalog_order() -> None:
    c = catalog()
    names = [s.name for s in c.tool_specs(["gitlab", "db"])]
    assert names[: len(c.core)] == c.core
    rest = names[len(c.core) :]
    assert rest == c.sets["db"].tools + c.sets["gitlab"].tools  # db sorts before gitlab
    assert c.size() < c.size(["gitlab"]) < c.full_size()


def test_down_server_toolsets_are_unavailable() -> None:
    cfg = example()
    c = toolsets.build(
        cfg, [s for s in specs(cfg) if not s.name.startswith("vps__")], {"vps": False}
    )
    assert not c.sets["gitlab"].available and "[unavailable]" in c.render()
    assert c.prefetch("why did the pipeline fail", [], 2.0, 2) == []


def test_core_wins_over_toolsets_and_leftovers_are_kept(tmp_path: Path) -> None:
    text = (ROOT / "examples" / "assistant.toml").read_text()
    text = text.replace(
        'core = ["list_services", "get_service_status", "get_resource_usage"]',
        'core = ["list_services", "db_overview"]',
    ).replace(
        'tools = ["list_jobs", "run_job", "job_status", "cancel_job"]', 'tools = ["nothing_*"]'
    )
    p = tmp_path / "a.toml"
    p.write_text(text)
    cfg = load(p)
    c = toolsets.build(cfg, specs(cfg), {"vps": True})
    assert "vps__db_overview" in c.core and "vps__db_overview" not in c.sets["db"].tools
    assert "jobs" not in c.sets  # matched nothing
    assert {"vps__run_job", "vps__get_service_status"} <= set(c.sets["vps-more"].tools)
    assert any("'jobs' matches no tool" in w for w in c.warnings)


def test_unconfigured_server_is_one_toolset(tmp_path: Path) -> None:
    from tests.assistant_fakes import config

    cfg = config(tmp_path)  # server "ops" with no groups
    sp = [ToolSpec(f"ops__{n}", n, {"type": "object"}) for n in ("status", "restart")]
    c = toolsets.build(cfg, sp + [toolsets.LOAD_SPEC], {"ops": True})
    assert c.sets["ops"].tools == ["ops__status", "ops__restart"]
    assert c.sets["ops"].description == "tools: status, restart"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("why did the pipeline fail on the addons repo?", "gitlab"),
        ("roll back the last deploy", "deploy"),
        ("restart odoo and show me its logs", "services"),
        ("cancel my 9pm reminder", "schedules"),
    ],
)
def test_scoring_picks_the_obvious_toolset(message: str, expected: str) -> None:
    picked = catalog().prefetch(message, [], 2.0, 2)
    assert picked and picked[0][0] == expected
    assert picked[0][2]  # matched words are reported


def test_threshold_limit_and_earlier_messages() -> None:
    c = catalog()
    assert c.prefetch("thanks!", [], 2.0, 2) == []
    assert c.prefetch("roll back the last deploy", [], 2.0, 0) == []
    assert c.prefetch("roll back the last deploy", [], 1000.0, 2) == []
    follow = c.prefetch("and the one before that?", ["why did the pipeline fail?"], 2.0, 2)
    assert [p[0] for p in follow][:1] == ["gitlab"]  # earlier words count at half weight


def test_common_words_do_not_load_everything() -> None:
    c = catalog()
    for msg in ("what's the status", "list everything", "show me the service"):
        assert len(c.prefetch(msg, [], 2.0, 5)) <= 1, msg


@pytest.mark.parametrize(
    "extra",
    [
        '[server.toolsets."Bad Name"]\ntools = ["x"]\n',
        '[server.toolsets.files]\ntools = ["x"]\n',  # clashes with a built-in toolset
        "[server.toolsets.empty]\ntools = []\n",
        "[tools]\nmax_auto = 9\n",
        "[tools]\nidle_turns = 0\n",
    ],
)
def test_toolset_config_errors(tmp_path: Path, extra: str) -> None:
    from assistant.config import ConfigError
    from tests.unit.pa.test_core import GOOD, write

    with pytest.raises(ConfigError):
        load(write(tmp_path, GOOD + "\n" + extra))


@pytest.mark.parametrize("name", ["tools", "files", "schedule"])
def test_builtin_server_names_are_reserved(tmp_path: Path, name: str) -> None:
    from assistant.config import ConfigError
    from tests.unit.pa.test_core import GOOD, write

    with pytest.raises(ConfigError):
        load(write(tmp_path, GOOD.replace('name = "vps"', f'name = "{name}"')))
