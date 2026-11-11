"""Config validation (T003), credentials (T005), access and render helpers (T010, T011)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from pydantic import ValidationError

from tests.fakegitlab import TOKEN
from tests.unit.gitlab.conftest import gitlab_section, gl_config
from vps_mcp.gitlab import access, render
from vps_mcp.gitlab.credentials import KeychainToken, TokenMissing
from vps_mcp.remote.redact import scrub


def bad(**over: Any) -> None:
    with pytest.raises(ValidationError):
        gl_config(**over)


def proj(**over: Any) -> list[dict[str, Any]]:
    return [{"alias": "p", "path": "g/p", **over}]


def test_valid_config_and_read_implied() -> None:
    cfg = gl_config()
    assert cfg.gitlab is not None
    assert cfg.gitlab.get_project("scripts").permissions == ["read"]  # type: ignore[union-attr]
    p = gl_config(project=proj(permissions=["deploy"]), deploy=[]).gitlab.project[0]  # type: ignore[union-attr]
    assert p.permissions == ["read", "deploy"]


@pytest.mark.parametrize(
    "url",
    [
        "http://gitlab.example.test",
        "https://user:pw@gitlab.example.test",
        "https://x/?a=1",
        "ftp://x",
    ],
)
def test_url_rules(url: str) -> None:
    bad(url=url)


@pytest.mark.parametrize(
    "over",
    [
        {"keychain_account": "bad account"},
        {"keychain_service": "a/b"},
        {"timeout_s": 1},
        {"max_archive_mb": 0},
        {"project": proj(alias="Bad")},
        {"project": [{"alias": "p"}]},
        {"project": [{"alias": "p", "id": 1, "path": "g/p"}]},
        {"project": proj(path="noslash")},
        {"project": proj(permissions=["admin"])},
        {"project": proj(close_issues=True)},
        {"project": proj(permissions=["write_issues"], close_any_issue=True)},
        {"project": proj(trigger_refs=["main"])},
        {"project": proj() + proj()},
        {"unknown": 1},
    ],
)
def test_settings_rules(over: dict[str, Any]) -> None:
    bad(**({"deploy": []} | over))


def target(**over: Any) -> list[dict[str, Any]]:
    t = gitlab_section()["deploy"][0] | over
    return [t]


@pytest.mark.parametrize(
    "over",
    [
        {"destination": "/etc/app"},
        {"destination": "/usr/local/app"},
        {"destination": "/var/lib/vps-mcp/x"},
        {"destination": "/opt"},
        {"destination": "relative/path"},
        {"destination": "/opt/../etc"},
        {"refs": []},
        {"project": "scripts"},
        {"project": "nope"},
        {"services": ["undeclared"]},
        {"keep_releases": 0},
        {"health_url": "file:///etc/passwd"},
        {"exclude": ["../x"]},
        {"owner": "Root"},
    ],
)
def test_deploy_rules(over: dict[str, Any]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        gl_config(deploy=target(**over))


def test_nested_destinations_rejected() -> None:
    t1 = gitlab_section()["deploy"][0]
    t2 = t1 | {"name": "inner", "destination": "/opt/odoo/custom-addons/sub"}
    bad(deploy=[t1, t2])


def test_remote_access_service_rejected() -> None:
    from tests.conftest import config_dict
    from vps_mcp.config.policy import AppConfig

    d = config_dict()
    d["service"].append({"name": "ssh", "kind": "container"})
    d["gitlab"] = gitlab_section(deploy=target(services=["ssh"]))
    with pytest.raises(ValidationError):
        AppConfig.model_validate(d)


# ------------------------------------------------------------------ credentials


def reader(code: int = 0, out: str = TOKEN + "\n") -> Any:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code, out, "")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_keychain_argv_cache_and_redaction() -> None:
    r = reader()
    kt = KeychainToken("svc", "acct", reader=r, platform="darwin")
    assert kt.get() == TOKEN and kt.get() == TOKEN
    assert r.calls == [
        ["/usr/bin/security", "find-generic-password", "-s", "svc", "-a", "acct", "-w"]
    ]
    assert TOKEN not in scrub(f"x {TOKEN} y", [])


@pytest.mark.parametrize(
    ("rd", "plat"),
    [(reader(code=44, out=""), "darwin"), (reader(out="  \n"), "darwin"), (reader(), "linux")],
)
def test_token_missing(rd: Any, plat: str) -> None:
    with pytest.raises(TokenMissing) as e:
        KeychainToken("svc", "acct", reader=rd, platform=plat).get()
    assert e.value.subtype == "gitlab_token_missing"
    assert "security add-generic-password -U -s svc -a acct -w" in e.value.message


def test_keychain_timeout() -> None:
    def boom(argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 10)

    with pytest.raises(TokenMissing):
        KeychainToken("s", "a", reader=boom, platform="darwin").get()


# -------------------------------------------------------------- access / render


def test_alias_resolution_and_permissions() -> None:
    s = gl_config().gitlab
    assert s is not None
    assert access.resolve(s, "addons", "deploy").alias == "addons"
    msgs = set()
    for alias in ("other", "../x", "acme/odoo-addons", "11", 5):
        with pytest.raises(Exception) as e:
            access.resolve(s, alias)
        msgs.add(str(e.value))
    assert len(msgs) == 1
    with pytest.raises(Exception, match="does not allow 'ci'"):
        access.resolve(s, "scripts", "ci")
    assert access.pid(s.get_project("addons")) == "acme%2Fodoo-addons"  # type: ignore[arg-type]
    assert access.pid(s.get_project("scripts")) == "22"  # type: ignore[arg-type]


def test_ref_matching() -> None:
    s = gl_config().gitlab
    assert s is not None
    t = s.deploy[0]
    assert access.target_allows(t, "tag", "v1.0")
    assert access.target_allows(t, "branch", "main")
    assert not access.target_allows(t, "branch", "v1.0")
    assert not access.target_allows(t, "tag", "main")
    p = s.get_project("addons")
    assert p is not None
    assert access.trigger_allowed(p, "main") and access.trigger_allowed(p, "branch:release/1")
    assert not access.trigger_allowed(p, "feature/x")


def test_render_helpers() -> None:
    m = render.Masker(["*PASSWORD*"], [])
    e = render.external(m, "password=x\n" + "y" * 20000, author="a")
    assert e["kind"] == "external_content" and "password=***" in e["text"] and e["truncated"]
    assert (
        render.is_binary(b"\x00abc")
        and render.is_binary(b"\xff\xfe")
        and not render.is_binary(b"ok")
    )
    assert (
        render.commit_line(
            {
                "id": "a" * 40,
                "committed_date": "2026-11-01T10:00:00Z",
                "author_name": "dev",
                "title": "T",
            }
        )
        == "aaaaaaaaaaaa 2026-11-01 10:00 dev: T"
    )
    assert (
        render.pipeline_line(
            {"id": 1, "status": "failed", "ref": "main", "sha": "b" * 40, "duration": 125}, ["test"]
        )
        == "1 failed main bbbbbbbbbbbb 2m05s failed:[test]"
    )
