"""SSH connection to the single target VPS (FR-002–FR-004, research R3)."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from typing import Any

import asyncssh

from vps_mcp.config.policy import TargetVPS
from vps_mcp.errors import ErrorCategory, VpsMcpError

log = logging.getLogger(__name__)

ReconnectCallback = Callable[[], Awaitable[object]]


def classify_connect_error(exc: BaseException, host: str) -> VpsMcpError:
    """Map a connection exception to ``connection_failure`` with a subtype.

    Messages are built from scratch so key paths and passphrases never leak (FR-033).
    """

    def fail(subtype: str, message: str) -> VpsMcpError:
        return VpsMcpError(ErrorCategory.CONNECTION_FAILURE, message, subtype=subtype)

    if isinstance(exc, asyncssh.HostKeyNotVerifiable):
        return fail(
            "identity_mismatch",
            f"the host key presented by {host} does not match the pinned key; refusing to "
            "connect (re-pin with `vps-mcp trust-host --replace` only if the change is expected)",
        )
    if isinstance(exc, asyncssh.PermissionDenied):
        return fail("auth_rejected", f"{host} rejected the SSH key for the configured user")
    if isinstance(exc, asyncssh.KeyImportError | asyncssh.KeyEncryptionError):
        return fail("auth_rejected", "the configured SSH private key could not be loaded")
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return fail("timeout", f"timed out connecting to {host}")
    if isinstance(exc, socket.gaierror):
        return fail("unreachable", f"host {host} could not be resolved")
    if isinstance(exc, OSError):
        return fail("unreachable", f"{host} is unreachable ({exc.__class__.__name__})")
    if isinstance(exc, asyncssh.Error):
        return fail("unreachable", f"SSH connection to {host} failed ({exc.__class__.__name__})")
    return fail("unreachable", f"connection to {host} failed ({exc.__class__.__name__})")


class VpsConnection:
    """One lazily (re)connected SSH connection with strict host-key pinning."""

    def __init__(self, target: TargetVPS, *, connect_timeout: float = 10.0) -> None:
        self.target = target
        self.connect_timeout = connect_timeout
        self._conn: asyncssh.SSHClientConnection | None = None
        self._lock = asyncio.Lock()
        self.on_reconnect: list[ReconnectCallback] = []
        self._ever_connected = False

    @property
    def is_connected(self) -> bool:
        return self._conn is not None and not self._conn.is_closed()

    def _connect_kwargs(self) -> dict[str, Any]:
        t = self.target
        kwargs: dict[str, Any] = {
            "host": t.host,
            "port": t.port,
            "username": t.user,
            "known_hosts": t.known_hosts_path,  # strict pinning; never trust-on-first-use
            "password": None,
            "preferred_auth": "publickey",
            "keepalive_interval": 15,
            "keepalive_count_max": 3,
            "connect_timeout": self.connect_timeout,
        }
        if t.key_path:
            kwargs["client_keys"] = [t.key_path]
            if t.key_passphrase_env:
                kwargs["passphrase"] = os.environ.get(t.key_passphrase_env)
        return kwargs

    async def get(self) -> asyncssh.SSHClientConnection:
        async with self._lock:
            if self.is_connected:
                assert self._conn is not None
                return self._conn
            try:
                self._conn = await asyncio.wait_for(
                    asyncssh.connect(**self._connect_kwargs()), self.connect_timeout + 1
                )
            except Exception as exc:  # noqa: BLE001 - every failure is classified
                self._conn = None
                err = classify_connect_error(exc, self.target.host)
                log.warning("connect failed: %s/%s", err.category.value, err.subtype)
                raise err from None
            reconnect = self._ever_connected
            self._ever_connected = True
        log.info("connected to %s (%s)", self.target.name, "reconnect" if reconnect else "initial")
        for cb in list(self.on_reconnect):
            try:
                await cb()
            except Exception:  # noqa: BLE001
                log.exception("reconnect callback failed")
        return self._conn

    def mark_lost(self) -> None:
        if self._conn is not None:
            self._conn.abort()
        self._conn = None

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None
