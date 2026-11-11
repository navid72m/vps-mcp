"""Archive sanitiser (T023) and deploy / rollback (T025)."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import SessionPerCall, call
from tests.fakegitlab import SHA_A, SHA_B, SHA_C, FakeGitLab
from tests.unit.gitlab.conftest import DEST, gerr
from vps_mcp.errors import VpsMcpError
from vps_mcp.gitlab import archive
from vps_mcp.server import create_server

ROOT = "/var/lib/vps-mcp/deploys/addons/releases"


def tgz(members: list[tuple[str, Any]], top: str = "proj-x") -> Path:
    import tempfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, spec in members:
            info = tarfile.TarInfo(f"{top}/{name}" if not name.startswith("/") else name)
            if isinstance(spec, bytes):
                info.size = len(spec)
                tar.addfile(info, io.BytesIO(spec))
            else:
                kind, val = spec
                info.type = {
                    "sym": tarfile.SYMTYPE,
                    "lnk": tarfile.LNKTYPE,
                    "fifo": tarfile.FIFOTYPE,
                    "chr": tarfile.CHRTYPE,
                }[kind]
                info.linkname = val
                tar.addfile(info)
    p = Path(tempfile.mkdtemp()) / "a.tar.gz"
    p.write_bytes(buf.getvalue())
    return p


def members(clean: archive.Clean) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(clean.tar_bytes)) as t:
        return {m.name: m for m in t.getmembers()}


def sanitize(
    src: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    cap: int = 10 << 20,
) -> archive.Clean:
    return archive.sanitize(
        src,
        include=include or [],
        exclude=exclude or [],
        cap_bytes=cap,
        release_dir="/rel",
        marker={"sha": "x"},
    )


def test_sanitize_strips_and_filters() -> None:
    src = tgz(
        [
            ("a.py", b"x"),
            ("sub/b.sh", b"y"),
            ("tests/t.py", b"z"),
            ("docs/x.md", b"d"),
            ("../evil", b"e"),
            ("ok-link", ("sym", "sub/b.sh")),
            ("bad-link", ("sym", "../../etc")),
            ("abs-link", ("sym", "/etc/passwd")),
            ("hard", ("lnk", "proj-x/a.py")),
            ("pipe", ("fifo", "")),
            ("dev", ("chr", "")),
        ]
    )
    c = sanitize(src, exclude=["tests/**", "*.md"])
    m = members(c)
    assert set(c.files) == {"a.py", "sub/b.sh"}
    assert "ok-link" in m and m["ok-link"].issym()
    skipped = dict(c.skipped)
    assert skipped["bad-link"] == "symlink leaves the tree"
    assert skipped["abs-link"] == "symlink leaves the tree"
    assert skipped["hard"] == skipped["pipe"] == skipped["dev"] == "hard link or special file"
    assert all(i.uid == 0 and i.gid == 0 for i in m.values())
    manifest = c.tar_bytes and m[archive.MANIFEST]
    assert manifest
    with tarfile.open(fileobj=io.BytesIO(c.tar_bytes)) as t:
        text = t.extractfile(archive.MANIFEST).read().decode()  # type: ignore[union-attr]
    assert "  /rel/a.py" in text and "  /rel/sub/b.sh" in text


def test_include_globs() -> None:
    src = tgz([("run.sh", b"1"), ("lib/x.py", b"2"), ("sql/a/b.sql", b"3"), ("README", b"4")])
    c = sanitize(src, include=["*.sh", "*.py", "sql/**"])
    assert sorted(c.files) == ["lib/x.py", "run.sh", "sql/a/b.sql"]


def test_modes_normalised() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, mode in (("x.sh", 0o4777), ("y.txt", 0o666)):
            info = tarfile.TarInfo(f"top/{name}")
            info.size, info.mode, info.uid = 1, mode, 1234
            tar.addfile(info, io.BytesIO(b"1"))
    p = Path(__import__("tempfile").mkdtemp()) / "a.tgz"
    p.write_bytes(buf.getvalue())
    m = members(sanitize(p))
    assert m["x.sh"].mode == 0o755 and m["y.txt"].mode == 0o644


def test_expansion_limit() -> None:
    src = tgz([("big.bin", b"\0" * (5 << 20))])
    with pytest.raises(VpsMcpError):
        sanitize(src, cap=1 << 20)


# ---------------------------------------------------------------------- deploy


def deploy_calls(vps: Any) -> list[list[str]]:
    return [c.argv for c in vps.mutating_calls()]


async def plan(client: SessionPerCall, ref: str = "v1.0") -> dict[str, Any]:
    res = await call(client, "deploy_from_gitlab", target="addons", ref=ref, confirm=False)
    assert res["preview"] is True
    return dict(res["plan"])


async def confirm(client: SessionPerCall, p: dict[str, Any], ref: str = "v1.0") -> dict[str, Any]:
    return await call(
        client, "deploy_from_gitlab", target="addons", ref=ref, confirm=True, plan_id=p["plan_id"]
    )


async def test_first_deploy(gapp: Any, gclient: SessionPerCall, gvps: Any) -> None:
    p = await plan(gclient)
    assert deploy_calls(gvps) == []  # planning changes nothing
    assert p["commit"]["sha"] == SHA_A and p["ref"] == "tag:v1.0"
    assert p["current"] == {"exists": False, "managed": False}
    assert "A stock_fix/models.py" in p["changes"]["files"]
    assert not any("tests/" in f for f in p["changes"]["files"])  # excluded
    assert p["pipeline"] == {"id": 700, "status": "success"}
    res = await confirm(gclient, p)
    assert res["status"] == "succeeded" and res["commit"]["sha"] == SHA_A
    release = f"{ROOT}/{res['release']}"
    assert gvps.links[DEST] == release
    assert gvps.files[f"{release}/stock_fix/models.py"] == "x = 1\n"
    assert f"{release}/tests/test_x.py" not in gvps.files
    assert '"sha": "' + SHA_A in gvps.files[f"{release}/{archive.MARKER}"]
    assert gvps.owners[release] == "worker:worker"
    assert ["systemctl", "restart", "worker.service"] in [c.argv[:3] for c in gvps.calls]
    tar_call = next(c for c in gvps.calls if c.argv[0] == "tar")
    assert tar_call.stdin_bytes and tar_call.sudo
    row = gapp.db.execute("SELECT * FROM gitlab_deploys WHERE status='succeeded'").fetchone()
    assert row["commit_sha"] == SHA_A and row["release_dir"] == release
    audit = gapp.db.execute("SELECT * FROM audit_records WHERE action='deploy'").fetchone()
    assert audit["outcome"] == "success" and audit["kind"] == "gitlab"
    assert audit["service_name"] == "addons"
    t = await call(gclient, "deploy_targets")
    assert t["targets"][0]["current"]["sha"] == SHA_A


async def test_upgrade_local_changes_and_rollback(gclient: SessionPerCall, gvps: Any) -> None:
    await confirm(gclient, await plan(gclient))
    first = gvps.links[DEST]
    gvps.files[f"{first}/stock_fix/models.py"] = "hand edit\n"
    p = await plan(gclient, "v2.0")
    assert p["current"]["sha"] == SHA_A
    assert p["current"]["local_changes"] == ["stock_fix/models.py"]
    assert "M stock_fix/models.py +1 -1" in p["changes"]["files"]
    assert "A stock_fix/new.py +1 -0" in p["changes"]["files"]
    assert any("changed on the VPS" in w for w in p["warnings"])
    res = await confirm(gclient, p, "v2.0")
    assert gvps.links[DEST].endswith(SHA_B[:12])
    pv = await call(gclient, "rollback_deploy", target="addons", confirm=False)
    assert pv["preview"] and pv["to"] == first.rsplit("/", 1)[1]
    rb = await call(gclient, "rollback_deploy", target="addons", confirm=True)
    assert rb["sha"] == SHA_A and gvps.links[DEST] == first
    assert res["status"] == "succeeded"


async def test_unmanaged_existing_dir_moved_aside(gclient: SessionPerCall, gvps: Any) -> None:
    gvps.files[f"{DEST}/old.py"] = "legacy\n"
    p = await plan(gclient)
    assert p["unmanaged_existing_dir"] is True
    assert any("moved" in w for w in p["warnings"])
    await confirm(gclient, p)
    aside = [f for f in gvps.files if f.startswith(f"{ROOT}/pre-vps-mcp-")]
    assert aside and gvps.files[aside[0]] == "legacy\n"
    assert gvps.links[DEST].startswith(ROOT)


async def test_health_failure_rolls_back(gapp: Any, gclient: SessionPerCall, gvps: Any) -> None:
    await confirm(gclient, await plan(gclient))
    first = gvps.links[DEST]
    gvps.units["worker.service"].fail_on_start = True
    p = await plan(gclient, "v2.0")
    e = await gerr(
        gclient,
        "deploy_from_gitlab",
        target="addons",
        ref="v2.0",
        confirm=True,
        plan_id=p["plan_id"],
    )
    assert e["error"]["category"] == "failed" and "rolled back" in e["error"]["message"]
    assert e["rolled_back"] is True and e["outcome"] == "rolled_back"
    assert gvps.links[DEST] == first
    row = gapp.db.execute(
        "SELECT outcome FROM audit_records WHERE action='deploy' ORDER BY id DESC"
    ).fetchone()
    assert row["outcome"] == "rolled_back"
    st = gapp.db.execute(
        "SELECT status FROM gitlab_deploys ORDER BY created_at DESC, rowid DESC"
    ).fetchone()
    assert st["status"] == "rolled_back"


async def test_health_url_failure(make_gapp: Any, gvps: Any) -> None:
    from tests.unit.gitlab.conftest import gitlab_section

    t = gitlab_section()["deploy"][0] | {"health_url": "http://127.0.0.1:8069/web/login"}
    client = SessionPerCall(create_server(make_gapp(deploy=[t])))
    gvps.http_ok["http://127.0.0.1:8069/web/login"] = False
    p = await plan(client)
    e = await gerr(
        client,
        "deploy_from_gitlab",
        target="addons",
        ref="v1.0",
        confirm=True,
        plan_id=p["plan_id"],
    )
    assert "health check" in e["error"]["message"]
    assert DEST not in gvps.links  # first deploy: nothing to restore, link removed


async def test_refusals(
    make_gapp: Any, gclient: SessionPerCall, gvps: Any, fgl: FakeGitLab
) -> None:
    e = await gerr(gclient, "deploy_from_gitlab", target="addons", ref="feature/x", confirm=False)
    assert e["error"]["category"] == "invalid_input"
    fgl.repos[11].tags.pop("v2.0")  # SHA_B now only on feature/x, which is not allowed
    e = await gerr(gclient, "deploy_from_gitlab", target="addons", ref=SHA_B, confirm=False)
    assert e["error"]["category"] == "invalid_input"
    e = await gerr(gclient, "deploy_from_gitlab", target="nope", ref="v1.0", confirm=False)
    assert e["error"]["category"] == "not_permitted"
    e = await gerr(
        gclient, "deploy_from_gitlab", target="addons", ref="v1.0", confirm=True, plan_id="forged"
    )
    assert e["error"]["category"] == "conflict" and e["plan"]["commit"]["sha"] == SHA_A
    p = await plan(gclient)
    await confirm(gclient, p)
    e = await gerr(
        gclient,
        "deploy_from_gitlab",
        target="addons",
        ref="v1.0",
        confirm=True,
        plan_id=p["plan_id"],
    )
    assert e["error"]["category"] == "conflict"  # single use
    ro = SessionPerCall(create_server(make_gapp(read_only=True)))
    e = await gerr(ro, "deploy_from_gitlab", target="addons", ref="v1.0", confirm=False)
    assert e["error"]["category"] == "read_only"
    assert fgl.writes() == []


async def test_moving_ref_deploys_planned_commit(
    gclient: SessionPerCall, gvps: Any, fgl: FakeGitLab
) -> None:
    p = await plan(gclient, "main")
    assert p["commit"]["sha"] == SHA_C
    fgl.repos[11].branches["main"] = SHA_B  # main moved; SHA_C no longer on an allowed ref
    e = await gerr(
        gclient,
        "deploy_from_gitlab",
        target="addons",
        ref="main",
        confirm=True,
        plan_id=p["plan_id"],
    )
    assert e["error"]["category"] == "invalid_input"
    fgl.repos[11].tags["v3.0"] = SHA_C  # reachable again through an allowed tag
    p2 = await plan(gclient, "main")
    fgl.repos[11].branches["main"] = SHA_A
    res = await confirm(gclient, p2, "main")
    assert res["commit"]["sha"] == SHA_B


async def test_pipeline_gate_and_drift(make_gapp: Any, gvps: Any) -> None:
    from tests.unit.gitlab.conftest import gitlab_section

    t = gitlab_section()["deploy"][0] | {"require_pipeline_success": True}
    client = SessionPerCall(create_server(make_gapp(deploy=[t])))
    p = await plan(client, "main")  # pipeline 900 failed
    assert any("will be refused" in w for w in p["warnings"])
    e = await gerr(
        client,
        "deploy_from_gitlab",
        target="addons",
        ref="main",
        confirm=True,
        plan_id=p["plan_id"],
    )
    assert e["error"]["category"] == "failed" and "pipeline" in e["error"]["message"]
    p = await plan(client, "v1.0")
    gvps.files[f"{DEST}/surprise.txt"] = "appeared"
    e = await gerr(
        client,
        "deploy_from_gitlab",
        target="addons",
        ref="v1.0",
        confirm=True,
        plan_id=p["plan_id"],
    )
    assert e["error"]["category"] == "conflict" and e["plan"]["unmanaged_existing_dir"]


async def test_busy_with_running_job_or_deploy(gapp: Any, gclient: SessionPerCall) -> None:
    from vps_mcp.gitlab.store import DeployStore

    p = await plan(gclient)
    DeployStore(gapp.db).add_plan("dep_x", "addons", "deploy", "h", {"commit": {"sha": SHA_A}})
    DeployStore(gapp.db).update("dep_x", status="running")
    e = await gerr(
        gclient,
        "deploy_from_gitlab",
        target="addons",
        ref="v1.0",
        confirm=True,
        plan_id=p["plan_id"],
    )
    assert e["error"]["category"] == "conflict" and "running" in e["error"]["message"]


async def test_retention_prunes_old_releases(
    make_gapp: Any, gvps: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from tests.unit.gitlab.conftest import gitlab_section
    from vps_mcp.gitlab import deploy as dmod

    t = gitlab_section()["deploy"][0] | {"keep_releases": 1}
    client = SessionPerCall(create_server(make_gapp(deploy=[t])))
    gvps.dirs.add(f"{ROOT}/pre-vps-mcp-20260101T000000")
    base = datetime(2026, 11, 11, tzinfo=UTC)
    for i, ref in enumerate(("v1.0", "v2.0", "main")):

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:  # noqa: ARG003
                return base + timedelta(minutes=i)

        monkeypatch.setattr(dmod, "datetime", FakeDT)
        res = await confirm(client, await plan(client, ref), ref)
    names = (await call(client, "deploy_targets"))["targets"][0]["releases"]
    assert len([n for n in names if not n.startswith("pre-")]) == 2  # active + 1 kept
    assert "pre-vps-mcp-20260101T000000" in names
    assert res["pruned"]
    rm = [c.argv for c in gvps.calls if c.argv[:2] == ["rm", "-rf"]]
    assert rm and all(a[-1].startswith(ROOT + "/") for a in rm)
