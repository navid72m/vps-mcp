"""Built-in config validator catalogue (research R8). Operators pick a key, never a command."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Validator:
    main_path: str
    in_place: Callable[[], list[str]]
    staged: Callable[[str], list[str]]


VALIDATORS: dict[str, Validator] = {
    "nginx": Validator(
        "/etc/nginx/nginx.conf",
        lambda: ["nginx", "-t", "-q"],
        lambda p: ["nginx", "-t", "-q", "-c", p],
    ),
    "sshd": Validator(
        "/etc/ssh/sshd_config",
        lambda: ["sshd", "-t"],
        lambda p: ["sshd", "-t", "-f", p],
    ),
    "caddy": Validator(
        "/etc/caddy/Caddyfile",
        lambda: ["caddy", "validate", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"],
        lambda p: ["caddy", "validate", "--config", p, "--adapter", "caddyfile"],
    ),
    "haproxy": Validator(
        "/etc/haproxy/haproxy.cfg",
        lambda: ["haproxy", "-c", "-f", "/etc/haproxy/haproxy.cfg"],
        lambda p: ["haproxy", "-c", "-f", p],
    ),
}

VALIDATOR_BINARIES = {"nginx": "nginx", "sshd": "sshd", "caddy": "caddy", "haproxy": "haproxy"}
