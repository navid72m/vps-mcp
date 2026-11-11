"""Operator CLI (T073–T075)."""

from pathlib import Path

import pytest

from tests.conftest import config_dict
from tests.unit.test_policy import to_toml
from vps_mcp import cli


def test_check_config_ok_and_errors(tmp_path: Path, capsys):
    kh = tmp_path / "kh"
    kh.write_text("203.0.113.10 ssh-ed25519 AAAA\n")
    d = config_dict()
    d["vps"]["known_hosts_path"] = str(kh)
    p = tmp_path / "c.toml"
    p.write_text(to_toml(d))
    assert cli.main(["check-config", "--config", str(p)]) == 0
    assert capsys.readouterr().out.startswith("OK: vps 'test-vps'")
    d["policy"]["typo"] = 1
    p.write_text(to_toml(d))
    assert cli.main(["check-config", "--config", str(p)]) == 1
    assert "typo" in capsys.readouterr().out


@pytest.mark.parametrize("runtime", ["docker", "podman", "none"])
def test_sudoers(runtime, capsys):
    assert cli.main(["bootstrap", "--print-sudoers", "--runtime", runtime, "--pm", "apt"]) == 0
    out = capsys.readouterr().out
    assert "vpsmcp ALL=(root) NOPASSWD:" in out and "/usr/bin/apt-get" in out
    assert "visudo -c" in out and "/usr/sbin/nginx" in out
    assert ("/usr/bin/" + runtime in out) is (runtime != "none")
    assert "ALL=(ALL) ALL" not in out and "/usr/bin/env" not in out


class _Key:
    def export_public_key(self, fmt):
        return b"ssh-ed25519 AAAANEWKEY"

    def get_algorithm(self):
        return "ssh-ed25519"

    def get_fingerprint(self, h):
        return "SHA256:abc"


def test_trust_host(tmp_path: Path, monkeypatch, capsys):
    async def fetch(host, port):
        return _Key()

    monkeypatch.setattr(cli, "_fetch_host_key", fetch)
    kh = tmp_path / "known_hosts"
    assert cli.main(["trust-host", "--host", "h.example", "--known-hosts", str(kh), "--yes"]) == 0
    assert kh.read_text() == "h.example ssh-ed25519 AAAANEWKEY\n"
    assert cli.main(["trust-host", "--host", "h.example", "--known-hosts", str(kh), "--yes"]) == 0
    kh.write_text("h.example ssh-ed25519 AAAAOLDKEY\n")
    assert cli.main(["trust-host", "--host", "h.example", "--known-hosts", str(kh), "--yes"]) == 2
    assert "DIFFERENT key" in capsys.readouterr().err
    assert (
        cli.main(
            ["trust-host", "--host", "h.example", "--known-hosts", str(kh), "--yes", "--replace"]
        )
        == 0
    )
    assert kh.read_text() == "h.example ssh-ed25519 AAAANEWKEY\n"
    monkeypatch.setattr("builtins.input", lambda _: "no")
    kh2 = tmp_path / "kh2"
    assert (
        cli.main(["trust-host", "--host", "h.example", "--port", "2222", "--known-hosts", str(kh2)])
        == 1
    )
    assert not kh2.exists()
