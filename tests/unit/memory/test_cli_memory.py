"""Memory path resolution (T004) and check-config output (T038)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import config_dict
from tests.unit.test_policy import to_toml
from vps_mcp import cli
from vps_mcp.config.loader import ConfigError, load_config, resolve_memory_path


def write_config(tmp_path: Path, **memory: object) -> Path:
    kh = tmp_path / "kh"
    kh.write_text("203.0.113.10 ssh-ed25519 AAAA\n")
    d = config_dict()
    d["vps"]["known_hosts_path"] = str(kh)
    text = to_toml(d)
    if memory:
        text += "\n[memory]\n" + "".join(
            f"{k} = {str(v).lower() if isinstance(v, bool) else repr(v).replace(chr(39), chr(34))}\n"
            for k, v in memory.items()
        )
    p = tmp_path / "myvps.toml"
    p.write_text(text)
    return p


def test_default_path_next_to_config(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    assert cfg.memory.path == str((tmp_path / "myvps.memory").resolve())


def test_relative_and_tilde_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_file = tmp_path / "c.toml"
    assert resolve_memory_path(cfg_file, "mem") == (tmp_path / "mem").resolve()
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_memory_path(cfg_file, "~/x.memory") == (tmp_path / "x.memory").resolve()


def test_path_inside_source_tree_rejected(tmp_path: Path) -> None:
    import vps_mcp

    inside = Path(vps_mcp.__file__).parent / "memory-here"
    with pytest.raises(ConfigError) as e:
        load_config(write_config(tmp_path, path=str(inside)))
    assert "memory.path" in str(e.value)


@pytest.mark.parametrize(("key", "value"), [("briefing_chars", 10), ("max_notes", 1), ("x", 1)])
def test_bad_memory_settings(tmp_path: Path, key: str, value: int) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, **{key: value}))


def test_check_config_prints_memory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = write_config(tmp_path)
    assert cli.main(["check-config", "--config", str(p)]) == 0
    assert "memory: " in capsys.readouterr().out
    (tmp_path / "myvps.memory" / "notes").mkdir(parents=True)
    (tmp_path / "myvps.memory" / "notes" / "a.md").write_text("---\n---\n")
    (tmp_path / "myvps.memory").chmod(0o755)
    assert cli.main(["check-config", "--config", str(p)]) == 0
    out = capsys.readouterr().out
    assert "(1 notes)" in out and "readable by other users" in out


def test_check_config_memory_disabled(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["check-config", "--config", str(write_config(tmp_path, enabled=False))]) == 0
    assert "memory: disabled" in capsys.readouterr().out


def test_public_git_remote_warning(tmp_path: Path) -> None:
    mem = tmp_path / "repo" / "myvps.memory"
    mem.mkdir(parents=True, mode=0o700)
    try:
        subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path / "repo"),
                "remote",
                "add",
                "origin",
                "git@github.com:someone/notes.git",
            ],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    lines = cli.memory_report(mem)
    assert any("hosted remote" in ln for ln in lines)
    assert not any("hosted remote" in ln for ln in cli.memory_report(tmp_path / "other"))
