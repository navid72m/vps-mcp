"""Run fixed command templates on the VPS (FR-032, research R4).

There is deliberately no API that accepts a shell string: callers pass an argv list, and
every element is shell-quoted. Environment assignments are validated key/value pairs.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass
from typing import Protocol

import asyncssh

from vps_mcp.errors import ConnectionLost, ErrorCategory, VpsMcpError
from vps_mcp.remote.connection import VpsConnection

_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_SUDO_DENIED = (
    "a password is required",
    "is not allowed to execute",
    "not in the sudoers",
    "may not run sudo",
)


@dataclass(frozen=True)
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_status == 0


class RemoteExecutor(Protocol):
    async def run(
        self,
        argv: list[str],
        *,
        sudo: bool = False,
        timeout: float = 30.0,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
    ) -> CommandResult: ...


def render_command(
    argv: list[str], *, sudo: bool, env: dict[str, str] | None = None, user: str | None = None
) -> str:
    if not argv:
        raise ValueError("empty argv")
    parts: list[str] = []
    for k, v in (env or {}).items():
        if not _ENV_KEY_RE.fullmatch(k):
            raise ValueError(f"invalid env key {k!r}")
        parts.append(f"{k}={shlex.quote(v)}")
    if user is not None and (not sudo or not _USER_RE.fullmatch(user)):
        raise ValueError(f"invalid sudo target user {user!r}")
    if sudo:
        parts += ["sudo", "-n", "-u", user, "--"] if user else ["sudo", "-n", "--"]
    parts += [shlex.quote(a) for a in argv]
    return " ".join(parts)


def check_sudo_denied(result: CommandResult, argv: list[str]) -> None:
    err = result.stderr.lower()
    if result.exit_status != 0 and err.startswith("sudo") and any(s in err for s in _SUDO_DENIED):
        raise VpsMcpError(
            ErrorCategory.PERMISSION_DENIED,
            f"the VPS user may not run '{argv[0]}' via sudo; check /etc/sudoers.d/vps-mcp "
            "(see `vps-mcp bootstrap --print-sudoers`)",
        )


class SshExecutor:
    def __init__(self, connection: VpsConnection) -> None:
        self.connection = connection

    async def run(
        self,
        argv: list[str],
        *,
        sudo: bool = False,
        timeout: float = 30.0,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
    ) -> CommandResult:
        command = render_command(argv, sudo=sudo, env=env, user=user)
        # Failures here happen before anything ran on the VPS: plain connection_failure.
        conn = await self.connection.get()
        try:
            proc = await asyncio.wait_for(
                conn.run(command, input=stdin, check=False, encoding="utf-8", errors="replace"),
                timeout,
            )
        except TimeoutError:
            raise VpsMcpError(
                ErrorCategory.TIMED_OUT, f"'{argv[0]}' did not finish within {timeout:.0f}s"
            ) from None
        except (
            asyncssh.ConnectionLost,
            asyncssh.DisconnectError,
            BrokenPipeError,
            ConnectionResetError,
            asyncssh.ChannelOpenError,
        ):
            self.connection.mark_lost()
            raise ConnectionLost() from None
        status = proc.exit_status if proc.exit_status is not None else 255
        if proc.exit_status is None and proc.exit_signal is None:
            # Channel closed without an exit status: the connection dropped mid-command.
            self.connection.mark_lost()
            raise ConnectionLost()
        result = CommandResult(status, str(proc.stdout or ""), str(proc.stderr or ""))
        if sudo:
            check_sudo_denied(result, argv)
        return result
