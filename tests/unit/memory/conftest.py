from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.conftest import PASSPHRASE, SessionPerCall, make_vps
from tests.unit.jobs.conftest import job_config, make_jvps
from vps_mcp.config.policy import AppConfig
from vps_mcp.jobs.store import JobStore
from vps_mcp.memory.notes import Note
from vps_mcp.migration import risk
from vps_mcp.migration.store import MigrationStore
from vps_mcp.server import build_context, create_server


def with_memory(cfg: AppConfig, path: Path, **settings: Any) -> AppConfig:
    data = cfg.model_dump(by_alias=True)
    data["memory"] = {"path": str(path), **settings}
    return AppConfig.model_validate(data)


def note_text(name: str, type: str = "lesson", title: str | None = None, body: str = "") -> str:
    return Note(
        name=name,
        type=type,
        title=title or name.replace("-", " "),
        body=body,
        created="2026-11-01T10:00:00+00:00",
        updated="2026-11-01T10:00:00+00:00",
        source="operator",
    ).to_text()


def seed_notes(mem_dir: Path, notes: dict[str, str]) -> None:
    (mem_dir / "notes").mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, text in notes.items():
        (mem_dir / "notes" / f"{name}.md").write_text(text)


def seed_history(app: Any) -> None:
    """One finished job run with a report, one rehearsal."""
    report = risk.build_report(
        "job", {"source": "shop_17", "result": "shop_19"}, [], [], job_state="succeeded"
    )
    report["verdict"] = "review_warnings"
    MigrationStore(app.db).save_report(report)
    js = JobStore(app.db)
    js.add_plan("job_1", "full-rehearsal", "h1", {"script_sha256": "x"})
    js.update(
        "job_1",
        status="succeeded",
        started_at="2026-11-10T01:00:00+00:00",
        ended_at="2026-11-10T05:00:00+00:00",
        report_id=report["id"],
    )


@pytest.fixture
def mem_dir(tmp_path: Path) -> Path:
    return tmp_path / "myvps.memory"


@pytest.fixture
def make_app(tmp_path: Path, mem_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VPS_MCP_TEST_PASSPHRASE", PASSPHRASE)

    def make(jobs: bool = True, read_only: bool = False, vps: Any = None, **settings: Any) -> Any:
        base = job_config(read_only=read_only) if jobs else _plain(read_only)
        cfg = with_memory(base, mem_dir, **settings)
        executor = vps if vps is not None else (make_jvps() if jobs else make_vps())
        return build_context(cfg, executor=executor, db_path=tmp_path / "state.db")

    return make


def _plain(read_only: bool) -> AppConfig:
    from tests.conftest import make_config

    return make_config(read_only=True) if read_only else make_config()


@pytest.fixture
def mapp(make_app: Any) -> Any:
    return make_app()


@pytest.fixture
def mclient(mapp: Any) -> SessionPerCall:
    return SessionPerCall(create_server(mapp))
