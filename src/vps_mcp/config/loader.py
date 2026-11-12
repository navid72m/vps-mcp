"""Load and validate the TOML policy file."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from pydantic import ValidationError

from vps_mcp.config.policy import AppConfig

log = logging.getLogger(__name__)


class ConfigError(Exception):
    """The configuration file is missing or invalid. ``errors`` lists every problem."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _format(exc: ValidationError) -> list[str]:
    out = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e.get("loc", ()) if p != "function-after")
        msg = str(e.get("msg", "invalid")).removeprefix("Value error, ")
        out.append(f"{loc}: {msg}" if loc else msg)
    return out


def known_hosts_has(path: Path, host: str, port: int) -> bool:
    wanted = {host, f"[{host}]:{port}"} if port != 22 else {host, f"[{host}]:22"}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if fields[0].startswith("@"):  # @cert-authority / @revoked markers
            fields = fields[1:]
        if fields and set(fields[0].split(",")) & wanted:
            return True
    return False


def resolve_memory_path(config_path: Path, configured: str | None) -> Path:
    """Default: next to the policy file, ``<stem>.memory`` (research M5)."""
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = config_path.expanduser().absolute().parent / path
    else:
        cp = config_path.expanduser().absolute()
        path = cp.parent / f"{cp.stem}.memory"
    path = path.resolve()
    source_root = Path(__file__).resolve().parent.parent.parent
    if path == source_root or source_root in path.parents:
        raise ValueError(f"must not be inside the vps-mcp source tree ({source_root})")
    return path


def resolve_store(configured: str | None) -> Path:
    from agent_memory.files import default_store

    path = Path(configured).expanduser() if configured else default_store()
    path = path.absolute().resolve()
    source_root = Path(__file__).resolve().parent.parent.parent
    if path == source_root or source_root in path.parents:
        raise ValueError(f"store must not be inside the vps-mcp source tree ({source_root})")
    return path


def legacy_memory_folder(config_path: Path) -> Path:
    cp = config_path.expanduser().absolute()
    return cp.parent / f"{cp.stem}.memory"


def load_config(path: str | Path) -> AppConfig:
    p = Path(path).expanduser()
    try:
        raw = tomllib.loads(p.read_text())
    except FileNotFoundError:
        raise ConfigError([f"config file not found: {p}"]) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"invalid TOML: {exc}"]) from None

    try:
        cfg = AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format(exc)) from None

    try:
        if cfg.memory.path:
            cfg.memory.path = str(resolve_memory_path(p, cfg.memory.path))
        cfg.memory.store = str(resolve_store(cfg.memory.store))
    except ValueError as exc:
        raise ConfigError([f"memory: {exc}"]) from None

    vps = cfg.vps
    vps.known_hosts_path = str(Path(vps.known_hosts_path).expanduser())
    if vps.key_path:
        vps.key_path = str(Path(vps.key_path).expanduser())

    kh = Path(vps.known_hosts_path)
    if not kh.is_file() or not known_hosts_has(kh, vps.host, vps.port):
        raise ConfigError(
            [
                f"vps.known_hosts_path: no pinned host key for {vps.host} "
                f"(run `vps-mcp trust-host --host {vps.host} --known-hosts {kh}`)"
            ]
        )
    if vps.user == "root":
        log.warning("connecting to the VPS as root (allow_root = true); least privilege is advised")
    return cfg
