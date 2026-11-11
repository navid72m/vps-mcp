import asyncio
import socket

import asyncssh
import pytest

from tests.conftest import KEY_PATH, PASSPHRASE, make_config
from vps_mcp.errors import ConnectionLost, ErrorCategory, VpsMcpError
from vps_mcp.remote.connection import VpsConnection, classify_connect_error
from vps_mcp.remote.executor import SshExecutor, render_command


@pytest.mark.parametrize(
    ("exc", "subtype"),
    [
        (asyncssh.HostKeyNotVerifiable("Host key is not trusted"), "identity_mismatch"),
        (asyncssh.PermissionDenied(f"key {KEY_PATH} with {PASSPHRASE} rejected"), "auth_rejected"),
        (asyncssh.KeyImportError(f"cannot read {KEY_PATH}"), "auth_rejected"),
        (TimeoutError(), "timeout"),
        (TimeoutError(), "timeout"),
        (socket.gaierror(f"unknown {KEY_PATH}"), "unreachable"),
        (ConnectionRefusedError(f"refused {PASSPHRASE}"), "unreachable"),
        (OSError("no route"), "unreachable"),
        (asyncssh.DisconnectError(11, f"bye {PASSPHRASE}"), "unreachable"),
    ],
)
def test_classification_without_secret_leak(exc, subtype):
    err = classify_connect_error(exc, "203.0.113.10")
    assert err.category is ErrorCategory.CONNECTION_FAILURE
    assert err.subtype == subtype
    assert KEY_PATH not in err.message and PASSPHRASE not in err.message


def test_connect_kwargs_strict(monkeypatch):
    monkeypatch.setenv("VPS_MCP_TEST_PASSPHRASE", PASSPHRASE)
    conn = VpsConnection(make_config().vps)
    kw = conn._connect_kwargs()
    assert kw["known_hosts"] == "/tmp/known_hosts"  # pinned file, never None (no TOFU)
    assert kw["password"] is None and kw["preferred_auth"] == "publickey"
    assert kw["client_keys"] == [KEY_PATH] and kw["passphrase"] == PASSPHRASE


async def test_connect_failure_is_classified(monkeypatch):
    async def boom(**kwargs):
        raise asyncssh.HostKeyNotVerifiable("changed")

    monkeypatch.setattr(asyncssh, "connect", boom)
    conn = VpsConnection(make_config().vps)
    with pytest.raises(VpsMcpError) as exc:
        await conn.get()
    assert exc.value.subtype == "identity_mismatch"


def test_render_command_quotes_everything():
    cmd = render_command(
        ["systemctl", "restart", "a;rm -rf /"], sudo=True, env={"DEBIAN_FRONTEND": "noninteractive"}
    )
    assert cmd == "DEBIAN_FRONTEND=noninteractive sudo -n -- systemctl restart 'a;rm -rf /'"
    with pytest.raises(ValueError):
        render_command(["x"], sudo=False, env={"BAD KEY": "1"})


class _Proc:
    def __init__(self, status, stdout="", stderr="", signal=None):
        self.exit_status, self.stdout, self.stderr, self.exit_signal = (
            status,
            stdout,
            stderr,
            signal,
        )


class _Conn:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.commands = []

    async def run(self, command, **kw):
        self.commands.append(command)
        return await self.behaviour()

    def is_closed(self):
        return False

    def abort(self):
        pass


def _executor(behaviour):
    conn = VpsConnection(make_config().vps)
    fake = _Conn(behaviour)
    conn._conn = fake  # type: ignore[assignment]
    return SshExecutor(conn), fake


async def test_executor_sudo_denied():
    async def b():
        return _Proc(1, stderr="sudo: a password is required")

    ex, _ = _executor(b)
    with pytest.raises(VpsMcpError) as exc:
        await ex.run(["systemctl", "stop", "x"], sudo=True)
    assert exc.value.category is ErrorCategory.PERMISSION_DENIED


async def test_executor_connection_lost():
    async def b():
        raise asyncssh.ConnectionLost("gone")

    ex, _ = _executor(b)
    with pytest.raises(ConnectionLost):
        await ex.run(["systemctl", "restart", "x"], sudo=True)
    assert not ex.connection.is_connected


async def test_executor_timeout():
    async def b():
        await asyncio.sleep(1)

    ex, _ = _executor(b)
    with pytest.raises(VpsMcpError) as exc:
        await ex.run(["sleep"], timeout=0.01)
    assert exc.value.category is ErrorCategory.TIMED_OUT
