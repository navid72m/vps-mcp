import copy
from pathlib import Path

import pytest

from tests.conftest import config_dict
from vps_mcp.config.loader import ConfigError, load_config

try:
    import tomli_w  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    tomli_w = None


def to_toml(d: dict) -> str:
    """Tiny TOML writer for the shapes used in these tests."""

    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        if isinstance(v, str):
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if isinstance(v, list):
            return "[" + ", ".join(val(x) for x in v) + "]"
        if isinstance(v, dict):
            return "{ " + ", ".join(f"{k} = {val(x)}" for k, x in v.items()) + " }"
        raise TypeError(v)

    lines = []
    for section in ("vps", "policy"):
        flat = {k: v for k, v in d.get(section, {}).items() if not isinstance(v, dict)}
        lines.append(f"[{section}]")
        lines += [f"{k} = {val(v)}" for k, v in flat.items()]
        for k, v in d.get(section, {}).items():
            if isinstance(v, dict):
                lines.append(f"[{section}.{k}]")
                lines += [f"{kk} = {val(vv)}" for kk, vv in v.items()]
    for svc in d.get("service", []):
        lines.append("[[service]]")
        lines += [f"{k} = {val(v)}" for k, v in svc.items()]
    return "\n".join(lines) + "\n"


@pytest.fixture
def write(tmp_path: Path):
    kh = tmp_path / "known_hosts"
    kh.write_text("203.0.113.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample\n")

    def _write(d: dict) -> Path:
        d = copy.deepcopy(d)
        d["vps"]["known_hosts_path"] = str(kh)
        p = tmp_path / "cfg.toml"
        p.write_text(to_toml(d))
        return p

    return _write


def errors_for(write, d) -> str:
    with pytest.raises(ConfigError) as exc:
        load_config(write(d))
    return " | ".join(exc.value.errors)


def test_valid_config_loads(write):
    cfg = load_config(write(config_dict()))
    assert cfg.vps.name == "test-vps"
    assert {s.name for s in cfg.service} == {"nginx", "api", "worker"}
    assert cfg.policy.timeouts.control_s == 60
    assert cfg.policy.min_free_disk_mb == 1024


def test_rule1_known_hosts_must_pin_host(write, tmp_path):
    d = config_dict()
    p = write(d)
    (tmp_path / "known_hosts").write_text("other.example ssh-ed25519 AAAA\n")
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert "trust-host" in exc.value.errors[0]


def test_rule2_root_requires_opt_in(write):
    d = config_dict()
    d["vps"]["user"] = "root"
    assert "allow_root" in errors_for(write, d)
    d["vps"]["allow_root"] = True
    assert load_config(write(d)).vps.user == "root"


def test_rule3_duplicate_services(write):
    d = config_dict()
    d["service"].append({"name": "api", "kind": "container"})
    assert "duplicate" in errors_for(write, d)


def test_rule4_remote_access_unit_cannot_be_declared(write):
    d = config_dict()
    d["service"].append({"name": "sshd", "kind": "os"})
    assert "remote-access" in errors_for(write, d)
    d = config_dict()
    d["service"].append({"name": "login", "kind": "os", "unit": "ssh.service"})
    assert "remote-access" in errors_for(write, d)


def test_rule5_os_only_fields(write):
    d = config_dict()
    d["service"][1]["config_files"] = [{"path": "/etc/x"}]
    assert "only allowed when kind = 'os'" in errors_for(write, d)
    d = config_dict()
    d["service"][0]["validate"] = "apache"
    assert "validate" in errors_for(write, d)


@pytest.mark.parametrize(
    "path", ["relative/path", "/etc/../shadow", "/proc/self/environ", "/dev/sda"]
)
def test_rule6_paths(write, path):
    d = config_dict()
    d["service"][2]["data_paths"] = [path]
    errors_for(write, d)


def test_rule7_unknown_keys_rejected(write):
    d = config_dict()
    d["policy"]["read_onlyy"] = True
    assert "read_onlyy" in errors_for(write, d)
    d = config_dict()
    d["service"][0]["protect"] = True
    assert "protect" in errors_for(write, d)


def test_invalid_toml(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text("[vps\n")
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert "invalid TOML" in exc.value.errors[0]


def test_example_config_parses_structurally():
    import tomllib

    from vps_mcp.config.policy import AppConfig

    raw = tomllib.loads(Path("examples/vps-mcp.toml").read_text())
    AppConfig.model_validate(raw)


def test_unit_name_suffixes():
    from vps_mcp.config.policy import ServiceDeclaration

    def unit(**kw):
        return ServiceDeclaration(kind="os", **kw).unit_name

    assert unit(name="php8.1-fpm") == "php8.1-fpm.service"
    assert unit(name="postgresql@18-main") == "postgresql@18-main.service"
    assert unit(name="x", unit="foo.socket") == "foo.socket"
    assert unit(name="nginx") == "nginx.service"
